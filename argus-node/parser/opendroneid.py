"""
parser/opendroneid.py
=====================
Parses FAA/ASTM F3411 Remote ID (OpenDroneID) byte frames into Python dataclasses.

Supports all message types defined in ASTM F3411-22a:
  Type 0x0 — Basic ID        (drone serial / registration)
  Type 0x1 — Location        (lat, lon, alt, speed, heading)
  Type 0x2 — Authentication  (cryptographic signature, if present)
  Type 0x3 — Self-ID         (operator description string)
  Type 0x4 — System          (operator lat/lon, area count, classification)
  Type 0x5 — Operator ID     (operator registration number)
  Type 0xF — Message Pack    (multiple messages bundled in one frame)

Reference spec: https://www.astm.org/f3411-22a.html
Open source reference: https://github.com/opendroneid/opendroneid-core-c
"""

import math
import struct
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional


# ------------------------------------------------------------------ #
# Enumerations
# ------------------------------------------------------------------ #

class MsgType(IntEnum):
    BASIC_ID     = 0x0
    LOCATION     = 0x1
    AUTH         = 0x2
    SELF_ID      = 0x3
    SYSTEM       = 0x4
    OPERATOR_ID  = 0x5
    MESSAGE_PACK = 0xF


class IDType(IntEnum):
    NONE              = 0
    SERIAL_NUMBER     = 1   # ANSI/CTA-2063-A
    CAA_REGISTRATION  = 2
    UTM_ASSIGNED      = 3
    SPECIFIC_SESSION  = 4


class UAType(IntEnum):
    NONE              = 0
    AEROPLANE         = 1
    HELICOPTER_OR_MR  = 2
    GYROPLANE         = 3
    HYBRID_LIFT       = 4
    ORNITHOPTER       = 5
    GLIDER            = 6
    KITE              = 7
    FREE_BALLOON      = 8
    CAPTIVE_BALLOON   = 9
    AIRSHIP           = 10
    UNPOWERED_PARACHUTE = 11
    POWERED_PARACHUTE = 12
    POWERED_PARAGLIDER = 13
    GROUND_OBSTACLE   = 14
    OTHER             = 15


class OperationalStatus(IntEnum):
    UNDECLARED  = 0
    GROUND      = 1
    AIRBORNE    = 2
    EMERGENCY   = 3
    REMOTE_ID_SYSTEM_FAILURE = 4


# ------------------------------------------------------------------ #
# Output dataclass
# ------------------------------------------------------------------ #

@dataclass
class RIDFrame:
    """Aggregated, human-readable Remote ID detection event."""

    # Source
    drone_id: str = ""              # From BasicID
    id_type: str = ""               # "serial", "caa_reg", etc.
    ua_type: str = ""               # "helicopter_mr", "aeroplane", etc.

    # Location
    lat: float = 0.0
    lon: float = 0.0
    alt_baro: float = 0.0           # Barometric altitude, meters
    alt_geo: float = 0.0            # Geodetic (GPS) altitude, meters
    height_agl: float = 0.0         # Height above ground, meters
    speed_h: float = 0.0            # Horizontal speed m/s
    speed_v: float = 0.0            # Vertical speed m/s
    heading: float = 0.0            # Degrees true north
    status: str = "undeclared"      # airborne / ground / emergency

    # Operator
    operator_id: str = ""           # From OperatorID message
    operator_lat: float = 0.0
    operator_lon: float = 0.0

    # Self-ID
    description: str = ""

    # Meta
    transport: str = ""             # "wifi_nan" or "bluetooth"
    timestamp: float = field(default_factory=time.time)
    raw_hex: str = ""


# ------------------------------------------------------------------ #
# Parser
# ------------------------------------------------------------------ #

FRAME_LEN = 25   # All OpenDroneID messages are exactly 25 bytes

class OpenDroneIDParser:
    """
    Stateless parser — call parse() with raw bytes.
    Returns a list of RIDFrame objects (usually 1, but a MessagePack can yield many).
    """

    def parse(self, data: bytes, transport: str = "") -> list[RIDFrame]:
        if not data or len(data) < FRAME_LEN:
            return []

        frames: list[RIDFrame] = []
        frame = RIDFrame(transport=transport, raw_hex=data.hex())

        # Peek at the first byte to determine message type
        msg_type = (data[0] >> 4) & 0x0F

        if msg_type == MsgType.MESSAGE_PACK:
            frames.extend(self._parse_message_pack(data, transport))
        else:
            self._apply_message(frame, data)
            if frame.drone_id:
                frames.append(frame)

        return frames

    # ------------------------------------------------------------------ #
    # Message pack (0xF) — multiple messages in one payload
    # ------------------------------------------------------------------ #

    def _parse_message_pack(self, data: bytes, transport: str) -> list[RIDFrame]:
        """
        MessagePack format:
          Byte 0:    0xF? header
          Byte 1:    message count (max 9)
          Bytes 2+:  25-byte messages back-to-back
        """
        if len(data) < 2:
            return []

        count = data[1] & 0x0F
        messages = []
        for i in range(count):
            offset = 2 + i * FRAME_LEN
            if offset + FRAME_LEN > len(data):
                break
            messages.append(data[offset:offset + FRAME_LEN])

        # Build a single aggregated frame from all sub-messages
        frame = RIDFrame(transport=transport, raw_hex=data.hex())
        for msg in messages:
            self._apply_message(frame, msg)

        if frame.drone_id or frame.lat != 0.0:
            return [frame]
        return []

    # ------------------------------------------------------------------ #
    # Per-message-type parsers
    # ------------------------------------------------------------------ #

    def _apply_message(self, frame: RIDFrame, data: bytes):
        """Decode a single 25-byte message and merge into frame."""
        if len(data) < FRAME_LEN:
            return
        msg_type = (data[0] >> 4) & 0x0F

        if msg_type == MsgType.BASIC_ID:
            self._parse_basic_id(frame, data)
        elif msg_type == MsgType.LOCATION:
            self._parse_location(frame, data)
        elif msg_type == MsgType.SELF_ID:
            self._parse_self_id(frame, data)
        elif msg_type == MsgType.SYSTEM:
            self._parse_system(frame, data)
        elif msg_type == MsgType.OPERATOR_ID:
            self._parse_operator_id(frame, data)
        # AUTH (0x2) — stored as raw hex, not decoded here

    def _parse_basic_id(self, frame: RIDFrame, data: bytes):
        """
        BasicID (Type 0x0):
          Byte 0:    0x0? | proto version
          Byte 1:    ID Type (high nibble) | UA Type (low nibble)
          Bytes 2–21: UAS ID (20-byte null-terminated ASCII)
          Bytes 22–24: reserved
        """
        id_type_raw = (data[1] >> 4) & 0x0F
        ua_type_raw = data[1] & 0x0F

        try:
            frame.id_type = IDType(id_type_raw).name.lower()
        except ValueError:
            frame.id_type = f"unknown_{id_type_raw}"

        try:
            frame.ua_type = UAType(ua_type_raw).name.lower()
        except ValueError:
            frame.ua_type = f"unknown_{ua_type_raw}"

        raw_id = data[2:22]
        frame.drone_id = raw_id.rstrip(b"\x00").decode("ascii", errors="replace").strip()

    def _parse_location(self, frame: RIDFrame, data: bytes):
        """
        Location (Type 0x1):
          Byte 0:    header
          Byte 1:    Status (hi 4b) | HeightType (bit 2) | EW direction (bit 1) | SpeedMult (bit 0)
          Byte 2:    Track direction (0–359, uint8, degree)
          Bytes 3–4: Speed (uint16 LE) × 0.25 m/s (or ×0.75 if SpeedMult=1)
          Bytes 5–6: Vertical speed (int16 LE) × 0.5 m/s
          Bytes 7–10: Latitude  (int32 LE) × 1e-7 degrees
          Bytes 11–14: Longitude (int32 LE) × 1e-7 degrees
          Bytes 15–16: Pressure altitude (uint16 LE): (val - 1000) × 0.5 m, 0xFFFF = unknown
          Bytes 17–18: Geodetic altitude   (uint16 LE): same encoding
          Bytes 19–20: Height AGL          (uint16 LE): same encoding
          Bytes 21: Horiz accuracy | Vert accuracy
          Bytes 22: Baro accuracy | Speed accuracy
          Bytes 23–24: Timestamp (uint16 LE) × 0.1 s since last hour
        """
        status_raw = (data[1] >> 4) & 0x0F
        speed_mult  = data[1] & 0x01
        ew_dir      = (data[1] >> 1) & 0x01  # 0=East, 1=West

        try:
            frame.status = OperationalStatus(status_raw).name.lower()
        except ValueError:
            frame.status = "undeclared"

        # Heading
        frame.heading = float(data[2])
        if ew_dir:
            frame.heading = 360.0 - frame.heading  # West-facing correction

        # Speeds
        raw_speed_h = struct.unpack_from("<H", data, 3)[0]
        multiplier  = 0.75 if speed_mult else 0.25
        frame.speed_h = round(raw_speed_h * multiplier, 2)

        raw_speed_v = struct.unpack_from("<h", data, 5)[0]   # signed
        frame.speed_v = round(raw_speed_v * 0.5, 2)

        # Position
        raw_lat = struct.unpack_from("<i", data, 7)[0]
        raw_lon = struct.unpack_from("<i", data, 11)[0]
        frame.lat = round(raw_lat * 1e-7, 7)
        frame.lon = round(raw_lon * 1e-7, 7)

        # Altitudes
        frame.alt_baro  = self._decode_alt(struct.unpack_from("<H", data, 15)[0])
        frame.alt_geo   = self._decode_alt(struct.unpack_from("<H", data, 17)[0])
        frame.height_agl = self._decode_alt(struct.unpack_from("<H", data, 19)[0])

    def _parse_self_id(self, frame: RIDFrame, data: bytes):
        """
        Self-ID (Type 0x3):
          Byte 1:    Description type (0=text)
          Bytes 2–24: Description (null-terminated ASCII)
        """
        frame.description = data[2:25].rstrip(b"\x00").decode("ascii", errors="replace").strip()

    def _parse_system(self, frame: RIDFrame, data: bytes):
        """
        System (Type 0x4):
          Bytes 1–4:  Operator latitude  (int32 LE) × 1e-7
          Bytes 5–8:  Operator longitude (int32 LE) × 1e-7
          ... (area count, area radius, ceiling, floor, classification)
        """
        raw_lat = struct.unpack_from("<i", data, 1)[0]
        raw_lon = struct.unpack_from("<i", data, 5)[0]
        frame.operator_lat = round(raw_lat * 1e-7, 7)
        frame.operator_lon = round(raw_lon * 1e-7, 7)

    def _parse_operator_id(self, frame: RIDFrame, data: bytes):
        """
        Operator ID (Type 0x5):
          Byte 1:    ID type (0=operator, 1=UTM)
          Bytes 2–21: Operator ID (20-byte ASCII)
        """
        raw = data[2:22].rstrip(b"\x00").decode("ascii", errors="replace").strip()
        frame.operator_id = raw

    @staticmethod
    def _decode_alt(raw: int) -> float:
        """Convert encoded altitude value to meters. 0xFFFF = unknown."""
        if raw == 0xFFFF:
            return float("nan")
        return round((raw / 2.0) - 1000.0, 1)
