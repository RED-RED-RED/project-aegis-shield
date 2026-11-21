"""
parser/assembler.py
===================
Builds a complete picture of a drone from Remote ID messages that arrive in
separate packets.

Bluetooth 4 Remote ID carries one 25-byte message per advertisement and
rotates through BasicID, Location, System, OperatorID and Self-ID.  Parsing
each advert on its own loses the position, because a Location advert has no
drone ID and a BasicID advert has no position.  The assembler keeps state per
transmitter address, merges every message into it, and hands back a snapshot
to publish at most once per `publish_interval` seconds.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from typing import Optional

from parser.opendroneid import OpenDroneIDParser, RIDFrame


@dataclass
class _State:
    frame: RIDFrame
    first_seen: float
    last_seen: float
    last_publish: float = float("-inf")


class RIDAssembler:
    def __init__(
        self,
        parser: Optional[OpenDroneIDParser] = None,
        publish_interval: float = 1.0,
        ttl: float = 30.0,
        id_grace: float = 3.0,
    ):
        """
        publish_interval: minimum seconds between snapshots for one drone
        ttl:              drop a transmitter after this many seconds of silence
        id_grace:         wait this long for a serial-number BasicID before
                          publishing under another ID type (avoids a second
                          track when a drone sends serial + CAA registration)
        """
        self.parser = parser or OpenDroneIDParser()
        self.publish_interval = publish_interval
        self.ttl = ttl
        self.id_grace = id_grace
        self._states: dict[str, _State] = {}
        self._last_expire = 0.0

    def feed(self, key: str, payload: bytes, transport: str,
             now: Optional[float] = None) -> Optional[RIDFrame]:
        """Merge a message (or pack) from transmitter `key`; maybe return a snapshot."""
        now = time.time() if now is None else now
        self._expire(now)

        st = self._states.get(key)
        if st is None:
            st = _State(RIDFrame(transport=transport), first_seen=now, last_seen=now)
            self._states[key] = st

        if not self.parser.apply(st.frame, payload):
            return None
        st.last_seen = now
        st.frame.timestamp = now
        st.frame.raw_hex = payload.hex()

        f = st.frame
        if not f.drone_id:
            return None
        if f.id_type != "serial_number" and now - st.first_seen < self.id_grace:
            return None
        if now - st.last_publish < self.publish_interval:
            return None
        st.last_publish = now
        return copy.copy(f)

    def __len__(self) -> int:
        return len(self._states)

    def _expire(self, now: float):
        if now - self._last_expire < 5.0:
            return
        self._last_expire = now
        for k in [k for k, s in self._states.items() if now - s.last_seen > self.ttl]:
            del self._states[k]
