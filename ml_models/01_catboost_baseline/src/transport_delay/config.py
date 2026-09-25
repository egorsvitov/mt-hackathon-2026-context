from __future__ import annotations

import os
from pathlib import Path

WINDOW_SECONDS = (60, 180, 300, 600)
MAX_SPEED_KMH = 150.0
MAX_CONTIGUOUS_GAP_S = 60.0
PEER_MAX_AGE_S = 120.0
PEER_RADII_M = (300, 700)


def data_dir(value: str | Path | None = None) -> Path:
    raw = value or os.environ.get("DATA_DIR")
    if not raw:
        raise ValueError("DATA_DIR is not set; pass --data-dir or export DATA_DIR")
    path = Path(raw).expanduser().resolve()
    required = ("train", "test", "validate", "labels", "sample_submission.csv")
    missing = [name for name in required if not (path / name).exists()]
    if missing:
        raise ValueError(f"Invalid DATA_DIR {path}: missing {', '.join(missing)}")
    return path
