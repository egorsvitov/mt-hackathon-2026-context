from typing import Any
from app.schemas.telemetry import RawNDTPRecord


def parse_ndtp_packet(raw_bytes: bytes) -> list[RawNDTPRecord]:
    """Точка подключения алгоритма десериализации бинарного NDTP от Паши.

    Принимает сырой байтовый буфер пакета, возвращает список валидированных записей.
    """
    # Заглушка до интеграции кода Паши
    return []


def parse_ndtp_dict(data: dict[str, Any]) -> RawNDTPRecord:
    """Вспомогательный метод для прямого разбора JSON/словаря от эмулятора."""
    return RawNDTPRecord(**data)