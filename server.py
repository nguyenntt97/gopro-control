"""
FastAPI Server & WebSocket Broadcast Hub for Multi-GoPro BLE Remote Control.
"""

import argparse
import asyncio
import json
import logging
import os
import sys

# Ensure local modules are found regardless of working directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from typing import Dict, Any, Optional, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from manager import MultiCameraManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ais_gopro.server")

# Command line arguments can enable mock mode by default
parser = argparse.ArgumentParser(description="GoPro Multi-Camera BLE Web Server")
parser.add_argument("--mock", action="store_true", help="Start in mock simulation mode")
parser.add_argument("--port", type=int, default=8000, help="Web server port (default: 8000)")
parser.add_argument("--host", type=str, default="0.0.0.0", help="Web server host (default: 0.0.0.0)")

args, _ = parser.parse_known_args()

app = FastAPI(title="GoPro HERO5 Multi-BLE Controller")
manager = MultiCameraManager(mock_mode=args.mock)

# --- WebSocket Hub ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        # Send initial full state
        await websocket.send_json({
            "type": "initial_state",
            "mock_mode": manager.mock_mode,
            "cameras": manager.get_states()
        })

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: Dict[str, Any]):
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                self.disconnect(connection)

ws_hub = ConnectionManager()

# Hook manager listener to WebSocket broadcaster
manager.add_listener(lambda data: asyncio.create_task(ws_hub.broadcast(data)))


# --- Pydantic Request Models ---
class RegisterCameraRequest(BaseModel):
    mac: str
    name: Optional[str] = "GoPro HERO5"

class RenameCameraRequest(BaseModel):
    nickname: str = ""

class UnregisterCameraRequest(BaseModel):
    mac: str

class CommandRequest(BaseModel):
    command: str  # shutter, mode, submode, locate, sleep, tag, wifi, sync_time
    target: str = "all"  # "all" or specific MAC address
    params: Optional[Dict[str, Any]] = None

class SettingRequest(BaseModel):
    setting_id: int
    value: int
    target: str = "all"

class MockModeRequest(BaseModel):
    enabled: bool


# --- API Routes ---

@app.get("/api/cameras")
async def get_cameras():
    return {
        "mock_mode": manager.mock_mode,
        "cameras": manager.get_states()
    }

@app.post("/api/cameras/scan")
async def scan_cameras(timeout: float = 5.0):
    discovered = await manager.scan_ble(timeout=timeout)
    return {"devices": discovered}

def _result(cam, ok: bool) -> Dict[str, Any]:
    """Uniform response: report the camera's own error text when a command fails."""
    if ok:
        return {"status": "ok"}
    return {"status": "error", "error": cam.state.last_error or "Command failed"}

@app.post("/api/cameras/register")
async def register_camera(req: RegisterCameraRequest):
    client = manager.register_camera(req.mac, req.name or "GoPro HERO5")
    await client.check_paired()
    return {"status": "ok", "camera": client.state.to_dict()}

@app.post("/api/cameras/unregister")
async def unregister_camera(req: UnregisterCameraRequest):
    manager.unregister_camera(req.mac)
    await ws_hub.broadcast({
        "type": "camera_removed",
        "mac": req.mac.upper()
    })
    return {"status": "ok"}

@app.post("/api/cameras/connect_all")
async def connect_all():
    results = await manager.connect_all()
    return {"status": "ok", "results": results}

@app.post("/api/cameras/disconnect_all")
async def disconnect_all():
    await manager.disconnect_all()
    return {"status": "ok"}

@app.post("/api/cameras/{mac}/connect")
async def connect_camera(mac: str):
    mac = mac.upper()
    if mac not in manager.cameras:
        raise HTTPException(status_code=404, detail="Camera not found")
    cam = manager.cameras[mac]
    ok = await cam.connect()
    return {**_result(cam, ok), "connected": cam.state.connected, "ready": cam.state.ready}

@app.post("/api/cameras/{mac}/rename")
async def rename_camera(mac: str, req: RenameCameraRequest):
    cam = manager.rename_camera(mac, req.nickname)
    if not cam:
        raise HTTPException(status_code=404, detail="Camera not found")
    return {"status": "ok", "camera": cam.state.to_dict()}

@app.post("/api/cameras/{mac}/pair")
async def pair_camera(mac: str, force: bool = False):
    """Pair from the PC (camera must be in Connect New Device > GoPro App), then connect."""
    mac = mac.upper()
    if mac not in manager.cameras:
        raise HTTPException(status_code=404, detail="Camera not found")
    cam = manager.cameras[mac]
    ok = await cam.pair(force=force)
    manager.save_cameras()
    return {**_result(cam, ok), "paired": cam.state.paired, "ready": cam.state.ready}

@app.post("/api/cameras/{mac}/disconnect")
async def disconnect_camera(mac: str):
    mac = mac.upper()
    if mac not in manager.cameras:
        raise HTTPException(status_code=404, detail="Camera not found")
    await manager.cameras[mac].disconnect()
    return {"status": "ok"}

@app.post("/api/cameras/command")
async def execute_command(req: CommandRequest):
    cmd = req.command.lower()
    target = req.target.upper()
    params = req.params or {}

    if target == "ALL":
        if cmd == "shutter":
            record = bool(params.get("record", True))
            results = await manager.shutter_all(record)
            return {"status": "ok", "results": results}
        elif cmd == "mode":
            mode = int(params.get("mode", 0))
            results = await manager.mode_all(mode)
            return {"status": "ok", "results": results}
        elif cmd == "submode":
            mode = int(params.get("mode", 0))
            submode = int(params.get("submode", 0))
            results = await manager.submode_all(mode, submode)
            return {"status": "ok", "results": results}
        elif cmd == "sleep":
            results = await manager.sleep_all()
            return {"status": "ok", "results": results}
        elif cmd == "sync_time":
            results = await manager.sync_time_all()
            return {"status": "ok", "results": results}
        else:
            raise HTTPException(status_code=400, detail=f"Broadcast not supported for command: {cmd}")

    else:
        if target not in manager.cameras:
            raise HTTPException(status_code=404, detail="Target camera not found")
        cam = manager.cameras[target]

        if cmd == "shutter":
            record = bool(params.get("record", True))
            ok = await (cam.start_recording() if record else cam.stop_recording())
        elif cmd == "mode":
            ok = await cam.set_mode(int(params.get("mode", 0)))
        elif cmd == "submode":
            ok = await cam.set_submode(int(params.get("mode", 0)), int(params.get("submode", 0)))
        elif cmd == "locate":
            # beep=true -> beep for `seconds` (default 3) then switch off; beep=false -> stop now
            if bool(params.get("beep", True)):
                ok = await cam.beep(float(params.get("seconds", 3.0)))
            else:
                ok = await cam.locate(False)
        elif cmd == "sleep":
            ok = await cam.sleep()
        elif cmd == "tag":
            ok = await cam.tag()
        elif cmd == "wifi":
            ok = await cam.set_wifi(bool(params.get("enable", True)))
        elif cmd == "sync_time":
            ok = await cam.sync_time()
        else:
            raise HTTPException(status_code=400, detail=f"Unknown command: {cmd}")
        return _result(cam, ok)

@app.post("/api/cameras/setting")
async def execute_setting(req: SettingRequest):
    target = req.target.upper()
    if target == "ALL":
        results = await manager.setting_all(req.setting_id, req.value)
        return {"status": "ok", "results": results}
    else:
        if target not in manager.cameras:
            raise HTTPException(status_code=404, detail="Target camera not found")
        cam = manager.cameras[target]
        ok = await cam.set_setting(req.setting_id, req.value)
        return _result(cam, ok)

@app.post("/api/mock_mode")
async def toggle_mock_mode(req: MockModeRequest):
    manager.mock_mode = req.enabled
    await manager.disconnect_all()
    manager.cameras.clear()
    manager.load_cameras()
    await ws_hub.broadcast({
        "type": "initial_state",
        "mock_mode": manager.mock_mode,
        "cameras": manager.get_states()
    })
    return {"status": "ok", "mock_mode": manager.mock_mode}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_hub.connect(websocket)
    try:
        while True:
            # Keep connection open and accept ping / messages from client
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
            except Exception:
                pass
    except WebSocketDisconnect:
        ws_hub.disconnect(websocket)


# --- Static Files / Single Page Web UI ---
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
INDEX_PATH = os.path.join(STATIC_DIR, "index.html")

@app.get("/")
async def serve_index():
    return FileResponse(INDEX_PATH)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host=args.host, port=args.port, reload=False)
