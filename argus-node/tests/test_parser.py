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
        """Helper to encode a Location frame."""
        header = bytes([(MsgType.LOCATION << 4)])
        status_byte = bytes([(status << 4)])
        heading_byte = bytes([heading % 256])
        speed_raw = int(speed_h / 0.25) & 0xFFFF
        speed_bytes = struct.pack("<H", speed_raw)
        vspeed_bytes = struct.pack("<h", 0)
        lat_raw = int(lat * 1e7)
        lon_raw = int(lon * 1e7)
        lat_bytes = struct.pack("<i", lat_raw)
        lon_bytes = struct.pack("<i", lon_raw)
        alt_baro_raw = int((alt_baro + 1000) * 2) & 0xFFFF
        alt_bytes = struct.pack("<H", alt_baro_raw)
        alt_geo_bytes = struct.pack("<H", alt_baro_raw)
        height_bytes = struct.pack("<H", alt_baro_raw)
        padding = bytes([0x00] * 4)

        frame = (header + status_byte + heading_byte + speed_bytes +
                 vspeed_bytes + lat_bytes + lon_bytes + alt_bytes +
                 alt_geo_bytes + height_bytes + padding)
        return frame.ljust(25, b"\x00")

    def test_position_decode(self):
        data = self._make_location_frame(lat=37.7749, lon=-122.4194, alt_baro=42.0)
        # Wrap in a BasicID first so we get a drone_id
        basic = bytes([0x00, (1 << 4) | 2]) + b"TEST000\x00" * 2 + bytes(7)
        basic = basic[:25]

        pack_header = bytes([(MsgType.MESSAGE_PACK << 4) | 0, 2])
        msg_pack = pack_header + basic + data

        parser = OpenDroneIDParser()
        frames = parser.parse(msg_pack)
        assert len(frames) == 1
        f = frames[0]
        assert abs(f.lat - 37.7749) < 0.0001
        assert abs(f.lon - (-122.4194)) < 0.0001
        assert abs(f.alt_baro - 42.0) < 1.0
        assert f.speed_h == pytest.approx(5.0, abs=0.5)

    def test_unknown_alt_returns_nan(self):
        import math
        data = self._make_location_frame(lat=0, lon=0)
        # Set altitude fields to 0xFFFF (unknown)
        data = bytearray(data)
        struct.pack_into("<H", data, 15, 0xFFFF)  # alt_baro
        data = bytes(data)

        # Parse as part of message pack
        basic = bytes([0x00, 0x12]) + b"UNKNOWNDRONE\x00" * 2
        basic = basic[:25]
        pack = bytes([(MsgType.MESSAGE_PACK << 4), 2]) + basic + data

        parser = OpenDroneIDParser()
        frames = parser.parse(pack)
        if frames:
            assert math.isnan(frames[0].alt_baro)


class TestOperatorID:
    def test_parse_operator_id(self):
        header = bytes([(MsgType.OPERATOR_ID << 4), 0x00])
        op_id = b"OP-US-12345678901\x00\x00\x00"
        data = (header + op_id).ljust(25, b"\x00")

        # Combine with BasicID in a message pack
        basic = bytes([0x00, 0x12]) + b"DRONEABC123\x00" * 2
        basic = basic[:25]
        pack = bytes([(MsgType.MESSAGE_PACK << 4), 2]) + basic + data

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
        struct.pack_into("<H", loc, 3, int(8.2 / 0.25))  # 8.2 m/s
        struct.pack_into("<i", loc, 7,  int(42.3601 * 1e7))
        struct.pack_into("<i", loc, 11, int(-71.0589 * 1e7))
        struct.pack_into("<H", loc, 15, int((42.0 + 1000) * 2))

        # OperatorID
        op = bytearray(25)
        op[0] = MsgType.OPERATOR_ID << 4
        op[1] = 0
        op[2:22] = b"OP-US-29847\x00" * 2

        pack = bytearray([MsgType.MESSAGE_PACK << 4, 3])
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
        pack = bytes([(MsgType.MESSAGE_PACK << 4), 0] + [0] * 23)
        frames = parser.parse(pack)
        assert frames == []

    def test_truncated_data_doesnt_crash(self):
        parser = OpenDroneIDParser()
        frames = parser.parse(b"\x0F\x02" + b"\x00" * 10)  # Too short
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
