"""
publisher/gps.py
================
Reads NMEA sentences from the u-blox GPS module over serial.
Maintains a thread-safe current position for use by all scanners.

Hardware: u-blox NEO-M8N on /dev/ttyAMA0 at 9600 baud
Requires:
  pip install pyserial pynmea2
  Enable UART in /boot/config.txt: enable_uart=1
  Disable serial console: sudo raspi-config → Interface → Serial → disable console
"""

import logging
import threading
import time
from typing import Optional

import pynmea2
import serial

log = logging.getLogger("gps")


class GPSDaemon:
    """
    Reads NMEA from serial GPS. Thread-safe position properties.
    Call run(stop_event) in a background thread.
    """

    def __init__(self, port: str = "/dev/ttyAMA0", baud: int = 9600):
        self.port = port
        self.baud = baud

        self._lock = threading.Lock()
        self._lat: float = 0.0
        self._lon: float = 0.0
        self._alt: float = 0.0
        self._has_fix: bool = False
        self._satellites: int = 0
        self._fix_event = threading.Event()

    # ------------------------------------------------------------------ #
    # Thread-safe properties
    # ------------------------------------------------------------------ #

    @property
    def lat(self) -> float:
        with self._lock:
            return self._lat

    @property
    def lon(self) -> float:
        with self._lock:
            return self._lon

    @property
    def alt(self) -> float:
        with self._lock:
            return self._alt

    @property
    def has_fix(self) -> bool:
        with self._lock:
            return self._has_fix

    @property
    def satellites(self) -> int:
        with self._lock:
            return self._satellites

    def wait_for_fix(self, timeout: float = 60) -> bool:
        return self._fix_event.wait(timeout=timeout)

    # ------------------------------------------------------------------ #
    # Run loop
    # ------------------------------------------------------------------ #

    def run(self, stop_event: threading.Event):
        log.info(f"GPS daemon starting on {self.port} @ {self.baud} baud")
        while not stop_event.is_set():
            try:
                with serial.Serial(self.port, self.baud, timeout=1) as ser:
                    log.info(f"GPS serial port opened: {self.port}")
                    while not stop_event.is_set():
                        try:
                            line = ser.readline().decode("ascii", errors="replace").strip()
                            self._parse_nmea(line)
                        except serial.SerialException as e:
                            log.warning(f"GPS read error: {e}")
                            break
            except serial.SerialException as e:
                log.error(f"GPS port error: {e} — retrying in 10 s")
                time.sleep(10)

        log.info("GPS daemon stopped.")

    # ------------------------------------------------------------------ #
    # NMEA parsing
    # ------------------------------------------------------------------ #

    def _parse_nmea(self, line: str):
        if not line.startswith("$"):
            return
        try:
            msg = pynmea2.parse(line)
        except pynmea2.ParseError:
            return

        # GGA — Fix data (lat, lon, alt, satellite count, fix quality)
        if isinstance(msg, pynmea2.types.talker.GGA):
            with self._lock:
                if msg.gps_qual and int(msg.gps_qual) > 0:
                    self._lat = float(msg.latitude)  if msg.latitude  else 0.0
                    self._lon = float(msg.longitude) if msg.longitude else 0.0
                    self._alt = float(msg.altitude)  if msg.altitude  else 0.0
                    self._satellites = int(msg.num_sats) if msg.num_sats else 0
                    if not self._has_fix:
                        log.info(f"GPS fix acquired: {self._lat:.6f}, {self._lon:.6f}, {self._alt:.1f}m, {self._satellites} sats")
                    self._has_fix = True
                    self._fix_event.set()
                else:
                    self._has_fix = False

        # RMC — Recommended minimum (speed, course) — lat/lon backup
        elif isinstance(msg, pynmea2.types.talker.RMC):
            if msg.status == "A":   # A = active / valid
                with self._lock:
                    if not self._has_fix:
                        self._lat = float(msg.latitude)  if msg.latitude  else 0.0
                        self._lon = float(msg.longitude) if msg.longitude else 0.0
                        self._has_fix = True
                        self._fix_event.set()
