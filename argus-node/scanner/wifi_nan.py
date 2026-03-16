"""
scanner/wifi_nan.py
===================
Captures Wi-Fi NAN (Neighbor Awareness Networking) Remote ID frames
using Scapy in monitor mode.

FAA Remote ID Wi-Fi NAN frames are IEEE 802.11 Action frames with:
  - Category: 0x04 (Public)
  - OUI:       FA:0B:BE  (ASTM / Remote ID)
  - Subtype:   0x0D

Hardware: Alfa AWUS036ACM (mt76x2u driver) — other cards may not
          pass NAN action frames through in monitor mode.

Requires:
  pip install scapy
  iface already in monitor mode:
    sudo ip link set wlan1 down
    sudo iw wlan1 set monitor none
    sudo ip link set wlan1 up
"""

import logging
import struct
import threading
import time
from dataclasses import dataclass
from typing import Optional

from scapy.all import sniff, RadioTap, Dot11, Raw

from parser.opendroneid import OpenDroneIDParser, RIDFrame
from publisher.mqtt_client import MQTTPublisher
from publisher.gps import GPSDaemon

log = logging.getLogger("wifi-nan")

# Remote ID / ASTM OUI in Wi-Fi NAN action frames
RID_OUI = bytes([0xFA, 0x0B, 0xBE])
RID_ACTION_CATEGORY = 0x04   # Public Action
RID_ACTION_CODE     = 0x09   # NAN (Neighbor Awareness Networking)
NAN_SVC_RID_SUBTYPE = 0x0D


@dataclass
class WiFiDetection:
    drone_id: str
    raw_bytes: bytes
    rssi: int
    src_mac: str
    channel: int
    timestamp: float


class WiFiNANScanner:
    """
    Sniffs 802.11 monitor-mode frames looking for Remote ID NAN action frames.
    Parsed frames are published via MQTTPublisher.
    """

    def __init__(
        self,
        iface: str,
        publisher: MQTTPublisher,
        gps: GPSDaemon,
        node_id: str,
        stop_event: threading.Event,
        channels: list[int] | None = None,
    ):
        self.iface = iface
        self.publisher = publisher
        self.gps = gps
        self.node_id = node_id
        self.stop_event = stop_event
        self.channels = channels or [6, 1, 11]  # Hop across common 2.4 GHz channels
        self._parser = OpenDroneIDParser()
        self._seen: dict[str, float] = {}   # dedup: drone_id → last_seen timestamp
        self._dedup_window = 2.0            # seconds

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def run(self):
        """Blocking run — called from a dedicated thread."""
        log.info(f"Wi-Fi NAN scanner starting on {self.iface}")
        hop_thread = threading.Thread(target=self._channel_hopper, daemon=True)
        hop_thread.start()

        while not self.stop_event.is_set():
            try:
                sniff(
                    iface=self.iface,
                    prn=self._handle_packet,
                    store=False,
                    timeout=5,          # Return every 5 s so we can check stop_event
                    lfilter=self._is_action_frame,
                )
            except OSError as e:
                log.error(f"Sniff error on {self.iface}: {e} — retrying in 5 s")
                time.sleep(5)

        log.info("Wi-Fi NAN scanner stopped.")

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _is_action_frame(self, pkt) -> bool:
        """Pre-filter: only pass 802.11 Action frames."""
        return pkt.haslayer(Dot11) and pkt[Dot11].type == 0 and pkt[Dot11].subtype == 13

    def _handle_packet(self, pkt):
        try:
            self._process(pkt)
        except Exception as e:
            log.debug(f"Packet parse error: {e}")

    def _process(self, pkt):
        if not pkt.haslayer(Raw):
            return

        payload = bytes(pkt[Raw])

        # Check Action frame header: Category=0x04, Code=0x09
        if len(payload) < 8:
            return
        category, code = payload[0], payload[1]
        if category != RID_ACTION_CATEGORY or code != RID_ACTION_CODE:
            return

        # Check OUI
        oui = payload[2:5]
        if oui != RID_OUI:
            return

        # The Remote ID payload starts after the 6-byte NAN header
        rid_payload = payload[6:]
        if not rid_payload:
            return

        # Extract RSSI from RadioTap header
        rssi = self._extract_rssi(pkt)

        # Get source MAC
        src_mac = pkt[Dot11].addr2 or "00:00:00:00:00:00"

        # Parse Remote ID frames
        frames: list[RIDFrame] = self._parser.parse(rid_payload, transport="wifi_nan")
        if not frames:
            return

        for frame in frames:
            # Dedup: skip if we saw this drone very recently
            now = time.time()
            last = self._seen.get(frame.drone_id, 0)
            if now - last < self._dedup_window:
                continue
            self._seen[frame.drone_id] = now

            log.info(f"RID via WiFi  id={frame.drone_id}  rssi={rssi} dBm  {frame.lat:.5f},{frame.lon:.5f}")

            self.publisher.publish_detection(
                node_id=self.node_id,
                transport="wifi_nan",
                frame=frame,
                rssi=rssi,
                src_addr=src_mac,
                node_lat=self.gps.lat,
                node_lon=self.gps.lon,
                node_alt=self.gps.alt,
            )

    def _extract_rssi(self, pkt) -> int:
        """Pull RSSI from RadioTap header. Returns 0 if unavailable."""
        try:
            if pkt.haslayer(RadioTap):
                rt = pkt[RadioTap]
                # RadioTap dBm_AntSignal field
                if hasattr(rt, "dBm_AntSignal"):
                    return int(rt.dBm_AntSignal)
        except Exception:
            pass
        return 0

    def _channel_hopper(self):
        """
        Cycle through Wi-Fi channels every 250 ms.
        Remote ID typically uses ch6 but hopping ensures we catch everything.
        """
        import subprocess
        idx = 0
        while not self.stop_event.is_set():
            ch = self.channels[idx % len(self.channels)]
            try:
                subprocess.run(
                    ["iw", "dev", self.iface, "set", "channel", str(ch)],
                    capture_output=True,
                    timeout=2,
                )
            except Exception:
                pass
            idx += 1
            time.sleep(0.25)
