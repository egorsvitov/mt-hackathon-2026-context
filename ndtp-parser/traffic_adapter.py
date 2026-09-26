import json
import time
from pathlib import Path
from threading import Lock

from schemas import Telemetry, TrafficRow


class UnknownUnitError(ValueError):
    """Для unit_id не найден соответствующий tr_id."""


def load_unit_mapping(path: str | Path) -> dict[int, int]:
    """Читает JSON-словарь вида {"unit_id": tr_id}."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {int(unit_id): int(tr_id) for unit_id, tr_id in raw.items()}


class PacketIdGenerator:
    """Генерирует уникальные packet_id внутри процесса."""

    def __init__(self) -> None:
        self._next_id = time.time_ns()
        self._lock = Lock()

    def next(self) -> int:
        with self._lock:
            packet_id = self._next_id
            self._next_id += 1
            return packet_id


class TrafficAdapter:
    """Преобразует разобранную телеметрию в схему train/traffic.csv."""

    def __init__(self, unit_to_tr_id: dict[int, int]) -> None:
        self._unit_to_tr_id = unit_to_tr_id
        self._packet_ids = PacketIdGenerator()

    def convert(self, telemetry: Telemetry) -> TrafficRow:
        tr_id = self._unit_to_tr_id.get(telemetry.unit_id)
        if tr_id is None:
            raise UnknownUnitError(
                f"Нет tr_id для unit_id={telemetry.unit_id}"
            )

        return TrafficRow(
            packet_id=self._packet_ids.next(),
            tr_id=tr_id,
            unit_id=telemetry.unit_id,
            event_time=telemetry.event_time,
            device_event_id=0,
            location_valid=telemetry.location_valid,
            gps_time=telemetry.event_time,
            lon=float(telemetry.lon),
            lat=float(telemetry.lat),
            alt=float(telemetry.alt),
            speed=float(telemetry.speed),
            heading=float(telemetry.heading),
            receive_time=telemetry.receive_time,
            is_hist_data=False,
        )
