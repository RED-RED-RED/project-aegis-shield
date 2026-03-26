"""
tests/test_wifi_nan.py
======================
Unit tests for WiFiNANScanner dual-adapter support.

Run: python -m pytest tests/test_wifi_nan.py -v
"""

import threading
import time
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import MagicMock, patch
from scanner.wifi_nan import (
    WiFiNANScanner,
    _CHANNELS_2G, _CHANNELS_5G,
    RID_BEACON_OUI_ASTM, RID_BEACON_OUI_ODID, RID_BEACON_IE_TYPE,
    WIFI_IE_VENDOR_SPECIFIC,
)
from config.settings import NodeConfig, load_config


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _make_scanner(band="2.4", iface="wlan1", channels=None, dwell_ms=200):
    publisher = MagicMock()
    gps = MagicMock()
    gps.lat = 42.0
    gps.lon = -71.0
    gps.alt = 10.0
    stop = threading.Event()
    return WiFiNANScanner(
        iface=iface,
        publisher=publisher,
        gps=gps,
        node_id="TEST-01",
        stop_event=stop,
        band=band,
        channels=channels,
        dwell_ms=dwell_ms,
    ), stop


# ------------------------------------------------------------------ #
# Channel list initialisation
# ------------------------------------------------------------------ #

class TestChannelInit:
    def test_2g_default_channels(self):
        scanner, _ = _make_scanner(band="2.4")
        assert scanner.channels == _CHANNELS_2G
        assert scanner.channels == [1, 6, 11]

    def test_5g_default_channels(self):
        scanner, _ = _make_scanner(band="5")
        assert scanner.channels == _CHANNELS_5G
        assert scanner.channels == [36, 40, 44, 48, 149, 153, 157, 161]

    def test_explicit_channels_override_band_default(self):
        """Caller-supplied channels take precedence over band defaults."""
        custom = [1, 2, 3]
        scanner, _ = _make_scanner(band="2.4", channels=custom)
        assert scanner.channels == custom

    def test_band_stored_on_instance(self):
        s24, _ = _make_scanner(band="2.4")
        s5, _  = _make_scanner(band="5")
        assert s24.band == "2.4"
        assert s5.band  == "5"

    def test_log_label_includes_band(self):
        s24, _ = _make_scanner(band="2.4")
        s5, _  = _make_scanner(band="5")
        assert "2.4GHz" in s24._label
        assert "5GHz"   in s5._label


# ------------------------------------------------------------------ #
# Graceful failure when interface is absent
# ------------------------------------------------------------------ #

class TestGracefulFailure:
    def test_run_exits_if_interface_missing(self):
        """run() should return without raising when ip link show fails."""
        scanner, _ = _make_scanner(iface="wlan99")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            # Should complete without exception
            scanner.run()

        # Publisher must not have been called
        scanner.publisher.publish_detection.assert_not_called()

    def test_run_proceeds_if_interface_present(self):
        """run() enters sniff loop when interface exists."""
        scanner, stop = _make_scanner(iface="wlan1")

        def fake_subprocess(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            return mock

        def sniff_then_stop(*args, **kwargs):
            stop.set()  # stop after first sniff call so run() exits cleanly

        with patch("subprocess.run", side_effect=fake_subprocess), \
             patch("scanner.wifi_nan.sniff", side_effect=sniff_then_stop) as mock_sniff:
            scanner.run()

        mock_sniff.assert_called_once()


# ------------------------------------------------------------------ #
# Simultaneous instantiation
# ------------------------------------------------------------------ #

class TestSimultaneousInstantiation:
    def test_two_scanners_independent(self):
        """2.4 GHz and 5 GHz scanners can coexist without sharing state."""
        s24, _ = _make_scanner(band="2.4", iface="wlan1")
        s5, _  = _make_scanner(band="5",   iface="wlan2")

        assert s24.iface    != s5.iface
        assert s24.channels != s5.channels
        assert s24.band     != s5.band
        assert s24._seen is not s5._seen   # independent dedup dicts

    def test_two_scanners_share_publisher_independently(self):
        """Each scanner gets its own publisher reference but they can be the same object."""
        shared_publisher = MagicMock()
        gps = MagicMock()
        stop = threading.Event()

        s24 = WiFiNANScanner("wlan1", shared_publisher, gps, "N1", stop, band="2.4")
        s5  = WiFiNANScanner("wlan2", shared_publisher, gps, "N1", stop, band="5")

        assert s24.publisher is s5.publisher  # same shared publisher


# ------------------------------------------------------------------ #
# Detection callback and band field in payload
# ------------------------------------------------------------------ #

class TestDetectionCallback:
    def test_band_passed_to_publish_detection_2g(self):
        scanner, _ = _make_scanner(band="2.4")
        rid_frame = MagicMock()
        rid_frame.drone_id = "DRONE001"
        rid_frame.lat = 42.0
        rid_frame.lon = -71.0
        rid_frame.timestamp = time.time()

        # Craft a valid NAN action frame header so _process passes all checks
        raw_bytes = bytes([0x04, 0x09, 0xFA, 0x0B, 0xBE, 0x0D]) + b"\x00" * 20

        from scapy.all import Dot11, RadioTap, Raw
        dot11 = MagicMock(addr2="aa:bb:cc:dd:ee:ff")

        def getitem(layer):
            if layer is Dot11:    return dot11
            if layer is RadioTap: return MagicMock(dBm_AntSignal=-70)
            if layer is Raw:      return MagicMock()
            return MagicMock()

        pkt = MagicMock()
        pkt.haslayer.return_value = True
        pkt.__getitem__ = MagicMock(side_effect=getitem)

        with patch.object(scanner._parser, "parse", return_value=[rid_frame]), \
             patch("scanner.wifi_nan.bytes", return_value=raw_bytes):
            scanner._process(pkt)

        call_kwargs = scanner.publisher.publish_detection.call_args
        assert call_kwargs is not None, "publish_detection was not called"
        assert call_kwargs.kwargs.get("band") == "2.4"

    def test_band_passed_to_publish_detection_5g(self):
        scanner, _ = _make_scanner(band="5", iface="wlan2")
        rid_frame = MagicMock()
        rid_frame.drone_id = "DRONE002"
        rid_frame.lat = 42.0
        rid_frame.lon = -71.0
        rid_frame.timestamp = time.time()

        raw_bytes = bytes([0x04, 0x09, 0xFA, 0x0B, 0xBE, 0x0D]) + b"\x00" * 20

        from scapy.all import Dot11, RadioTap, Raw
        dot11 = MagicMock(addr2="aa:bb:cc:dd:ee:ff")

        def getitem(layer):
            if layer is Dot11:    return dot11
            if layer is RadioTap: return MagicMock(dBm_AntSignal=-55)
            if layer is Raw:      return MagicMock()
            return MagicMock()

        pkt = MagicMock()
        pkt.haslayer.return_value = True
        pkt.__getitem__ = MagicMock(side_effect=getitem)

        with patch.object(scanner._parser, "parse", return_value=[rid_frame]), \
             patch("scanner.wifi_nan.bytes", return_value=raw_bytes):
            scanner._process(pkt)

        call_kwargs = scanner.publisher.publish_detection.call_args
        assert call_kwargs is not None, "publish_detection was not called"
        assert call_kwargs.kwargs.get("band") == "5"


# ------------------------------------------------------------------ #
# Backward compatibility — old wifi.iface config key
# ------------------------------------------------------------------ #

class TestBackwardCompat:
    def test_old_iface_key_maps_to_interface_2g(self, tmp_path):
        """Legacy wifi.iface config is mapped to wifi_interface_2g."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "node_id: TEST-COMPAT\n"
            "wifi:\n"
            "  enabled: true\n"
            "  iface: wlan1mon\n"
            "  channel: 6\n"
        )

        import os
        old_env = os.environ.get("ARGUS_CONFIG")
        os.environ["ARGUS_CONFIG"] = str(config_file)

        try:
            # Reload settings module to pick up env change
            import importlib
            import config.settings as settings_mod
            importlib.reload(settings_mod)
            cfg = settings_mod.load_config()
        finally:
            if old_env is None:
                del os.environ["ARGUS_CONFIG"]
            else:
                os.environ["ARGUS_CONFIG"] = old_env

        assert cfg.wifi_interface_2g == "wlan1mon"
        assert cfg.wifi_enabled_2g is True

    def test_new_interface_2g_key_not_overwritten_by_legacy(self, tmp_path):
        """New interface_2g key takes priority; legacy iface key is ignored."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "node_id: TEST-NOOVERWRITE\n"
            "wifi:\n"
            "  interface_2g: wlan1\n"
            "  enabled_2g: true\n"
        )

        import os
        old_env = os.environ.get("ARGUS_CONFIG")
        os.environ["ARGUS_CONFIG"] = str(config_file)

        try:
            import importlib
            import config.settings as settings_mod
            importlib.reload(settings_mod)
            cfg = settings_mod.load_config()
        finally:
            if old_env is None:
                del os.environ["ARGUS_CONFIG"]
            else:
                os.environ["ARGUS_CONFIG"] = old_env

        assert cfg.wifi_interface_2g == "wlan1"

    def test_5g_disabled_by_default(self, tmp_path):
        """enabled_5g defaults to False — single-adapter deployments unaffected."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "node_id: TEST-SINGLE\n"
            "wifi:\n"
            "  interface_2g: wlan1\n"
            "  enabled_2g: true\n"
        )

        import os
        old_env = os.environ.get("ARGUS_CONFIG")
        os.environ["ARGUS_CONFIG"] = str(config_file)

        try:
            import importlib
            import config.settings as settings_mod
            importlib.reload(settings_mod)
            cfg = settings_mod.load_config()
        finally:
            if old_env is None:
                del os.environ["ARGUS_CONFIG"]
            else:
                os.environ["ARGUS_CONFIG"] = old_env

        assert cfg.wifi_enabled_5g is False


# ------------------------------------------------------------------ #
# Wi-Fi Beacon Remote ID detection
# ------------------------------------------------------------------ #

def _make_ie_chain(ies):
    """
    Build a linked list of mock 802.11 IE objects.

    ies — list of (element_id, info_bytes) tuples ordered as they appear
          in the Beacon frame body.

    The chain terminates with an object that has no 'ID' attribute, which
    causes _extract_beacon_rid_payload's hasattr(elt.payload, 'ID') loop
    guard to stop iteration — matching the scapy NoPayload sentinel.
    """
    class _Terminus:
        """Sentinel: no ID attribute, so hasattr check returns False."""

    if not ies:
        return None

    nodes = []
    for ie_id, info in ies:
        m = MagicMock()
        m.ID   = ie_id
        m.info = info
        nodes.append(m)

    for i in range(len(nodes) - 1):
        nodes[i].payload = nodes[i + 1]
    nodes[-1].payload = _Terminus()

    return nodes[0]


def _make_rid_ie_info(oui=None, ie_type=None, rid_data=None):
    """Build the raw bytes for a vendor-specific IE body containing Remote ID."""
    oui      = oui      or RID_BEACON_OUI_ASTM
    ie_type  = ie_type  if ie_type is not None else RID_BEACON_IE_TYPE
    rid_data = rid_data or (b"\x00" * 25)
    return oui + bytes([ie_type]) + rid_data


def _beacon_pkt(ie_chain, src_mac="aa:bb:cc:dd:ee:ff", rssi=-65):
    """Return a minimal mock 802.11 Beacon packet for use in unit tests."""
    from scapy.all import Dot11, RadioTap

    dot11 = MagicMock()
    dot11.subtype = 8
    dot11.addr2   = src_mac

    def getitem(layer):
        if layer is Dot11:    return dot11
        if layer is RadioTap: return MagicMock(dBm_AntSignal=rssi)
        return MagicMock()

    pkt = MagicMock()
    pkt.haslayer.return_value = True
    pkt.__getitem__ = MagicMock(side_effect=getitem)
    pkt.getlayer.return_value = ie_chain
    return pkt


class TestBeaconDetection:

    # ── _extract_beacon_rid_payload ────────────────────────────────────────

    def test_extract_returns_payload_for_astm_oui(self):
        """Vendor-specific IE with ASTM OUI returns the RID payload bytes."""
        scanner, _ = _make_scanner()
        rid_bytes   = b"\xF0" * 25
        ie_chain    = _make_ie_chain([
            (WIFI_IE_VENDOR_SPECIFIC, _make_rid_ie_info(oui=RID_BEACON_OUI_ASTM, rid_data=rid_bytes)),
        ])
        pkt = _beacon_pkt(ie_chain)
        result = scanner._extract_beacon_rid_payload(pkt)
        assert result == rid_bytes

    def test_extract_returns_payload_for_odid_oui(self):
        """OpenDroneID alternative OUI (ESP32 uav_electronic_ids) also accepted."""
        scanner, _ = _make_scanner()
        rid_bytes   = b"\xAB" * 25
        ie_chain    = _make_ie_chain([
            (WIFI_IE_VENDOR_SPECIFIC, _make_rid_ie_info(oui=RID_BEACON_OUI_ODID, rid_data=rid_bytes)),
        ])
        pkt = _beacon_pkt(ie_chain)
        result = scanner._extract_beacon_rid_payload(pkt)
        assert result == rid_bytes

    def test_extract_skips_non_vendor_ie_and_finds_rid(self):
        """Non-vendor IEs before the Remote ID IE are skipped."""
        scanner, _ = _make_scanner()
        rid_bytes   = b"\x12" * 25
        ie_chain    = _make_ie_chain([
            (0,   b"SSID-bytes"),             # SSID IE (not vendor-specific)
            (1,   b"\x82\x84\x8b\x96"),       # Supported Rates IE
            (WIFI_IE_VENDOR_SPECIFIC, _make_rid_ie_info(rid_data=rid_bytes)),
        ])
        pkt = _beacon_pkt(ie_chain)
        result = scanner._extract_beacon_rid_payload(pkt)
        assert result == rid_bytes

    def test_extract_returns_none_for_wrong_oui(self):
        """Vendor-specific IE with an unrecognised OUI is not treated as RID."""
        scanner, _ = _make_scanner()
        ie_chain = _make_ie_chain([
            (WIFI_IE_VENDOR_SPECIFIC, bytes([0x00, 0x50, 0xF2, 0x01]) + b"\x00" * 25),
        ])
        pkt = _beacon_pkt(ie_chain)
        assert scanner._extract_beacon_rid_payload(pkt) is None

    def test_extract_returns_none_for_wrong_ie_type_byte(self):
        """Correct OUI but wrong OUI-type byte (not 0x0D) is rejected."""
        scanner, _ = _make_scanner()
        ie_chain = _make_ie_chain([
            (WIFI_IE_VENDOR_SPECIFIC, _make_rid_ie_info(ie_type=0x01)),
        ])
        pkt = _beacon_pkt(ie_chain)
        assert scanner._extract_beacon_rid_payload(pkt) is None

    def test_extract_returns_none_when_no_vendor_ie(self):
        """Beacon with no vendor-specific IE at all returns None."""
        scanner, _ = _make_scanner()
        ie_chain = _make_ie_chain([
            (0, b"my-ssid"),
            (1, b"\x82\x84"),
        ])
        pkt = _beacon_pkt(ie_chain)
        assert scanner._extract_beacon_rid_payload(pkt) is None

    def test_extract_returns_none_for_empty_ie_chain(self):
        """getlayer(Dot11Elt) returning None (no IEs at all) is handled safely."""
        scanner, _ = _make_scanner()
        pkt = _beacon_pkt(None)
        assert scanner._extract_beacon_rid_payload(pkt) is None

    # ── _process_beacon end-to-end ─────────────────────────────────────────

    def test_beacon_rid_publishes_detection(self):
        """A Beacon with a valid RID IE calls publish_detection once."""
        scanner, _ = _make_scanner(band="2.4")
        rid_frame = MagicMock()
        rid_frame.drone_id = "BEACON-DRONE-01"
        rid_frame.lat = 42.0
        rid_frame.lon = -71.0
        rid_frame.timestamp = time.time()

        ie_chain = _make_ie_chain([
            (WIFI_IE_VENDOR_SPECIFIC, _make_rid_ie_info()),
        ])
        pkt = _beacon_pkt(ie_chain)

        with patch.object(scanner, "_extract_beacon_rid_payload", return_value=b"\x00" * 25), \
             patch.object(scanner._parser, "parse", return_value=[rid_frame]):
            scanner._process_beacon(pkt)

        scanner.publisher.publish_detection.assert_called_once()

    def test_beacon_rid_tagged_as_wifi_beacon(self):
        """publish_detection receives transport='wifi_beacon' for Beacon frames."""
        scanner, _ = _make_scanner(band="2.4")
        rid_frame = MagicMock()
        rid_frame.drone_id = "BEACON-DRONE-02"
        rid_frame.lat = 42.0
        rid_frame.lon = -71.0
        rid_frame.timestamp = time.time()

        pkt = _beacon_pkt(_make_ie_chain([
            (WIFI_IE_VENDOR_SPECIFIC, _make_rid_ie_info()),
        ]))

        with patch.object(scanner, "_extract_beacon_rid_payload", return_value=b"\x00" * 25), \
             patch.object(scanner._parser, "parse", return_value=[rid_frame]):
            scanner._process_beacon(pkt)

        kwargs = scanner.publisher.publish_detection.call_args.kwargs
        assert kwargs["transport"] == "wifi_beacon"

    def test_nan_detection_tagged_as_wifi_nan(self):
        """Existing NAN Action frame path still tags detections as 'wifi_nan'."""
        scanner, _ = _make_scanner(band="2.4")
        rid_frame = MagicMock()
        rid_frame.drone_id = "NAN-DRONE-01"
        rid_frame.lat = 42.0
        rid_frame.lon = -71.0
        rid_frame.timestamp = time.time()

        raw_bytes = bytes([0x04, 0x09, 0xFA, 0x0B, 0xBE, 0x0D]) + b"\x00" * 20

        from scapy.all import Dot11, RadioTap, Raw
        dot11 = MagicMock(addr2="aa:bb:cc:dd:ee:ff")

        def getitem(layer):
            if layer is Dot11:    return dot11
            if layer is RadioTap: return MagicMock(dBm_AntSignal=-70)
            if layer is Raw:      return MagicMock()
            return MagicMock()

        pkt = MagicMock()
        pkt.haslayer.return_value = True
        pkt.__getitem__ = MagicMock(side_effect=getitem)

        with patch.object(scanner._parser, "parse", return_value=[rid_frame]), \
             patch("scanner.wifi_nan.bytes", return_value=raw_bytes):
            scanner._process(pkt)

        kwargs = scanner.publisher.publish_detection.call_args.kwargs
        assert kwargs["transport"] == "wifi_nan"

    def test_beacon_without_rid_ie_does_not_publish(self):
        """A Beacon with no Remote ID IE must not call publish_detection."""
        scanner, _ = _make_scanner()
        ie_chain = _make_ie_chain([
            (0, b"my-network"),
            (1, b"\x82\x84\x8b\x96"),
        ])
        pkt = _beacon_pkt(ie_chain)

        # _extract_beacon_rid_payload will return None → no publication
        scanner._process_beacon(pkt)

        scanner.publisher.publish_detection.assert_not_called()

    def test_handle_packet_routes_beacon_subtype_to_process_beacon(self):
        """_handle_packet dispatches subtype-8 frames to _process_beacon."""
        scanner, _ = _make_scanner()
        pkt = MagicMock()
        pkt.haslayer.return_value = True
        pkt.__getitem__ = MagicMock(return_value=MagicMock(subtype=8))

        with patch.object(scanner, "_process_beacon") as mock_beacon, \
             patch.object(scanner, "_process") as mock_nan:
            scanner._handle_packet(pkt)

        mock_beacon.assert_called_once_with(pkt)
        mock_nan.assert_not_called()

    def test_handle_packet_routes_action_subtype_to_process_nan(self):
        """_handle_packet dispatches non-Beacon frames to _process (NAN path)."""
        scanner, _ = _make_scanner()
        pkt = MagicMock()
        pkt.haslayer.return_value = True
        pkt.__getitem__ = MagicMock(return_value=MagicMock(subtype=13))

        with patch.object(scanner, "_process_beacon") as mock_beacon, \
             patch.object(scanner, "_process") as mock_nan:
            scanner._handle_packet(pkt)

        mock_nan.assert_called_once_with(pkt)
        mock_beacon.assert_not_called()

    def test_beacon_band_passed_to_publish_detection(self):
        """Beacon detections include the correct band in publish_detection call."""
        scanner, _ = _make_scanner(band="5", iface="wlan2")
        rid_frame = MagicMock()
        rid_frame.drone_id = "BEACON-DRONE-05G"
        rid_frame.lat = 42.0
        rid_frame.lon = -71.0
        rid_frame.timestamp = time.time()

        pkt = _beacon_pkt(_make_ie_chain([
            (WIFI_IE_VENDOR_SPECIFIC, _make_rid_ie_info()),
        ]))

        with patch.object(scanner, "_extract_beacon_rid_payload", return_value=b"\x00" * 25), \
             patch.object(scanner._parser, "parse", return_value=[rid_frame]):
            scanner._process_beacon(pkt)

        kwargs = scanner.publisher.publish_detection.call_args.kwargs
        assert kwargs["band"] == "5"

    def test_is_rid_frame_accepts_beacon_subtype(self):
        """_is_rid_frame passes 802.11 management frames with subtype 8."""
        scanner, _ = _make_scanner()
        pkt = MagicMock()
        pkt.haslayer.return_value = True
        pkt.__getitem__ = MagicMock(return_value=MagicMock(type=0, subtype=8))
        assert scanner._is_rid_frame(pkt) is True

    def test_is_rid_frame_accepts_action_subtype(self):
        """_is_rid_frame still passes 802.11 Action frames (subtype 13)."""
        scanner, _ = _make_scanner()
        pkt = MagicMock()
        pkt.haslayer.return_value = True
        pkt.__getitem__ = MagicMock(return_value=MagicMock(type=0, subtype=13))
        assert scanner._is_rid_frame(pkt) is True

    def test_is_rid_frame_rejects_data_frame(self):
        """_is_rid_frame rejects non-management frames (type != 0)."""
        scanner, _ = _make_scanner()
        pkt = MagicMock()
        pkt.haslayer.return_value = True
        pkt.__getitem__ = MagicMock(return_value=MagicMock(type=2, subtype=0))
        assert scanner._is_rid_frame(pkt) is False
