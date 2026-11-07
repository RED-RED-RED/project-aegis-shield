"""
models/schemas.py
=================
Pydantic v2 models for API request/response validation and serialization.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict


# ------------------------------------------------------------------ #
# Node models
# ------------------------------------------------------------------ #

class NodePosition(BaseModel):
    lat: float
    lon: float
    alt: float = 0.0


class NodeHeartbeat(BaseModel):
    """Parsed from MQTT argus/<node_id>/heartbeat"""
    node_id: str
    status: str
    ts: float
    gps: dict
    system: dict
    radios:         list[str] = []
    jamming_state:  Optional[str] = None   # GPS jamming indicator from UBX-NAV-STATUS
    spoofing_state: Optional[str] = None   # GPS spoofing indicator from UBX-NAV-STATUS


class NodeOut(BaseModel):
    node_id: str
    site_name: Optional[str] = None
    status: str
    last_seen: Optional[datetime] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt: Optional[float] = None
    gps_fix: bool = False
    satellites: int = 0
    cpu_pct: Optional[float] = None
    mem_pct: Optional[float] = None
    disk_pct: Optional[float] = None
    temp_c: Optional[float] = None
    uptime_s: Optional[int] = None
    radios: list[str] = []

    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------ #
# Detection / drone models
# ------------------------------------------------------------------ #

class DroneData(BaseModel):
    id: str
    id_type: Optional[str] = None
    ua_type: Optional[str] = None
    status: Optional[str] = None
    lat: float = 0.0
    lon: float = 0.0
    alt_baro: Optional[float] = None
    alt_geo: Optional[float] = None
    height_agl: Optional[float] = None
    speed_h: Optional[float] = None
    speed_v: Optional[float] = None
    heading: Optional[float] = None
    operator_id: Optional[str] = None
    operator_lat: Optional[float] = None
    operator_lon: Optional[float] = None
    description: Optional[str] = None


class DetectionEvent(BaseModel):
    """Parsed from MQTT argus/<node_id>/detection"""
    node_id: str
    transport: str
    band: Optional[str] = None
    rssi: Optional[int] = None
    src_addr: Optional[str] = None
    node_position: NodePosition
    drone: DroneData
    ts: float


class DetectionOut(BaseModel):
    id: int
    detected_at: datetime
    node_id: str
    transport: str
    band: Optional[str] = None
    rssi: Optional[int] = None
    drone_id: str
    drone_lat: Optional[float] = None
    drone_lon: Optional[float] = None
    alt_baro: Optional[float] = None
    height_agl: Optional[float] = None
    speed_h: Optional[float] = None
    heading: Optional[float] = None
    status: Optional[str] = None
    operator_id: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class DroneTrackOut(BaseModel):
    drone_id: str
    first_seen: datetime
    last_seen: datetime
    last_node_id: Optional[str] = None
    last_transport: Optional[str] = None
    last_rssi: Optional[int] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt_baro: Optional[float] = None
    alt_geo: Optional[float] = None
    height_agl: Optional[float] = None
    speed_h: Optional[float] = None
    speed_v: Optional[float] = None
    heading: Optional[float] = None
    status: Optional[str] = None
    id_type: Optional[str] = None
    ua_type: Optional[str] = None
    operator_id: Optional[str] = None
    operator_lat: Optional[float] = None
    operator_lon: Optional[float] = None
    description: Optional[str] = None
    has_valid_rid: bool = False
    detection_count: int = 0
    detecting_nodes: list[str] = []

    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------ #
# RF contact models (SDR-detected links, no Remote ID decode)
# ------------------------------------------------------------------ #

class RFSignal(BaseModel):
    """One classified RF emission from the SDR scanner."""
    signal_type: str                        # elrs_2g | elrs_900 | crossfire_900 |
                                            # dji_ocusync | fpv_video_analog | fpv_video_5g |
                                            # lora_900 | video_1g2 | unknown
    band: str                               # 2g4 | 5g8 | 900 | 1g2 | 433
    center_freq_hz: int
    bandwidth_hz: Optional[int] = None
    rssi: float                             # dBm
    snr_db: Optional[float] = None
    confidence: float = 0.0                 # 0..1 classifier confidence
    hop_rate_hz: Optional[float] = None     # FHSS hop rate, if measured
    burst_rate_hz: Optional[float] = None   # bursts/sec, if measured


class RFDetectionEvent(BaseModel):
    """Parsed from MQTT argus/<node_id>/rf_detection."""
    node_id: str
    node_position: NodePosition
    signal: RFSignal
    ts: float
    # Stable key for one emitter as seen by one node. The node sets it so repeated
    # detections update a single contact instead of creating new ones.
    contact_key: Optional[str] = None


class RFContactOut(BaseModel):
    contact_id: str                         # "<node_id>:<signal_type>:<band>" unless node overrides
    first_seen: datetime
    last_seen: datetime
    signal_type: str
    band: str
    center_freq_hz: Optional[int] = None
    bandwidth_hz: Optional[int] = None
    last_rssi: Optional[float] = None
    best_rssi: Optional[float] = None
    confidence: Optional[float] = None
    hop_rate_hz: Optional[float] = None
    detecting_nodes: list[str] = []
    detection_count: int = 0
    # Correlation with Remote ID
    correlated: bool = False                # a Remote ID track lines up with this link
    correlated_drone_id: Optional[str] = None
    compliant: Optional[bool] = None        # None = unknown, False = link but no RID (dark)
    # Rough position (RSSI-weighted centroid of detecting nodes; not MLAT-grade)
    est_lat: Optional[float] = None
    est_lon: Optional[float] = None
    est_radius_m: Optional[float] = None
    threat_score: Optional[float] = None
    threat_level: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------ #
# Alert models
# ------------------------------------------------------------------ #

class AlertOut(BaseModel):
    id: int
    created_at: datetime
    level: str
    category: str
    drone_id: Optional[str] = None
    node_id: Optional[str] = None
    title: str
    description: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    acknowledged: bool = False
    acknowledged_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class AcknowledgeAlert(BaseModel):
    acknowledged: bool = True


# ------------------------------------------------------------------ #
# WebSocket push models
# ------------------------------------------------------------------ #

class WSMessage(BaseModel):
    """Envelope for all WebSocket push messages."""
    type: str    # "live_state" | "alert" | "node_update"
    payload: dict


class LiveState(BaseModel):
    """Full live state pushed to AEGIS Shield clients every N ms."""
    drones: list[DroneTrackOut]
    nodes: list[NodeOut]
    recent_alerts: list[AlertOut]
    rf_contacts: list[RFContactOut] = []
    detection_rate: float   # detections per minute (rolling 5 min)
    ts: float
