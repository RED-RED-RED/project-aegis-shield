"""
scanner/sdr_source.py
=====================
Pluggable IQ sources for the RF scanner, so the classifier is independent of
the radio. The scanner talks to this interface; the hardware details live here.

  IQSource          abstract: open / set_center / read / close, plus the
                    tunable range so the scanner can skip bands a radio can't
                    reach (an RTL-SDR can't see 2.4 / 5.8 GHz).
  HackRFSource      HackRF One / Pro via SoapySDR (preferred) or pyhackrf.
                    1 MHz - 6 GHz, 20 MHz span: the whole drone picture.
  RtlSdrSource      RTL-SDR via pyrtlsdr. Up to ~1.76 GHz, ~2.4 MHz span.
                    Keeps older nodes working for 433 / 900 / 1.2 GHz.
  SyntheticSource   returns IQ from a callable. For tests and dry runs; no
                    hardware, no imports.

Real SDR libraries are imported lazily inside open(), so importing this module
(and running the tests) never needs SoapySDR, pyhackrf or pyrtlsdr installed.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

import numpy as np

log = logging.getLogger("sdr")


class SDRSourceError(Exception):
    pass


class IQSource:
    """Abstract IQ source."""

    #: inclusive (low, high) tunable centre range in Hz
    tune_range_hz: tuple[float, float] = (0.0, 0.0)
    #: usable instantaneous span (sample rate) in Hz
    sample_rate: float = 0.0
    name: str = "base"

    def open(self) -> None:
        raise NotImplementedError

    def set_center(self, freq_hz: float) -> None:
        raise NotImplementedError

    def read(self, n: int) -> np.ndarray:
        """Return n complex64 samples at the current centre."""
        raise NotImplementedError

    def close(self) -> None:
        pass

    def can_tune(self, freq_hz: float) -> bool:
        lo, hi = self.tune_range_hz
        return lo <= freq_hz <= hi


# ── HackRF (SoapySDR preferred, pyhackrf fallback) ───────────────────────────

class HackRFSource(IQSource):
    name = "hackrf"
    tune_range_hz = (1e6, 6e9)

    def __init__(self, sample_rate: float = 20e6, lna_gain: int = 32,
                 vga_gain: int = 20, amp: bool = False, device_args: str = ""):
        self.sample_rate = float(sample_rate)
        self.lna_gain = lna_gain
        self.vga_gain = vga_gain
        self.amp = amp
        self.device_args = device_args
        self._dev = None
        self._stream = None
        self._soapy = None

    def open(self) -> None:
        try:
            import SoapySDR
            from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
        except Exception as e:
            raise SDRSourceError(
                f"SoapySDR not available for HackRF: {e}. "
                f"Install soapysdr-module-hackrf and python3-soapysdr."
            ) from e
        self._soapy = SoapySDR
        args = dict(driver="hackrf")
        if self.device_args:
            for kv in self.device_args.split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    args[k.strip()] = v.strip()
        try:
            self._dev = SoapySDR.Device(args)
            self._dev.setSampleRate(SOAPY_SDR_RX, 0, self.sample_rate)
            self._dev.setBandwidth(SOAPY_SDR_RX, 0, self.sample_rate)
            try:
                self._dev.setGain(SOAPY_SDR_RX, 0, "LNA", self.lna_gain)
                self._dev.setGain(SOAPY_SDR_RX, 0, "VGA", self.vga_gain)
                self._dev.setGain(SOAPY_SDR_RX, 0, "AMP", 14 if self.amp else 0)
            except Exception:
                self._dev.setGain(SOAPY_SDR_RX, 0, self.lna_gain + self.vga_gain)
            self._stream = self._dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
            self._dev.activateStream(self._stream)
        except Exception as e:
            raise SDRSourceError(f"Failed to open HackRF: {e}") from e
        log.info(f"HackRF open: {self.sample_rate/1e6:.0f} MS/s, "
                 f"LNA {self.lna_gain} VGA {self.vga_gain} AMP {'on' if self.amp else 'off'}")

    def set_center(self, freq_hz: float) -> None:
        from SoapySDR import SOAPY_SDR_RX
        self._dev.setFrequency(SOAPY_SDR_RX, 0, float(freq_hz))

    def read(self, n: int) -> np.ndarray:
        buf = np.empty(n, np.complex64)
        got = 0
        while got < n:
            chunk = np.empty(n - got, np.complex64)
            sr = self._dev.readStream(self._stream, [chunk], len(chunk), timeoutUs=1_000_000)
            nread = sr.ret
            if nread <= 0:
                break
            buf[got:got + nread] = chunk[:nread]
            got += nread
        return buf[:got]

    def close(self) -> None:
        try:
            if self._dev and self._stream:
                from SoapySDR import SOAPY_SDR_RX  # noqa: F401
                self._dev.deactivateStream(self._stream)
                self._dev.closeStream(self._stream)
        except Exception:
            pass
        self._dev = None
        self._stream = None


# ── RTL-SDR (pyrtlsdr) ───────────────────────────────────────────────────────

class RtlSdrSource(IQSource):
    name = "rtlsdr"
    tune_range_hz = (24e6, 1.766e9)      # R820T/R828D ceiling

    def __init__(self, device_index: int = 0, sample_rate: float = 2.4e6,
                 gain="auto"):
        self.device_index = device_index
        self.sample_rate = float(sample_rate)
        self.gain = gain
        self._sdr = None

    def open(self) -> None:
        try:
            from rtlsdr import RtlSdr
        except Exception as e:
            raise SDRSourceError(f"pyrtlsdr not available: {e}") from e
        try:
            self._sdr = RtlSdr(self.device_index)
            self._sdr.sample_rate = self.sample_rate
            self._sdr.gain = self.gain
        except Exception as e:
            raise SDRSourceError(f"Failed to open RTL-SDR {self.device_index}: {e}") from e
        log.info(f"RTL-SDR open: device {self.device_index}, {self.sample_rate/1e6:.2f} MS/s")

    def set_center(self, freq_hz: float) -> None:
        self._sdr.center_freq = float(freq_hz)

    def read(self, n: int) -> np.ndarray:
        return self._sdr.read_samples(n).astype(np.complex64)

    def close(self) -> None:
        try:
            if self._sdr:
                self._sdr.close()
        except Exception:
            pass
        self._sdr = None


# ── Synthetic (tests / dry run) ──────────────────────────────────────────────

class SyntheticSource(IQSource):
    """
    Returns IQ from a generator callable: gen(center_hz, n, fs) -> complex64.
    Lets the scanner be exercised end to end with no hardware.
    """
    name = "synthetic"

    def __init__(self, generator: Callable[[float, int, float], np.ndarray],
                 sample_rate: float = 20e6,
                 tune_range_hz: tuple[float, float] = (1e6, 6e9)):
        self.generator = generator
        self.sample_rate = float(sample_rate)
        self.tune_range_hz = tune_range_hz
        self._center = 0.0
        self.opened = False

    def open(self) -> None:
        self.opened = True

    def set_center(self, freq_hz: float) -> None:
        self._center = float(freq_hz)

    def read(self, n: int) -> np.ndarray:
        return np.asarray(self.generator(self._center, n, self.sample_rate),
                          dtype=np.complex64)

    def close(self) -> None:
        self.opened = False


def make_source(backend: str, device_index: int = 0, sample_rate: Optional[float] = None,
                **kwargs) -> IQSource:
    """Factory: 'hackrf' | 'rtlsdr'. Raises SDRSourceError for unknown backends."""
    backend = (backend or "").lower()
    if backend == "hackrf":
        return HackRFSource(sample_rate=sample_rate or 20e6, **kwargs)
    if backend in ("rtlsdr", "rtl-sdr", "rtl"):
        return RtlSdrSource(device_index=device_index, sample_rate=sample_rate or 2.4e6)
    raise SDRSourceError(f"Unknown SDR backend: {backend!r}")
