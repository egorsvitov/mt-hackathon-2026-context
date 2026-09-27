"""Схемы ответов API для дашборда, подробнее в dashboard/CONTRACT.md.

Время в формате ISO 8601 с зоной +03:00, задержки в секундах со знаком.
"""

from datetime import datetime
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field

TrId = Union[int, str]
Severity = Literal["ok", "warning", "critical", "early", "unknown"]


class Stop(BaseModel):
    """Остановка."""
    stop_key: int = Field(
        ..., description="Физическая остановка (уникальная точка geom)"
    )
    lat: float
    lon: float
    name: str


class RouteSegment(BaseModel):
    """Участок маршрута между двумя остановками."""
    model_config = ConfigDict(populate_by_name=True)

    from_: int = Field(..., alias="from", description="stop_key начала участка")
    to: int
    synthetic: bool = Field(..., description="true, если GPS-геометрии нет и участок нарисован прямой")
    path: list[list[float]] = Field(..., description="[[lat, lon], ...]")
    segment_id: str | None = None
    route_pattern_id: str | None = None
    source: str | None = None
    quality: str | None = None
    support: int | None = None
    length_m: float | None = None


class Route(BaseModel):
    """Маршрут одного ТС: остановки и участки."""
    route_id: str
    tr_id: TrId
    name: str
    speed_norm_kmh: float | None = None
    stops: list[int]
    segments: list[RouteSegment]


class Network(BaseModel):
    """Все остановки и маршруты."""
    stops: list[Stop]
    routes: list[Route]


class Visit(BaseModel):
    """Плановое посещение остановки и фактическое время, если детектор его засёк."""
    visit_id: int = Field(..., description="Плановое посещение, tt_action_item_id из датасета")
    stop_key: int
    time_plan: datetime
    time_fact: datetime | None = Field(
        None, description="Прибытие по детектору; только уже произошедшие"
    )


class Schedule(BaseModel):
    """Плановые посещения одного ТС."""
    tr_id: TrId
    visits: list[Visit]


class Vehicle(BaseModel):
    """Последнее положение ТС."""
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


class Reason(BaseModel):
    """Вероятная причина отклонения."""
    code: str
    title: str
    detail: str | None = None


class Evidence(BaseModel):
    """Признак, на который опирается причина, и его обычное значение."""
    code: str
    label: str
    value: float | None = None
    norm: float | None = None
    unit: str
    flag: bool = False


class SegmentRef(BaseModel):
    """Участок от последней пройденной остановки до целевой."""
    from_stop_id: int
    from_stop_name: str
    to_stop_id: int
    to_stop_name: str


class Prediction(BaseModel):
    """Прогноз отклонения ТС на остановке через 10-15 минут."""
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
    """Инцидент: эпизод риска по ТС."""
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
    """Прогноз, сверенный с фактическим прибытием."""
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
    """Состояние системы: поток, задержка обработки, точность."""
    now: datetime
    mode: Literal["replay", "live"]
    source: str
    ingest_status: Literal["ok", "degraded", "down"]
    model_version: str
    ml_status: str = Field(
        ..., description="ok, если прогнозы идут от ML-сервиса, fallback, если он недоступен"
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
    """Пороги, горизонт и параметры модели для дашборда."""
    thresholds: dict[str, float]
    horizon_s: list[int]
    predict_every_s: int
    model: dict
    replay: dict | None = Field(
        None, description="Параметры воспроизведения: день, скорость, обрыв связи"
    )
