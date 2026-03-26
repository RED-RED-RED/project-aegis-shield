"""
tests/test_gps.py
=================
Unit tests for GPS auto-detection, UBX message construction, survey state
persistence, and NMEA validation.

All tests run without real hardware — serial.Serial is mocked throughout.
"""

import json
import math
import struct
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from publisher.gps import (
    GPSDaemon,
    _probe_port,
    build_ubx,
    build_ubx_cfg_msg,
    build_ubx_cfg_tmode3_svin,
    build_ubx_cfg_tmode3_fixed,
    detect_gps_port,
    is_valid_nmea,
    load_survey_state,
    save_survey_state,
    _ubx_checksum,
    _ecef_to_llh,
    UBX_SYNC1, UBX_SYNC2,
    UBX_CLASS_NAV, UBX_CLASS_CFG,
    UBX_CFG_MSG, UBX_CFG_PRT, UBX_CFG_TMODE3,
    UBX_NAV_STATUS, UBX_NAV_PVT, UBX_NAV_SVIN,
    SVIN_MIN_DUR_S, SVIN_ACC_LIMIT_01MM,
)


# ── NMEA validation ────────────────────────────────────────────────────────

class TestNMEAValidation:

    def test_valid_gga(self):
        line = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
        assert is_valid_nmea(line) is True

    def test_valid_rmc(self):
        line = "$GPRMC,220516,A,5133.82,N,00042.24,W,173.8,231.8,130694,004.2,W*70"
        assert is_valid_nmea(line) is True

    def test_empty_string(self):
        assert is_valid_nmea("") is False

    def test_no_dollar(self):
        assert is_valid_nmea("GPGGA,123519,4807.038,N") is False

    def test_ubx_binary_not_valid(self):
        assert is_valid_nmea("\xb5\x62garbage") is False

    def test_truncated_nmea(self):
        # Malformed sentence — unknown talker type that pynmea2 cannot parse
        assert is_valid_nmea("$XXXXX,not,valid,nmea,data") is False


# ── UBX frame construction ─────────────────────────────────────────────────

class TestUBXFrameConstruction:

    def test_build_ubx_sync_bytes(self):
        frame = build_ubx(0x06, 0x01, b"\xAA\xBB")
        assert frame[0] == UBX_SYNC1
        assert frame[1] == UBX_SYNC2

    def test_build_ubx_class_and_id(self):
        frame = build_ubx(0x06, 0x71, b"")
        assert frame[2] == 0x06
        assert frame[3] == 0x71

    def test_build_ubx_length_field(self):
        payload = b"\x01\x02\x03\x04"
        frame = build_ubx(0x01, 0x03, payload)
        length = struct.unpack_from("<H", frame, 4)[0]
        assert length == 4

    def test_build_ubx_empty_payload(self):
        frame = build_ubx(0x0A, 0x04, b"")
        # sync(2) + class(1) + id(1) + len(2) + checksum(2) = 8 bytes
        assert len(frame) == 8

    def test_build_ubx_checksum_is_correct(self):
        """Recompute checksum from frame and verify it matches."""
        frame = build_ubx(0x06, 0x01, b"\x01\x03\x01\x01\x01\x01\x01\x01")
        body  = frame[2:-2]
        ck_a, ck_b = _ubx_checksum(body)
        assert frame[-2] == ck_a
        assert frame[-1] == ck_b

    def test_cfg_msg_payload_size(self):
        """UBX-CFG-MSG payload should be 8 bytes: 2 ids + 6 rates."""
        frame = build_ubx_cfg_msg(UBX_CLASS_NAV, UBX_NAV_STATUS, rate=1)
        length = struct.unpack_from("<H", frame, 4)[0]
        assert length == 8

    def test_cfg_msg_encodes_class_and_id(self):
        frame = build_ubx_cfg_msg(0x01, 0x03, rate=1)
        payload_start = 6
        assert frame[payload_start]     == 0x01  # msg class
        assert frame[payload_start + 1] == 0x03  # msg id

    def test_cfg_msg_rate_applied_to_all_ports(self):
        frame = build_ubx_cfg_msg(0x01, 0x03, rate=5)
        payload = frame[6:-2]
        rates = payload[2:]   # skip class/id
        assert list(rates) == [5, 5, 5, 5, 5, 5]

    def test_cfg_tmode3_svin_payload_size(self):
        """CFG-TMODE3 payload is always 40 bytes."""
        frame = build_ubx_cfg_tmode3_svin()
        length = struct.unpack_from("<H", frame, 4)[0]
        assert length == 40

    def test_cfg_tmode3_svin_mode_flag(self):
        """Survey-in: flags word bits 0-7 should equal 1."""
        frame = build_ubx_cfg_tmode3_svin()
        payload = frame[6:-2]
        flags = struct.unpack_from("<H", payload, 2)[0]
        mode = flags & 0xFF
        assert mode == 1

    def test_cfg_tmode3_svin_encodes_duration(self):
        frame = build_ubx_cfg_tmode3_svin(min_dur_s=300)
        payload = frame[6:-2]
        # svinMinDur is at byte offset 24 in the payload
        min_dur = struct.unpack_from("<I", payload, 24)[0]
        assert min_dur == 300

    def test_cfg_tmode3_svin_encodes_accuracy_limit(self):
        frame = build_ubx_cfg_tmode3_svin(acc_limit_01mm=50000)
        payload = frame[6:-2]
        acc = struct.unpack_from("<I", payload, 28)[0]
        assert acc == 50000

    def test_cfg_tmode3_svin_default_values(self):
        frame = build_ubx_cfg_tmode3_svin()
        payload = frame[6:-2]
        min_dur = struct.unpack_from("<I", payload, 24)[0]
        acc_lim = struct.unpack_from("<I", payload, 28)[0]
        assert min_dur == SVIN_MIN_DUR_S
        assert acc_lim == SVIN_ACC_LIMIT_01MM

    def test_cfg_tmode3_fixed_mode_flag(self):
        """Fixed mode: flags word bits 0-7 = 2, bit 8 (LLA) = 1."""
        frame = build_ubx_cfg_tmode3_fixed(51.5, -0.1, 10.0)
        payload = frame[6:-2]
        flags = struct.unpack_from("<H", payload, 2)[0]
        assert (flags & 0xFF) == 2    # mode=fixed
        assert (flags >> 8) & 1 == 1  # lla=1

    def test_cfg_tmode3_fixed_encodes_lat_lon(self):
        lat, lon = 51.477928, -0.001545
        frame = build_ubx_cfg_tmode3_fixed(lat, lon, 5.0)
        payload = frame[6:-2]
        lat_i = struct.unpack_from("<i", payload, 4)[0]
        lon_i = struct.unpack_from("<i", payload, 8)[0]
        assert abs(lat_i - round(lat * 1e7)) <= 1
        assert abs(lon_i - round(lon * 1e7)) <= 1

    def test_cfg_tmode3_fixed_payload_size(self):
        frame = build_ubx_cfg_tmode3_fixed(0.0, 0.0, 0.0)
        length = struct.unpack_from("<H", frame, 4)[0]
        assert length == 40


# ── Survey state persistence ───────────────────────────────────────────────

class TestSurveyStatePersistence:

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "survey.json"
            save_survey_state(51.4774, -0.0014, 1.23, alt_m=42.5, path=path)
            data = load_survey_state(path)
            assert data is not None
            assert data["complete"] is True
            assert abs(data["lat"]   - 51.4774) < 1e-6
            assert abs(data["lon"]   - (-0.0014)) < 1e-6
            assert abs(data["acc_m"] - 1.23) < 1e-4
            assert abs(data["alt_m"] - 42.5) < 1e-3
            assert "timestamp" in data

    def test_load_missing_file_returns_none(self):
        path = Path("/tmp/does_not_exist_argus_test.json")
        assert load_survey_state(path) is None

    def test_load_incomplete_survey_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "survey.json"
            path.write_text(json.dumps({"complete": False, "lat": 0.0, "lon": 0.0}))
            assert load_survey_state(path) is None

    def test_load_malformed_json_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "survey.json"
            path.write_text("not valid json {{{")
            assert load_survey_state(path) is None

    def test_save_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subdir" / "survey.json"
            save_survey_state(0.0, 0.0, 1.0, path=path)
            assert path.exists()


# ── Port auto-detection ────────────────────────────────────────────────────

class TestPortAutoDetect:

    def _make_nmea_serial(self, nmea_line: str):
        """Build a mock serial.Serial that yields one NMEA line on readline()."""
        mock_ser = MagicMock()
        mock_ser.__enter__ = MagicMock(return_value=mock_ser)
        mock_ser.__exit__ = MagicMock(return_value=False)
        mock_ser.readline.return_value = (nmea_line + "\r\n").encode("ascii")
        return mock_ser

    def test_configured_port_tried_first(self):
        """Configured port is probed before falling through candidates."""
        gga = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
        mock_ser = self._make_nmea_serial(gga)
        with patch("publisher.gps.serial.Serial", return_value=mock_ser):
            port, mode = detect_gps_port("/dev/ttyACM0", 9600, "usb")
        assert port == "/dev/ttyACM0"
        assert mode == "usb"

    def test_falls_through_to_next_candidate(self):
        """If first port fails, next candidate is tried."""
        gga = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"

        call_count = [0]
        def fake_serial(port, baud, timeout):
            call_count[0] += 1
            if "/dev/ttyACM0" in port:
                raise OSError("not found")
            m = MagicMock()
            m.__enter__ = MagicMock(return_value=m)
            m.__exit__  = MagicMock(return_value=False)
            m.readline.return_value = (gga + "\r\n").encode("ascii")
            return m

        with patch("publisher.gps.serial.Serial", side_effect=fake_serial):
            port, mode = detect_gps_port("/dev/ttyACM0", 9600, "usb")

        assert port == "/dev/ttyACM1"
        assert mode == "usb"

    def test_uart_port_detected_as_uart_mode(self):
        """UART fallback port returns mode='uart'."""
        gga = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"

        def fake_serial(port, baud, timeout):
            if port != "/dev/ttyAMA0":
                raise OSError("not found")
            m = MagicMock()
            m.__enter__ = MagicMock(return_value=m)
            m.__exit__  = MagicMock(return_value=False)
            m.readline.return_value = (gga + "\r\n").encode("ascii")
            return m

        with patch("publisher.gps.serial.Serial", side_effect=fake_serial):
            port, mode = detect_gps_port("/dev/ttyACM0", 9600, "usb")

        assert port == "/dev/ttyAMA0"
        assert mode == "uart"

    def test_no_port_found_raises(self):
        """RuntimeError when no candidate yields valid NMEA."""
        with patch("publisher.gps.serial.Serial", side_effect=OSError("not found")):
            with pytest.raises(RuntimeError, match="No GPS found"):
                detect_gps_port("/dev/ttyACM0", 9600, "usb")

    def test_non_nmea_port_raises_oserror_falls_through(self):
        """Port that raises OSError is skipped; next valid port is accepted."""
        gga = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"

        def fake_serial(port, baud, timeout):
            if port == "/dev/ttyACM0":
                raise OSError("device not found")
            m = MagicMock()
            m.__enter__ = MagicMock(return_value=m)
            m.__exit__  = MagicMock(return_value=False)
            m.readline.return_value = (gga + "\r\n").encode("ascii")
            return m

        with patch("publisher.gps.serial.Serial", side_effect=fake_serial):
            port, _ = detect_gps_port("/dev/ttyACM0", 9600, "usb")
        assert port == "/dev/ttyACM1"


# ── GPSDaemon UBX parsing ──────────────────────────────────────────────────

class TestGPSDaemonUBXParsing:

    def _daemon(self, tmp_path=None):
        path = Path(tmp_path or "/tmp") / "survey.json"
        return GPSDaemon(survey_state_path=path)

    def test_nav_status_ok_state(self):
        d = self._daemon()
        # UBX-NAV-STATUS layout: iTOW(I4) gpsFix(B) flags(B) fixStat(B) flags2(B) ...
        # flags2 = 0b00001000 → bits 3-4 = spoofDetState=1 (ok), bits 6-7 = 0 (unknown)
        # Arguments: iTOW=0, gpsFix=3, flags=0, fixStat=0, flags2=0b00001000, ttff=0, msss=0
        payload = struct.pack("<IBBBBIi", 0, 3, 0, 0, 0b00001000, 0, 0)
        d._parse_nav_status(payload)
        assert d.spoofing_state == "ok"
        assert d.jamming_state  == "unknown"

    def test_nav_status_jamming_warning(self):
        d = self._daemon()
        # flags2: jammingState=2 (warning) → bits 6-7 = 0b10 → 0b10000000 = 0x80
        payload = bytearray(16)
        payload[7] = 0b10000000  # jammingState=2 (warning)
        d._parse_nav_status(bytes(payload))
        assert d.jamming_state == "warning"

    def test_nav_status_jamming_critical(self):
        d = self._daemon()
        payload = bytearray(16)
        payload[7] = 0b11000000  # jammingState=3 (critical)
        d._parse_nav_status(bytes(payload))
        assert d.jamming_state == "critical"

    def test_nav_status_spoofing_detected(self):
        d = self._daemon()
        payload = bytearray(16)
        payload[7] = 0b00010000  # spoofDetState=2 (spoofing) → bits 3-4=0b10
        d._parse_nav_status(bytes(payload))
        assert d.spoofing_state == "spoofing"

    def test_nav_status_too_short_ignored(self):
        d = self._daemon()
        d._parse_nav_status(b"\x00" * 8)
        assert d.jamming_state  is None
        assert d.spoofing_state is None

    def test_nav_svin_incomplete_ignored(self):
        """Survey-in with valid=0 should not save state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            d = self._daemon(tmpdir)
            payload = bytearray(40)
            # valid=0, active=1 — survey still running
            struct.pack_into("<IIiiibbbbIIBB", payload, 0,
                             0, 100, 0, 0, 0, 0, 0, 0, 0, 5000, 50, 0, 1)
            d._parse_nav_svin(bytes(payload))
            assert d.survey_complete is False

    def test_nav_svin_complete_saves_state(self):
        """survey_complete flag set and survey.json written on valid survey."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "survey.json"
            d = GPSDaemon(survey_state_path=path)
            # ECEF coords roughly corresponding to Greenwich Observatory
            # (51.477°N, 0.0°W, ~65m)
            # Approx ECEF: X=3980543, Y=12, Z=4966868 (cm → m * 100)
            x_cm = int(3_980_543 * 100)
            y_cm = int(12 * 100)
            z_cm = int(4_966_868 * 100)
            payload = bytearray(40)
            struct.pack_into("<IIiiibbbbIIBB", payload, 0,
                             0, 620, x_cm, y_cm, z_cm, 0, 0, 0, 0,
                             12000, 680, 1, 0)  # valid=1, active=0
            d._parse_nav_svin(bytes(payload))
            assert d.survey_complete is True
            assert path.exists()
            saved = json.loads(path.read_text())
            assert saved["complete"] is True
            assert "lat" in saved and "lon" in saved

    def test_nav_svin_not_repeated(self):
        """Second call with valid survey does not overwrite completed state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "survey.json"
            d = GPSDaemon(survey_state_path=path)
            d._survey_complete = True   # already completed
            payload = bytearray(40)
            struct.pack_into("<IIiiibbbbIIBB", payload, 0,
                             0, 620, 0, 0, 6356752, 0, 0, 0, 0,
                             5000, 100, 1, 0)
            mtime_before = None
            if path.exists():
                mtime_before = path.stat().st_mtime
            d._parse_nav_svin(bytes(payload))
            # File should not have been written again
            if mtime_before is not None:
                assert path.stat().st_mtime == mtime_before


# ── GPSDaemon heartbeat extras ─────────────────────────────────────────────

class TestGPSDaemonHeartbeatExtras:

    def test_heartbeat_extras_keys(self):
        d = GPSDaemon()
        extras = d.heartbeat_extras()
        assert "detected_port"   in extras
        assert "gps_mode"        in extras
        assert "jamming_state"   in extras
        assert "spoofing_state"  in extras
        assert "survey_complete" in extras

    def test_heartbeat_extras_defaults(self):
        d = GPSDaemon(port="/dev/ttyACM0", mode="usb")
        extras = d.heartbeat_extras()
        assert extras["detected_port"]   == "/dev/ttyACM0"
        assert extras["gps_mode"]        == "usb"
        assert extras["jamming_state"]   is None
        assert extras["spoofing_state"]  is None
        assert extras["survey_complete"] is False


# ── GPS config passthrough (Bug 1 regression) ──────────────────────────────

class TestGPSDaemonConfigPassthrough:
    """
    Verify that GPSDaemon correctly stores auto_detect and mode when passed
    explicitly — these are the values agent.py now forwards from NodeConfig.
    """

    def test_auto_detect_false_stored(self):
        """GPSDaemon stores auto_detect=False as passed from config."""
        d = GPSDaemon(port="/dev/ttyACM0", baud=38400, mode="usb", auto_detect=False)
        assert d.auto_detect is False

    def test_auto_detect_true_stored(self):
        """GPSDaemon stores auto_detect=True as passed from config."""
        d = GPSDaemon(port="/dev/ttyACM0", baud=38400, mode="usb", auto_detect=True)
        assert d.auto_detect is True

    def test_mode_usb_stored(self):
        """GPSDaemon stores mode='usb' as passed from config."""
        d = GPSDaemon(port="/dev/ttyACM0", baud=38400, mode="usb", auto_detect=False)
        assert d.mode == "usb"

    def test_mode_uart_stored(self):
        """GPSDaemon stores mode='uart' as passed from config."""
        d = GPSDaemon(port="/dev/ttyAMA0", baud=9600, mode="uart", auto_detect=False)
        assert d.mode == "uart"


# ── _probe_port UBX-CFG-PRT injection (Bug 2 regression) ──────────────────

class TestProbePortUBXCFGPRT:
    """
    Verify that _probe_port sends UBX-CFG-PRT before attempting to read NMEA,
    and that it correctly handles a NEO-M9N returning UBX binary before NMEA.
    """

    _GGA = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"

    def _make_serial(self, readline_returns):
        mock_ser = MagicMock()
        mock_ser.__enter__ = MagicMock(return_value=mock_ser)
        mock_ser.__exit__  = MagicMock(return_value=False)
        mock_ser.readline.side_effect = [
            (line + "\r\n").encode("ascii") if isinstance(line, str) else line
            for line in readline_returns
        ]
        return mock_ser

    def test_write_called_before_readline(self):
        """UBX-CFG-PRT write happens before the first readline attempt."""
        mock_ser = self._make_serial([self._GGA])
        with patch("publisher.gps.serial.Serial", return_value=mock_ser), \
             patch("publisher.gps.time.sleep"):
            _probe_port("/dev/ttyACM0", 38400)

        call_names = [c[0] for c in mock_ser.method_calls]
        assert "write" in call_names
        assert "readline" in call_names
        assert call_names.index("write") < call_names.index("readline")

    def test_write_sends_ubx_cfg_prt_for_usb_port(self):
        """Written frame is UBX-CFG-PRT with portID=3 and outProtoMask=3."""
        mock_ser = self._make_serial([self._GGA])
        with patch("publisher.gps.serial.Serial", return_value=mock_ser), \
             patch("publisher.gps.time.sleep"):
            _probe_port("/dev/ttyACM0", 38400)

        frame = mock_ser.write.call_args_list[0][0][0]
        assert frame[2] == UBX_CLASS_CFG
        assert frame[3] == UBX_CFG_PRT
        payload = frame[6:-2]
        port_id, _, _, _, _, in_proto, out_proto, _, _ = struct.unpack("<BBHIIHHHH", payload)
        assert port_id   == 3   # USB
        assert out_proto == 3   # UBX+NMEA

    def test_returns_true_when_nmea_follows_ubx_binary(self):
        """Returns True when NMEA appears after initial UBX binary garbage."""
        mock_ser = self._make_serial([
            b"\xb5\x62\x01\x03\x10\x00garbage_ubx_binary",  # UBX frame
            self._GGA,                                        # valid NMEA follows
        ])
        with patch("publisher.gps.serial.Serial", return_value=mock_ser), \
             patch("publisher.gps.time.sleep"):
            result = _probe_port("/dev/ttyACM0", 38400)

        assert result is True

    def test_reset_input_buffer_called_after_sleep(self):
        """Input buffer is flushed after the 500ms wait to clear UBX binary."""
        mock_ser = self._make_serial([self._GGA])
        with patch("publisher.gps.serial.Serial", return_value=mock_ser), \
             patch("publisher.gps.time.sleep"):
            _probe_port("/dev/ttyACM0", 38400)

        call_names = [c[0] for c in mock_ser.method_calls]
        assert "reset_input_buffer" in call_names
        assert call_names.index("reset_input_buffer") < call_names.index("readline")


# ── UBX-NAV-PVT parsing ────────────────────────────────────────────────────

def _make_pvt_payload(
    fix_type: int = 3,
    num_sv:   int = 8,
    lat_deg:  float = 51.477928,
    lon_deg:  float = -0.001545,
    alt_mm:   int   = 65_000,
) -> bytes:
    """
    Build a minimal 36-byte NAV-PVT payload with the fields used by
    _parse_nav_pvt.  Fields not relevant to the handler are zeroed.

    Layout (first 36 bytes only):
      offset  0: iTOW      U4   (zeroed)
      offset  4–19:        16 bytes reserved/other fields (zeroed)
      offset 20: fixType   U1
      offset 21: flags     U1   (zeroed)
      offset 22: flags2    U1   (zeroed)
      offset 23: numSV     U1
      offset 24: lon       I4   degrees × 1e-7
      offset 28: lat       I4   degrees × 1e-7
      offset 32: height    I4   mm above ellipsoid
    """
    buf = bytearray(36)
    buf[20] = fix_type
    buf[23] = num_sv
    struct.pack_into("<i", buf, 24, int(round(lon_deg * 1e7)))
    struct.pack_into("<i", buf, 28, int(round(lat_deg * 1e7)))
    struct.pack_into("<i", buf, 32, alt_mm)
    return bytes(buf)


class TestNavPVTParsing:

    def _daemon(self):
        return GPSDaemon(survey_state_path=Path("/tmp/survey_pvt_test.json"))

    def test_fix_type_3_sets_has_fix_true(self):
        """fix_type=3 (3D fix) should set _has_fix=True."""
        d = self._daemon()
        d._parse_nav_pvt(_make_pvt_payload(fix_type=3))
        assert d.has_fix is True

    def test_fix_type_4_sets_has_fix_true(self):
        """fix_type=4 (GNSS+dead reck) also counts as a valid fix."""
        d = self._daemon()
        d._parse_nav_pvt(_make_pvt_payload(fix_type=4))
        assert d.has_fix is True

    def test_fix_type_0_sets_has_fix_false(self):
        """fix_type=0 (no fix) must leave _has_fix=False."""
        d = self._daemon()
        d._parse_nav_pvt(_make_pvt_payload(fix_type=0))
        assert d.has_fix is False

    def test_fix_type_2_sets_has_fix_false(self):
        """fix_type=2 (2D fix — below threshold 3) must not set a valid fix."""
        d = self._daemon()
        d._parse_nav_pvt(_make_pvt_payload(fix_type=2))
        assert d.has_fix is False

    def test_lat_lon_decoded_correctly(self):
        """lat/lon I4 fields (degrees × 1e-7) are decoded to float degrees."""
        d = self._daemon()
        lat, lon = 51.477928, -0.001545
        d._parse_nav_pvt(_make_pvt_payload(fix_type=3, lat_deg=lat, lon_deg=lon))
        assert abs(d.lat - lat) < 1e-6
        assert abs(d.lon - lon) < 1e-6

    def test_altitude_decoded_from_mm_to_metres(self):
        """Height field (mm) is converted to metres."""
        d = self._daemon()
        d._parse_nav_pvt(_make_pvt_payload(fix_type=3, alt_mm=65_432))
        assert abs(d.alt - 65.432) < 0.001

    def test_num_sv_stored(self):
        """numSV field is stored on the daemon instance."""
        d = self._daemon()
        d._parse_nav_pvt(_make_pvt_payload(fix_type=3, num_sv=12))
        assert d.satellites == 12

    def test_payload_too_short_is_ignored(self):
        """Payloads shorter than 36 bytes must not raise and must not set fix."""
        d = self._daemon()
        d._parse_nav_pvt(b"\x00" * 35)   # one byte too short
        assert d.has_fix is False

    def test_payload_empty_is_ignored(self):
        """Empty payload must not raise."""
        d = self._daemon()
        d._parse_nav_pvt(b"")
        assert d.has_fix is False

    def test_fix_acquired_sets_fix_event(self):
        """_fix_event is set when a valid fix arrives."""
        d = self._daemon()
        assert not d._fix_event.is_set()
        d._parse_nav_pvt(_make_pvt_payload(fix_type=3))
        assert d._fix_event.is_set()

    def test_lost_fix_clears_has_fix(self):
        """Losing fix (fix_type=0 after fix_type=3) sets _has_fix back to False."""
        d = self._daemon()
        d._parse_nav_pvt(_make_pvt_payload(fix_type=3))
        assert d.has_fix is True
        d._parse_nav_pvt(_make_pvt_payload(fix_type=0))
        assert d.has_fix is False


# ── _handle_ubx NAV-PVT dispatch ───────────────────────────────────────────

class TestHandleUBXDispatch:

    def _daemon(self):
        return GPSDaemon(survey_state_path=Path("/tmp/survey_dispatch_test.json"))

    def test_handle_ubx_dispatches_nav_pvt(self):
        """_handle_ubx routes class=0x01 id=0x07 to _parse_nav_pvt."""
        d = self._daemon()
        payload = _make_pvt_payload(fix_type=3, lat_deg=42.0, lon_deg=-71.0)
        with patch.object(d, "_parse_nav_pvt") as mock_pvt:
            d._handle_ubx(UBX_CLASS_NAV, UBX_NAV_PVT, payload)
        mock_pvt.assert_called_once_with(payload)

    def test_handle_ubx_dispatches_nav_status(self):
        """_handle_ubx still routes 0x01/0x03 to _parse_nav_status."""
        d = self._daemon()
        with patch.object(d, "_parse_nav_status") as mock_status:
            d._handle_ubx(UBX_CLASS_NAV, UBX_NAV_STATUS, b"\x00" * 16)
        mock_status.assert_called_once()

    def test_handle_ubx_dispatches_nav_svin(self):
        """_handle_ubx still routes 0x01/0x3B to _parse_nav_svin."""
        d = self._daemon()
        with patch.object(d, "_parse_nav_svin") as mock_svin:
            d._handle_ubx(UBX_CLASS_NAV, UBX_NAV_SVIN, b"\x00" * 40)
        mock_svin.assert_called_once()

    def test_handle_ubx_unknown_message_is_silently_ignored(self):
        """Unknown UBX class/id combinations must not raise."""
        d = self._daemon()
        d._handle_ubx(0xFF, 0xFF, b"\x00" * 10)   # no exception expected


# ── NodeConfig default baud rate ───────────────────────────────────────────

class TestNodeConfigDefaults:

    def test_default_gps_baud_is_38400(self):
        """Default gps_baud must be 38400 to match NEO-M9N USB default."""
        from config.settings import NodeConfig
        cfg = NodeConfig()
        assert cfg.gps_baud == 38400
