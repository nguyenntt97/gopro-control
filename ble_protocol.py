"""
GoPro HERO5 BLE Protocol Definitions, Packet Encoders, and Notification Decoders.
Reverse-engineered based on KonradIT/goprowifihack and BLE captures.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
import struct

# --- UUIDs ---
SERVICE_GOPRO = "0000fea6-0000-1000-8000-00805f9b34fb"
SERVICE_GOPRO_ALT = "0000fea5-0000-1000-8000-00805f9b34fb"

UUID_COMMAND_REQ = "b5f90072-aa8d-11e3-9046-0002a5d5c51b"  # Handle 0x002f
UUID_COMMAND_RSP = "b5f90073-aa8d-11e3-9046-0002a5d5c51b"  # Handle 0x0031
UUID_SETTING_REQ = "b5f90074-aa8d-11e3-9046-0002a5d5c51b"  # Handle 0x0034
UUID_SETTING_RSP = "b5f90075-aa8d-11e3-9046-0002a5d5c51b"  # Handle 0x0036
UUID_QUERY_REQ   = "b5f90076-aa8d-11e3-9046-0002a5d5c51b"  # Handle 0x0039
UUID_QUERY_RSP   = "b5f90077-aa8d-11e3-9046-0002a5d5c51b"  # Handle 0x003b

UUID_WIFI_SSID   = "b5f90002-aa8d-11e3-9046-0002a5d5c51b"
UUID_WIFI_PASS   = "b5f90003-aa8d-11e3-9046-0002a5d5c51b"

# --- Modes & Settings Constants ---
MODE_VIDEO = 0
MODE_PHOTO = 1
MODE_MULTISHOT = 2

SUBMODE_VIDEO_SINGLE = (0, 0)
SUBMODE_VIDEO_TIMELAPSE = (0, 1)
SUBMODE_VIDEO_PHOTO = (0, 2)
SUBMODE_VIDEO_LOOP = (0, 3)

SUBMODE_PHOTO_SINGLE = (1, 1)
SUBMODE_PHOTO_NIGHT = (1, 2)

SUBMODE_MULTISHOT_BURST = (2, 0)
SUBMODE_MULTISHOT_TIMELAPSE = (2, 1)
SUBMODE_MULTISHOT_NITELAPSE = (2, 2)

RESOLUTION_NAMES = {
    1: "4K",
    4: "2.7K",
    6: "2.7K 4:3",
    7: "1440p",
    9: "1080p",
    10: "960p",
    12: "720p",
    17: "WVGA"
}

FPS_NAMES = {
    0: "240fps",
    1: "120fps",
    2: "100fps",
    3: "90fps",
    4: "80fps",
    5: "60fps",
    6: "50fps",
    7: "48fps",
    8: "30fps",
    9: "25fps"
}

FOV_NAMES = {
    0: "Wide",
    1: "Medium",
    2: "Narrow",
    3: "SuperView",
    4: "Linear"
}

# --- Command Builders ---

def cmd_shutter(record: bool) -> bytes:
    """03 01 01 01 to start, 03 01 01 00 to stop"""
    return bytes([0x03, 0x01, 0x01, 0x01 if record else 0x00])

def cmd_mode(mode: int) -> bytes:
    """Switch primary mode: 0=Video, 1=Photo, 2=MultiShot"""
    return bytes([0x03, 0x02, 0x01, mode & 0xFF])

def cmd_submode(mode: int, submode: int) -> bytes:
    """Switch sub-mode: e.g. 05 03 01 <mode> 01 <submode>"""
    return bytes([0x05, 0x03, 0x01, mode & 0xFF, 0x01, submode & 0xFF])

def cmd_sleep() -> bytes:
    """Puts camera to sleep while keeping BLE advertising active (02 01 05)"""
    return bytes([0x02, 0x01, 0x05])

def cmd_locate(beep: bool) -> bytes:
    """Locate beeping sound: 03 16 01 01 (on) or 03 16 01 00 (off)"""
    return bytes([0x03, 0x16, 0x01, 0x01 if beep else 0x00])

def cmd_tag() -> bytes:
    """Add a HiLight moment tag"""
    return bytes([0x02, 0x01, 0x18])

def cmd_wifi(enable: bool) -> bytes:
    """Turn WiFi AP on/off: 03 17 01 01 (on) or 03 17 01 00 (off)"""
    return bytes([0x03, 0x17, 0x01, 0x01 if enable else 0x00])

def cmd_camera_info() -> bytes:
    """Request camera info (name, fw, serial) on Command Req (01 3c)"""
    return bytes([0x01, 0x3C])

def cmd_datetime(dt: Optional[datetime] = None) -> bytes:
    """
    Set camera date and time.
    Format: 09 0D 07 <YY_hi> <YY_lo> <MM> <DD> <hh> <mm> <ss>
    """
    if dt is None:
        dt = datetime.now()
    year_hi = (dt.year >> 8) & 0xFF
    year_lo = dt.year & 0xFF
    return bytes([0x09, 0x0D, 0x07, year_hi, year_lo, dt.month, dt.day, dt.hour, dt.minute, dt.second])

# --- Query Requests (sent to UUID_QUERY_REQ) ---

def query_subscribe_updates() -> bytes:
    """Send 01 53 to Query Req to activate periodic status notifications"""
    return bytes([0x01, 0x53])

def query_status() -> bytes:
    """Send 01 13 to Query Req for full status dump"""
    return bytes([0x01, 0x13])

def query_settings() -> bytes:
    """Send 01 12 to Query Req for full settings dump"""
    return bytes([0x01, 0x12])

# --- Setting Builders (sent to UUID_SETTING_REQ) ---

def setting_write_1byte(setting_id: int, value: int) -> bytes:
    """Set a single 1-byte setting: 03 <setting_id> 01 <value>"""
    return bytes([0x03, setting_id & 0xFF, 0x01, value & 0xFF])

def setting_resolution(value: int) -> bytes:
    """Setting ID 2: 1=4K, 4=2.7K, 7=1440p, 9=1080p, 12=720p"""
    return setting_write_1byte(2, value)

def setting_fps(value: int) -> bytes:
    """Setting ID 3: 0=240, 1=120, 5=60, 8=30, 9=25"""
    return setting_write_1byte(3, value)

def setting_fov(value: int) -> bytes:
    """Setting ID 4: 0=Wide, 1=Medium, 2=Narrow, 3=SuperView, 4=Linear"""
    return setting_write_1byte(4, value)

def setting_eis(enable: bool) -> bytes:
    """Setting ID 78: Video stabilization 1=On, 0=Off"""
    return setting_write_1byte(78, 1 if enable else 0)

def setting_leds(mode: int) -> bytes:
    """Setting ID 91: 0=Off, 1=Front off, 2=All on"""
    return setting_write_1byte(91, mode)

def setting_beeps(mode: int) -> bytes:
    """Setting ID 56: 0=Full, 1=70%, 2=Off"""
    return setting_write_1byte(56, mode)


# --- Multi-Packet Reassembler ---

class BlePacketReassembler:
    """
    Reassembles multi-packet GoPro BLE notifications.
    Packets may start with:
    - len < 0x20: single packet of specified length.
    - 0x20 <len>: start of multi-packet message with 1-byte length.
    - 0x21 <len_lo>: start of multi-packet message with 12-bit length ((byte[0] & 0x0F) << 8 | byte[1]).
    Follow-up packets start with sequence indicator 0x80..0x8F.
    """
    def __init__(self):
        self.buffer = bytearray()
        self.expected_len = 0
        self.in_progress = False

    def reset(self):
        self.buffer.clear()
        self.expected_len = 0
        self.in_progress = False

    def feed(self, data: bytes) -> Optional[bytes]:
        """
        Feeds a chunk of bytes. Returns full assembled message payload (without header bytes)
        when complete, or None if more packets are expected.
        """
        if not data:
            return None

        first = data[0]

        # Check if continuation packet (0x80 to 0x8F)
        if 0x80 <= first <= 0x8F and self.in_progress:
            # Continuation: strip sequence byte
            self.buffer.extend(data[1:])
            if len(self.buffer) >= self.expected_len:
                full = bytes(self.buffer[:self.expected_len])
                self.reset()
                return full
            return None

        # New message starting
        self.reset()

        if first < 0x20:
            # Single packet: length is 'first'
            self.expected_len = first
            chunk = data[1:]
            self.buffer.extend(chunk)
            if len(self.buffer) >= self.expected_len:
                full = bytes(self.buffer[:self.expected_len])
                self.reset()
                return full
            else:
                self.in_progress = True
                return None

        elif first == 0x20:
            # Multi-packet with 1-byte length at data[1]
            if len(data) < 2:
                return None
            self.expected_len = data[1]
            chunk = data[2:]
            self.buffer.extend(chunk)
            self.in_progress = True
            if len(self.buffer) >= self.expected_len:
                full = bytes(self.buffer[:self.expected_len])
                self.reset()
                return full
            return None

        elif (first & 0xF0) == 0x20:
            # Multi-packet with 12-bit length in (first & 0x0F) << 8 | data[1]
            if len(data) < 2:
                return None
            self.expected_len = ((first & 0x0F) << 8) | data[1]
            chunk = data[2:]
            self.buffer.extend(chunk)
            self.in_progress = True
            if len(self.buffer) >= self.expected_len:
                full = bytes(self.buffer[:self.expected_len])
                self.reset()
                return full
            return None

        else:
            # Unknown header, treat as raw payload
            return data


# --- Decoders for Status & Settings ---

def parse_tlv_parameters(data: bytes) -> Dict[int, Any]:
    """
    Parse GoPro TLV (Type-Length-Value) parameter lists from status or setting queries.
    Format: [Param_ID: 1 byte, Length: 1 byte, Value: Length bytes...]
    """
    params: Dict[int, Any] = {}
    idx = 0
    while idx + 2 <= len(data):
        param_id = data[idx]
        length = data[idx + 1]
        val_bytes = data[idx + 2 : idx + 2 + length]
        if len(val_bytes) < length:
            break

        # Decode by length
        if length == 1:
            val = val_bytes[0]
        elif length == 2:
            val = int.from_bytes(val_bytes, byteorder="big", signed=False)
        elif length == 4:
            val = int.from_bytes(val_bytes, byteorder="big", signed=False)
        elif length == 8:
            val = int.from_bytes(val_bytes, byteorder="big", signed=False)
        else:
            val = val_bytes

        params[param_id] = val
        idx += 2 + length
    return params


def parse_string_list(data: bytes) -> List[str]:
    """
    Parse GoPro string lists (such as Camera Info response 0x3C).
    Format: [Length: 1 byte, ASCII String: Length bytes...]
    """
    strings: List[str] = []
    idx = 0
    while idx + 1 <= len(data):
        length = data[idx]
        if length == 0:
            strings.append("")
            idx += 1
            continue
        str_bytes = data[idx + 1 : idx + 1 + length]
        if len(str_bytes) < length:
            break
        try:
            strings.append(str_bytes.decode("utf-8", errors="replace"))
        except Exception:
            strings.append(str_bytes.hex())
        idx += 1 + length
    return strings


@dataclass
class CameraState:
    mac: str
    name: str = "GoPro HERO5"
    nickname: str = ""        # user label kept in cameras.json; never overwritten by the camera
    connected: bool = False   # BLE link is up
    paired: bool = False      # BlueZ holds a bond for this camera
    ready: bool = False       # camera actually answers commands
    last_error: str = ""
    recording: bool = False
    record_seconds: int = 0
    battery_level: int = 0
    battery_status: int = 0  # 0=empty, 1=low, 2=halfway, 3=full, 4=charging
    mode: int = 0  # 0=video, 1=photo, 2=multishot
    submode: int = 0
    free_kb: int = 0
    resolution: int = 9  # default 1080p
    fps: int = 5  # default 60fps
    fov: int = 0  # default Wide
    eis: bool = False
    wifi_on: bool = False
    firmware_version: str = ""
    serial_number: str = ""
    last_seen: float = 0.0

    @property
    def mode_name(self) -> str:
        return {0: "Video", 1: "Photo", 2: "MultiShot"}.get(self.mode, "Unknown")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mac": self.mac,
            "name": self.name,
            "nickname": self.nickname,
            "display_name": self.nickname or self.name,
            "connected": self.connected,
            "paired": self.paired,
            "ready": self.ready,
            "last_error": self.last_error,
            "recording": self.recording,
            "record_seconds": self.record_seconds,
            "battery_level": self.battery_level,
            "battery_status": self.battery_status,
            "mode": self.mode,
            "mode_name": self.mode_name,
            "free_gb": round(self.free_kb / 1024 / 1024, 2) if self.free_kb else 0,
            "resolution": self.resolution,
            "resolution_name": RESOLUTION_NAMES.get(self.resolution, str(self.resolution)),
            "fps": self.fps,
            "fps_name": FPS_NAMES.get(self.fps, str(self.fps)),
            "fov": self.fov,
            "fov_name": FOV_NAMES.get(self.fov, str(self.fov)),
            "eis": self.eis,
            "wifi_on": self.wifi_on,
            "firmware_version": self.firmware_version,
            "serial_number": self.serial_number,
            "last_seen": self.last_seen,
        }

    def update_from_status_params(self, params: Dict[int, Any]):
        """Updates internal state from parsed Status TLV dictionary"""
        if 8 in params:
            self.recording = (params[8] == 1)
        if 13 in params:
            self.record_seconds = int(params[13])
        if 70 in params:
            self.battery_level = int(params[70])
        if 2 in params:
            self.battery_status = int(params[2])
        if 43 in params:
            self.mode = int(params[43])
        if 54 in params:
            self.free_kb = int(params[54])
        elif 44 in params:
            self.free_kb = int(params[44])

    def update_from_settings_params(self, params: Dict[int, Any]):
        """Updates internal state from parsed Settings TLV dictionary"""
        if 2 in params:
            self.resolution = int(params[2])
        if 3 in params:
            self.fps = int(params[3])
        if 4 in params:
            self.fov = int(params[4])
        if 78 in params:
            self.eis = (params[78] == 1)
