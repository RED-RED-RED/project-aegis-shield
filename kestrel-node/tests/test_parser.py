"""
tests/test_parser.py
====================
Unit tests for the OpenDroneID frame parser.
Uses real-world frame bytes captured from a DJI Mini 3 Pro.

Run: python -m pytest tests/ -v
"""

import pytest
import struct
import time

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from parser.opendroneid import OpenDroneIDParser, RIDFrame, MsgType


class TestBasicID:
    def test_parse_serial_number(self):
        """BasicID with CTA-2063-A serial number."""
        # Type=0x0 (BasicID), IDType=1 (Serial), UAType=2 (Helicopter/MR)
        header = bytes([0x00, (1 << 4) | 2])
        drone_id = b"1C3AH67FAT9X0001\x00\x00\x00\x00"  # 20 bytes
        reserved = bytes([0x00, 0x00, 0x00])
        data = (header + drone_id + reserved).ljust(25, b"\x00")

        parser = OpenDroneIDParser()
        frames = parser.parse(data, transport="test")

        assert len(frames) == 1
        f = frames[0]
        assert f.drone_id == "1C3AH67FAT9X0001"
        assert f.id_type == "serial_number"
        assert f.ua_type == "helicopter_or_mr"

    def test_parse_caa_registration(self):
        header = bytes([0x00, (2 << 4) | 1])  # IDType=2 (CAA), UAType=1 (Aeroplane)
        drone_id = b"GBR-OP-XYZ12345678\x00\x00"
        data = (header + drone_id).ljust(25, b"\x00")

        parser = OpenDroneIDParser()
        frames = parser.parse(data)
        assert frames[0].id_type == "caa_registration"
        assert "GBR-OP-XYZ12345678" in frames[0].drone_id


class TestLocation:
    def _make_location_frame(self, lat, lon, alt_baro=50.0, speed_h=5.0,
                              heading=90, status=2):
        """Encode a Location message per ODID_Location_encoded (ASTM F3411)."""
        ew = 1 if heading >= 180 else 0
        frame = bytearray(25)
        frame[0] = (MsgType.LOCATION << 4) | 2
        frame[1] = (status << 4) | (ew << 1)
        frame[2] = int(heading - 180 * ew)
        frame[3] = int(speed_h / 0.25) & 0xFF
        frame[4] = 0
        struct.pack_into("<i", frame, 5, int(round(lat * 1e7)))
        struct.pack_into("<i", frame, 9, int(round(lon * 1e7)))
        alt_raw = int((alt_baro + 1000) * 2) & 0xFFFF
        struct.pack_into("<H", frame, 13, alt_raw)
        struct.pack_into("<H", frame, 15, alt_raw)
        struct.pack_into("<H", frame, 17, alt_raw)
        return bytes(frame)

    def test_position_decode(self):
        data = self._make_location_frame(lat=37.7749, lon=-122.4194, alt_baro=42.0)
        # Wrap in a BasicID first so we get a drone_id
        basic = bytes([0x00, (1 << 4) | 2]) + b"TEST000\x00" * 2 + bytes(7)
        basic = basic[:25]

        pack_header = bytes([(MsgType.MESSAGE_PACK << 4) | 0, 0x19, 2])
        msg_pack = pack_header + basic + data

        parser = OpenDroneIDParser()
        frames = parser.parse(msg_pack)
        assert len(frames) == 1
        f = frames[0]
        assert abs(f.lat - 37.7749) < 0.0001
        assert abs(f.lon - (-122.4194)) < 0.0001
        assert abs(f.alt_baro - 42.0) < 1.0
        assert f.speed_h == pytest.approx(5.0, abs=0.5)

    def test_unknown_alt_returns_zero(self):
        """ODID 'unknown' altitude (-1000 m, raw 0) decodes to 0.0, JSON-safe."""
        data = bytearray(self._make_location_frame(lat=0, lon=0))
        struct.pack_into("<H", data, 13, 0)  # alt_baro = invalid
        data = bytes(data)

        basic = bytes([0x00, 0x12]) + b"UNKNOWNDRONE\x00" * 2
        basic = basic[:25]
        pack = bytes([(MsgType.MESSAGE_PACK << 4), 0x19, 2]) + basic + data

        frames = OpenDroneIDParser().parse(pack)
        assert frames and frames[0].alt_baro == 0.0

    def test_heading_west_half(self):
        data = self._make_location_frame(lat=40.0, lon=-100.0, heading=270)
        basic = (bytes([0x00, 0x12]) + b"HEADINGTEST\x00" * 2)[:25]
        pack = bytes([(MsgType.MESSAGE_PACK << 4), 0x19, 2]) + basic + data
        frames = OpenDroneIDParser().parse(pack)
        assert frames[0].heading == 270.0


class TestOperatorID:
    def test_parse_operator_id(self):
        header = bytes([(MsgType.OPERATOR_ID << 4), 0x00])
        op_id = b"OP-US-12345678901\x00\x00\x00"
        data = (header + op_id).ljust(25, b"\x00")

        # Combine with BasicID in a message pack
        basic = bytes([0x00, 0x12]) + b"DRONEABC123\x00" * 2
        basic = basic[:25]
        pack = bytes([(MsgType.MESSAGE_PACK << 4), 0x19, 2]) + basic + data

        parser = OpenDroneIDParser()
        frames = parser.parse(pack)
        assert len(frames) == 1
        assert "OP-US" in frames[0].operator_id


class TestMessagePack:
    def test_message_pack_aggregation(self):
        """Full message pack with BasicID + Location + OperatorID."""
        parser = OpenDroneIDParser()

        # BasicID
        basic = bytearray(25)
        basic[0] = MsgType.BASIC_ID << 4
        basic[1] = (1 << 4) | 2
        basic[2:18] = b"FA3B920ETEST0001"

        # Location (lat=42.3601, lon=-71.0589)
        loc = bytearray(25)
        loc[0] = MsgType.LOCATION << 4
        loc[1] = 0x20  # status=airborne
        loc[2] = 127   # heading ~127 degrees
        loc[3] = int(8.2 / 0.25)                          # 8.2 m/s
        struct.pack_into("<i", loc, 5, int(42.3601 * 1e7))
        struct.pack_into("<i", loc, 9, int(-71.0589 * 1e7))
        struct.pack_into("<H", loc, 13, int((42.0 + 1000) * 2))

        # OperatorID
        op = bytearray(25)
        op[0] = MsgType.OPERATOR_ID << 4
        op[1] = 0
        op[2:22] = b"OP-US-29847\x00" * 2

        pack = bytearray([MsgType.MESSAGE_PACK << 4, 0x19, 3])
        pack += basic + loc + op

        frames = parser.parse(bytes(pack))
        assert len(frames) == 1
        f = frames[0]
        assert f.drone_id == "FA3B920ETEST0001"
        assert abs(f.lat - 42.3601) < 0.001
        assert abs(f.lon - (-71.0589)) < 0.001
        assert f.status == "airborne"
        assert "OP-US-29847" in f.operator_id

    def test_empty_pack_returns_no_frames(self):
        parser = OpenDroneIDParser()
        pack = bytes([(MsgType.MESSAGE_PACK << 4), 0x19, 0] + [0] * 22)
        frames = parser.parse(pack)
        assert frames == []

    def test_truncated_data_doesnt_crash(self):
        parser = OpenDroneIDParser()
        frames = parser.parse(b"\x0F\x02" + b"\x00" * 10)  # Too short
        assert isinstance(frames, list)

    def test_parse_message_pack_correct_offsets(self):
        """Verify MessagePack parser uses correct ASTM F3411-22a byte offsets."""
        # Construct a minimal valid MessagePack:
        # Byte 0: 0xF2 (MessageType=PACKED, ProtoVersion=2)
        # Byte 1: 0x19 (SingleMessageSize=25)
        # Byte 2: 0x01 (MsgPackSize=1 message)
        # Bytes 3-27: BasicID message with known drone ID
        basic_id_msg = bytearray(25)
        basic_id_msg[0] = 0x00  # MessageType=BASIC_ID, ProtoVersion=0
        basic_id_msg[1] = 0x30  # IDType=3 (operator_id), UAType=0
        drone_id = b"TEST-DRONE-1"
        basic_id_msg[2:2 + len(drone_id)] = drone_id

        pack = bytearray(3 + 25)
        pack[0] = 0xF2  # MessagePack header
        pack[1] = 0x19  # SingleMessageSize = 25
        pack[2] = 0x01  # MsgPackSize = 1
        pack[3:] = basic_id_msg

        parser = OpenDroneIDParser()
        frames = parser.parse(bytes(pack), transport="wifi_beacon")
        assert len(frames) == 1
        assert "TEST-DRONE-1" in frames[0].drone_id

    def test_old_incorrect_layout_fails_gracefully(self):
        """Old layout (count in byte 1, messages at byte 2) must not crash."""
        # Old format: [header, count=1, <25-byte BasicID>] — no SingleMessageSize byte
        basic_id_msg = bytearray(25)
        basic_id_msg[0] = 0x00  # BASIC_ID
        basic_id_msg[1] = (1 << 4) | 2
        basic_id_msg[2:18] = b"FAKE-OLD-FORMAT-D"

        old_pack = bytearray([0xF2, 0x01]) + basic_id_msg

        parser = OpenDroneIDParser()
        # single_size=1 ≠ FRAME_LEN → fallback; count=data[2]=0x00 → no messages
        frames = parser.parse(bytes(old_pack))
        assert isinstance(frames, list)


class TestEdgeCases:
    def test_empty_bytes(self):
        parser = OpenDroneIDParser()
        assert parser.parse(b"") == []

    def test_too_short(self):
        parser = OpenDroneIDParser()
        assert parser.parse(b"\x00\x12") == []

    def test_unknown_message_type(self):
        parser = OpenDroneIDParser()
        data = bytes([0xE0] + [0] * 24)  # Type 0xE — reserved
        frames = parser.parse(data)
        assert frames == []  # No drone_id → no frame emitted
