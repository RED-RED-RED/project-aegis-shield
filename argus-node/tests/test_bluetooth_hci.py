"""
Bluetooth Remote ID: HCI scanner, per-transmitter assembly, end-to-end publish.

Advert layout matches a real BT4 module capture (btmon on the ARGUS nRF52840):

    Service Data: ASTM Remote ID (0xfffa)
      Data[27]: 0d b1 02 12 <20-byte serial> 00 00 00
                ^  ^  ^  ^
                |  |  |  IDType 1 (serial) / UAType 2 (multirotor)
                |  |  BasicID, protocol 2
                |  message counter
                app code 0x0D

The real module rotated BasicID (serial), BasicID (CAA registration) and
OperatorID, one message per advert.  IDs and the MAC address here are made up.
"""

import struct
import threading
from collections import deque
from unittest.mock import MagicMock

import pytest

from parser.assembler import RIDAssembler
from parser.opendroneid import OpenDroneIDParser
from scanner import bluetooth as bt_mod
from scanner.bluetooth import BluetoothScanner
from scanner.hci_le import (
    ExtReassembler, HCIError, HCIUnavailable, LEScanner, AdvReport,
    OP_LE_READ_FEATURES, OP_LE_SET_EXT_SCAN_ENABLE, OP_LE_SET_EXT_SCAN_PARAMS,
    OP_LE_SET_EVENT_MASK, OP_LE_SET_RANDOM_ADDR, OP_LE_SET_SCAN_ENABLE,
    OP_LE_SET_SCAN_PARAMS, OP_RESET,
    LE_FEATURE_CODED_PHY, LE_FEATURE_EXT_ADV,
    format_address, iter_ad_structures, parse_extended_reports,
    parse_legacy_reports, rid_service_data,
)

SERIAL = b"TEST00SERIAL"          # stands in for the module's serial number
CAA    = b"FA3TESTREG1"           # stands in for an FAA registration number
MAC    = "02:00:00:AA:BB:CC"
MAC_LE = bytes(reversed(bytes.fromhex(MAC.replace(":", ""))))

LAT, LON = 40.0012345, -100.0054321


# ── Message builders ─────────────────────────────────────────────────────────

def basic_id(id_type: int, uas_id: bytes, ua_type: int = 2) -> bytes:
    return bytes([0x02, (id_type << 4) | ua_type]) + uas_id.ljust(20, b"\x00") + b"\x00" * 3

def operator_id_empty() -> bytes:
    return bytes([0x52, 0x00]) + b"\x00" * 23

def location(lat=LAT, lon=LON, height=2.5, speed=1.0, heading=90) -> bytes:
    m = bytearray(25)
    m[0] = 0x12
    ew = 1 if heading >= 180 else 0
    m[1] = (2 << 4) | (ew << 1)                  # airborne
    m[2] = int(heading - 180 * ew)
    m[3] = int(speed / 0.25)
    struct.pack_into("<i", m, 5, round(lat * 1e7))
    struct.pack_into("<i", m, 9, round(lon * 1e7))
    alt = int((300 + 1000) * 2)
    struct.pack_into("<H", m, 13, alt)
    struct.pack_into("<H", m, 15, alt)
    struct.pack_into("<H", m, 17, int((height + 1000) * 2))
    return bytes(m)

def service_body(msg: bytes, counter: int = 0xB1) -> bytes:
    return bytes([0x0D, counter]) + msg

def ad_data(body: bytes) -> bytes:
    """Service data AD structure exactly as the module sends it (31 bytes for BT4)."""
    return bytes([len(body) + 3, 0x16, 0xFA, 0xFF]) + body

def legacy_report_event(data: bytes, rssi: int = -62) -> bytes:
    body = bytes([0x02, 1, 0x03, 0x00]) + MAC_LE + bytes([len(data)]) + data + struct.pack("<b", rssi)
    return bytes([0x04, 0x3E, len(body)]) + body

def ext_report_event(data: bytes, rssi: int = -80, phy: int = 0x03, status: int = 0,
                     legacy: bool = False, sid: int = 1) -> bytes:
    ev_type = (0x10 if legacy else 0x00) | (status << 5)
    rep = (struct.pack("<H", ev_type) + bytes([0x00]) + MAC_LE
           + bytes([phy, 0x00 if legacy else phy, sid, 0x7F]) + struct.pack("<b", rssi)
           + struct.pack("<H", 0) + bytes([0x00]) + b"\x00" * 6
           + bytes([len(data)]) + data)
    body = bytes([0x0D, 1]) + rep
    return bytes([0x04, 0x3E, len(body)]) + body


# ── Fake controller ──────────────────────────────────────────────────────────

class FakeController:
    """Answers every HCI command with Command Complete; replays queued events."""

    def __init__(self, features: int = LE_FEATURE_EXT_ADV | LE_FEATURE_CODED_PHY,
                 fail: set = frozenset(), on_empty=None):
        self.features = features
        self.fail = set(fail)
        self.commands: list[tuple[int, bytes]] = []
        self.rx: deque[bytes] = deque()
        self.adverts: deque[bytes] = deque()
        self.on_empty = on_empty
        self.scanning = False

    def write(self, pkt: bytes):
        assert pkt[0] == 0x01
        opcode = struct.unpack_from("<H", pkt, 1)[0]
        params = pkt[4:4 + pkt[3]]
        self.commands.append((opcode, params))
        status = 0x0C if opcode in self.fail else 0x00
        ret = bytes([status])
        if opcode == OP_LE_READ_FEATURES:
            ret += struct.pack("<Q", self.features)
        body = bytes([1]) + struct.pack("<H", opcode) + ret
        self.rx.append(bytes([0x04, 0x0E, len(body)]) + body)
        if opcode in (OP_LE_SET_EXT_SCAN_ENABLE, OP_LE_SET_SCAN_ENABLE):
            self.scanning = params[0] == 1

    def read(self, timeout: float):
        if self.rx:
            return self.rx.popleft()
        if self.scanning and self.adverts:
            return self.adverts.popleft()
        if self.on_empty:
            self.on_empty()
        return None

    # HCIUserChannel interface, for the end-to-end test
    def open(self): pass
    def close(self): pass

    def opcodes(self):
        return [op for op, _ in self.commands]

    def params(self, opcode):
        return [p for op, p in self.commands if op == opcode][-1]


# ── AD structures ────────────────────────────────────────────────────────────

class TestAdvertisingData:
    def test_ruko_shaped_advert_yields_service_body(self):
        body = service_body(basic_id(1, SERIAL))
        data = ad_data(body)
        assert len(data) == 31 and len(body) == 27       # same sizes as the capture
        assert rid_service_data(data) == body

    def test_non_rid_advert_ignored(self):
        data = bytes([0x02, 0x01, 0x06, 0x05, 0x09]) + b"Beac"
        assert rid_service_data(data) is None

    def test_other_service_uuid_ignored(self):
        data = bytes([0x06, 0x16, 0x0F, 0x18, 0x01, 0x02, 0x03])
        assert rid_service_data(data) is None

    def test_malformed_lengths_do_not_crash(self):
        assert list(iter_ad_structures(bytes([0x1E, 0x16, 0xFA]))) == []
        assert rid_service_data(b"") is None

    def test_rid_found_after_other_structures(self):
        body = service_body(location())
        data = bytes([0x02, 0x01, 0x06]) + ad_data(body)
        assert rid_service_data(data) == body


# ── HCI report parsing ───────────────────────────────────────────────────────

class TestReportParsing:
    def test_legacy_report(self):
        data = ad_data(service_body(basic_id(1, SERIAL)))
        evt = legacy_report_event(data, rssi=-62)
        reps = parse_legacy_reports(evt[4:])
        assert len(reps) == 1
        assert reps[0].address == MAC
        assert reps[0].rssi == -62
        assert reps[0].data == data
        assert reps[0].phy_label == "1M"

    def test_legacy_report_two_reports_field_major(self):
        d1, d2 = b"\x02\x01\x06", b"\x03\x09AB"
        a1, a2 = MAC_LE, bytes(6)
        body = (bytes([2, 0x03, 0x00, 0x00, 0x01]) + a1 + a2
                + bytes([len(d1), len(d2)]) + d1 + d2 + struct.pack("<bb", -50, -70))
        reps = parse_legacy_reports(body)
        assert [r.data for r in reps] == [d1, d2]
        assert [r.rssi for r in reps] == [-50, -70]
        assert reps[0].address == MAC

    def test_extended_report_coded(self):
        pack = bytes([0xF2, 0x19, 2]) + basic_id(1, SERIAL) + location()
        data = ad_data(service_body(pack))
        evt = ext_report_event(data, rssi=-91, phy=0x03)
        reps = parse_extended_reports(evt[4:])
        assert len(reps) == 1
        r = reps[0]
        assert (r.address, r.rssi, r.legacy, r.phy_label) == (MAC, -91, False, "coded")
        assert r.data == data

    def test_extended_report_legacy_pdu(self):
        data = ad_data(service_body(basic_id(1, SERIAL)))
        r = parse_extended_reports(ext_report_event(data, phy=0x01, legacy=True)[4:])[0]
        assert r.legacy and r.phy_label == "1M"

    def test_truncated_event_is_ignored(self):
        evt = ext_report_event(ad_data(service_body(location())))
        assert parse_extended_reports(evt[4:-5]) == []

    def test_format_address(self):
        assert format_address(MAC_LE) == MAC


class TestReassembly:
    def test_fragments_join(self):
        ra = ExtReassembler()
        a = AdvReport(MAC, 0, -80, b"abc", legacy=False, sid=1, data_status=1)
        b = AdvReport(MAC, 0, -80, b"def", legacy=False, sid=1, data_status=0)
        assert ra.add(a, now=0.0) is None
        assert ra.add(b, now=0.1).data == b"abcdef"

    def test_truncated_is_dropped(self):
        ra = ExtReassembler()
        ra.add(AdvReport(MAC, 0, -80, b"abc", legacy=False, sid=1, data_status=1), now=0.0)
        assert ra.add(AdvReport(MAC, 0, -80, b"d", legacy=False, sid=1, data_status=2), now=0.1) is None

    def test_legacy_passes_through(self):
        r = AdvReport(MAC, 0, -60, b"x", legacy=True)
        assert ExtReassembler().add(r) is r


# ── Controller configuration ─────────────────────────────────────────────────

class TestLEScannerSetup:
    def test_extended_coded_scan_with_duplicates_off(self):
        fc = FakeController()
        sc = LEScanner(fc, coded_phy=True)
        sc.start()
        assert fc.opcodes()[0] == OP_RESET
        assert sc.extended and sc.coded

        addr = fc.params(OP_LE_SET_RANDOM_ADDR)
        assert len(addr) == 6 and addr[5] & 0xC0 == 0xC0      # random static

        p = fc.params(OP_LE_SET_EXT_SCAN_PARAMS)
        assert p[0] == 0x01                                    # own address: random
        assert p[2] == 0x05                                    # 1M + Coded
        assert len(p) == 3 + 5 * 2                             # one block per PHY
        assert p[3] == 0x00 and p[8] == 0x00                   # passive on both

        en = fc.params(OP_LE_SET_EXT_SCAN_ENABLE)
        assert en[0] == 1 and en[1] == 0                       # enabled, no dup filter
        assert OP_LE_SET_SCAN_PARAMS not in fc.opcodes()       # never mix legacy cmds

    def test_extended_without_coded_feature(self):
        fc = FakeController(features=LE_FEATURE_EXT_ADV)
        sc = LEScanner(fc, coded_phy=True)
        sc.start()
        assert sc.extended and not sc.coded
        p = fc.params(OP_LE_SET_EXT_SCAN_PARAMS)
        assert p[2] == 0x01 and len(p) == 3 + 5

    def test_coded_disabled_by_config(self):
        fc = FakeController()
        sc = LEScanner(fc, coded_phy=False)
        sc.start()
        assert not sc.coded
        assert fc.params(OP_LE_SET_EXT_SCAN_PARAMS)[2] == 0x01

    def test_legacy_controller(self):
        fc = FakeController(features=0)
        sc = LEScanner(fc)
        sc.start()
        assert not sc.extended
        p = fc.params(OP_LE_SET_SCAN_PARAMS)
        assert p[0] == 0x00 and p[5] == 0x01                   # passive, random addr
        assert fc.params(OP_LE_SET_SCAN_ENABLE) == bytes([1, 0])
        assert OP_LE_SET_EXT_SCAN_PARAMS not in fc.opcodes()

    def test_coded_without_ext_bit_tries_extended(self):
        """Controllers that report Coded PHY but not Extended Advertising."""
        fc = FakeController(features=LE_FEATURE_CODED_PHY)
        sc = LEScanner(fc)
        sc.start()
        assert sc.extended and sc.coded
        assert fc.params(OP_LE_SET_EXT_SCAN_PARAMS)[2] == 0x05

    def test_rejected_extended_falls_back_to_legacy_after_reset(self):
        fc = FakeController(features=LE_FEATURE_CODED_PHY,
                            fail={OP_LE_SET_EXT_SCAN_PARAMS})
        sc = LEScanner(fc)
        sc.start()
        assert not sc.extended and not sc.coded and sc.ext_error
        ops = fc.opcodes()
        last_ext = max(i for i, op in enumerate(ops) if op == OP_LE_SET_EXT_SCAN_PARAMS)
        assert OP_RESET in ops[last_ext:]                    # reset before legacy commands
        assert fc.params(OP_LE_SET_SCAN_ENABLE) == bytes([1, 0])

    def test_coded_rejected_but_1m_extended_works(self):
        class CodedFails(FakeController):
            def write(self, pkt):
                op = struct.unpack_from("<H", pkt, 1)[0]
                self.fail = {OP_LE_SET_EXT_SCAN_PARAMS} if op == OP_LE_SET_EXT_SCAN_PARAMS and pkt[6] == 0x05 else set()
                super().write(pkt)
        fc = CodedFails()
        sc = LEScanner(fc)
        sc.start()
        assert sc.extended and not sc.coded
        assert fc.params(OP_LE_SET_EXT_SCAN_PARAMS)[2] == 0x01

    def test_feature_summary(self):
        fc = FakeController(features=LE_FEATURE_CODED_PHY)
        sc = LEScanner(fc)
        sc.start()
        assert "coded PHY: yes" in sc.feature_summary
        assert "extended advertising: no" in sc.feature_summary

    def test_command_failure_raises(self):
        fc = FakeController(fail={OP_LE_SET_EVENT_MASK})
        with pytest.raises(HCIError):
            LEScanner(fc).start()

    def test_no_response_raises(self):
        class Silent(FakeController):
            def write(self, pkt): self.commands.append((0, b""))
        with pytest.raises(HCIError):
            LEScanner(Silent())._command(OP_RESET, timeout=0.05)

    def test_reports_during_command_are_kept(self):
        fc = FakeController()
        sc = LEScanner(fc)
        sc.start()
        data = ad_data(service_body(basic_id(1, SERIAL)))
        fc.rx.append(legacy_report_event(data))       # arrives before the reply
        sc.stop()
        fc.scanning = True
        reps = sc.read_reports(timeout=0)
        assert reps and reps[0].data == data

    def test_scanner_decodes_fragmented_extended_advert(self):
        fc = FakeController()
        sc = LEScanner(fc)
        sc.start()
        pack = bytes([0xF2, 0x19, 2]) + basic_id(1, SERIAL) + location()
        data = ad_data(service_body(pack))
        fc.adverts.append(ext_report_event(data[:30], status=1))
        fc.adverts.append(ext_report_event(data[30:], status=0))
        got = sc.read_reports(0) + sc.read_reports(0)
        assert len(got) == 1 and got[0].data == data


# ── Parser: competing BasicIDs ───────────────────────────────────────────────

class TestBasicIdPriority:
    @pytest.mark.parametrize("order", [(1, 2), (2, 1)])
    def test_serial_wins_in_a_pack(self, order):
        ids = {1: basic_id(1, SERIAL), 2: basic_id(2, CAA)}
        pack = bytes([0xF2, 0x19, 2]) + ids[order[0]] + ids[order[1]]
        f = OpenDroneIDParser().parse(pack)[0]
        assert f.drone_id == SERIAL.decode() and f.id_type == "serial_number"

    def test_empty_basic_id_does_not_erase(self):
        p = OpenDroneIDParser()
        f = p.parse(basic_id(1, SERIAL))[0]
        p.apply(f, basic_id(0, b""))
        assert f.drone_id == SERIAL.decode()


# ── Assembly per transmitter ─────────────────────────────────────────────────

def payload(msg):
    return msg  # app code + counter already stripped


class TestAssembler:
    def test_ruko_rotation_produces_position(self):
        a = RIDAssembler(publish_interval=1.0)
        s1 = a.feed(MAC, basic_id(1, SERIAL), "bluetooth", now=0.0)
        assert s1 is not None and s1.lat == 0.0                 # identity first
        assert a.feed(MAC, basic_id(2, CAA), "bluetooth", now=0.3) is None
        assert a.feed(MAC, operator_id_empty(), "bluetooth", now=0.6) is None
        assert a.feed(MAC, location(), "bluetooth", now=0.8) is None   # rate limited
        s2 = a.feed(MAC, location(), "bluetooth", now=1.2)
        assert s2.drone_id == SERIAL.decode()
        assert s2.lat == pytest.approx(LAT, abs=1e-6)
        assert s2.lon == pytest.approx(LON, abs=1e-6)
        assert s2.height_agl == pytest.approx(2.5)
        assert s2.status == "airborne"

    def test_location_before_identity(self):
        a = RIDAssembler()
        assert a.feed(MAC, location(), "bluetooth", now=0.0) is None
        s = a.feed(MAC, basic_id(1, SERIAL), "bluetooth", now=0.2)
        assert s.drone_id == SERIAL.decode() and s.lat == pytest.approx(LAT, abs=1e-6)

    def test_caa_first_then_serial_keeps_one_id(self):
        a = RIDAssembler(id_grace=3.0, publish_interval=0.0)
        assert a.feed(MAC, basic_id(2, CAA), "bluetooth", now=0.0) is None   # wait for serial
        s = a.feed(MAC, basic_id(1, SERIAL), "bluetooth", now=0.5)
        assert s.drone_id == SERIAL.decode()
        s = a.feed(MAC, basic_id(2, CAA), "bluetooth", now=1.0)
        assert s.drone_id == SERIAL.decode()

    def test_caa_only_drone_published_after_grace(self):
        a = RIDAssembler(id_grace=3.0, publish_interval=0.0)
        assert a.feed(MAC, basic_id(2, CAA), "bluetooth", now=0.0) is None
        s = a.feed(MAC, basic_id(2, CAA), "bluetooth", now=3.1)
        assert s.drone_id == CAA.decode() and s.id_type == "caa_registration"

    def test_snapshot_is_a_copy(self):
        a = RIDAssembler(publish_interval=0.0)
        s = a.feed(MAC, basic_id(1, SERIAL), "bluetooth", now=0.0)
        a.feed(MAC, location(), "bluetooth", now=0.1)
        assert s.lat == 0.0

    def test_transmitters_are_separate(self):
        a = RIDAssembler(publish_interval=0.0)
        a.feed("AA", location(), "bluetooth", now=0.0)
        s = a.feed("BB", basic_id(1, SERIAL), "bluetooth", now=0.1)
        assert s.lat == 0.0

    def test_silent_transmitters_expire(self):
        a = RIDAssembler(ttl=30.0)
        a.feed(MAC, basic_id(1, SERIAL), "bluetooth", now=0.0)
        a.feed("OTHER", basic_id(1, b"X1"), "bluetooth", now=40.0)
        assert len(a) == 1

    def test_garbage_ignored(self):
        assert RIDAssembler().feed(MAC, b"\x01\x02", "bluetooth", now=0.0) is None


# ── BluetoothScanner end to end ──────────────────────────────────────────────

def _scanner(backend="auto"):
    pub = MagicMock()
    gps = MagicMock(lat=40.0, lon=-100.0, alt=300.0)
    stop = threading.Event()
    s = BluetoothScanner(hci_index=1, publisher=pub, gps=gps, node_id="TEST-NODE",
                         stop_event=stop, enable_coded_phy=True, backend=backend)
    return s, pub, stop


class TestBluetoothScanner:
    def test_hci_path_publishes_position(self, monkeypatch):
        """Raw HCI advert events in, MQTT detection with a position out."""
        s, pub, stop = _scanner()
        s._assembler = RIDAssembler(publish_interval=0.0)
        fc = FakeController(on_empty=stop.set)
        for msg, ctr in [(basic_id(1, SERIAL), 0xB1), (basic_id(2, CAA), 0xC2),
                         (operator_id_empty(), 0xD2), (location(), 0xE3)]:
            fc.adverts.append(legacy_report_event(ad_data(service_body(msg, ctr)), rssi=-65))
        monkeypatch.setattr(bt_mod, "HCIUserChannel", lambda idx: fc)

        s.run_sync()

        calls = [c.kwargs for c in pub.publish_detection.call_args_list]
        assert calls, "nothing published"
        assert all(c["transport"] == "bluetooth" and c["src_addr"] == MAC for c in calls)
        last = calls[-1]["frame"]
        assert last.drone_id == SERIAL.decode()
        assert last.lat == pytest.approx(LAT, abs=1e-6)
        assert last.lon == pytest.approx(LON, abs=1e-6)
        assert calls[-1]["rssi"] == -65
        assert fc.params(OP_LE_SET_EXT_SCAN_ENABLE)[0] == 0        # scan stopped on exit

    def test_bt5_long_range_pack(self, monkeypatch):
        s, pub, stop = _scanner()
        fc = FakeController(on_empty=stop.set)
        pack = bytes([0xF2, 0x19, 3]) + basic_id(1, SERIAL) + location() + operator_id_empty()
        fc.adverts.append(ext_report_event(ad_data(service_body(pack)), rssi=-95, phy=0x03))
        monkeypatch.setattr(bt_mod, "HCIUserChannel", lambda idx: fc)
        s.run_sync()
        f = pub.publish_detection.call_args.kwargs["frame"]
        assert f.drone_id == SERIAL.decode() and f.lat == pytest.approx(LAT, abs=1e-6)

    def test_falls_back_to_bleak_when_hci_unavailable(self, monkeypatch):
        s, pub, stop = _scanner(backend="auto")

        class NoHCI:
            def __init__(self, idx): pass
            def open(self): raise HCIUnavailable("no permission")
            def close(self): pass
        monkeypatch.setattr(bt_mod, "HCIUserChannel", NoHCI)
        monkeypatch.setattr(bt_mod.subprocess, "run", MagicMock())
        ran = []
        async def fake_bleak(): ran.append(True)
        monkeypatch.setattr(s, "_run_bleak", fake_bleak)
        s.run_sync()
        assert ran == [True]

    def test_hci_only_backend_does_not_fall_back(self, monkeypatch):
        s, pub, stop = _scanner(backend="hci")

        class NoHCI:
            def __init__(self, idx): pass
            def open(self): raise HCIUnavailable("missing")
            def close(self): pass
        monkeypatch.setattr(bt_mod, "HCIUserChannel", NoHCI)
        ran = []
        async def fake_bleak(): ran.append(True)
        monkeypatch.setattr(s, "_run_bleak", fake_bleak)
        s.run_sync()
        assert ran == []

    def test_bleak_callback_uses_assembler(self):
        s, pub, stop = _scanner()
        s._assembler = RIDAssembler(publish_interval=0.0)
        dev = MagicMock(address=MAC)
        for msg in (basic_id(1, SERIAL), location()):
            adv = MagicMock(service_data={"0000fffa-0000-1000-8000-00805f9b34fb": service_body(msg)},
                            manufacturer_data={}, rssi=-60)
            s._on_advertisement(dev, adv)
        f = pub.publish_detection.call_args.kwargs["frame"]
        assert f.lat == pytest.approx(LAT, abs=1e-6)


# ── Config ───────────────────────────────────────────────────────────────────

def test_bluetooth_section_alias(tmp_path, monkeypatch):
    from config import settings
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("bluetooth:\n  hci_index: 2\n  backend: bleak\nbt:\n  coded_phy: false\n")
    monkeypatch.setattr(settings, "CONFIG_PATH", cfg_file)
    cfg = settings.load_config()
    assert cfg.bt_hci_index == 2
    assert cfg.bt_backend == "bleak"
    assert cfg.bt_coded_phy is False
