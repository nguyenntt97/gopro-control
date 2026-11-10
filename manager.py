"""
Multi-Camera Manager for GoPro HERO5 BLE devices.
Manages connections, persistent camera list, and simultaneous command execution.
"""

import asyncio
import json
import logging
import os
from typing import Dict, List, Optional, Callable, Any

from bleak import BleakScanner

from ble_protocol import (
    SERVICE_GOPRO, SERVICE_GOPRO_ALT, CameraState
)
from camera_client import CameraClient, MockCameraClient

logger = logging.getLogger("ais_gopro.manager")

CAMERAS_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cameras.json")

class MultiCameraManager:
    def __init__(self, mock_mode: bool = False):
        self.mock_mode = mock_mode
        self.cameras: Dict[str, CameraClient] = {}
        self._listeners: List[Callable[[Dict[str, Any]], Any]] = []
        self.load_cameras()

    def add_listener(self, callback: Callable[[Dict[str, Any]], Any]):
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[Dict[str, Any]], Any]):
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _broadcast_change(self, state: CameraState):
        data = {
            "type": "camera_update",
            "camera": state.to_dict()
        }
        for listener in list(self._listeners):
            try:
                res = listener(data)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception as e:
                logger.error(f"Error notifying listener: {e}")

    def load_cameras(self):
        """Load saved cameras from cameras.json"""
        if os.path.exists(CAMERAS_CONFIG_FILE):
            try:
                with open(CAMERAS_CONFIG_FILE, "r", encoding="utf-8") as f:
                    configs = json.load(f)
                    for item in configs:
                        mac = item.get("mac", "").upper()
                        name = item.get("name", "GoPro HERO5")
                        if mac:
                            cam = self.register_camera(mac, name, save=False)
                            cam.state.nickname = item.get("nickname", "")
            except Exception as e:
                logger.error(f"Failed to load {CAMERAS_CONFIG_FILE}: {e}")
        elif self.mock_mode and not self.cameras:
            # Seed with 3 mock cameras
            self.register_mock_camera("C1:22:33:44:55:01", "GoPro HERO5 (Front)")
            self.register_mock_camera("C2:22:33:44:55:02", "GoPro HERO5 (Left)")
            self.register_mock_camera("C3:22:33:44:55:03", "GoPro HERO5 (Right)")

    def save_cameras(self):
        """Save known cameras to cameras.json"""
        data = []
        for mac, client in self.cameras.items():
            entry = {"mac": mac, "name": client.state.name}
            if client.state.nickname:
                entry["nickname"] = client.state.nickname
            data.append(entry)
        try:
            with open(CAMERAS_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save {CAMERAS_CONFIG_FILE}: {e}")

    def register_camera(self, mac: str, name: str = "GoPro HERO5", save: bool = True) -> CameraClient:
        mac = mac.upper()
        if mac in self.cameras:
            if name:
                self.cameras[mac].state.name = name
            return self.cameras[mac]

        client_cls = MockCameraClient if self.mock_mode else CameraClient
        client = client_cls(mac, name, on_state_change=self._broadcast_change)
        self.cameras[mac] = client
        if save:
            self.save_cameras()
        self._broadcast_change(client.state)
        return client

    def rename_camera(self, mac: str, nickname: str) -> Optional[CameraClient]:
        """Set (or clear, with "") the user nickname shown in the dashboard."""
        cam = self.cameras.get(mac.upper())
        if not cam:
            return None
        cam.state.nickname = nickname.strip()[:40]
        self.save_cameras()
        self._broadcast_change(cam.state)
        return cam

    def register_mock_camera(self, mac: str, name: str) -> CameraClient:
        mac = mac.upper()
        client = MockCameraClient(mac, name, on_state_change=self._broadcast_change)
        self.cameras[mac] = client
        self._broadcast_change(client.state)
        return client

    def unregister_camera(self, mac: str):
        mac = mac.upper()
        if mac in self.cameras:
            asyncio.create_task(self.cameras[mac].disconnect())
            del self.cameras[mac]
            self.save_cameras()

    def get_states(self) -> List[Dict[str, Any]]:
        return [cam.state.to_dict() for cam in self.cameras.values()]

    async def scan_ble(self, timeout: float = 6.0) -> List[Dict[str, str]]:
        """Scans for nearby GoPro BLE devices"""
        found = []
        if self.mock_mode:
            await asyncio.sleep(1.0)
            return [
                {"mac": "C1:22:33:44:55:01", "name": "GoPro 2495 (Mock)", "rssi": -55},
                {"mac": "C2:22:33:44:55:02", "name": "GoPro 8821 (Mock)", "rssi": -62},
                {"mac": "C3:22:33:44:55:03", "name": "GoPro 3104 (Mock)", "rssi": -70},
            ]

        try:
            devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
            for d, adv in devices.values():
                name = adv.local_name or d.name or ""
                uuids = [u.lower() for u in (adv.service_uuids or [])]
                is_gopro = (
                    "gopro" in name.lower() or
                    SERVICE_GOPRO.lower() in uuids or
                    SERVICE_GOPRO_ALT.lower() in uuids
                )
                mfr = adv.manufacturer_data.get(0x02F2)  # GoPro, Inc.
                if mfr is not None:
                    is_gopro = True
                if is_gopro:
                    # byte 1 of GoPro manufacturer data holds camera status flags:
                    # 0x01 awake, 0x04 in "Connect New Device" pairing mode
                    flags = mfr[1] if mfr and len(mfr) > 1 else None
                    if flags is None:
                        status = "unknown"
                    elif flags & 0x04:
                        status = "pairing mode"
                    elif flags & 0x01:
                        status = "awake"
                    else:
                        status = "asleep"
                    mac = d.address.upper()
                    known = self.cameras.get(mac)
                    found.append({
                        "mac": mac,
                        "name": name or (known.state.name if known else "GoPro Camera"),
                        "rssi": adv.rssi,
                        "status": status,
                    })
        except Exception as e:
            logger.error(f"Error during BLE scan: {e}")

        return found

    # --- Multi-Camera Simultaneous Commands ---

    async def connect_all(self) -> Dict[str, bool]:
        tasks = [cam.connect() for cam in self.cameras.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {mac: (res is True) for mac, res in zip(self.cameras.keys(), results)}

    async def disconnect_all(self):
        tasks = [cam.disconnect() for cam in self.cameras.values()]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def shutter_all(self, record: bool) -> Dict[str, bool]:
        """Trigger record start/stop on ALL connected cameras concurrently"""
        tasks = []
        macs = []
        for mac, cam in self.cameras.items():
            if cam.state.connected:
                macs.append(mac)
                tasks.append(cam.start_recording() if record else cam.stop_recording())
        if not tasks:
            return {}
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {mac: (res is True) for mac, res in zip(macs, results)}

    async def mode_all(self, mode: int) -> Dict[str, bool]:
        tasks = [cam.set_mode(mode) for cam in self.cameras.values() if cam.state.connected]
        macs = [mac for mac, cam in self.cameras.items() if cam.state.connected]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {mac: (res is True) for mac, res in zip(macs, results)}

    async def submode_all(self, mode: int, submode: int) -> Dict[str, bool]:
        tasks = [cam.set_submode(mode, submode) for cam in self.cameras.values() if cam.state.connected]
        macs = [mac for mac, cam in self.cameras.items() if cam.state.connected]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {mac: (res is True) for mac, res in zip(macs, results)}

    async def sync_time_all(self) -> Dict[str, bool]:
        tasks = [cam.sync_time() for cam in self.cameras.values() if cam.state.connected]
        macs = [mac for mac, cam in self.cameras.items() if cam.state.connected]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {mac: (res is True) for mac, res in zip(macs, results)}

    async def sleep_all(self) -> Dict[str, bool]:
        tasks = [cam.sleep() for cam in self.cameras.values() if cam.state.connected]
        macs = [mac for mac, cam in self.cameras.items() if cam.state.connected]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {mac: (res is True) for mac, res in zip(macs, results)}

    async def setting_all(self, setting_id: int, value: int) -> Dict[str, bool]:
        tasks = [cam.set_setting(setting_id, value) for cam in self.cameras.values() if cam.state.connected]
        macs = [mac for mac, cam in self.cameras.items() if cam.state.connected]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {mac: (res is True) for mac, res in zip(macs, results)}
