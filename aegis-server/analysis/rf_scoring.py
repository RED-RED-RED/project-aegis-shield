"""
analysis/rf_scoring.py
======================
Threat score (0-100) for an RF contact: a link the SDR classified without a
Remote ID decode. This is deliberately separate from threat_scoring.py, which
is built around Remote ID fields (operator ID, broadcast position, MLAT).

An RF contact has no operator ID and no self-reported position, so the whole
question is different: what kind of link is it, and does a compliant Remote ID
broadcast line up with it? A control or video link with no matching Remote ID
is the thing a counter-UAS operator most wants flagged.

Factors
-------
signal_type_severity   40   A control/video link that implies a flying aircraft
no_rid_correlation      35   SDR sees a drone link but nothing is broadcasting RID
multi_node              10   Seen by several nodes -> real, localizable, close
strong_signal           10   High RSSI -> nearby
sustained                5   Seen repeatedly -> not a transient blip
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time

HIGH_THRESHOLD = 70
MEDIUM_THRESHOLD = 40

WEIGHTS = {
    "signal_type_severity": 40,
    "no_rid_correlation":   35,
    "multi_node":           10,
    "strong_signal":        10,
    "sustained":             5,
}
assert sum(WEIGHTS.values()) == 100, "RF weights must sum to 100"

# How drone-like each signal type is. 1.0 = almost certainly an aircraft link,
# lower = could be ground clutter (ISM, metering) that shares the band.
SIGNAL_SEVERITY = {
    "dji_ocusync":       1.0,   # DJI control link, no RID decoded = evasive or non-US
    "fpv_video_5g":      1.0,   # 5.8 GHz video = an FPV aircraft is flying
    "fpv_video_analog":  1.0,
    "video_1g2":         0.9,
    "elrs_2g":           0.9,   # control link, almost always a drone/RC aircraft
    "crossfire_900":     0.9,
    "elrs_900":          0.9,
    "lora_900":          0.4,   # could be metering/telemetry sensors, not just drones
    "unknown":           0.3,
}

# Bands where the band itself can't be decoded to Remote ID, so "no RID
# correlation" is expected and should NOT by itself scream non-compliant.
# 2.4 GHz and 5.8 GHz video are the bands a real drone would use that the
# Wi-Fi/BT scanners could in principle also see, so a dark link there is
# more suspicious.
_RID_CAPABLE_BANDS = {"2g4"}


@dataclass
class RFThreat:
    signal_type_severity: float = 0.0
    no_rid_correlation:   float = 0.0
    multi_node:           float = 0.0
    strong_signal:        float = 0.0
    sustained:            float = 0.0
    score: float = 0.0
    level: str = "low"
    computed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            "level": self.level,
            "factors": {k: round(getattr(self, k), 3) for k in WEIGHTS},
            "computed_at": self.computed_at,
        }


def _level(score: float) -> str:
    if score >= HIGH_THRESHOLD:
        return "high"
    if score >= MEDIUM_THRESHOLD:
        return "medium"
    return "low"


def score_rf_contact(contact: dict) -> RFThreat:
    """
    Score a contact dict (an rf_contacts row, or an equivalent dict).

    Expected keys: signal_type, band, correlated (bool), best_rssi/last_rssi
    (dBm), detecting_nodes (list) or node_count (int), detection_count (int).
    """
    t = RFThreat()
    sig = contact.get("signal_type", "unknown")
    t.signal_type_severity = SIGNAL_SEVERITY.get(sig, 0.3)

    # No Remote ID correlation. A classified drone link with nothing broadcasting
    # Remote ID is the core "non-compliant drone" signal. Weight it by how
    # drone-like the signal is, so lora_900/unknown don't over-alert.
    correlated = bool(contact.get("correlated"))
    if not correlated:
        base = 1.0 if contact.get("band") in _RID_CAPABLE_BANDS else 0.85
        t.no_rid_correlation = base * t.signal_type_severity
    else:
        t.no_rid_correlation = 0.0

    nodes = contact.get("detecting_nodes")
    node_count = contact.get("node_count")
    if node_count is None:
        node_count = len(nodes) if nodes else 1
    t.multi_node = min(max(node_count - 1, 0) / 2.0, 1.0)   # 1 node 0, 3+ nodes 1.0

    rssi = contact.get("best_rssi")
    if rssi is None:
        rssi = contact.get("last_rssi")
    if rssi is not None:
        # -40 dBm (very close) -> 1.0, -90 dBm (far) -> 0.0
        t.strong_signal = min(max((rssi + 90) / 50.0, 0.0), 1.0)

    count = contact.get("detection_count", 0) or 0
    t.sustained = min(count / 10.0, 1.0)

    t.score = sum(WEIGHTS[k] * getattr(t, k) for k in WEIGHTS)
    t.level = _level(t.score)
    return t
