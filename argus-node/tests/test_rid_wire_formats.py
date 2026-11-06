"""
End-to-end wire format tests using real Remote ID bytes.

The message bytes below were captured from the ESP32 test broadcaster
(uav_electronic_ids random_flight) and match the layouts produced by
opendroneid-core-c libopendroneid/wifi.c, which compliant drones follow:

  Wi-Fi Beacon vendor IE : FA:0B:BC | 0x0D | counter | message pack
  Wi-Fi NAN SDF          : 04 09 50:6F:9A 13 | SDA(0x03) ... counter | message pack
  Bluetooth service data : 0x0D | counter | message (BT4) or pack (BT5)

These tests caught the bug where the counter byte was passed to the parser,
which shifted every field by one byte (garbled IDs, 0.0 or garbage lat/lon).
"""

import time
from unittest.mock import MagicMock

import pytest

scapy_all = pytest.importorskip("scapy.all")
if not hasattr(scapy_all, "__file__"):          # conftest stub, not real scapy
    pytest.skip("real scapy not installed", allow_module_level=True)
from scapy.all import RadioTap, Dot11, Dot11Beacon, Dot11Elt, Raw

from parser.opendroneid import OpenDroneIDParser
from scanner.bluetooth import strip_bt_rid_header
from scanner.wifi_nan import WiFiNANScanner, NAN_RID_SERVICE_ID


# ── Real messages captured from the ESP32 broadcaster ────────────────────────

def _pad(b: bytes) -> bytes:
    return b + b"\x00" * (25 - len(b))

DRONE_ID = b"USA-OP-AEGISTEST1"

# BasicID: type 0 / proto 2, IDType 2 (CAA reg) / UAType 0, then 20-byte ID
BASIC_ID = _pad(bytes([0x02, 0x20]) + DRONE_ID.ljust(20, b"\x00"))

# Location: ESP32 capture layout, lat/lon replaced with a neutral test point
LOCATION = bytes.fromhex(
    "12 00 72 52 7e 39 b4 d7 17 cf 61 64 c4 00 00 1e 0b 98 08 4a 41 00 00 0f 00"
)

# OperatorID: type 5 / proto 2, OperatorIdType 0, then 20-byte ID
OPERATOR_ID = _pad(bytes([0x52, 0x00]) + DRONE_ID.ljust(20, b"\x00"))

PACK = bytes([0xF2, 0x19, 0x03]) + BASIC_ID + LOCATION + OPERATOR_ID

EXPECTED_LAT = 40.0012345
EXPECTED_LON = -100.0054321


def _scanner():
    publisher = MagicMock()
    gps = MagicMock(lat=40.0, lon=-100.0, alt=300.0)
    scanner = WiFiNANScanner(
        iface="wlan1",
        publisher=publisher,
        gps=gps,
        node_id="TEST-NODE",
        stop_event=MagicMock(),
        band="2.4",
    )
    return scanner, publisher


def _published_frame(publisher):
    assert publisher.publish_detection.called, "publish_detection was not called"
    return publisher.publish_detection.call_args.kwargs["frame"]


# ── Parser ───────────────────────────────────────────────────────────────────

class TestMessagePack:
    def test_pack_decodes_id_and_position(self):
        frames = OpenDroneIDParser().parse(PACK, transport="wifi_beacon")
        assert len(frames) == 1
        f = frames[0]
        assert f.drone_id == "USA-OP-AEGISTEST1"
        assert f.lat == pytest.approx(EXPECTED_LAT, abs=1e-6)
        assert f.lon == pytest.approx(EXPECTED_LON, abs=1e-6)

    def test_pack_with_counter_prefix_is_not_decoded_as_valid(self):
        """Regression: the old scanner passed counter+pack to the parser."""
        frames = OpenDroneIDParser().parse(b"\x07" + PACK, transport="wifi_beacon")
        for f in frames:
            assert f.drone_id != "USA-OP-AEGISTEST1"


# ── Wi-Fi Beacon ─────────────────────────────────────────────────────────────

def _beacon_packet(counter: int = 0xE0):
    ie_body = bytes([0xFA, 0x0B, 0xBC, 0x0D, counter]) + PACK
    pkt = (
        RadioTap()
        / Dot11(type=0, subtype=8, addr1="ff:ff:ff:ff:ff:ff",
                addr2="02:00:00:aa:bb:cc", addr3="02:00:00:aa:bb:cc")
        / Dot11Beacon(cap=0x0411)
        / Dot11Elt(ID=0, info=b"USA-OP-AEGISTEST1")
        / Dot11Elt(ID=1, info=b"\x8b\x96\x82\x84\x0c\x18\x30\x60")
        / Dot11Elt(ID=221, info=ie_body)
    )
    # Re-dissect from bytes so the test sees exactly what sniff() would
    return RadioTap(bytes(pkt))


class TestBeaconWireFormat:
    def test_extract_strips_counter(self):
        scanner, _ = _scanner()
        assert scanner._extract_beacon_rid_payload(_beacon_packet()) == PACK

    @pytest.mark.parametrize("counter", [0x00, 0x07, 0x12, 0x52, 0xE0, 0xFF])
    def test_any_counter_value_decodes(self, counter):
        scanner, publisher = _scanner()
        scanner._process_beacon(_beacon_packet(counter))
        f = _published_frame(publisher)
        assert f.drone_id == "USA-OP-AEGISTEST1"
        assert f.lat == pytest.approx(EXPECTED_LAT, abs=1e-6)
        assert f.lon == pytest.approx(EXPECTED_LON, abs=1e-6)
        assert publisher.publish_detection.call_args.kwargs["transport"] == "wifi_beacon"


# ── Wi-Fi NAN Service Discovery ──────────────────────────────────────────────

def _nan_packet(counter: int = 0x05, with_extra_attr: bool = True):
    service_info = bytes([counter]) + PACK
    sda_value = (
        NAN_RID_SERVICE_ID
        + bytes([0x01, 0x00, 0x10, len(service_info)])
        + service_info
    )
    sda = bytes([0x03]) + len(sda_value).to_bytes(2, "little") + sda_value
    # Service Descriptor Extension attribute, as sent by the reference encoder
    sdea_value = bytes([0x01, 0x00, 0x02, 0x00])
    sdea = bytes([0x0E]) + len(sdea_value).to_bytes(2, "little") + sdea_value
    body = bytes([0x04, 0x09, 0x50, 0x6F, 0x9A, 0x13]) + sda
    if with_extra_attr:
        body += sdea
    pkt = (
        RadioTap()
        / Dot11(type=0, subtype=13, addr1="51:6f:9a:01:00:00",
                addr2="60:60:1f:aa:bb:cc", addr3="50:6f:9a:01:00:ff")
        / Raw(body)
    )
    return RadioTap(bytes(pkt))


class TestNanWireFormat:
    def test_extract_returns_pack(self):
        assert WiFiNANScanner.extract_nan_rid_payload(_nan_packet()) == PACK

    def test_process_publishes_decoded_detection(self):
        scanner, publisher = _scanner()
        scanner._process(_nan_packet())
        f = _published_frame(publisher)
        assert f.drone_id == "USA-OP-AEGISTEST1"
        assert f.lat == pytest.approx(EXPECTED_LAT, abs=1e-6)
        assert publisher.publish_detection.call_args.kwargs["transport"] == "wifi_nan"

    def test_non_rid_nan_frame_ignored(self):
        body = bytes([0x04, 0x09, 0x50, 0x6F, 0x9A, 0x13, 0x03, 0x0A, 0x00]) + b"\x11" * 10
        pkt = RadioTap(bytes(
            RadioTap() / Dot11(type=0, subtype=13, addr2="aa:bb:cc:dd:ee:ff") / Raw(body)
        ))
        assert WiFiNANScanner.extract_nan_rid_payload(pkt) is None

    def test_other_public_action_ignored(self):
        body = bytes([0x04, 0x09, 0x00, 0x10, 0x18, 0x01]) + b"\x00" * 30
        pkt = RadioTap(bytes(
            RadioTap() / Dot11(type=0, subtype=13, addr2="aa:bb:cc:dd:ee:ff") / Raw(body)
        ))
        assert WiFiNANScanner.extract_nan_rid_payload(pkt) is None


# ── Bluetooth ────────────────────────────────────────────────────────────────

class TestBluetoothWireFormat:
    def test_bt4_single_message(self):
        data = strip_bt_rid_header(bytes([0x0D, 0x33]) + LOCATION)
        assert data == LOCATION

    def test_bt5_message_pack(self):
        data = strip_bt_rid_header(bytes([0x0D, 0x01]) + PACK)
        frames = OpenDroneIDParser().parse(data, transport="bluetooth")
        assert frames and frames[0].drone_id == "USA-OP-AEGISTEST1"
        assert frames[0].lat == pytest.approx(EXPECTED_LAT, abs=1e-6)

    def test_bt4_basic_id(self):
        data = strip_bt_rid_header(bytes([0x0D, 0x02]) + BASIC_ID)
        frames = OpenDroneIDParser().parse(data, transport="bluetooth")
        assert frames and frames[0].drone_id == "USA-OP-AEGISTEST1"
