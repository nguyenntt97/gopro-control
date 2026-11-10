#!/usr/bin/env python3
"""
GoPro HERO5 BLE Command Line Diagnostic & Test Tool
Usage:
  python cli_test.py --scan
  python cli_test.py --mac DE:71:28:EE:A7:FD --beep
  python cli_test.py --mac DE:71:28:EE:A7:FD --shutter start
  python cli_test.py --mac DE:71:28:EE:A7:FD --shutter stop
  python cli_test.py --mac DE:71:28:EE:A7:FD --status
  python cli_test.py --mock --shutter start
"""

import argparse
import asyncio
import logging
import os
import sys

# Ensure local modules are found regardless of working directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from camera_client import CameraClient, MockCameraClient
from manager import MultiCameraManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cli_test")

async def main():
    parser = argparse.ArgumentParser(description="GoPro HERO5 BLE Direct CLI")
    parser.add_argument("--scan", action="store_true", help="Scan for nearby GoPro BLE devices")
    parser.add_argument("--mac", type=str, help="Target GoPro Bluetooth MAC address")
    parser.add_argument("--mock", action="store_true", help="Run with simulated mock camera")
    parser.add_argument("--beep", action="store_true", help="Trigger camera locate beep (on 2s, then off)")
    parser.add_argument("--shutter", choices=["start", "stop"], help="Trigger recording start or stop")
    parser.add_argument("--mode", choices=["video", "photo", "multishot"], help="Switch primary mode")
    parser.add_argument("--sleep", action="store_true", help="Put camera to sleep (BLE stays active)")
    parser.add_argument("--wifi", choices=["on", "off"], help="Turn camera WiFi AP on or off")
    parser.add_argument("--sync-time", action="store_true", help="Sync camera clock to PC")
    parser.add_argument("--status", action="store_true", help="Connect and display camera status for 5 seconds")

    args = parser.parse_args()

    if args.scan:
        logger.info("Scanning for GoPro BLE devices (5 seconds)...")
        mgr = MultiCameraManager(mock_mode=args.mock)
        found = await mgr.scan_ble(timeout=5.0)
        if not found:
            print("No GoPro devices discovered in this scan.")
            print("Tip: On the GoPro, swipe down -> Connections -> Connect New Device -> GoPro App to ensure BLE is advertising.")
        else:
            print("\nFound GoPro devices:")
            for d in found:
                print(f"  - MAC: {d['mac']} | Name: {d['name']} | RSSI: {d.get('rssi')} dBm")
        return

    if not args.mac and not args.mock:
        parser.print_help()
        sys.exit(1)

    mac = args.mac or "C1:22:33:44:55:01"
    client_cls = MockCameraClient if args.mock else CameraClient

    def state_callback(s):
        logger.info(f"State Update: Rec={s.recording} ({s.record_seconds}s) | Bat={s.battery_level}% | Mode={s.mode_name} | Free={s.free_kb/1024/1024:.2f}GB")

    client = client_cls(mac, on_state_change=state_callback)

    logger.info(f"Connecting to {mac}...")
    connected = await client.connect()
    if not connected:
        logger.error(f"Failed to connect to {mac}.")
        sys.exit(1)

    try:
        if args.beep:
            logger.info("Turning locate beep ON...")
            await client.locate(True)
            await asyncio.sleep(2.0)
            logger.info("Turning locate beep OFF...")
            await client.locate(False)

        elif args.shutter == "start":
            logger.info("Starting recording...")
            await client.start_recording()
            logger.info("Recording started. Waiting 3 seconds...")
            await asyncio.sleep(3.0)

        elif args.shutter == "stop":
            logger.info("Stopping recording...")
            await client.stop_recording()

        elif args.mode:
            modes = {"video": 0, "photo": 1, "multishot": 2}
            logger.info(f"Setting mode to {args.mode}...")
            await client.set_mode(modes[args.mode])

        elif args.sleep:
            logger.info("Sending sleep command...")
            await client.sleep()

        elif args.wifi:
            logger.info(f"Turning WiFi {args.wifi.upper()}...")
            await client.set_wifi(args.wifi == "on")

        elif args.sync_time:
            logger.info("Synchronizing time with host...")
            await client.sync_time()

        elif args.status or True:
            logger.info("Listening for camera status for 3 seconds...")
            await asyncio.sleep(3.0)
            print("\nCamera Status:")
            for k, v in client.state.to_dict().items():
                print(f"  {k:18}: {v}")

    finally:
        logger.info("Disconnecting...")
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
