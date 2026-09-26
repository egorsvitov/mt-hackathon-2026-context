from __future__ import annotations

import os
from pathlib import Path

from transport_delay.config import data_dir

TIMEZONE = "Europe/Moscow"
STOP_INNER_M = 40.0
STOP_OUTER_M = 60.0
STOPPED_KMH = 3.0
MAX_EVENT_GAP_S = 45.0
PEER_MAX_AGE_S = 120.0
EXPERT_MIN_EVENTS = 100
EXPERT_MIN_TRIPS = 3
EXPERT_SHRINKAGE = 100.0


def catalog_path(value: str | Path | None = None) -> Path:
    raw = value or os.environ.get("ROUTE_CATALOG_PATH")
    if not raw:
        raise ValueError(
            "ROUTE_CATALOG_PATH is not set; pass --catalog or export ROUTE_CATALOG_PATH"
        )
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Route catalog does not exist: {path}")
    return path


__all__ = ["catalog_path", "data_dir"]

