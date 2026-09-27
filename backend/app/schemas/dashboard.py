"""Ответы API для диспетчерского дашборда (контракт: dashboard/CONTRACT.md).

Времена — ISO 8601 со смещением (+03:00), задержки — секунды со знаком.
"""

from datetime import datetime
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field

TrId = Union[int, str]
Severity = Literal["ok", "warning", "critical", "early", "unknown"]


class Stop(BaseModel):
    stop_key: int = Field(
        ..., description="Физическая остановка (уникальная точка geom)"
    )
    lat: float
    lon: float
    name: str


class RouteSegment(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: int = Field(..., alias="from", description="stop_key начала участка")
    to: int
    synthetic: bool = Field(..., description="true — нет GPS-геометрии, прямой отрезок")
    path: list[list[float]] = Field(..., description="[[lat, lon], ...]")
    segment_id: str | None = None
    route_pattern_id: str | None = None
    source: str | None = None
    quality: str | None = None
    support: int | None = None
    length_m: float | None = None


class Route(BaseModel):
    route_id: str
    tr_id: TrId
    name: str
    speed_norm_kmh: float | None = None
    stops: list[int]
    segments: list[RouteSegment]
    reserve_of: TrId | None = Field(None, description="Резервный автобус линии этого ТС (мера диспетчера)")


class Network(BaseModel):
    stops: list[Stop]
    routes: list[Route]


class Visit(BaseModel):
    visit_id: int = Field(..., description="tt_action_item_id — плановое посещение")
    stop_key: int
    time_plan: datetime
    time_fact: datetime | None = Field(
        None, description="Прибытие по детектору; только уже произошедшие"
    )


class Schedule(BaseModel):
    tr_id: TrId
    visits: list[Visit]


class Vehicle(BaseModel):
    tr_id: TrId
    unit_id: int | None = None
    route_id: str | None = None
    event_time: datetime
    lat: float
    lon: float
    speed: float | None = None
    heading: float | None = None
    location_valid: bool = True
    data_age_s: float
    status: Literal["live", "stale", "offline"]
    source: str
    route_pattern_id: str | None = None
    position_quality: str | None = None
    off_route: bool | None = None
    reserve_of: TrId | None = Field(None, description="Резервный автобус, выпущенный на линию этого ТС")


class Reason(BaseModel):
    code: str
    title: str
    detail: str | None = None


class Evidence(BaseModel):
    code: str
    label: str
    value: float | None = None
    norm: float | None = None
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
    route_id: str | None = None
    as_of: datetime = Field(..., description="Момент прогноза T")
    generated_at: datetime
    target_stop_id: int
    target_stop_name: str
    target_time_begin: datetime
    horizon_s: float
    prediction_delay_s: float
    predicted_arrival: datetime
    late_probability: float | None = None
    cur_dev_s: float | None = None
    cur_dev_source: str = Field("detector", description="detector | none")
    severity: Severity
    status: Literal["model", "fallback", "stale"]
    model_version: str
    data_age_s: float | None = None
    segment: SegmentRef
    reason: Reason | None = None
    evidence: list[Evidence] = []
    recommendation: str | None = None


class Incident(BaseModel):
    incident_id: str
    tr_id: TrId
    route_id: str | None = None
    kind: Literal["late", "early"]
    status: Literal["active", "resolved"]
    first_detected_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    severity: Severity
    peak_severity: Severity
    target_stop_id: int
    target_stop_name: str
    target_time_begin: datetime
    horizon_s: float
    prediction_delay_s: float
    predicted_arrival: datetime
    late_probability: float | None = None
    segment: SegmentRef
    prediction_status: str
    suspected_reason: Reason | None = None
    evidence: list[Evidence] = []
    recommendation: str | None = None
    alert_prediction_delay_s: float | None = None
    alert_target_stop_name: str | None = None
    alert_target_time_begin: datetime | None = None
    outcome_delay_s: float | None = None


class Verified(BaseModel):
    sample_id: str | None = None
    as_of: datetime
    tr_id: TrId
    route_id: str | None = None
    target_time_begin: datetime
    target_stop_name: str
    prediction_delay_s: float
    outcome_delay_s: float
    cur_dev_s: float | None = None
    late_probability: float | None = None
    status: str | None = None
    severity: Severity | None = None
    reason_title: str | None = None


class Metrics(BaseModel):
    now: datetime
    mode: Literal["replay", "live"]
    source: str
    ingest_status: Literal["ok", "degraded", "down"]
    model_version: str
    ml_status: str = Field(
        ..., description="ok — прогнозы от ML-сервиса; fallback — ML недоступен"
    )
    vehicles_live: int
    packets_per_min: int
    last_packet_at: datetime | None = None
    inference_latency_ms_p50: float | None = None
    inference_latency_ms_p95: float | None = None
    queue_lag_s: float = 0.0
    reconnects: int = 0
    rejected_future: int = Field(
        0, description="Отброшено отметок новее часов системы больше чем на MAX_FUTURE_SKEW_SEC"
    )
    mae_live_s: float | None = None
    mae_baseline_live_s: float | None = None
    n_verified: int = 0
    n_verified_grid5: int = 0
    arrival_detector_mae_s: float | None = Field(
        None, description="Ошибка детектора прибытий против факта (replay)"
    )
    offline_eval: dict | None = None
    replay_speed: float | None = Field(
        None, description="Скорость часов воспроизведения (replay)"
    )


class Config(BaseModel):
    thresholds: dict[str, float]
    horizon_s: list[int]
    predict_every_s: int
    model: dict
    replay: dict | None = Field(
        None, description="Параметры воспроизведения: день, скорость, обрыв связи"
    )
