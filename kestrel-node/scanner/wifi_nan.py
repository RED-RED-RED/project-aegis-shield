"""
scanner/wifi_nan.py
===================
Captures Wi-Fi Remote ID frames in monitor mode using Scapy.

Supports two ASTM F3411-22a Wi-Fi transport types simultaneously:

  Wi-Fi NAN Service Discovery frames (802.11 Action, subtype 13):
    - Public Action: Category 0x04, Action 0x09 (vendor specific)
    - OUI 50:6F:9A (Wi-Fi Alliance), OUI type 0x13 (NAN)
    - Service Descriptor Attribute (0x03) with Service ID 88:69:19:9D:92:09
      (first 6 bytes of SHA-256("org.opendroneid.remoteid"))
    - Service Info = 1-byte message counter + ODID message pack
    - Detections tagged transport="wifi_nan"

  Wi-Fi Beacon frames (802.11 Management, subtype 8):
    - Vendor-specific IE (221) with OUI FA:0B:BC, OUI type 0x0D
    - IE body = OUI(3) + type(1) + message counter(1) + ODID message pack
    - Detections tagged transport="wifi_beacon"

  Layouts match opendroneid-core-c libopendroneid/wifi.c (the reference
  implementation used by compliant transmitters).

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
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional

from scapy.all import sniff, RadioTap, Dot11, Dot11Elt, Raw

from parser.opendroneid import OpenDroneIDParser, RIDFrame
from publisher.mqtt_client import MQTTPublisher
from publisher.gps import GPSDaemon

log = logging.getLogger("wifi-nan")

# ── Wi-Fi NAN Service Discovery frame constants ─────────────────────────────
NAN_ACTION_CATEGORY  = 0x04                              # Public Action
NAN_ACTION_CODE      = 0x09                              # Vendor specific
NAN_WFA_OUI          = bytes([0x50, 0x6F, 0x9A])         # Wi-Fi Alliance
NAN_OUI_TYPE         = 0x13                              # NAN
NAN_ATTR_SDA         = 0x03                              # Service Descriptor Attribute
NAN_RID_SERVICE_ID   = bytes([0x88, 0x69, 0x19, 0x9D, 0x92, 0x09])

# Backward-compatible aliases (older code/tests referenced these names)
RID_ACTION_CATEGORY  = NAN_ACTION_CATEGORY
RID_ACTION_CODE      = NAN_ACTION_CODE

# ── Wi-Fi Beacon vendor-specific IE constants ───────────────────────────────
RID_BEACON_OUI_ASTM = bytes([0xFA, 0x0B, 0xBC])   # ASTM F3411 Beacon OUI
RID_BEACON_OUI_ODID = bytes([0x6A, 0x5C, 0x35])   # OpenDroneID / ESP32 alternative OUI
RID_BEACON_IE_TYPE  = 0x0D                         # OUI type byte for Remote ID payload
RID_COUNTER_LEN     = 1                            # message counter precedes the pack
WIFI_IE_VENDOR_SPECIFIC = 221                      # 802.11 vendor-specific IE element ID


@dataclass
class WiFiDetection:
    drone_id: str
    raw_bytes: bytes
    rssi: int
    src_mac: str
    channel: int
    timestamp: float


_CHANNELS_2G = [1, 6, 11]
_CHANNELS_5G = [36, 40, 44, 48, 149, 153, 157, 161]  # UNII-1 + UNII-3


class WiFiNANScanner:
    """
    Sniffs 802.11 monitor-mode frames looking for Remote ID NAN action frames.
    Parsed frames are published via MQTTPublisher.

    Instantiate once per adapter. Pass band="2.4" or band="5" to configure
    channel list and log labels. Both instances share the same publisher so
    detections from either band feed the same MQTT pipeline.
    """

    def __init__(
        self,
        iface: str,
        publisher: MQTTPublisher,
        gps: GPSDaemon,
        node_id: str,
        stop_event: threading.Event,
        band: str = "2.4",
        channels: list[int] | None = None,
        dwell_ms: int = 200,
    ):
        self.iface = iface
        self.publisher = publisher
        self.gps = gps
        self.node_id = node_id
        self.stop_event = stop_event
        self.band = band
        self.dwell_ms = dwell_ms

        if channels is not None:
            self.channels = channels
        elif band == "5":
            self.channels = _CHANNELS_5G
        else:
            self.channels = _CHANNELS_2G

        self._label = f"WiFi NAN {self.band}GHz"
        self._parser = OpenDroneIDParser()
        self._seen: dict[str, float] = {}   # dedup: drone_id → last_seen timestamp
        self._dedup_window = 2.0            # seconds

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def run(self):
        """Blocking run — called from a dedicated thread."""
        # Verify the interface exists before committing to scan
        result = subprocess.run(
            ["ip", "link", "show", self.iface],
            capture_output=True,
        )
        if result.returncode != 0:
            log.warning(
                f"[{self._label}] Interface {self.iface} not found — "
                "skipping scanner. Single-adapter deployments are unaffected."
            )
            return

        log.info(f"[{self._label}] Scanner starting on {self.iface} "
                 f"(channels={self.channels}, dwell={self.dwell_ms}ms)")
        hop_thread = threading.Thread(target=self._channel_hopper, daemon=True)
        hop_thread.start()

        while not self.stop_event.is_set():
            try:
                sniff(
                    iface=self.iface,
                    prn=self._handle_packet,
                    store=False,
                    timeout=5,          # Return every 5 s so we can check stop_event
                    lfilter=self._is_rid_frame,
                )
            except OSError as e:
                log.error(f"[{self._label}] Sniff error on {self.iface}: {e} — retrying in 5 s")
                time.sleep(5)

        log.info(f"[{self._label}] Scanner stopped.")

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _is_rid_frame(self, pkt) -> bool:
        """Pre-filter: pass 802.11 Action frames (subtype 13) and Beacon frames (subtype 8)."""
        return (
            pkt.haslayer(Dot11)
            and pkt[Dot11].type == 0
            and pkt[Dot11].subtype in (8, 13)
        )

    def _handle_packet(self, pkt):
        try:
            if pkt.haslayer(Dot11) and pkt[Dot11].subtype == 8:
                self._process_beacon(pkt)
            else:
                self._process(pkt)
        except Exception as e:
            log.debug(f"Packet parse error: {e}")

    def _process(self, pkt):
        rid_payload = self.extract_nan_rid_payload(pkt)
        if rid_payload is None:
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

            log.info(
                f"[{self._label}] RID id={frame.drone_id}  "
                f"rssi={rssi} dBm  {frame.lat:.5f},{frame.lon:.5f}"
            )

            self.publisher.publish_detection(
                node_id=self.node_id,
                transport="wifi_nan",
                frame=frame,
                rssi=rssi,
                src_addr=src_mac,
                node_lat=self.gps.lat,
                node_lon=self.gps.lon,
                node_alt=self.gps.alt,
                band=self.band,
            )

    @staticmethod
    def extract_nan_rid_payload(pkt) -> Optional[bytes]:
        """
        Return the ODID message pack from a NAN Service Discovery frame, or None.

        Action body layout (opendroneid-core-c wifi.c):
          [0]    category 0x04
          [1]    action   0x09
          [2:5]  OUI      50:6F:9A
          [5]    OUI type 0x13
          [6:]   NAN attributes: id(1) len(2, LE) value(len)

        Service Descriptor Attribute (id 0x03) value:
          service_id(6) instance_id(1) requestor_id(1) service_control(1)
          service_info_len(1) service_info(...)
        service_info = message_counter(1) + message pack
        """
        if not pkt.haslayer(Dot11):
            return None
        body = bytes(pkt[Dot11].payload)
        if len(body) < 6:
            return None
        if body[0] != NAN_ACTION_CATEGORY or body[1] != NAN_ACTION_CODE:
            return None
        if body[2:5] != NAN_WFA_OUI or body[5] != NAN_OUI_TYPE:
            return None

        i = 6
        while i + 3 <= len(body):
            attr_id  = body[i]
            attr_len = body[i + 1] | (body[i + 2] << 8)
            value    = body[i + 3:i + 3 + attr_len]
            i += 3 + attr_len
            if attr_id != NAN_ATTR_SDA or len(value) < 10:
                continue
            if value[0:6] != NAN_RID_SERVICE_ID:
                continue
            si_len = value[9]
            si     = value[10:10 + si_len]
            if len(si) <= RID_COUNTER_LEN:
                return None
            return si[RID_COUNTER_LEN:]
        return None

    def _process_beacon(self, pkt):
        rid_payload = self._extract_beacon_rid_payload(pkt)
        if rid_payload is None:
            return

        rssi   = self._extract_rssi(pkt)
        src_mac = pkt[Dot11].addr2 or "00:00:00:00:00:00"

        frames: list[RIDFrame] = self._parser.parse(rid_payload, transport="wifi_beacon")
        if not frames:
            return

        for frame in frames:
            now  = time.time()
            last = self._seen.get(frame.drone_id, 0)
            if now - last < self._dedup_window:
                continue
            self._seen[frame.drone_id] = now

            log.info(
                f"[{self._label}] RID(beacon) id={frame.drone_id}  "
                f"rssi={rssi} dBm  {frame.lat:.5f},{frame.lon:.5f}"
            )

            self.publisher.publish_detection(
                node_id=self.node_id,
                transport="wifi_beacon",
                frame=frame,
                rssi=rssi,
                src_addr=src_mac,
                node_lat=self.gps.lat,
                node_lon=self.gps.lon,
                node_alt=self.gps.alt,
                band=self.band,
            )

    def _extract_beacon_rid_payload(self, pkt) -> Optional[bytes]:
        """
        Walk the 802.11 information element chain looking for a vendor-specific
        IE (element ID 221) whose OUI matches the ASTM or OpenDroneID Remote ID
        OUIs.  Returns the ODID message pack (after OUI, type and counter)
        or None if no matching IE is found.

        The IE body layout is:
          [0:3]  OUI (3 bytes)
          [3]    OUI type byte, must be RID_BEACON_IE_TYPE (0x0D)
          [4]    message counter (increments every transmission)
          [5:]   ODID message pack passed to the parser
        """
        elt = pkt.getlayer(Dot11Elt)
        while elt is not None:
            if elt.ID == WIFI_IE_VENDOR_SPECIFIC:
                data = bytes(elt.info)
                if len(data) > 4 + RID_COUNTER_LEN:
                    oui     = data[:3]
                    ie_type = data[3]
                    if (
                        oui in (RID_BEACON_OUI_ASTM, RID_BEACON_OUI_ODID)
                        and ie_type == RID_BEACON_IE_TYPE
                    ):
                        payload = data[4 + RID_COUNTER_LEN:]
                        if payload:
                            return payload
            # Advance to the next IE; stop when payload has no 'ID' attribute
            # (end of the IE chain — scapy uses Padding / NoPayload sentinel).
            elt = elt.payload if hasattr(elt.payload, "ID") else None
        return None

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
        Cycle through Wi-Fi channels every dwell_ms milliseconds.
        Remote ID typically uses ch6 (2.4 GHz) but hopping ensures we catch everything.
        """
        idx = 0
        dwell_s = self.dwell_ms / 1000.0
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
            time.sleep(dwell_s)
