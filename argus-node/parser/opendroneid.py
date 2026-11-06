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

import logging
import math
import struct
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

log = logging.getLogger(__name__)


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
        """
        Decode raw OpenDroneID bytes into RIDFrame objects.

        MessagePack detection: (data[0] >> 4) & 0x0F == 0xF (MsgType.MESSAGE_PACK).
        A MessagePack payload uses the 3-byte ASTM F3411-22a header before messages;
        all other types are parsed directly as a single 25-byte message.
        """
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
        ASTM F3411-22a ODID_MessagePack_encoded layout:
          Byte 0:    MessageType(4) | ProtoVersion(4) — 0xF2 for packed
          Byte 1:    SingleMessageSize — always 25 (0x19)
          Byte 2:    MsgPackSize — number of messages (max 9)
          Bytes 3+:  25-byte messages back-to-back
        """
        if len(data) < 3:
            return []

        single_size = data[1]
        count       = data[2]
        count       = min(count, 9)   # clamp to spec maximum
        if single_size != FRAME_LEN:
            log.warning(
                "MessagePack SingleMessageSize=%d, expected %d — "
                "payload may be malformed; falling back to FRAME_LEN",
                single_size, FRAME_LEN,
            )
            single_size = FRAME_LEN

        messages = []
        for i in range(count):
            offset = 3 + i * single_size
            if offset + single_size > len(data):
                break
            messages.append(data[offset:offset + single_size])

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
        Location (Type 0x1), per ODID_Location_encoded in opendroneid-core-c:
          Byte 0:      header
          Byte 1:      Status (bits 7-4) | reserved (3) | HeightType (2) |
                       EWDirection (1) | SpeedMult (0)
          Byte 2:      Direction (uint8); add 180 when EWDirection = 1
          Byte 3:      Horizontal speed (uint8): x0.25 m/s, or
                       x0.75 + 63.75 m/s when SpeedMult = 1
          Byte 4:      Vertical speed (int8) x0.5 m/s
          Bytes 5-8:   Latitude  (int32 LE) x1e-7 deg
          Bytes 9-12:  Longitude (int32 LE) x1e-7 deg
          Bytes 13-14: Pressure altitude (uint16 LE) x0.5 - 1000 m
          Bytes 15-16: Geodetic altitude (uint16 LE) x0.5 - 1000 m
          Bytes 17-18: Height above takeoff/ground (uint16 LE) x0.5 - 1000 m
          Byte 19:     VertAccuracy | HorizAccuracy
          Byte 20:     BaroAccuracy | SpeedAccuracy
          Bytes 21-22: Timestamp (uint16 LE) x0.1 s since the hour
          Byte 23:     TSAccuracy
        Invalid markers (direction 361, speed 255 / 63, altitude -1000) map to 0.0.
        """
        status_raw = (data[1] >> 4) & 0x0F
        ew_dir     = (data[1] >> 1) & 0x01
        speed_mult = data[1] & 0x01

        try:
            frame.status = OperationalStatus(status_raw).name.lower()
        except ValueError:
            frame.status = "undeclared"

        heading = float(data[2]) + (180.0 if ew_dir else 0.0)
        frame.heading = heading if heading < 360.0 else 0.0

        raw_speed_h = data[3]
        if raw_speed_h == 255:
            frame.speed_h = 0.0
        elif speed_mult:
            frame.speed_h = round(raw_speed_h * 0.75 + 255 * 0.25, 2)
        else:
            frame.speed_h = round(raw_speed_h * 0.25, 2)

        raw_speed_v = struct.unpack_from("<b", data, 4)[0]
        frame.speed_v = 0.0 if raw_speed_v == 126 else round(raw_speed_v * 0.5, 2)

        raw_lat = struct.unpack_from("<i", data, 5)[0]
        raw_lon = struct.unpack_from("<i", data, 9)[0]
        frame.lat = round(raw_lat * 1e-7, 7)
        frame.lon = round(raw_lon * 1e-7, 7)

        frame.alt_baro   = self._decode_alt(struct.unpack_from("<H", data, 13)[0])
        frame.alt_geo    = self._decode_alt(struct.unpack_from("<H", data, 15)[0])
        frame.height_agl = self._decode_alt(struct.unpack_from("<H", data, 17)[0])

    def _parse_self_id(self, frame: RIDFrame, data: bytes):
        """
        Self-ID (Type 0x3):
          Byte 1:    Description type (0=text)
          Bytes 2–24: Description (null-terminated ASCII)
        """
        frame.description = data[2:25].rstrip(b"\x00").decode("ascii", errors="replace").strip()

    def _parse_system(self, frame: RIDFrame, data: bytes):
        """
        System (Type 0x4), per ODID_System_encoded:
          Byte 1:     OperatorLocationType | ClassificationType
          Bytes 2-5:  Operator latitude  (int32 LE) x1e-7
          Bytes 6-9:  Operator longitude (int32 LE) x1e-7
          Bytes 10-16: area count, radius, ceiling, floor
          Byte 17:    EU category | class
          Bytes 18-19: Operator geodetic altitude
          Bytes 20-23: Timestamp
        """
        raw_lat = struct.unpack_from("<i", data, 2)[0]
        raw_lon = struct.unpack_from("<i", data, 6)[0]
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
        """
        Convert encoded altitude to meters (x0.5 - 1000).
        ODID uses -1000 m (raw 0) as "unknown"; return 0.0 for that so the
        value stays JSON-safe (NaN is not valid JSON and breaks the server).
        """
        if raw == 0 or raw == 0xFFFF:
            return 0.0
        return round((raw / 2.0) - 1000.0, 1)
