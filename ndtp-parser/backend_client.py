import os

import requests


BACKEND_URL = os.getenv(
    "BACKEND_URL",
    "http://localhost:8000/api/v1/stream/telemetry",
)


def send_telemetry(traffic_row) -> None:
    """Отправляет разобранную телеметрию в backend."""
    location_valid = traffic_row.location_valid
    data = {
        "tr_id": str(traffic_row.tr_id),
        "timestamp": int(traffic_row.event_time.timestamp()),
        "unit_id": traffic_row.unit_id,
        "lat": traffic_row.lat if location_valid else None,
        "lon": traffic_row.lon if location_valid else None,
        "speed": traffic_row.speed,
        "heading": traffic_row.heading,
        "location_valid": location_valid,
        "source": "ndtp",
    }

    try:
        response = requests.post(BACKEND_URL, json=data, timeout=2)
        response.raise_for_status()
    except requests.RequestException as error:
        print(f"Не удалось отправить телеметрию в backend: {error}")
