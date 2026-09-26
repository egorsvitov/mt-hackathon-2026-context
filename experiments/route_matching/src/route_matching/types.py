"""Immutable public contracts; times are UTC epoch seconds, distances are metres."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from zoneinfo import ZoneInfo

Coord = tuple[float, float]  # longitude, latitude


def timestamp(value: str | float | datetime, timezone_name: str = "UTC") -> float:
    if isinstance(value, (float, int)):
        return float(value)
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(timezone_name))
    return dt.timestamp()


def iso(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()[:24]


@dataclass(frozen=True)
class Event:
    tr_id: int
    event_time: float
    lon: float | None
    lat: float | None
    speed: float | None = None
    heading: float | None = None
    location_valid: bool = True
    packet_id: str = ""
    receive_time: float | None = None

    @property
    def valid(self) -> bool:
        return (self.location_valid and self.lon is not None and self.lat is not None
                and math.isfinite(self.lon) and math.isfinite(self.lat)
                and 30 < self.lon < 45 and 50 < self.lat < 60)

    @property
    def coord(self) -> Coord:
        if not self.valid:
            raise ValueError("No valid GPS coordinate")
        return self.lon, self.lat

    @property
    def key(self) -> tuple:
        return self.tr_id, self.event_time, self.lon, self.lat, self.location_valid

    @property
    def available_time(self) -> float:
        """When the observation was knowable by a causal consumer."""
        return (max(self.event_time, self.receive_time)
                if self.receive_time is not None else self.event_time)


@dataclass(frozen=True)
class Visit:
    visit_id: str
    tr_id: int
    time: float
    stop_id: str
    coord: Coord
    address: str = ""


@dataclass(frozen=True)
class StopGroup:
    """Co-located consecutive visits; equal-time DIFFERENT locations stay alternatives."""
    stop_id: str
    coord: Coord
    visits: tuple[Visit, ...]

    @property
    def time(self) -> float:
        return self.visits[0].time


@dataclass(frozen=True)
class Edge:
    edge_id: str  # graph_version:id
    way_id: str
    shape: tuple[Coord, ...]  # geometry in traversal direction, clipped at path ends
    length_m: float
    source_fraction: float = 0.0
    target_fraction: float = 1.0
    begin_node: str | None = None
    end_node: str | None = None


@dataclass(frozen=True)
class RoadPath:
    edges: tuple[Edge, ...]

    @property
    def length_m(self) -> float:
        return sum(e.length_m for e in self.edges)

    @property
    def signature(self) -> tuple[str, ...]:
        return tuple(e.edge_id for e in self.edges)


@dataclass(frozen=True)
class MatchedPoint:
    coord: Coord | None
    edge_index: int | None
    fraction: float | None
    distance_m: float | None
    discontinuity: bool = False


@dataclass(frozen=True)
class Trace:
    path: RoadPath
    points: tuple[MatchedPoint, ...]


@dataclass(frozen=True)
class Passage:
    segment_id: str
    tr_id: int
    from_visit: str
    to_visit: str
    start_time: float
    end_time: float
    available_at: float
    path: RoadPath
    quality: float

    @property
    def key(self) -> tuple:
        # Never count aliases, duplicate packets or alternate schedule hypotheses twice.
        return self.tr_id, self.start_time, self.end_time


@dataclass(frozen=True)
class Variant:
    variant_id: str
    path: RoadPath
    source: str
    support: int
    support_fraction: float
    provenance: tuple[tuple[int, float, float], ...] = ()
    available_at: float | None = None
    median_duration_s: float | None = None

    @property
    def confirmed(self) -> bool:
        return self.source == "observed" and self.support >= 2


@dataclass(frozen=True)
class Leg:
    index: int
    segment_id: str
    start: StopGroup
    end: StopGroup


@dataclass(frozen=True)
class Sequence:
    sequence_id: str
    tr_id: int
    stops: tuple[StopGroup, ...]
    legs: tuple[Leg, ...]
    ambiguous: bool = False


@dataclass(frozen=True)
class Config:
    radius_m: float = 100.0
    gps_sigma_m: float = 25.0
    heading_min_kmh: float = 5.0
    heading_sigma_deg: float = 45.0
    backtrack_m: float = 30.0
    max_speed_kmh: float = 150.0
    gap_s: float = 180.0
    window_s: float = 600.0
    beam: int = 32
    transition_beta_m: float = 50.0
    schedule_sigma_s: float = 900.0
    offroute_m: float = 150.0
    hysteresis_count: int = 3
    hysteresis_s: float = 30.0
    fallback_timeout_s: float = 1.0
    stop_radius_m: float = 60.0
    cluster_distance: float = 0.2


@dataclass(frozen=True)
class MatchState:
    tr_id: int
    T: float
    catalog_version: str
    graph_version: str
    mode: str = "unmatched"
    edge_id: str | None = None
    edge_occurrence: str | None = None
    edge_offset_m: float | None = None
    lon: float | None = None
    lat: float | None = None
    sequence_id: str | None = None
    visit_id: str | None = None
    visit_index: int | None = None
    segment_id: str | None = None
    variant_id: str | None = None
    progress: float | None = None
    remaining_distance_m: float | None = None
    remaining_visits: int | None = None
    speed_mps: float | None = None
    speed_1m_mps: float | None = None
    speed_3m_mps: float | None = None
    speed_5m_mps: float | None = None
    route_distance_m: float | None = None
    position_quality: str = "unknown"
    route_quality: str = "unknown"
    route_source: str | None = None
    confidence_margin: float | None = None
    off_route: bool | None = None
    gps_age_s: float | None = None
    max_event_time: float | None = None
    reason: str | None = None

    def features(self) -> dict[str, float | None]:
        names = ("progress", "remaining_distance_m", "remaining_visits", "speed_mps",
                 "speed_1m_mps", "speed_3m_mps", "speed_5m_mps", "route_distance_m",
                 "confidence_margin", "gps_age_s")
        result = {f"mm_{k}": getattr(self, k) for k in names}
        result.update(mm_matched=float(self.mode != "unmatched"),
                      mm_confirmed=float(self.route_quality == "confirmed"),
                      mm_fallback=float(self.mode == "road"),
                      mm_off_route=None if self.off_route is None else float(self.off_route))
        return {**result, **{k + "_missing": float(v is None) for k, v in result.items()}}

    def to_dict(self) -> dict:
        return asdict(self)
