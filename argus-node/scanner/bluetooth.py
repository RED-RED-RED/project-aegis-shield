"""
scanner/bluetooth.py
====================
Scans for Remote ID broadcasts over:
  - Bluetooth 4 Legacy advertisements (ADV_IND)
  - Bluetooth 5 Long Range / Coded PHY (ADV_EXT_IND, S=8 or S=2)

BT4 works with most USB dongles via bleak.
BT5 Coded PHY **requires** an nRF52840-based dongle and direct HCI commands
because the Linux BlueZ stack doesn't expose Coded PHY scanning via normal APIs.

nRF52840 USB dongles:
  - Nordic nRF52840 USB Dongle (PCA10059) — reference hardware, ~$10
  - Makerdiary nRF52840 MDK USB Dongle

Requires:
  pip install bleak
  sudo hciconfig hci0 up

For Coded PHY — patch BlueZ or use direct HCI socket (see _enable_coded_phy).
"""

import asyncio
import logging
import struct
import threading
import time
from dataclasses import dataclass
from typing import Optional

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from parser.opendroneid import OpenDroneIDParser, RIDFrame
from publisher.mqtt_client import MQTTPublisher
from publisher.gps import GPSDaemon

log = logging.getLogger("bluetooth")

# ASTM Remote ID UUID (16-bit short: 0xFFFA)
RID_SERVICE_UUID_16  = 0xFFFA
RID_SERVICE_UUID_STR = "0000fffa-0000-1000-8000-00805f9b34fb"

# Company ID for Remote ID in manufacturer-specific data: 0x02E5 (Bluetooth SIG ASTM)
RID_COMPANY_ID = 0x02E5


class BluetoothScanner:
    """
    Scans for BT4 + BT5 LR Remote ID advertisements.
    Uses asyncio/bleak internally; run from a thread via run_sync().
    """

    def __init__(
        self,
        hci_index: int,
        publisher: MQTTPublisher,
        gps: GPSDaemon,
        node_id: str,
        stop_event: threading.Event,
        enable_coded_phy: bool = True,
    ):
        self.hci_index = hci_index
        self.publisher = publisher
        self.gps = gps
        self.node_id = node_id
        self.stop_event = stop_event
        self.enable_coded_phy = enable_coded_phy
        self._parser = OpenDroneIDParser()
        self._seen: dict[str, float] = {}
        self._dedup_window = 2.0

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def run_sync(self):
        """Called from a regular thread — creates an asyncio event loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run_async())
        finally:
            loop.close()

    # ------------------------------------------------------------------ #
    # Async internals
    # ------------------------------------------------------------------ #

    async def _run_async(self):
        log.info(f"Bluetooth scanner starting on hci{self.hci_index}")

        if self.enable_coded_phy:
            await self._enable_coded_phy()

        scanner = BleakScanner(
            detection_callback=self._on_advertisement,
            adapter=f"hci{self.hci_index}",
            scanning_mode="active",
        )

        while not self.stop_event.is_set():
            try:
                async with scanner:
                    # Scan in 10-second windows, then restart to keep the adapter fresh
                    await asyncio.sleep(10)
            except Exception as e:
                log.error(f"BLE scan error: {e} — retrying in 5 s")
                await asyncio.sleep(5)

        log.info("Bluetooth scanner stopped.")

    def _on_advertisement(self, device: BLEDevice, adv: AdvertisementData):
        """Callback for every received BLE advertisement."""
        payload = self._extract_rid_payload(adv)
        if payload is None:
            return

        try:
            frames: list[RIDFrame] = self._parser.parse(payload, transport="bluetooth")
        except Exception as e:
            log.debug(f"BT parse error: {e}")
            return

        rssi = adv.rssi or 0

        for frame in frames:
            now = time.time()
            last = self._seen.get(frame.drone_id, 0)
            if now - last < self._dedup_window:
                continue
            self._seen[frame.drone_id] = now

            log.info(f"RID via BT    id={frame.drone_id}  rssi={rssi} dBm  {frame.lat:.5f},{frame.lon:.5f}")

            self.publisher.publish_detection(
                node_id=self.node_id,
                transport="bluetooth",
                frame=frame,
                rssi=rssi,
                src_addr=device.address,
                node_lat=self.gps.lat,
                node_lon=self.gps.lon,
                node_alt=self.gps.alt,
            )

    def _extract_rid_payload(self, adv: AdvertisementData) -> Optional[bytes]:
        """
        Pull the Remote ID byte payload from an advertisement.
        Checks service data (UUID 0xFFFA) and manufacturer-specific data.
        """
        # Method 1: Service data keyed by 16-bit UUID
        if adv.service_data:
            for uuid, data in adv.service_data.items():
                if RID_SERVICE_UUID_STR in uuid.lower() or "fffa" in uuid.lower():
                    return bytes(data)

        # Method 2: Manufacturer-specific data with ASTM company ID
        if adv.manufacturer_data:
            raw = adv.manufacturer_data.get(RID_COMPANY_ID)
            if raw:
                return bytes(raw)

        return None

    async def _enable_coded_phy(self):
        """
        Send raw HCI commands to enable LE Coded PHY scanning on nRF52840.
        BlueZ / bleak don't expose this via high-level APIs.

        HCI_LE_Set_Extended_Scan_Parameters (OGF=0x08, OCF=0x0041):
          - Own_Address_Type = 0x00 (public)
          - Scanning_Filter_Policy = 0x00 (all)
          - Scanning_PHYs = 0x05 (1M + Coded)
          - Per PHY: Scan_Type=0x00 (passive), Interval=0x0140, Window=0x0140
        """
        import socket

        log.info("Attempting to enable BT5 Coded PHY scanning via HCI…")

        try:
            # Open raw HCI socket
            hci_sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
            hci_sock.bind((self.hci_index,))

            # LE Set Extended Scan Parameters
            # OGF=0x08 (LE Controller), OCF=0x0041 → opcode = 0x2041
            opcode = 0x2041
            params = bytes([
                0x00,           # Own_Address_Type: public
                0x00,           # Scanning_Filter_Policy: accept all
                0x05,           # Scanning_PHYs: bit0=1M, bit2=Coded → 0b00000101
                # PHY 1 (1M):
                0x00,           # Scan_Type: passive
                0x40, 0x01,     # Scan_Interval: 0x0140 = 200ms
                0x40, 0x01,     # Scan_Window:   0x0140 = 200ms
                # PHY 2 (Coded):
                0x00,           # Scan_Type: passive
                0x40, 0x01,     # Scan_Interval
                0x40, 0x01,     # Scan_Window
            ])
            hci_cmd = struct.pack("<HB", opcode, len(params)) + params
            hci_sock.send(b"\x01" + hci_cmd)   # 0x01 = HCI command packet indicator

            await asyncio.sleep(0.1)

            # LE Set Extended Scan Enable
            opcode_enable = 0x2042
            enable_params = bytes([
                0x01,   # Enable
                0x00,   # Filter_Duplicates: off
                0x00, 0x00,  # Duration: 0 = scan continuously
                0x00, 0x00,  # Period: 0
            ])
            hci_cmd2 = struct.pack("<HB", opcode_enable, len(enable_params)) + enable_params
            hci_sock.send(b"\x01" + hci_cmd2)

            hci_sock.close()
            log.info("BT5 Coded PHY scanning enabled.")

        except PermissionError:
            log.warning("No permission for raw HCI socket — run with sudo or CAP_NET_RAW. Coded PHY disabled.")
        except Exception as e:
            log.warning(f"Could not enable Coded PHY: {e}. Falling back to BT4 only.")
