from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class NplHeader:
    data_size: int
    crc: int
    packet_type: int
    unit_id: int
    request_id: int


@dataclass(frozen=True)
class NphHeader:
    service_id: int
    message_type: int
    request_id: int


@dataclass(frozen=True)
class Telemetry:
    unit_id: int
    event_time: datetime
    receive_time: datetime
    location_valid: bool
    lon: float
    lat: float
    alt: int
    speed: int
    heading: int


@dataclass(frozen=True)
class TrafficRow:
    """Строка в формате traffic.csv из датасета."""

    packet_id: int
    tr_id: int
    unit_id: int
    event_time: datetime
    device_event_id: int
    location_valid: bool
    gps_time: datetime
    lon: float
    lat: float
    alt: float
    speed: float
    heading: float
    receive_time: datetime
    is_hist_data: bool
