"""
scanner/hci_le.py
=================
Direct HCI access to a Bluetooth LE controller, bypassing BlueZ.

Why: BlueZ/bleak drop most Remote ID traffic.  BlueZ discovery runs with
duplicate filtering, so a device that rotates through BasicID / Location /
OperatorID adverts is reported about once per scan session, and bleak never
delivered the 0xFFFA service data at all on the ARGUS nodes.  BlueZ also
rewrites the scan parameters on every discovery restart, so BT5 Coded PHY
(Long Range) set from outside never stayed on.

How: the controller is opened on an HCI *user channel* (HCI_CHANNEL_USER).
The kernel hands the device to this process exclusively and removes it from
BlueZ.  We then configure it ourselves:

  HCI_Reset → event masks → read LE features → random static address
  → LE Set Extended Scan Parameters (1M + Coded, passive)
  → LE Set Extended Scan Enable (Filter_Duplicates = 0)

Controllers without extended advertising fall back to the legacy scan
commands, still with duplicate filtering off.  Every advertising report is
returned; extended reports split across several HCI events are reassembled.

Only Linux is supported.  Needs CAP_NET_ADMIN (the systemd unit grants it).
The Pi's onboard Bluetooth (hci0) stays with BlueZ.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import select
import struct
import subprocess
import time
from dataclasses import dataclass
from typing import Iterator, Optional

log = logging.getLogger("bluetooth")

# ── Linux socket constants ───────────────────────────────────────────────────
AF_BLUETOOTH      = 31
BTPROTO_HCI       = 1
SOCK_RAW          = 3
SOCK_CLOEXEC      = 0o2000000
HCI_CHANNEL_USER  = 1
HCIDEVDOWN        = 0x400448CA      # _IOW('H', 202, int)

# ── HCI packet indicators ────────────────────────────────────────────────────
HCI_COMMAND_PKT   = 0x01
HCI_EVENT_PKT     = 0x04

# ── Events ───────────────────────────────────────────────────────────────────
EVT_CMD_COMPLETE  = 0x0E
EVT_CMD_STATUS    = 0x0F
EVT_LE_META       = 0x3E
LE_ADV_REPORT     = 0x02
LE_EXT_ADV_REPORT = 0x0D

# ── Commands (OGF << 10 | OCF) ───────────────────────────────────────────────
OP_RESET                 = 0x0C03
OP_SET_EVENT_MASK        = 0x0C01
OP_LE_SET_EVENT_MASK     = 0x2001
OP_LE_READ_FEATURES      = 0x2003
OP_LE_SET_RANDOM_ADDR    = 0x2005
OP_LE_SET_SCAN_PARAMS    = 0x200B
OP_LE_SET_SCAN_ENABLE    = 0x200C
OP_LE_SET_EXT_SCAN_PARAMS = 0x2041
OP_LE_SET_EXT_SCAN_ENABLE = 0x2042

# Event mask: everything BlueZ enables, plus bit 61 (LE Meta event)
EVENT_MASK    = 0x3DBFF807FFFBFFFF
# LE event mask: bits 0-19, includes Advertising Report (1) and
# Extended Advertising Report (12)
LE_EVENT_MASK = 0x00000000000FFFFF

LE_FEATURE_CODED_PHY   = 1 << 11
LE_FEATURE_EXT_ADV     = 1 << 12

# PHY values reported in Extended Advertising Reports
PHY_1M    = 0x01
PHY_CODED = 0x03

# Scanning_PHYs bit field for LE Set Extended Scan Parameters
PHY_1M_BIT    = 0x01
PHY_CODED_BIT = 0x04

# Scan timing, units of 0.625 ms
SCAN_INTERVAL_SINGLE = 0x0060   # 60 ms interval, 100 % duty on one PHY
SCAN_WINDOW_SINGLE   = 0x0060
SCAN_INTERVAL_DUAL   = 0x00A0   # 100 ms interval, 50 ms on each PHY
SCAN_WINDOW_DUAL     = 0x0050

# AD types
AD_SERVICE_DATA_16 = 0x16
RID_UUID_16        = 0xFFFA


class HCIError(Exception):
    """A command failed or the controller stopped responding."""


class HCIUnavailable(HCIError):
    """The controller could not be opened (permission, missing, busy)."""


# ── Packet helpers ───────────────────────────────────────────────────────────

def build_command(opcode: int, params: bytes = b"") -> bytes:
    return bytes([HCI_COMMAND_PKT]) + struct.pack("<HB", opcode, len(params)) + params


def format_address(raw: bytes) -> str:
    """HCI addresses are little-endian on the wire."""
    return ":".join(f"{b:02X}" for b in reversed(raw))


def iter_ad_structures(data: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield (ad_type, value) for each AD structure in advertising data."""
    i = 0
    while i < len(data):
        length = data[i]
        if length == 0:
            break
        if i + 1 + length > len(data):
            break
        yield data[i + 1], data[i + 2:i + 1 + length]
        i += 1 + length


def rid_service_data(ad_data: bytes) -> Optional[bytes]:
    """
    Return the ASTM Remote ID service data body (app code 0x0D, counter,
    message or message pack), or None if the advert carries no Remote ID.
    """
    for ad_type, value in iter_ad_structures(ad_data):
        if ad_type == AD_SERVICE_DATA_16 and len(value) > 2:
            if struct.unpack_from("<H", value, 0)[0] == RID_UUID_16:
                return value[2:]
    return None


@dataclass
class AdvReport:
    address: str
    address_type: int
    rssi: int
    data: bytes
    legacy: bool = True
    primary_phy: int = PHY_1M
    sid: int = 0xFF
    data_status: int = 0     # 0 complete, 1 more to come, 2 truncated

    @property
    def phy_label(self) -> str:
        return "coded" if self.primary_phy == PHY_CODED else "1M"


def parse_legacy_reports(body: bytes) -> list[AdvReport]:
    """
    LE Advertising Report (subevent 0x02).  Fields are stored as arrays,
    field by field: all event types, then all address types, and so on.
    """
    if not body:
        return []
    n = body[0]
    try:
        o = 1
        ev_types   = list(body[o:o + n]); o += n
        addr_types = list(body[o:o + n]); o += n
        addrs = [body[o + 6 * i:o + 6 * (i + 1)] for i in range(n)]; o += 6 * n
        lens = list(body[o:o + n]); o += n
        datas = []
        for ln in lens:
            datas.append(body[o:o + ln]); o += ln
        rssis = [struct.unpack_from("<b", body, o + i)[0] for i in range(n)]
    except (IndexError, struct.error):
        return []
    reports = []
    for i in range(n):
        if len(addrs[i]) != 6 or len(datas[i]) != lens[i]:
            continue
        reports.append(AdvReport(
            address=format_address(addrs[i]), address_type=addr_types[i],
            rssi=rssis[i], data=bytes(datas[i]), legacy=True,
        ))
    return reports


def parse_extended_reports(body: bytes) -> list[AdvReport]:
    """LE Extended Advertising Report (subevent 0x0D), one report after another."""
    if not body:
        return []
    n = body[0]
    o = 1
    reports = []
    for _ in range(n):
        if o + 24 > len(body):
            break
        ev_type, = struct.unpack_from("<H", body, o)
        addr_type = body[o + 2]
        addr      = body[o + 3:o + 9]
        primary   = body[o + 9]
        sid       = body[o + 11]
        rssi      = struct.unpack_from("<b", body, o + 13)[0]
        data_len  = body[o + 23]
        data      = body[o + 24:o + 24 + data_len]
        o += 24 + data_len
        if len(data) != data_len:
            break
        reports.append(AdvReport(
            address=format_address(addr), address_type=addr_type, rssi=rssi,
            data=bytes(data), legacy=bool(ev_type & 0x10), primary_phy=primary,
            sid=sid, data_status=(ev_type >> 5) & 0x03,
        ))
    return reports


class ExtReassembler:
    """Join extended advertising data that arrives split across reports."""

    def __init__(self, max_age: float = 1.0):
        self._parts: dict[tuple[str, int], tuple[float, bytearray]] = {}
        self._max_age = max_age

    def add(self, r: AdvReport, now: Optional[float] = None) -> Optional[AdvReport]:
        now = time.monotonic() if now is None else now
        if r.legacy:
            return r
        key = (r.address, r.sid)
        if r.data_status == 1:                       # more data coming
            started, buf = self._parts.get(key, (now, bytearray()))
            buf.extend(r.data)
            self._parts[key] = (started, buf)
            return None
        started, buf = self._parts.pop(key, (now, bytearray()))
        if r.data_status == 2:                       # truncated, discard
            return None
        buf.extend(r.data)
        if now - started > self._max_age:
            return None
        return AdvReport(r.address, r.address_type, r.rssi, bytes(buf),
                         legacy=False, primary_phy=r.primary_phy, sid=r.sid)

    def expire(self, now: Optional[float] = None):
        now = time.monotonic() if now is None else now
        for k in [k for k, (t, _) in self._parts.items() if now - t > self._max_age]:
            del self._parts[k]


# ── Transport ────────────────────────────────────────────────────────────────

class HCIUserChannel:
    """An HCI user channel socket on one controller (Linux only)."""

    def __init__(self, dev_id: int):
        self.dev_id = dev_id
        self.fd = -1

    def open(self, attempts: int = 5):
        try:
            libc = ctypes.CDLL(None, use_errno=True)
        except OSError as e:
            raise HCIUnavailable(f"libc unavailable: {e}") from e

        last_err = 0
        for _ in range(attempts):
            self._power_down(libc)
            fd = libc.socket(AF_BLUETOOTH, SOCK_RAW | SOCK_CLOEXEC, BTPROTO_HCI)
            if fd < 0:
                err = ctypes.get_errno()
                raise HCIUnavailable(f"cannot create HCI socket: {os.strerror(err)}")
            addr = struct.pack("<HHH", AF_BLUETOOTH, self.dev_id, HCI_CHANNEL_USER)
            buf = ctypes.create_string_buffer(addr, len(addr))
            if libc.bind(fd, buf, len(addr)) == 0:
                self.fd = fd
                return
            last_err = ctypes.get_errno()
            os.close(fd)
            if last_err in (errno.EPERM, errno.EACCES):
                raise HCIUnavailable("no permission for HCI user channel (needs CAP_NET_ADMIN)")
            if last_err == errno.ENODEV:
                raise HCIUnavailable(f"hci{self.dev_id} not found")
            time.sleep(0.3)                          # EBUSY / EALREADY: BlueZ raced us
        raise HCIUnavailable(f"hci{self.dev_id} busy: {os.strerror(last_err)}")

    def _power_down(self, libc):
        """The device must be down before the user channel can bind."""
        try:
            subprocess.run(["btmgmt", "--index", str(self.dev_id), "power", "off"],
                           capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        fd = libc.socket(AF_BLUETOOTH, SOCK_RAW | SOCK_CLOEXEC, BTPROTO_HCI)
        if fd >= 0:
            libc.ioctl(fd, HCIDEVDOWN, ctypes.c_int(self.dev_id))
            os.close(fd)

    def write(self, pkt: bytes):
        os.write(self.fd, pkt)

    def read(self, timeout: float) -> Optional[bytes]:
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        return os.read(self.fd, 1024)

    def close(self):
        if self.fd >= 0:
            try:
                os.close(self.fd)
            finally:
                self.fd = -1


# ── Scanner ──────────────────────────────────────────────────────────────────

class LEScanner:
    """
    Configures a controller for continuous passive scanning with duplicate
    filtering off, then yields every advertising report.

    `transport` needs write(bytes) and read(timeout) -> bytes | None, so the
    scanner can be driven by a fake controller in tests.
    """

    def __init__(self, transport, coded_phy: bool = True):
        self.t = transport
        self.want_coded = coded_phy
        self.extended = False
        self.coded = False
        self._pending: list[bytes] = []
        self._reasm = ExtReassembler()

    # -- commands --
    def _command(self, opcode: int, params: bytes = b"", timeout: float = 2.0) -> bytes:
        self.t.write(build_command(opcode, params))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pkt = self.t.read(max(0.0, deadline - time.monotonic()))
            if not pkt or pkt[0] != HCI_EVENT_PKT or len(pkt) < 3:
                continue
            code, body = pkt[1], pkt[3:3 + pkt[2]]
            if code == EVT_CMD_COMPLETE and len(body) >= 4:
                if struct.unpack_from("<H", body, 1)[0] == opcode:
                    ret = body[3:]
                    if ret and ret[0] != 0:
                        raise HCIError(f"command 0x{opcode:04X} failed, status 0x{ret[0]:02X}")
                    return ret
            elif code == EVT_CMD_STATUS and len(body) >= 4:
                if struct.unpack_from("<H", body, 2)[0] == opcode:
                    if body[0] != 0:
                        raise HCIError(f"command 0x{opcode:04X} failed, status 0x{body[0]:02X}")
                    return b"\x00"
            else:
                self._pending.append(pkt)       # adverts arriving during a command
        raise HCIError(f"no response to command 0x{opcode:04X}")

    def start(self):
        self._command(OP_RESET, timeout=5.0)
        self._pending.clear()
        self._command(OP_SET_EVENT_MASK, struct.pack("<Q", EVENT_MASK))
        self._command(OP_LE_SET_EVENT_MASK, struct.pack("<Q", LE_EVENT_MASK))

        ret = self._command(OP_LE_READ_FEATURES)
        features = struct.unpack_from("<Q", ret, 1)[0] if len(ret) >= 9 else 0
        self.extended = bool(features & LE_FEATURE_EXT_ADV)
        self.coded = self.extended and self.want_coded and bool(features & LE_FEATURE_CODED_PHY)

        # Passive scanning never transmits, but controllers still validate the
        # own-address type.  The nRF52840 has no public address, so use a
        # random static one (two top bits set).
        addr = bytearray(os.urandom(6))
        addr[5] |= 0xC0
        self._command(OP_LE_SET_RANDOM_ADDR, bytes(addr))

        if self.extended:
            phys = PHY_1M_BIT | (PHY_CODED_BIT if self.coded else 0)
            if self.coded:
                per_phy = struct.pack("<BHH", 0x00, SCAN_INTERVAL_DUAL, SCAN_WINDOW_DUAL)
                params = bytes([0x01, 0x00, phys]) + per_phy + per_phy
            else:
                per_phy = struct.pack("<BHH", 0x00, SCAN_INTERVAL_SINGLE, SCAN_WINDOW_SINGLE)
                params = bytes([0x01, 0x00, phys]) + per_phy
            self._command(OP_LE_SET_EXT_SCAN_PARAMS, params)
            # Enable, Filter_Duplicates=0, Duration=0, Period=0
            self._command(OP_LE_SET_EXT_SCAN_ENABLE, struct.pack("<BBHH", 1, 0, 0, 0))
        else:
            # Passive, interval, window, own address random, accept all
            self._command(OP_LE_SET_SCAN_PARAMS, struct.pack(
                "<BHHBB", 0x00, SCAN_INTERVAL_SINGLE, SCAN_WINDOW_SINGLE, 0x01, 0x00))
            # Enable, Filter_Duplicates=0
            self._command(OP_LE_SET_SCAN_ENABLE, bytes([0x01, 0x00]))

    def stop(self):
        try:
            if self.extended:
                self._command(OP_LE_SET_EXT_SCAN_ENABLE, struct.pack("<BBHH", 0, 0, 0, 0), timeout=1.0)
            else:
                self._command(OP_LE_SET_SCAN_ENABLE, bytes([0x00, 0x00]), timeout=1.0)
        except (HCIError, OSError):
            pass

    @property
    def mode(self) -> str:
        if self.coded:
            return "extended scanning, 1M + Coded PHY (BT4 + BT5 Long Range)"
        if self.extended:
            return "extended scanning, 1M PHY only (BT4 + BT5 legacy)"
        return "legacy scanning, 1M PHY (BT4)"

    # -- reports --
    def read_reports(self, timeout: float) -> list[AdvReport]:
        pkts = self._pending
        self._pending = []
        if not pkts:
            pkt = self.t.read(timeout)
            if pkt:
                pkts = [pkt]
        out: list[AdvReport] = []
        for pkt in pkts:
            out.extend(self._decode(pkt))
        self._reasm.expire()
        return out

    def _decode(self, pkt: bytes) -> list[AdvReport]:
        if len(pkt) < 4 or pkt[0] != HCI_EVENT_PKT or pkt[1] != EVT_LE_META:
            return []
        body = pkt[3:3 + pkt[2]]
        sub, rest = body[0], body[1:]
        if sub == LE_ADV_REPORT:
            return parse_legacy_reports(rest)
        if sub == LE_EXT_ADV_REPORT:
            done = []
            for r in parse_extended_reports(rest):
                full = self._reasm.add(r)
                if full is not None:
                    done.append(full)
            return done
        return []

