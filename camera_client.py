"""
GoPro HERO5 Camera Client using the Linux gatttool backend.

Every command waits for the camera's response notification, so a camera that
is connected but not paired (it silently drops commands) is reported as an
error instead of a false "ok". Pairing is initiated from the PC through
bluetoothctl, which is the only flow that leaves a stored bond.
"""

import asyncio
import logging
import re
import time
from typing import Callable, Optional, Dict

from ble_protocol import (
    CameraState, BlePacketReassembler,
    cmd_shutter, cmd_mode, cmd_submode, cmd_locate, cmd_sleep, cmd_tag, cmd_wifi,
    cmd_datetime,
    query_subscribe_updates, query_status, query_settings,
    setting_write_1byte, parse_tlv_parameters, parse_string_list
)

logger = logging.getLogger("ais_gopro.camera")

# Handle mapping for GoPro HERO5
HANDLE_WIFI_SSID = 0x0023
HANDLE_WIFI_PASS = 0x0025
HANDLE_CMD_REQ   = 0x002F
HANDLE_CMD_RSP   = 0x0031
HANDLE_CMD_CCCD  = 0x0032  # 0100 to enable notifications
HANDLE_SET_REQ   = 0x0034
HANDLE_SET_RSP   = 0x0036
HANDLE_SET_CCCD  = 0x0037
HANDLE_QRY_REQ   = 0x0039
HANDLE_QRY_RSP   = 0x003B
HANDLE_QRY_CCCD  = 0x003C

RESPONSE_TIMEOUT = 3.0
DEFAULT_BEEP_SECONDS = 3.0

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

NOT_PAIRED_MSG = "Camera does not answer. It is probably not paired: put it in Connect New Device > GoPro App and press Pair."


class CameraClient:
    def __init__(self, mac: str, name: str = "GoPro HERO5", on_state_change: Optional[Callable[[CameraState], None]] = None):
        self.mac = mac.upper()
        self.state = CameraState(mac=self.mac, name=name)
        self.on_state_change = on_state_change

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._beep_off_task: Optional[asyncio.Task] = None
        self._reassemblers: Dict[int, BlePacketReassembler] = {
            HANDLE_CMD_RSP: BlePacketReassembler(),
            HANDLE_SET_RSP: BlePacketReassembler(),
            HANDLE_QRY_RSP: BlePacketReassembler(),
        }
        # response handle -> future waiting for the next complete payload on it
        self._waiters: Dict[int, asyncio.Future] = {}
        self._lock = asyncio.Lock()      # connect / disconnect / pair
        self._io_lock = asyncio.Lock()   # one request in flight at a time
        self._connected_event = asyncio.Event()

    def _notify_change(self):
        self.state.last_seen = time.time()
        if self.on_state_change:
            try:
                self.on_state_change(self.state)
            except Exception as e:
                logger.error(f"[{self.mac}] Error in state change callback: {e}")

    def _set_error(self, msg: str):
        self.state.last_error = msg
        self._notify_change()

    # --- Pairing ---------------------------------------------------------

    async def check_paired(self) -> bool:
        """Ask BlueZ whether a bond exists for this camera."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl", "info", self.mac,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            paired = bool(re.search(r"Paired:\s*yes", ANSI_RE.sub("", out.decode(errors="ignore"))))
        except Exception as e:
            logger.warning(f"[{self.mac}] bluetoothctl info failed: {e}")
            return self.state.paired
        if paired != self.state.paired:
            self.state.paired = paired
            self._notify_change()
        return paired

    async def pair(self, timeout: float = 30.0, force: bool = False) -> bool:
        """
        Pair from the PC side (scan, trust, pair) with a NoInputNoOutput agent.
        The camera must be on Connect New Device > GoPro App.
        Accepting a pairing that the camera starts does not store keys, so we
        always start it ourselves.
        """
        if await self.check_paired():
            if not force:
                # Never drop a bond on our own: one failed connect is not proof it is stale
                if await self.connect():
                    return True
                self._set_error("Already paired, but the camera did not answer. Retry Connect; "
                                "if the camera was reset, use Re-pair (camera on Connect New Device > GoPro App).")
                return False
            # Explicit re-pair requested: drop the old bond first
            await self.disconnect()
            logger.info(f"[{self.mac}] Re-pair requested, removing stored pairing")
            try:
                proc = await asyncio.create_subprocess_exec(
                    "bluetoothctl", "remove", self.mac,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except Exception:
                pass
            await self.check_paired()

        async with self._lock:
            await self._cleanup()
            self.state.connected = False
            self.state.ready = False
            self._set_error("")

            logger.info(f"[{self.mac}] Pairing via bluetoothctl...")
            try:
                proc = await asyncio.create_subprocess_exec(
                    "bluetoothctl",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT)
            except Exception as e:
                self._set_error(f"Cannot start bluetoothctl: {e}")
                return False

            buf = ""

            async def send(line: str):
                proc.stdin.write((line + "\n").encode())
                await proc.stdin.drain()

            async def read_until(patterns, secs: float) -> Optional[str]:
                nonlocal buf
                deadline = time.monotonic() + secs
                while time.monotonic() < deadline:
                    for p in patterns:
                        if re.search(p, buf):
                            return p
                    try:
                        chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    if not chunk:
                        break
                    text = ANSI_RE.sub("", chunk.decode(errors="ignore"))
                    buf += text
                    # answer any confirmation the agent asks for
                    if re.search(r"Accept pairing|Confirm passkey|Authorize service|\(yes/no\)", text):
                        await send("yes")
                return None

            mac_re = re.escape(self.mac)
            ok = False
            try:
                await send("agent NoInputNoOutput")
                await send("default-agent")
                await send("scan le")
                # wait until BlueZ has seen the camera advertise (needed for pair)
                await read_until([rf"Device {mac_re}"], 10.0)
                await send(f"trust {self.mac}")
                await send(f"pair {self.mac}")
                hit = await read_until([r"Pairing successful", r"AlreadyExists",
                                        r"Failed to pair[^\n]*", r"not available"], timeout)
                ok = hit in (r"Pairing successful", r"AlreadyExists")
                if not ok:
                    m = re.search(r"Failed to pair[^\n]*", buf)
                    reason = m.group(0).strip() if m else ("camera not found while scanning"
                                                           if hit == r"not available" else "timed out")
                    self.state.last_error = (f"Pairing failed ({reason}). Put the camera in "
                                             "Connect New Device > GoPro App and try again.")
                # release the link so gatttool can take it
                await send(f"disconnect {self.mac}")
                await asyncio.sleep(1.0)
                await send("scan off")
                await send("quit")
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except Exception as e:
                self.state.last_error = f"Pairing error: {e}"
            finally:
                if proc.returncode is None:
                    proc.kill()

            logger.info(f"[{self.mac}] Pairing {'succeeded' if ok else 'failed'}")
            self._notify_change()

        await self.check_paired()
        if ok:
            await asyncio.sleep(1.5)
            return await self.connect()
        return False

    # --- Connection ------------------------------------------------------

    async def connect(self, timeout: float = 12.0) -> bool:
        """Open an interactive gatttool session and verify the camera answers."""
        async with self._lock:
            if self._proc and self.state.connected and self.state.ready:
                return True

            await self._cleanup()
            await self.check_paired()

            logger.info(f"[{self.mac}] Spawning interactive gatttool session...")
            self.state.connected = False
            self.state.ready = False
            self._notify_change()
            self._connected_event.clear()

            try:
                self._proc = await asyncio.create_subprocess_exec(
                    "gatttool", "-t", "random", "-b", self.mac, "-I",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT
                )
            except Exception as e:
                self._set_error(f"Failed to start gatttool: {e}")
                return False

            self._reader_task = asyncio.create_task(self._read_loop())
            await self._send_raw(b"connect\n")

            try:
                await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                await self._cleanup()
                self._set_error("Bluetooth connection timed out. Is the camera on and in range?")
                return False

            # Enable notifications on the three response characteristics
            await asyncio.sleep(0.3)
            for h in (HANDLE_CMD_CCCD, HANDLE_SET_CCCD, HANDLE_QRY_CCCD):
                await self._send_raw(f"char-write-req 0x{h:04x} 0100\n".encode())
                await asyncio.sleep(0.2)

        # Outside the connect lock: prove the camera answers
        rsp = await self._request(HANDLE_QRY_REQ, HANDLE_QRY_RSP, query_status(), timeout=4.0)
        if rsp is None:
            self.state.ready = False
            self._set_error(NOT_PAIRED_MSG if not self.state.paired else
                            "Camera does not answer. Try Pair again (the camera may have reset its pairing).")
            return False

        self.state.ready = True
        self.state.last_error = ""
        self._notify_change()

        await self._send_raw(f"char-read-hnd 0x{HANDLE_WIFI_SSID:04x}\n".encode())
        await self._request(HANDLE_QRY_REQ, HANDLE_QRY_RSP, query_settings())
        await self._request(HANDLE_QRY_REQ, HANDLE_QRY_RSP, query_subscribe_updates())
        return True

    async def _send_raw(self, cmd: bytes):
        if self._proc and self._proc.stdin:
            try:
                self._proc.stdin.write(cmd)
                await self._proc.stdin.drain()
            except Exception as e:
                logger.error(f"[{self.mac}] Error sending command {cmd}: {e}")

    async def _read_loop(self):
        """Continuously reads lines from gatttool stdout"""
        try:
            while self._proc and self._proc.stdout:
                line_bytes = await self._proc.stdout.readline()
                if not line_bytes:
                    break
                line = ANSI_RE.sub("", line_bytes.decode("utf-8", errors="ignore")).strip()

                if "Connection successful" in line:
                    logger.info(f"[{self.mac}] Connection successful!")
                    self.state.connected = True
                    self._connected_event.set()
                    self._notify_change()

                elif "Notification handle" in line:
                    self._parse_notification_line(line)

                elif "Characteristic value/descriptor:" in line:
                    # the line may start with the prompt "[AA:BB:..][LE]>", so don't split on ":"
                    m = re.search(r"Characteristic value/descriptor:\s*([0-9a-fA-F ]+)", line)
                    val_hex = m.group(1).replace(" ", "") if m else ""
                    try:
                        decoded = bytes.fromhex(val_hex).decode("utf-8", errors="ignore").strip()
                        if decoded and decoded.startswith("GP"):
                            self.state.name = f"GoPro {decoded[-4:]}"
                            self._notify_change()
                    except Exception:
                        pass

                elif ("Disconnected" in line or "Invalid file descriptor" in line
                      or ("error" in line.lower() and ("fail" in line.lower() or "connect" in line.lower()))):
                    if self.state.connected:
                        logger.warning(f"[{self.mac}] gatttool session disconnected: {line}")
                        self.state.connected = False
                        self.state.ready = False
                        self.state.recording = False
                        self._notify_change()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[{self.mac}] Exception in read loop: {e}")
        finally:
            if self.state.connected:
                self.state.connected = False
                self.state.ready = False
                self._notify_change()

    def _parse_notification_line(self, line: str):
        m = re.search(r"handle\s*=\s*(0x[0-9a-fA-F]+)\s*value:\s*([0-9a-fA-F\s]+)", line)
        if not m:
            return

        handle = int(m.group(1), 16)
        reassembler = self._reassemblers.get(handle)
        if reassembler is None:
            return
        payload = reassembler.feed(bytes.fromhex(m.group(2).replace(" ", "")))
        if not payload:
            return

        waiter = self._waiters.get(handle)
        if waiter and not waiter.done():
            waiter.set_result(payload)

        if len(payload) < 2:
            return
        req_id, status = payload[0], payload[1]

        if handle == HANDLE_CMD_RSP:
            if req_id == 0x3C and status == 0:
                strings = parse_string_list(payload[2:])
                if len(strings) >= 2 and strings[1]:
                    self.state.name = strings[1]
                if len(strings) >= 4 and strings[3]:
                    self.state.firmware_version = strings[3]
                if len(strings) >= 5 and strings[4]:
                    self.state.serial_number = strings[4]
                self._notify_change()

        elif handle == HANDLE_SET_RSP:
            params = parse_tlv_parameters(payload[2:])
            if params:
                self.state.update_from_settings_params(params)
                self._notify_change()

        elif handle == HANDLE_QRY_RSP:
            params = parse_tlv_parameters(payload[2:])
            if req_id in (0x12, 0x52, 0x92):
                self.state.update_from_settings_params(params)
            else:
                self.state.update_from_status_params(params)
            self._notify_change()

    async def _request(self, req_handle: int, rsp_handle: int, data: bytes,
                       timeout: float = RESPONSE_TIMEOUT) -> Optional[bytes]:
        """Write to req_handle and wait for the next complete response on rsp_handle."""
        if not self._proc:
            return None
        async with self._io_lock:
            fut = asyncio.get_running_loop().create_future()
            self._waiters[rsp_handle] = fut
            try:
                await self._send_raw(f"char-write-req 0x{req_handle:04x} {data.hex()}\n".encode())
                return await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                return None
            finally:
                if self._waiters.get(rsp_handle) is fut:
                    del self._waiters[rsp_handle]

    async def _command(self, req_handle: int, rsp_handle: int, data: bytes,
                       require_ack: bool = True) -> bool:
        """Send a command/setting and report success only if the camera acknowledges it."""
        if not (self._proc and self.state.connected and self.state.ready):
            if not await self.connect():
                return False

        if not require_ack:
            await self._send_raw(f"char-write-req 0x{req_handle:04x} {data.hex()}\n".encode())
            return True

        rsp = await self._request(req_handle, rsp_handle, data)
        if rsp is None:
            self.state.ready = False
            self._set_error(NOT_PAIRED_MSG if not self.state.paired else
                            "Camera did not answer the command (connection lost?).")
            return False
        if len(rsp) >= 2 and rsp[1] != 0:
            self._set_error(f"Camera rejected command 0x{data[1]:02x} (status {rsp[1]}).")
            return False
        if self.state.last_error:
            self.state.last_error = ""
            self._notify_change()
        return True

    async def disconnect(self):
        """Disconnect session (turns a running beep off first)."""
        if self._beep_off_task and not self._beep_off_task.done():
            self._beep_off_task.cancel()
            if self.state.connected:
                await self._command(HANDLE_CMD_REQ, HANDLE_CMD_RSP, cmd_locate(False))
        async with self._lock:
            await self._cleanup()
            self.state.connected = False
            self.state.ready = False
            self.state.recording = False
            self._notify_change()

    async def _cleanup(self):
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        for fut in self._waiters.values():
            if not fut.done():
                fut.cancel()
        self._waiters.clear()
        for r in self._reassemblers.values():
            r.reset()
        if self._proc:
            try:
                if self._proc.stdin:
                    self._proc.stdin.write(b"disconnect\nexit\n")
                    await self._proc.stdin.drain()
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    # --- High-level Control Actions ---

    async def start_recording(self) -> bool:
        ok = await self._command(HANDLE_CMD_REQ, HANDLE_CMD_RSP, cmd_shutter(True))
        if ok:
            self.state.recording = True
            self._notify_change()
        return ok

    async def stop_recording(self) -> bool:
        ok = await self._command(HANDLE_CMD_REQ, HANDLE_CMD_RSP, cmd_shutter(False))
        if ok:
            self.state.recording = False
            self._notify_change()
        return ok

    async def set_mode(self, mode: int) -> bool:
        ok = await self._command(HANDLE_CMD_REQ, HANDLE_CMD_RSP, cmd_mode(mode))
        if ok:
            self.state.mode = mode
            self._notify_change()
        return ok

    async def set_submode(self, mode: int, submode: int) -> bool:
        ok = await self._command(HANDLE_CMD_REQ, HANDLE_CMD_RSP, cmd_submode(mode, submode))
        if ok:
            self.state.mode = mode
            self.state.submode = submode
            self._notify_change()
        return ok

    async def locate(self, enable: bool) -> bool:
        if not enable and self._beep_off_task and not self._beep_off_task.done():
            self._beep_off_task.cancel()
        return await self._command(HANDLE_CMD_REQ, HANDLE_CMD_RSP, cmd_locate(enable))

    async def beep(self, seconds: float = DEFAULT_BEEP_SECONDS) -> bool:
        """Locate beep that switches itself off after `seconds`."""
        ok = await self.locate(True)
        if ok and seconds > 0:
            if self._beep_off_task and not self._beep_off_task.done():
                self._beep_off_task.cancel()
            self._beep_off_task = asyncio.create_task(self._beep_off_later(seconds))
        return ok

    async def _beep_off_later(self, seconds: float):
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        for _ in range(3):  # make sure it really stops
            if await self._command(HANDLE_CMD_REQ, HANDLE_CMD_RSP, cmd_locate(False)):
                return
            await asyncio.sleep(1.0)
        logger.error(f"[{self.mac}] Could not switch the locate beep off")

    async def sleep(self) -> bool:
        # Camera may power down before answering, so don't wait for an ack
        ok = await self._command(HANDLE_CMD_REQ, HANDLE_CMD_RSP, cmd_sleep(), require_ack=False)
        if ok:
            self.state.recording = False
            self.state.connected = False
            self.state.ready = False
            self._notify_change()
        return ok

    async def tag(self) -> bool:
        return await self._command(HANDLE_CMD_REQ, HANDLE_CMD_RSP, cmd_tag())

    async def set_wifi(self, enable: bool) -> bool:
        ok = await self._command(HANDLE_CMD_REQ, HANDLE_CMD_RSP, cmd_wifi(enable))
        if ok:
            self.state.wifi_on = enable
            self._notify_change()
        return ok

    async def sync_time(self) -> bool:
        return await self._command(HANDLE_CMD_REQ, HANDLE_CMD_RSP, cmd_datetime())

    async def set_setting(self, setting_id: int, value: int) -> bool:
        ok = await self._command(HANDLE_SET_REQ, HANDLE_SET_RSP, setting_write_1byte(setting_id, value))
        if ok:
            if setting_id == 2:
                self.state.resolution = value
            elif setting_id == 3:
                self.state.fps = value
            elif setting_id == 4:
                self.state.fov = value
            elif setting_id == 78:
                self.state.eis = (value == 1)
            self._notify_change()
        return ok


class MockCameraClient(CameraClient):
    """Simulated camera client for development & testing without physical hardware"""
    def __init__(self, mac: str, name: str = "GoPro HERO5 (Mock)", on_state_change: Optional[Callable[[CameraState], None]] = None):
        super().__init__(mac, name, on_state_change)
        self.state.battery_level = 88
        self.state.free_kb = 64 * 1024 * 1024  # 64 GB
        self.state.firmware_version = "HD5.02.02.70.00"
        self.state.serial_number = f"C31{mac.replace(':', '')[-7:]}"
        self._timer_task: Optional[asyncio.Task] = None

    async def check_paired(self) -> bool:
        return self.state.paired

    async def pair(self, timeout: float = 1.0, force: bool = False) -> bool:
        await asyncio.sleep(0.5)
        self.state.paired = True
        self._notify_change()
        return await self.connect()

    async def beep(self, seconds: float = 3.0) -> bool:
        logger.info(f"[{self.mac}] [MOCK] Beep for {seconds}s")
        return True

    async def connect(self, timeout: float = 1.0) -> bool:
        await asyncio.sleep(0.3)
        self.state.connected = True
        self.state.paired = True
        self.state.ready = True
        self._notify_change()
        if not self._timer_task:
            self._timer_task = asyncio.create_task(self._sim_loop())
        return True

    async def disconnect(self):
        self.state.connected = False
        self.state.ready = False
        self.state.recording = False
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None
        self._notify_change()

    async def _sim_loop(self):
        try:
            while True:
                await asyncio.sleep(1.0)
                if self.state.recording:
                    self.state.record_seconds += 1
                    if self.state.free_kb > 5000:
                        self.state.free_kb -= 5000
                    self._notify_change()
        except asyncio.CancelledError:
            pass

    async def start_recording(self) -> bool:
        self.state.recording = True
        self.state.record_seconds = 0
        self._notify_change()
        return True

    async def stop_recording(self) -> bool:
        self.state.recording = False
        self._notify_change()
        return True

    async def set_mode(self, mode: int) -> bool:
        self.state.mode = mode
        self._notify_change()
        return True

    async def set_submode(self, mode: int, submode: int) -> bool:
        self.state.mode = mode
        self.state.submode = submode
        self._notify_change()
        return True

    async def locate(self, enable: bool) -> bool:
        logger.info(f"[{self.mac}] [MOCK] Beep {'ON' if enable else 'OFF'}")
        return True

    async def sleep(self) -> bool:
        self.state.recording = False
        self.state.connected = False
        self._notify_change()
        return True

    async def tag(self) -> bool:
        logger.info(f"[{self.mac}] [MOCK] Tag moment added!")
        return True

    async def set_wifi(self, enable: bool) -> bool:
        self.state.wifi_on = enable
        self._notify_change()
        return True

    async def sync_time(self) -> bool:
        return True

    async def set_setting(self, setting_id: int, value: int) -> bool:
        if setting_id == 2:
            self.state.resolution = value
        elif setting_id == 3:
            self.state.fps = value
        elif setting_id == 4:
            self.state.fov = value
        elif setting_id == 78:
            self.state.eis = (value == 1)
        self._notify_change()
        return True
