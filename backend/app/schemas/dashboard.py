"""Ответы API для диспетчерского дашборда (контракт: dashboard/CONTRACT.md).

Времена — ISO 8601 со смещением (+03:00), задержки — секунды со знаком.
"""

from datetime import datetime
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

TrId = Union[int, str]
Severity = Literal["ok", "warning", "critical", "early", "unknown"]


class Stop(BaseModel):
    stop_key: int = Field(..., description="Физическая остановка (уникальная точка geom)")
    lat: float
    lon: float
    name: str


class RouteSegment(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: int = Field(..., alias="from", description="stop_key начала участка")
    to: int
    synthetic: bool = Field(..., description="true — нет GPS-геометрии, прямой отрезок")
    path: list[list[float]] = Field(..., description="[[lat, lon], ...]")
    segment_id: Optional[str] = None
    route_pattern_id: Optional[str] = None
    source: Optional[str] = None
    quality: Optional[str] = None
    support: Optional[int] = None
    length_m: Optional[float] = None


class Route(BaseModel):
    route_id: str
    tr_id: TrId
    name: str
    speed_norm_kmh: Optional[float] = None
    route_pattern_ids: list[str] = Field(default_factory=list)
    default_route_pattern_id: Optional[str] = None
    stops: list[int]
    segments: list[RouteSegment]


class RoutePattern(BaseModel):
    route_pattern_id: str
    direction_id: Optional[str] = None
    name: str
    tr_ids: list[TrId]
    stops: list[int]
    segments: list[RouteSegment]


class Network(BaseModel):
    stops: list[Stop]
    route_patterns: list[RoutePattern] = Field(default_factory=list)
    routes: list[Route] = Field(..., description="Рейсы/плановые траектории конкретных ТС")


class Visit(BaseModel):
    visit_id: int = Field(..., description="tt_action_item_id — плановое посещение")
    stop_key: int
    time_plan: datetime
    time_fact: Optional[datetime] = Field(None, description="Прибытие по детектору; только уже произошедшие")


class Schedule(BaseModel):
    tr_id: TrId
    visits: list[Visit]


class Vehicle(BaseModel):
    tr_id: TrId
    unit_id: Optional[int] = None
    route_id: Optional[str] = None
    event_time: datetime
    lat: float
    lon: float
    speed: Optional[float] = None
    heading: Optional[float] = None
    location_valid: bool = True
    data_age_s: float
    status: Literal["live", "stale", "offline"]
    source: str
    route_pattern_id: Optional[str] = None
    position_quality: Optional[str] = None
    off_route: Optional[bool] = None


class Reason(BaseModel):
    code: str
    title: str
    detail: Optional[str] = None


class Evidence(BaseModel):
    code: str
    label: str
    value: Optional[float] = None
    norm: Optional[float] = None
    unit: str
    flag: bool = False


class SegmentRef(BaseModel):
    from_stop_id: int
    from_stop_name: str
    to_stop_id: int
    to_stop_name: str


class Prediction(BaseModel):
    sample_id: str = Field(..., description="{tr_id}_{T}, как в points.csv")
    tr_id: TrId
    route_id: Optional[str] = None
    as_of: datetime = Field(..., description="Момент прогноза T")
    generated_at: datetime
    target_stop_id: int
    target_stop_name: str
    target_time_begin: datetime
    horizon_s: float
    prediction_delay_s: float
    predicted_arrival: datetime
    late_probability: Optional[float] = None
    cur_dev_s: Optional[float] = None
    cur_dev_source: str = Field("detector", description="detector | none")
    severity: Severity
    status: Literal["model", "fallback", "stale"]
    model_version: str
    data_age_s: Optional[float] = None
    segment: SegmentRef
    reason: Optional[Reason] = None
    evidence: list[Evidence] = []
    recommendation: Optional[str] = None


class Incident(BaseModel):
    incident_id: str
    tr_id: TrId
    route_id: Optional[str] = None
    kind: Literal["late", "early"]
    status: Literal["active", "resolved"]
    first_detected_at: datetime
    updated_at: datetime
    closed_at: Optional[datetime] = None
    severity: Severity
    peak_severity: Severity
    target_stop_id: int
    target_stop_name: str
    target_time_begin: datetime
    horizon_s: float
    prediction_delay_s: float
    predicted_arrival: datetime
    late_probability: Optional[float] = None
    segment: SegmentRef
    prediction_status: str
    suspected_reason: Optional[Reason] = None
    evidence: list[Evidence] = []
    recommendation: Optional[str] = None
    alert_prediction_delay_s: Optional[float] = None
    alert_target_stop_name: Optional[str] = None
    alert_target_time_begin: Optional[datetime] = None
    outcome_delay_s: Optional[float] = None


class Verified(BaseModel):
    as_of: datetime
    tr_id: TrId
    target_time_begin: datetime
    target_stop_name: str
    prediction_delay_s: float
    outcome_delay_s: float
    cur_dev_s: Optional[float] = None


class Metrics(BaseModel):
    now: datetime
    mode: Literal["replay", "live"]
    source: str
    ingest_status: Literal["ok", "degraded", "down"]
    model_version: str
    ml_status: str = Field(..., description="ok — прогнозы от ML-сервиса; fallback — ML недоступен")
    vehicles_live: int
    packets_per_min: int
    last_packet_at: Optional[datetime] = None
    inference_latency_ms_p50: Optional[float] = None
    inference_latency_ms_p95: Optional[float] = None
    queue_lag_s: float = 0.0
    reconnects: int = 0
    mae_live_s: Optional[float] = None
    mae_baseline_live_s: Optional[float] = None
    n_verified: int = 0
    n_verified_grid5: int = 0
    arrival_detector_mae_s: Optional[float] = Field(None, description="Ошибка детектора прибытий против факта (replay)")
    offline_eval: Optional[dict] = None
    replay_speed: Optional[float] = Field(None, description="Скорость часов воспроизведения (replay)")


class Config(BaseModel):
    thresholds: dict[str, float]
    horizon_s: list[int]
    predict_every_s: int
    model: dict
    replay: Optional[dict] = Field(None, description="Параметры воспроизведения: день, скорость, обрыв связи")
