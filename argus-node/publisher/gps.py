"""
publisher/gps.py
================
GPS daemon with auto-detection, u-blox NEO-M9N initialisation, and UBX
binary protocol parsing.

Supports both USB (NEO-M9N — recommended default) and UART (legacy NEO-M8N)
GPS receivers, with automatic port scanning on startup.

Auto-detect priority order:
  /dev/ttyACM0  (USB u-blox, NEO-M9N default)
  /dev/ttyACM1
  /dev/ttyUSB0
  /dev/ttyUSB1
  /dev/ttyAMA0  (UART fallback, legacy)

u-blox NEO-M9N features (when device is identified by UBX-MON-VER response):
  - UBX-NAV-STATUS enabled for jamming/spoofing indicators
  - UBX-NAV-SVIN enabled for survey-in progress reporting
  - Survey-in runs for 10 min / 3 m accuracy on first boot
  - On subsequent boots, saved fixed position from survey.json is used
  - Jamming and spoofing state included in heartbeat payload

Survey state is persisted to /var/lib/argus-node/survey.json.

Requires:
  pip install pyserial pynmea2
"""

import json
import logging
import math
import struct
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

import pynmea2
import serial

log = logging.getLogger("gps")

# ── Port detection ─────────────────────────────────────────────────────────

GPS_CANDIDATE_PORTS = [
    ("/dev/ttyACM0", "usb"),   # USB u-blox (NEO-M9N default)
    ("/dev/ttyACM1", "usb"),
    ("/dev/ttyUSB0", "usb"),
    ("/dev/ttyUSB1", "usb"),
    ("/dev/ttyAMA0", "uart"),  # UART fallback (legacy NEO-M8N)
]

SURVEY_STATE_PATH = Path("/var/lib/argus-node/survey.json")

# ── UBX protocol constants ─────────────────────────────────────────────────

UBX_SYNC1 = 0xB5
UBX_SYNC2 = 0x62

# Class bytes
UBX_CLASS_NAV = 0x01
UBX_CLASS_CFG = 0x06
UBX_CLASS_MON = 0x0A

# Message IDs
UBX_MON_VER    = 0x04   # Receiver / software version poll
UBX_CFG_PRT    = 0x00   # Port configuration
UBX_CFG_MSG    = 0x01   # Enable / disable periodic messages
UBX_CFG_TMODE3 = 0x71   # Time mode 3 (survey-in / fixed)
UBX_NAV_STATUS = 0x03   # Navigation status (fix, jamming, spoofing)
UBX_NAV_PVT    = 0x07   # Position, velocity, time
UBX_NAV_SVIN   = 0x3B   # Survey-in progress

# Decode tables
_SPOOF_STATE = {0: "unknown", 1: "ok", 2: "spoofing", 3: "multiple"}
_JAM_STATE   = {0: "unknown", 1: "ok", 2: "warning",  3: "critical"}

# Survey-in parameters
SVIN_MIN_DUR_S       = 600    # 10 minutes
SVIN_ACC_LIMIT_01MM  = 30000  # 3.0 m expressed in 0.1 mm units


# ── UBX frame helpers ──────────────────────────────────────────────────────

def _ubx_checksum(data: bytes) -> Tuple[int, int]:
    """Fletcher-8 checksum over class + id + length + payload bytes."""
    ck_a = ck_b = 0
    for b in data:
        ck_a = (ck_a + b) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return ck_a, ck_b


def build_ubx(cls: int, msg_id: int, payload: bytes = b"") -> bytes:
    """Build a complete UBX frame: sync + class + id + length + payload + checksum."""
    length = len(payload)
    header = bytes([cls, msg_id]) + struct.pack("<H", length)
    body   = header + payload
    ck_a, ck_b = _ubx_checksum(body)
    return bytes([UBX_SYNC1, UBX_SYNC2]) + body + bytes([ck_a, ck_b])


def build_ubx_cfg_msg(msg_cls: int, msg_id: int, rate: int = 1) -> bytes:
    """UBX-CFG-MSG: enable a message on all 6 ports at the given rate."""
    # Payload: msgClass(1) msgID(1) rates[6] — one byte per port
    payload = bytes([msg_cls, msg_id]) + bytes([rate] * 6)
    return build_ubx(UBX_CLASS_CFG, UBX_CFG_MSG, payload)


def build_ubx_cfg_tmode3_svin(
    min_dur_s: int = SVIN_MIN_DUR_S,
    acc_limit_01mm: int = SVIN_ACC_LIMIT_01MM,
) -> bytes:
    """UBX-CFG-TMODE3: configure survey-in mode."""
    # flags word: bits 0-7 = mode (1 = survey-in), bit 8 = lla (0 = ECEF)
    payload = struct.pack(
        "<BBHiiiBBBBIII8x",
        0,             # version
        0,             # reserved1
        0x0001,        # flags: mode=1 (survey-in), lla=0
        0, 0, 0,       # ecefX/Y/Z — unused in survey-in
        0, 0, 0,       # HP extensions — unused
        0,             # reserved2
        0,             # fixedPosAcc — unused in survey-in
        min_dur_s,     # svinMinDur (seconds)
        acc_limit_01mm,# svinAccLimit (0.1 mm units)
    )
    return build_ubx(UBX_CLASS_CFG, UBX_CFG_TMODE3, payload)


def build_ubx_cfg_tmode3_fixed(lat: float, lon: float, alt_m: float = 0.0) -> bytes:
    """UBX-CFG-TMODE3: configure fixed position mode from survey result."""
    # flags: mode=2 (fixed), bit 8 set → LLA coordinates
    flags  = 0x0102
    lat_i  = int(round(lat   * 1e7))   # degrees × 1e-7  (I4)
    lon_i  = int(round(lon   * 1e7))
    alt_cm = int(round(alt_m * 100))   # metres → cm     (I4)
    payload = struct.pack(
        "<BBHiiiBBBBIII8x",
        0,        # version
        0,        # reserved1
        flags,    # mode=fixed, lla=1
        lat_i,    # lat (deg × 1e-7)
        lon_i,    # lon (deg × 1e-7)
        alt_cm,   # alt (cm)
        0, 0, 0,  # HP extensions
        0,        # reserved2
        10,       # fixedPosAcc: 1.0 mm (0.1 mm units)
        0,        # svinMinDur — unused in fixed mode
        0,        # svinAccLimit — unused in fixed mode
    )
    return build_ubx(UBX_CLASS_CFG, UBX_CFG_TMODE3, payload)


# ── Survey state persistence ───────────────────────────────────────────────

def load_survey_state(path: Path = SURVEY_STATE_PATH) -> Optional[dict]:
    """Load survey.json if it exists and complete=true. Returns dict or None."""
    try:
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            if data.get("complete"):
                return data
    except Exception as e:
        log.warning(f"Could not load survey state: {e}")
    return None


def save_survey_state(
    lat: float,
    lon: float,
    acc_m: float,
    alt_m: float = 0.0,
    path: Path = SURVEY_STATE_PATH,
) -> None:
    """Write survey completion state to survey.json atomically (write-then-rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "complete":  True,
        "lat":       lat,
        "lon":       lon,
        "alt_m":     alt_m,
        "acc_m":     acc_m,
        "timestamp": time.time(),
    }
    tmp_path = path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f)
        tmp_path.replace(path)   # atomic on POSIX; safe on Windows too in Python 3.3+
        log.info(
            f"Survey state saved: lat={lat:.7f} lon={lon:.7f} "
            f"alt={alt_m:.1f}m acc={acc_m:.3f}m"
        )
    except Exception as e:
        log.error(f"Could not save survey state: {e}")
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


# ── NMEA validation ────────────────────────────────────────────────────────

def is_valid_nmea(line: str) -> bool:
    """Return True if line is a parseable NMEA sentence."""
    if not line.startswith("$"):
        return False
    try:
        pynmea2.parse(line)
        return True
    except pynmea2.ParseError:
        return False


# ── Port probing ───────────────────────────────────────────────────────────

def _probe_port(port: str, baud: int, timeout: float = 3.0) -> bool:
    """
    Open port and attempt to read at least one valid NMEA sentence within
    timeout seconds. Returns True if NMEA is flowing.

    Sends UBX-CFG-PRT to enable NMEA output on the USB port before reading.
    This is required for the NEO-M9N, which defaults to UBX-only binary
    output on USB and will not emit NMEA sentences until switched.
    """
    try:
        with serial.Serial(port, baud, timeout=1.0) as ser:
            # Enable NMEA output on USB port (portID=3).  The NEO-M9N powers
            # up in UBX-only mode on USB; without this the probe loop reads
            # binary garbage and never finds a valid NMEA sentence.
            cfg_prt_payload = struct.pack(
                "<BBHIIHHHH",
                3,    # portID: USB
                0,    # reserved
                0,    # txReady
                0,    # mode (unused for USB)
                0,    # baudRate (unused for USB)
                7,    # inProtoMask: UBX+NMEA+RTCM
                3,    # outProtoMask: UBX+NMEA
                0,    # flags
                0,    # reserved2
            )
            ser.write(build_ubx(UBX_CLASS_CFG, UBX_CFG_PRT, cfg_prt_payload))
            time.sleep(0.5)
            ser.reset_input_buffer()

            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    raw  = ser.readline()
                    line = raw.decode("ascii", errors="replace").strip()
                    if is_valid_nmea(line):
                        return True
                except serial.SerialException:
                    return False
    except PermissionError:
        log.warning(
            f"Permission denied opening {port} — "
            f"add user to 'dialout' group: sudo usermod -aG dialout $USER"
        )
    except (serial.SerialException, OSError):
        pass
    return False


def _probe_ublox(port: str, baud: int) -> bool:
    """
    Poll UBX-MON-VER and check that the device responds with the UBX sync
    header 0xB5 0x62. Returns True for confirmed u-blox devices.
    """
    poll = build_ubx(UBX_CLASS_MON, UBX_MON_VER, b"")
    try:
        with serial.Serial(port, baud, timeout=1.0) as ser:
            ser.write(poll)
            time.sleep(0.3)
            data = ser.read(64)
            return bytes([UBX_SYNC1, UBX_SYNC2]) in data
    except (serial.SerialException, OSError):
        return False


def detect_gps_port(
    configured_port: str,
    baud: int,
    configured_mode: str = "usb",
) -> Tuple[str, str]:
    """
    Scan for a working GPS port.

    1. Try the configured port first.
    2. Fall through GPS_CANDIDATE_PORTS in priority order.

    For each candidate, open the port and confirm valid NMEA is flowing
    before accepting it.

    Returns (port, mode) or raises RuntimeError if no port found.
    """
    seen = set()
    candidates = [(configured_port, configured_mode)]
    for p, m in GPS_CANDIDATE_PORTS:
        if p != configured_port:
            candidates.append((p, m))

    for port, mode in candidates:
        if port in seen:
            continue
        seen.add(port)
        log.debug(f"Probing GPS port {port} ({mode}) @ {baud} baud…")
        if _probe_port(port, baud):
            log.info(f"GPS detected on {port} ({mode}) @ {baud} baud")
            return port, mode

    raise RuntimeError(
        f"No GPS found. Tried: {[p for p, _ in candidates]}"
    )


# ── ECEF → geodetic helper ─────────────────────────────────────────────────

def _ecef_to_llh(x: float, y: float, z: float) -> Tuple[float, float, float]:
    """
    Convert ECEF coordinates (metres) to geodetic lat / lon / alt (WGS-84).
    Uses iterative Bowring method.
    """
    a   = 6_378_137.0           # WGS-84 semi-major axis (m)
    f   = 1 / 298.257_223_563   # flattening
    b   = a * (1 - f)
    e2  = 1 - (b / a) ** 2     # eccentricity²

    lon = math.atan2(y, x)
    p   = math.sqrt(x * x + y * y)
    lat = math.atan2(z, p * (1 - e2))

    for _ in range(5):
        sin_lat = math.sin(lat)
        N       = a / math.sqrt(1 - e2 * sin_lat ** 2)
        lat     = math.atan2(z + e2 * N * sin_lat, p)

    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    N       = a / math.sqrt(1 - e2 * sin_lat ** 2)
    if abs(cos_lat) > 1e-10:
        alt = p / cos_lat - N
    else:
        alt = abs(z) / abs(sin_lat) - N * (1 - e2)

    return math.degrees(lat), math.degrees(lon), alt


# ── Main daemon ────────────────────────────────────────────────────────────

class GPSDaemon:
    """
    Thread-safe GPS daemon. Reads NMEA sentences and, on u-blox devices,
    parses UBX binary frames for jamming / spoofing indicators and
    survey-in status.

    Call run(stop_event) in a background thread.
    """

    def __init__(
        self,
        port: str = "/dev/ttyACM0",
        baud: int = 9600,
        mode: str = "usb",
        auto_detect: bool = True,
        survey_state_path: Path = SURVEY_STATE_PATH,
    ):
        self.port              = port
        self.baud              = baud
        self.mode              = mode
        self.auto_detect       = auto_detect
        self._survey_state_path = survey_state_path

        self._lock       = threading.Lock()
        self._lat:       float = 0.0
        self._lon:       float = 0.0
        self._alt:       float = 0.0
        self._has_fix:   bool  = False
        self._satellites: int  = 0
        self._fix_event  = threading.Event()

        # Set after successful port detection
        self._active_port: str = port
        self._active_mode: str = mode

        # u-blox state
        self._is_ublox:       bool           = False
        self._jamming_state:  Optional[str]  = None
        self._spoofing_state: Optional[str]  = None
        self._survey_complete: bool          = False

    # ── Thread-safe properties ─────────────────────────────────────────────

    @property
    def lat(self) -> float:
        with self._lock: return self._lat

    @property
    def lon(self) -> float:
        with self._lock: return self._lon

    @property
    def alt(self) -> float:
        with self._lock: return self._alt

    @property
    def has_fix(self) -> bool:
        with self._lock: return self._has_fix

    @property
    def satellites(self) -> int:
        with self._lock: return self._satellites

    @property
    def detected_port(self) -> str:
        return self._active_port

    @property
    def gps_mode(self) -> str:
        return self._active_mode

    @property
    def jamming_state(self) -> Optional[str]:
        with self._lock: return self._jamming_state

    @property
    def spoofing_state(self) -> Optional[str]:
        with self._lock: return self._spoofing_state

    @property
    def survey_complete(self) -> bool:
        return self._survey_complete

    def wait_for_fix(self, timeout: float = 60) -> bool:
        return self._fix_event.wait(timeout=timeout)

    def heartbeat_extras(self) -> dict:
        """Extra fields to merge into the MQTT heartbeat payload."""
        return {
            "detected_port":   self._active_port,
            "gps_mode":        self._active_mode,
            "jamming_state":   self.jamming_state,
            "spoofing_state":  self.spoofing_state,
            "survey_complete": self._survey_complete,
        }

    # ── Run loop ───────────────────────────────────────────────────────────

    def run(self, stop_event: threading.Event) -> None:
        log.info(
            f"GPS daemon starting (port={self.port}, mode={self.mode}, "
            f"auto_detect={self.auto_detect})"
        )
        while not stop_event.is_set():
            try:
                # Port detection
                if self.auto_detect:
                    active_port, active_mode = detect_gps_port(
                        self.port, self.baud, self.mode
                    )
                else:
                    active_port, active_mode = self.port, self.mode

                self._active_port = active_port
                self._active_mode = active_mode

                # Identify u-blox
                self._is_ublox = _probe_ublox(active_port, self.baud)
                if self._is_ublox:
                    log.info(f"u-blox device confirmed on {active_port}")

                with serial.Serial(active_port, self.baud, timeout=1) as ser:
                    log.info(f"GPS serial opened: {active_port} ({active_mode})")
                    if self._is_ublox:
                        self._init_ublox(ser)

                    svin_last_poll = 0.0

                    while not stop_event.is_set():
                        try:
                            byte = ser.read(1)
                            if not byte:
                                continue
                            b = byte[0]

                            if b == ord("$"):
                                # NMEA sentence
                                rest = ser.readline()
                                line = (byte + rest).decode("ascii", errors="replace").strip()
                                self._parse_nmea(line)

                            elif b == UBX_SYNC1:
                                # Possible UBX frame
                                next_b = ser.read(1)
                                if next_b and next_b[0] == UBX_SYNC2:
                                    self._recv_ubx(ser)

                            # Poll UBX-NAV-SVIN every 30 s while survey pending
                            if (self._is_ublox
                                    and not self._survey_complete
                                    and time.time() - svin_last_poll > 30):
                                ser.write(build_ubx(UBX_CLASS_NAV, UBX_NAV_SVIN, b""))
                                svin_last_poll = time.time()

                        except serial.SerialException as e:
                            log.warning(f"GPS read error: {e}")
                            break

            except RuntimeError as e:
                log.error(f"GPS port detection failed: {e} — retrying in 30 s")
                time.sleep(30)
            except serial.SerialException as e:
                log.error(f"GPS port error: {e} — retrying in 10 s")
                time.sleep(10)

        log.info("GPS daemon stopped.")

    # ── u-blox initialisation ──────────────────────────────────────────────

    def _init_ublox(self, ser: serial.Serial) -> None:
        """Send initialisation commands to u-blox NEO-M9N."""
        log.info("Initialising u-blox NEO-M9N…")

        # Enable NMEA output on USB port (portID=3).  The NEO-M9N defaults to
        # UBX-only on USB; without this the NMEA parse path never fires.
        ser.write(build_ubx(
            UBX_CLASS_CFG, UBX_CFG_PRT,
            struct.pack("<BBHIIHHHH", 3, 0, 0, 0, 0, 7, 3, 0, 0),
        ))
        time.sleep(0.1)
        ser.reset_input_buffer()

        # Enable UBX-NAV-PVT (primary position/velocity/time), 1 Hz
        ser.write(build_ubx_cfg_msg(UBX_CLASS_NAV, UBX_NAV_PVT, rate=1))
        time.sleep(0.05)

        # Enable UBX-NAV-STATUS (jamming + spoofing indicators), 1 Hz
        ser.write(build_ubx_cfg_msg(UBX_CLASS_NAV, UBX_NAV_STATUS, rate=1))
        time.sleep(0.05)

        # Enable UBX-NAV-SVIN (survey-in progress), 1 Hz
        ser.write(build_ubx_cfg_msg(UBX_CLASS_NAV, UBX_NAV_SVIN, rate=1))
        time.sleep(0.05)

        # Survey-in vs. fixed-position mode
        survey = load_survey_state(self._survey_state_path)
        if survey:
            log.info(
                f"Restoring saved fixed position: "
                f"lat={survey['lat']:.7f} lon={survey['lon']:.7f} "
                f"acc={survey['acc_m']:.3f}m"
            )
            self._survey_complete = True
            cmd = build_ubx_cfg_tmode3_fixed(
                survey["lat"], survey["lon"], survey.get("alt_m", 0.0)
            )
        else:
            log.info(
                f"Starting survey-in (min {SVIN_MIN_DUR_S} s, "
                f"target {SVIN_ACC_LIMIT_01MM / 10000:.1f} m accuracy)"
            )
            cmd = build_ubx_cfg_tmode3_svin()

        ser.write(cmd)
        time.sleep(0.1)
        log.info("u-blox initialisation complete")

    # ── UBX frame receiver ─────────────────────────────────────────────────

    def _recv_ubx(self, ser: serial.Serial) -> None:
        """Read and dispatch one UBX frame (sync bytes already consumed)."""
        header = ser.read(4)   # class(1) id(1) length(2)
        if len(header) < 4:
            return
        cls, msg_id = header[0], header[1]
        length      = struct.unpack_from("<H", header, 2)[0]
        payload     = ser.read(length)
        ser.read(2)   # consume checksum bytes
        if len(payload) < length:
            return
        self._handle_ubx(cls, msg_id, bytes(payload))

    def _handle_ubx(self, cls: int, msg_id: int, payload: bytes) -> None:
        """Dispatch a parsed UBX message to the appropriate handler."""
        if cls == UBX_CLASS_NAV and msg_id == UBX_NAV_STATUS:
            self._parse_nav_status(payload)
        elif cls == UBX_CLASS_NAV and msg_id == UBX_NAV_PVT:
            self._parse_nav_pvt(payload)
        elif cls == UBX_CLASS_NAV and msg_id == UBX_NAV_SVIN:
            self._parse_nav_svin(payload)

    # ── UBX-NAV-STATUS ─────────────────────────────────────────────────────

    def _parse_nav_status(self, payload: bytes) -> None:
        """
        UBX-NAV-STATUS payload (16 bytes):
          iTOW(4) gpsFix(1) flags(1) fixStat(1) flags2(1) ttff(4) msss(4)

        flags2 bit layout:
          bits 0-1 : psmState
          bits 3-4 : spoofDetState  (0=unknown, 1=ok, 2=spoofing, 3=multiple)
          bits 6-7 : jammingState   (0=unknown, 1=ok, 2=warning, 3=critical)
        """
        if len(payload) < 16:
            return
        flags2      = payload[7]
        spoof_raw   = (flags2 >> 3) & 0x03
        jam_raw     = (flags2 >> 6) & 0x03
        spoof_str   = _SPOOF_STATE.get(spoof_raw, "unknown")
        jam_str     = _JAM_STATE.get(jam_raw, "unknown")

        with self._lock:
            self._spoofing_state = spoof_str
            self._jamming_state  = jam_str

        if jam_str not in ("ok", "unknown"):
            log.warning(f"GPS jamming detected on {self._active_port}: {jam_str}")
        if spoof_str not in ("ok", "unknown"):
            log.warning(f"GPS spoofing detected on {self._active_port}: {spoof_str}")

    # ── UBX-NAV-PVT ────────────────────────────────────────────────────────

    def _parse_nav_pvt(self, payload: bytes) -> None:
        """
        UBX-NAV-PVT payload (92 bytes — only the first 36 are needed here):
          offset  0: iTOW       (U4) ms since week
          offset 20: fixType    (U1) 0=no fix, 3=3D, 4=GNSS+dead reck, 5=time only
          offset 23: numSV      (U1) number of satellites used in navigation solution
          offset 24: lon        (I4) degrees × 1e-7
          offset 28: lat        (I4) degrees × 1e-7
          offset 32: height     (I4) mm above ellipsoid

        fix_type >= 3 is treated as a valid position fix.
        """
        if len(payload) < 36:
            return
        fix_type = payload[20]
        num_sv   = payload[23]
        lon_raw  = struct.unpack_from("<i", payload, 24)[0]
        lat_raw  = struct.unpack_from("<i", payload, 28)[0]
        alt_raw  = struct.unpack_from("<i", payload, 32)[0]
        lat = lat_raw * 1e-7
        lon = lon_raw * 1e-7
        alt = alt_raw / 1000.0   # mm → metres

        with self._lock:
            self._satellites = num_sv
            if fix_type >= 3:
                self._lat = lat
                self._lon = lon
                self._alt = alt
                if not self._has_fix:
                    log.info(
                        f"GPS fix acquired (UBX): {lat:.6f}, {lon:.6f}, "
                        f"{alt:.1f}m, {num_sv} sats [{self._active_port}]"
                    )
                self._has_fix = True
                self._fix_event.set()
            else:
                self._has_fix = False

    # ── UBX-NAV-SVIN ──────────────────────────────────────────────────────

    def _parse_nav_svin(self, payload: bytes) -> None:
        """
        UBX-NAV-SVIN payload (40 bytes):
          iTOW(4) dur(4) meanX(4) meanY(4) meanZ(4)
          meanXHP(1) meanYHP(1) meanZHP(1) reserved(1)
          meanAcc(4) obs(4) valid(1) active(1) reserved2(2)

        When survey completes (valid=1, active=0), converts ECEF to LLH
        and persists the result.
        """
        if len(payload) < 40:
            return
        (itow, dur,
         mean_x, mean_y, mean_z,
         _xhp, _yhp, _zhp, _,
         mean_acc, obs, valid, active) = struct.unpack_from(
            "<IIiiibbbbIIBB", payload
        )

        acc_m = mean_acc / 10000.0   # 0.1 mm units → metres
        log.debug(
            f"Survey-in: dur={dur}s acc={acc_m:.3f}m obs={obs} "
            f"valid={valid} active={active}"
        )

        if valid and not active and not self._survey_complete:
            # Convert ECEF (cm from receiver) to geodetic
            lat, lon, alt_m = _ecef_to_llh(
                mean_x * 0.01, mean_y * 0.01, mean_z * 0.01
            )
            log.info(
                f"Survey-in COMPLETE after {dur} s: "
                f"lat={lat:.7f} lon={lon:.7f} alt={alt_m:.1f}m acc={acc_m:.3f}m"
            )
            self._survey_complete = True
            save_survey_state(lat, lon, acc_m, alt_m, self._survey_state_path)

    # ── NMEA parsing ───────────────────────────────────────────────────────

    def _parse_nmea(self, line: str) -> None:
        if not line.startswith("$"):
            return
        try:
            msg = pynmea2.parse(line)
        except pynmea2.ParseError:
            return

        if isinstance(msg, pynmea2.types.talker.GGA):
            with self._lock:
                if msg.gps_qual and int(msg.gps_qual) > 0:
                    self._lat       = float(msg.latitude)  if msg.latitude  else 0.0
                    self._lon       = float(msg.longitude) if msg.longitude else 0.0
                    self._alt       = float(msg.altitude)  if msg.altitude  else 0.0
                    self._satellites = int(msg.num_sats)    if msg.num_sats  else 0
                    if not self._has_fix:
                        log.info(
                            f"GPS fix acquired: {self._lat:.6f}, {self._lon:.6f}, "
                            f"{self._alt:.1f}m, {self._satellites} sats "
                            f"[{self._active_port}]"
                        )
                    self._has_fix = True
                    self._fix_event.set()
                else:
                    self._has_fix = False

        elif isinstance(msg, pynmea2.types.talker.RMC):
            if msg.status == "A":
                with self._lock:
                    if not self._has_fix:
                        self._lat     = float(msg.latitude)  if msg.latitude  else 0.0
                        self._lon     = float(msg.longitude) if msg.longitude else 0.0
                        self._has_fix = True
                        self._fix_event.set()
