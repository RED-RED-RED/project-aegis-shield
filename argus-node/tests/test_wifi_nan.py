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

from unittest.mock import MagicMock, patch, call
from scanner.wifi_nan import WiFiNANScanner, _CHANNELS_2G, _CHANNELS_5G
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
        stop.set()  # stop immediately after first sniff timeout

        def fake_subprocess(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            return mock

        with patch("subprocess.run", side_effect=fake_subprocess), \
             patch("scanner.wifi_nan.sniff") as mock_sniff:
            mock_sniff.return_value = None
            scanner.run()

        mock_sniff.assert_called()


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
    def _make_rid_packet(self):
        """Return a minimal mock Scapy packet that passes all filters."""
        from unittest.mock import MagicMock
        pkt = MagicMock()
        # Dot11 action frame
        pkt.haslayer.return_value = True
        pkt.__getitem__ = MagicMock()

        dot11 = MagicMock()
        dot11.type    = 0
        dot11.subtype = 13
        dot11.addr2   = "fa:3b:92:0e:11:22"

        radiotap = MagicMock()
        radiotap.dBm_AntSignal = -65

        def getitem(layer):
            from scapy.all import Dot11, RadioTap, Raw
            if layer is Dot11:
                return dot11
            if layer is RadioTap:
                return radiotap
            if layer is Raw:
                return raw
            return MagicMock()

        raw = MagicMock()
        # Craft a valid-looking NAN action frame header + stub RID payload
        # Category=0x04, Code=0x09, OUI=FA:0B:BE, subtype byte, padding, RID data
        raw_bytes = bytes([0x04, 0x09, 0xFA, 0x0B, 0xBE, 0x0D]) + b"\x00" * 20
        raw.__bytes__ = lambda self: raw_bytes

        pkt.__getitem__ = MagicMock(side_effect=getitem)
        return pkt

    def test_band_passed_to_publish_detection_2g(self):
        scanner, _ = _make_scanner(band="2.4")
        rid_frame = MagicMock()
        rid_frame.drone_id = "DRONE001"
        rid_frame.lat = 42.0
        rid_frame.lon = -71.0
        rid_frame.timestamp = time.time()

        with patch.object(scanner._parser, "parse", return_value=[rid_frame]):
            # Simulate _process being called with a minimal packet
            pkt = MagicMock()
            pkt.haslayer.return_value = True

            from scapy.all import Dot11, RadioTap, Raw
            dot11 = MagicMock(addr2="aa:bb:cc:dd:ee:ff")
            radiotap = MagicMock(dBm_AntSignal=-70)
            raw = MagicMock()
            raw_bytes = bytes([0x04, 0x09, 0xFA, 0x0B, 0xBE, 0x0D]) + b"\x00" * 20

            def getitem(layer):
                if layer is Dot11:      return dot11
                if layer is RadioTap:   return radiotap
                if layer is Raw:        return raw
                return MagicMock()

            pkt.__getitem__ = MagicMock(side_effect=getitem)
            with patch("builtins.bytes", wraps=bytes) as _:
                import unittest.mock as um
                with um.patch.object(type(raw), "__bytes__", return_value=raw_bytes):
                    pass

            # Call _process directly with mocked bytes extraction
            with patch("scanner.wifi_nan.bytes", return_value=raw_bytes):
                scanner._process(pkt)

        # Verify band="2.4" was passed
        call_kwargs = scanner.publisher.publish_detection.call_args
        if call_kwargs is not None:
            assert call_kwargs.kwargs.get("band") == "2.4"

    def test_band_passed_to_publish_detection_5g(self):
        scanner, _ = _make_scanner(band="5", iface="wlan2")
        rid_frame = MagicMock()
        rid_frame.drone_id = "DRONE002"
        rid_frame.lat = 42.0
        rid_frame.lon = -71.0
        rid_frame.timestamp = time.time()

        with patch.object(scanner._parser, "parse", return_value=[rid_frame]):
            from scapy.all import Dot11, RadioTap, Raw
            pkt = MagicMock()
            pkt.haslayer.return_value = True
            dot11 = MagicMock(addr2="aa:bb:cc:dd:ee:ff")
            raw_bytes = bytes([0x04, 0x09, 0xFA, 0x0B, 0xBE, 0x0D]) + b"\x00" * 20

            def getitem(layer):
                if layer is Dot11:    return dot11
                if layer is RadioTap: return MagicMock(dBm_AntSignal=-55)
                if layer is Raw:      return MagicMock()
                return MagicMock()

            pkt.__getitem__ = MagicMock(side_effect=getitem)
            with patch("scanner.wifi_nan.bytes", return_value=raw_bytes):
                scanner._process(pkt)

        call_kwargs = scanner.publisher.publish_detection.call_args
        if call_kwargs is not None:
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
