import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from backend_client import send_telemetry
from protocol import ProtocolError, parse_frame
from receiver import run_server
from traffic_adapter import TrafficAdapter, UnknownUnitError, load_unit_mapping


HOST = "0.0.0.0"
PORT = 9201
MAPPING_PATH = Path(__file__).with_name("unit_mapping.json")


def print_all_fields(value) -> None:
    """Печатает все поля dataclass в JSON."""
    data = asdict(value)
    for field, field_value in data.items():
        if isinstance(field_value, datetime):
            data[field] = field_value.isoformat()
    print(json.dumps(data, ensure_ascii=False))


def handle_frame(
    frame: bytes,
    receive_time: datetime,
    adapter: TrafficAdapter,
) -> None:
    """Разбирает NDTP-кадр и отправляет телеметрию в backend."""
    try:
        telemetry = parse_frame(frame, receive_time)
    except ProtocolError as error:
        print(f"Кадр отброшен: {error}")
        return

    if telemetry is None:
        print("Получен handshake")
        return

    try:
        traffic_row = adapter.convert(telemetry)
    except UnknownUnitError as error:
        print(error)
        print_all_fields(telemetry)
        return

    print_all_fields(traffic_row)
    send_telemetry(traffic_row)


def main() -> None:
    """Загружает соответствие unit_id и tr_id и запускает TCP-сервер."""
    mapping = load_unit_mapping(MAPPING_PATH)
    adapter = TrafficAdapter(mapping)

    def on_frame(frame: bytes, receive_time: datetime) -> None:
        """Обрабатывает один кадр от устройства."""
        handle_frame(frame, receive_time, adapter)

    run_server(HOST, PORT, on_frame)


if __name__ == "__main__":
    main()
