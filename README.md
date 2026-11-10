# ais_gopro — Multi-GoPro HERO5 Remote Controller (BLE Web Interface)

A multi-camera Bluetooth Low Energy (BLE) control system for **GoPro HERO5** cameras on Linux, featuring simultaneous recording, synchronized settings, live telemetry, and an interactive browser dashboard.

---

## Why BLE Instead of Wi-Fi?
Normally, each GoPro camera runs its own Wi-Fi Access Point on the same fixed IP address (`10.5.5.9`). Controlling multiple cameras over Wi-Fi requires multiple physical Wi-Fi adapters or complex routing. With **Bluetooth Low Energy (BLE)**, a single Bluetooth 4.0+ adapter on your PC can control multiple cameras concurrently without messing with Wi-Fi connections.

---

## Features
- **Master Synchronized Controls:**
  - One-click **Record All / Stop All** executed concurrently via `asyncio.gather`.
  - Global Mode Switching (**Video**, **Photo**, **MultiShot**).
  - Global Resolution & Framerate sync (4K, 2.7K, 1440p, 1080p, 720p).
  - **Clock Sync:** Aligns all camera internal date/timestamps to the host PC for perfect multi-cam video alignment.
  - Global **Sleep All** (keeps camera BLE awake for remote wake-up).
- **Per-Camera Dashboard Cards:**
  - Real-time recording indicator and elapsed recording timer (`00:00`).
  - Live battery percentage gauge and free SD card space (GB).
  - Individual locate beep (find camera), moment tagging, and Wi-Fi AP toggles.
  - Per-camera resolution, FPS, and FOV adjustments.
- **BLE Discovery & Pairing:**
  - Built-in 5-second BLE scanner to auto-discover advertising cameras.
  - Mock simulation mode for testing the full web UI offline.

---

## Installation & Setup

The project runs in the dedicated **`gopro-ble`** conda environment:

```bash
conda activate gopro-ble
```

### Initial Camera Pairing (One-time step per camera)
GoPro requires an initial BLE pairing step before accepting GATT commands:
1. On each GoPro HERO5, swipe down from the top:
   **Preferences > Connections > Connect New Device > GoPro App**.
2. Run `bluetoothctl`:
   ```bash
   bluetoothctl
   scan on
   ```
3. Locate your camera (e.g. `[NEW] Device DE:71:28:EE:A7:FD GoPro 2495`).
4. Trust and pair:
   ```bash
   trust DE:71:28:EE:A7:FD
   pair DE:71:28:EE:A7:FD
   exit
   ```

*(Note: If pairing hangs on Linux BlueZ 5.50+, ensure `ControllerMode = le` in `/etc/bluetooth/main.conf` as documented in `goprowifihack/Bluetooth/Platforms/RaspberryPi.md`).*

---

## Running the Web Interface

### 1. Start Server (Live Bluetooth Mode)
```bash
conda run -n gopro-ble python /home/elchris/projects/3dprint/GoPro_UPDATE/ais_gopro/server.py --port 8000
```
Open your browser at: **`http://localhost:8000`**

### 2. Start Server in Mock Simulation Mode (For testing without cameras)
```bash
conda run -n gopro-ble python /home/elchris/projects/3dprint/GoPro_UPDATE/ais_gopro/server.py --mock --port 8000
```
Or simply toggle the **"Mock Simulation"** checkbox in the top-right corner of the web interface at any time!

---

## Command Line Direct Test Tool (`cli_test.py`)

If you want to quickly test a camera from the terminal:

- **Scan for nearby GoPros:**
  ```bash
  conda run -n gopro-ble python ais_gopro/cli_test.py --scan
  ```
- **Locate Beep Test:**
  ```bash
  conda run -n gopro-ble python ais_gopro/cli_test.py --mac <CAMERA_MAC> --beep
  ```
- **Start / Stop Recording:**
  ```bash
  conda run -n gopro-ble python ais_gopro/cli_test.py --mac <CAMERA_MAC> --shutter start
  conda run -n gopro-ble python ais_gopro/cli_test.py --mac <CAMERA_MAC> --shutter stop
  ```
- **Sync Clock:**
  ```bash
  conda run -n gopro-ble python ais_gopro/cli_test.py --mac <CAMERA_MAC> --sync-time
  ```
- **Query Status:**
  ```bash
  conda run -n gopro-ble python ais_gopro/cli_test.py --mac <CAMERA_MAC> --status
  ```
# gopro-control
