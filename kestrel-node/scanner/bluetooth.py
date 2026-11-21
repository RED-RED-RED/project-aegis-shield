"""
scanner/bluetooth.py
====================
Scans for ASTM F3411 Remote ID broadcast over Bluetooth:
  - Bluetooth 4 legacy advertisements: one 25-byte message per advert
  - Bluetooth 5 Long Range (Coded PHY) extended advertisements: message pack

Both use 16-bit service data UUID 0xFFFA.  The service data body is
  [0] application code 0x0D
  [1] message counter
  [2:] one message (BT4) or a message pack (BT5)

Backends
--------
hci   (default)  Direct HCI user channel on the dongle (scanner/hci_le.py).
                 Every advert is delivered (no duplicate filtering) and
                 Coded PHY stays enabled.  The dongle is taken away from
                 BlueZ while the agent runs.
bleak (fallback) BlueZ via bleak.  Used only if the HCI channel can't be
                 opened.  BlueZ duplicate filtering drops most adverts and
                 Coded PHY is not available, so detection is degraded.

Messages from each transmitter are merged by parser/assembler.py so a drone's
BasicID and Location, which arrive in different BT4 adverts, end up in the
same detection.

Hardware: Nordic nRF52840 dongle (PCA10059) running Zephyr hci_usb.
"""

import asyncio
import logging
import subprocess
import threading
import time
from typing import Optional

from parser.assembler import RIDAssembler
from parser.opendroneid import OpenDroneIDParser, RIDFrame
from publisher.mqtt_client import MQTTPublisher
from publisher.gps import GPSDaemon
from scanner.hci_le import (
    HCIError, HCIUnavailable, HCIUserChannel, LEScanner, rid_service_data,
)

log = logging.getLogger("bluetooth")

# ASTM Remote ID UUID (16-bit short: 0xFFFA)
RID_SERVICE_UUID_16  = 0xFFFA
RID_SERVICE_UUID_STR = "0000fffa-0000-1000-8000-00805f9b34fb"

# Company ID for Remote ID in manufacturer-specific data: 0x02E5 (Bluetooth SIG ASTM)
RID_COMPANY_ID = 0x02E5
RID_BT_APP_CODE = 0x0D   # ASTM application code that prefixes the counter

STATS_INTERVAL = 60.0    # seconds between scanner health log lines


def strip_bt_rid_header(data: bytes) -> Optional[bytes]:
    """Drop the 0x0D app code and message counter that precede the ODID payload."""
    if len(data) >= 2 and data[0] == RID_BT_APP_CODE:
        data = data[2:]
    return data or None


class BluetoothScanner:
    """
    Scans for BT4 + BT5 LR Remote ID advertisements.
    Blocking; run from a thread via run_sync().
    """

    def __init__(
        self,
        hci_index: int,
        publisher: MQTTPublisher,
        gps: GPSDaemon,
        node_id: str,
        stop_event: threading.Event,
        enable_coded_phy: bool = True,
        backend: str = "auto",
    ):
        self.hci_index = hci_index
        self.publisher = publisher
        self.gps = gps
        self.node_id = node_id
        self.stop_event = stop_event
        self.enable_coded_phy = enable_coded_phy
        self.backend = backend
        self._parser = OpenDroneIDParser()
        self._assembler = RIDAssembler(self._parser)
        self._adverts = 0
        self._rid_adverts = 0
        self._published = 0
        self._last_stats = time.monotonic()

    # ── Entry point ─────────────────────────────────────────────────────────

    def run_sync(self):
        log.info(f"Bluetooth scanner starting on hci{self.hci_index} (backend={self.backend})")
        if self.backend in ("auto", "hci"):
            try:
                self._run_hci()
                log.info("Bluetooth scanner stopped.")
                return
            except HCIUnavailable as e:
                if self.backend == "hci":
                    log.error(f"HCI backend unavailable: {e}. Bluetooth scanning disabled.")
                    return
                log.warning(
                    f"HCI backend unavailable ({e}). Falling back to BlueZ/bleak: "
                    "most adverts will be missed and Coded PHY is off."
                )
        # The HCI attempt powers the dongle off; give it back to BlueZ powered on.
        try:
            subprocess.run(["btmgmt", "--index", str(self.hci_index), "power", "on"],
                           capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            asyncio.run(self._run_bleak())
        except ImportError:
            log.error("bleak is not installed. Bluetooth scanning disabled.")
        log.info("Bluetooth scanner stopped.")

    # ── HCI backend ─────────────────────────────────────────────────────────

    def _run_hci(self):
        """Scan on the HCI user channel. Raises HCIUnavailable if it can't open."""
        first = True
        while not self.stop_event.is_set():
            chan = HCIUserChannel(self.hci_index)
            try:
                chan.open()                       # HCIUnavailable propagates on the first try
            except HCIUnavailable:
                if first:
                    raise
                log.warning(f"hci{self.hci_index} not available, retrying in 5 s")
                self.stop_event.wait(5)
                continue

            scanner = LEScanner(chan, coded_phy=self.enable_coded_phy)
            try:
                scanner.start()
                first = False
                log.info(f"hci{self.hci_index}: {scanner.mode}, duplicate filtering off")
                log.info(f"hci{self.hci_index}: {scanner.feature_summary}")
                if scanner.ext_error:
                    log.warning(f"hci{self.hci_index}: extended scan commands rejected ({scanner.ext_error})")
                if self.enable_coded_phy and not scanner.coded:
                    log.warning(
                        "BT5 Long Range (Coded PHY) is off: the dongle firmware does not "
                        "support extended scanning. BT4 Remote ID still works."
                    )
                while not self.stop_event.is_set():
                    for r in scanner.read_reports(timeout=0.5):
                        self._adverts += 1
                        body = rid_service_data(r.data)
                        if body is not None:
                            self._handle_rid(r.address, r.rssi, body, phy=r.phy_label)
                    self._log_stats()
            except (HCIError, OSError) as e:
                if first:
                    raise HCIUnavailable(f"controller did not initialise: {e}") from e
                log.error(f"HCI scan error: {e}. Reopening in 5 s")
                self.stop_event.wait(5)
            finally:
                scanner.stop()
                chan.close()

    # ── bleak fallback ──────────────────────────────────────────────────────

    async def _run_bleak(self):
        from bleak import BleakScanner

        scanner = BleakScanner(
            detection_callback=self._on_advertisement,
            adapter=f"hci{self.hci_index}",
            scanning_mode="active",
        )
        while not self.stop_event.is_set():
            try:
                async with scanner:
                    await asyncio.sleep(10)
            except Exception as e:
                log.error(f"BLE scan error: {e} — retrying in 5 s")
                await asyncio.sleep(5)
            self._log_stats()

    def _on_advertisement(self, device, adv):
        """bleak detection callback."""
        self._adverts += 1
        body = self._extract_rid_payload(adv)
        if body is not None:
            self._handle_rid(device.address, adv.rssi or 0, body, phy="1M")

    def _extract_rid_payload(self, adv) -> Optional[bytes]:
        """Raw service data body (0x0D + counter + payload) from a bleak advert."""
        if adv.service_data:
            for uuid, data in adv.service_data.items():
                if RID_SERVICE_UUID_STR in uuid.lower() or "fffa" in uuid.lower():
                    return bytes(data)
        if adv.manufacturer_data:
            raw = adv.manufacturer_data.get(RID_COMPANY_ID)
            if raw:
                return bytes(raw)
        return None

    # ── Shared handling ─────────────────────────────────────────────────────

    def _handle_rid(self, address: str, rssi: int, body: bytes, phy: str = "1M"):
        """Merge one Remote ID service data body and publish when a snapshot is due."""
        self._rid_adverts += 1
        payload = strip_bt_rid_header(body)
        if payload is None:
            return
        try:
            frame: Optional[RIDFrame] = self._assembler.feed(address, payload, "bluetooth")
        except Exception as e:
            log.debug(f"BT parse error from {address}: {e}")
            return
        if frame is None:
            return

        self._published += 1
        log.info(
            f"RID via BT ({phy})  id={frame.drone_id}  rssi={rssi} dBm  "
            f"{frame.lat:.5f},{frame.lon:.5f}  agl={frame.height_agl:.1f}m"
        )
        self.publisher.publish_detection(
            node_id=self.node_id,
            transport="bluetooth",
            frame=frame,
            rssi=rssi,
            src_addr=address,
            node_lat=self.gps.lat,
            node_lon=self.gps.lon,
            node_alt=self.gps.alt,
        )

    def _log_stats(self):
        now = time.monotonic()
        if now - self._last_stats < STATS_INTERVAL:
            return
        log.info(
            f"BT stats (last {int(now - self._last_stats)} s): {self._adverts} adverts, "
            f"{self._rid_adverts} Remote ID, {self._published} detections published, "
            f"{len(self._assembler)} transmitters tracked"
        )
        self._adverts = self._rid_adverts = self._published = 0
        self._last_stats = now
