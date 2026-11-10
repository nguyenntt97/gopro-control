"""
Unit tests for GoPro HERO5 BLE Protocol Encoders & Decoders
"""

import unittest
from ble_protocol import (
    cmd_shutter, cmd_mode, cmd_submode, cmd_locate, cmd_sleep, cmd_wifi, cmd_camera_info,
    setting_resolution, setting_fps, setting_fov, setting_eis,
    BlePacketReassembler, parse_tlv_parameters, parse_string_list, CameraState
)

class TestGoProBleProtocol(unittest.TestCase):

    def test_command_encoders(self):
        # Shutter
        self.assertEqual(cmd_shutter(True), bytes([0x03, 0x01, 0x01, 0x01]))
        self.assertEqual(cmd_shutter(False), bytes([0x03, 0x01, 0x01, 0x00]))

        # Mode
        self.assertEqual(cmd_mode(0), bytes([0x03, 0x02, 0x01, 0x00]))
        self.assertEqual(cmd_mode(1), bytes([0x03, 0x02, 0x01, 0x01]))

        # Submode (Photo Night: mode 1, submode 2)
        self.assertEqual(cmd_submode(1, 2), bytes([0x05, 0x03, 0x01, 0x01, 0x01, 0x02]))

        # Locate
        self.assertEqual(cmd_locate(True), bytes([0x03, 0x16, 0x01, 0x01]))
        self.assertEqual(cmd_locate(False), bytes([0x03, 0x16, 0x01, 0x00]))

        # Sleep
        self.assertEqual(cmd_sleep(), bytes([0x02, 0x01, 0x05]))

        # Wifi
        self.assertEqual(cmd_wifi(True), bytes([0x03, 0x17, 0x01, 0x01]))
        self.assertEqual(cmd_wifi(False), bytes([0x03, 0x17, 0x01, 0x00]))

    def test_settings_encoders(self):
        # 4K resolution (Setting 2, val 1)
        self.assertEqual(setting_resolution(1), bytes([0x03, 0x02, 0x01, 0x01]))
        # EIS On (Setting 78, val 1)
        self.assertEqual(setting_eis(True), bytes([0x03, 0x4E, 0x01, 0x01]))

    def test_single_packet_reassembly(self):
        reassembler = BlePacketReassembler()
        # Single packet: length byte = 3, followed by 3 bytes
        raw = bytes([0x03, 0x01, 0x02, 0x03])
        out = reassembler.feed(raw)
        self.assertEqual(out, bytes([0x01, 0x02, 0x03]))

    def test_multi_packet_reassembly(self):
        reassembler = BlePacketReassembler()
        # Start packet with 0x20 and length 8:
        # Header: 20 08, first chunk: AA BB CC
        pkt1 = bytes([0x20, 0x08, 0xAA, 0xBB, 0xCC])
        res1 = reassembler.feed(pkt1)
        self.assertIsNone(res1)

        # Continuation packet 1: 80 DD EE FF
        pkt2 = bytes([0x80, 0xDD, 0xEE, 0xFF])
        res2 = reassembler.feed(pkt2)
        self.assertIsNone(res2)

        # Continuation packet 2: 81 11 22
        pkt3 = bytes([0x81, 0x11, 0x22])
        res3 = reassembler.feed(pkt3)
        self.assertIsNotNone(res3)
        self.assertEqual(res3, bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF, 0x11, 0x22]))

    def test_tlv_status_decoding(self):
        # From bluetooth-api.md example:
        # 0D 04 00 00 00 CE 36 08 00 00 06 C4 21 FD
        # ID 0x0D (13) = recording duration: 4 bytes = 0x000000CE (206)
        # ID 0x36 (54) = remaining free space: 8 bytes
        sample = bytes.fromhex("0D 04 00 00 00 CE 36 08 00 00 00 06 C4 21 FD 00")
        params = parse_tlv_parameters(sample)
        self.assertEqual(params[13], 206)
        self.assertIn(54, params)

        # Apply to CameraState
        state = CameraState(mac="11:22:33:44:55:66")
        state.update_from_status_params(params)
        self.assertEqual(state.record_seconds, 206)

    def test_string_list_decoding(self):
        # Example from bluetooth-api.md line 200:
        # 0B HERO8 Black 04 0x05 0F HD8.01.02.50.00
        raw = bytes.fromhex("0B 48 45 52 4F 38 20 42 6C 61 63 6B 04 30 78 30 35 0F 48 44 38 2E 30 31 2E 30 32 2E 35 30 2E 30 30")
        strings = parse_string_list(raw)
        self.assertEqual(strings[0], "HERO8 Black")
        self.assertEqual(strings[1], "0x05")
        self.assertEqual(strings[2], "HD8.01.02.50.00")

if __name__ == "__main__":
    unittest.main()
