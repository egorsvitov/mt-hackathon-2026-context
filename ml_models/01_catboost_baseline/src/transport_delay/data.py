from __future__ import annotations

from pathlib import Path

import pandas as pd

POINT_COLUMNS = ["sample_id", "tr_id", "T", "target_stop_id", "target_time_begin", "cur_dev_s"]
SCHEDULE_COLUMNS = ["tt_action_item_id", "time_begin", "tr_id", "geom", "building_address"]


def read_points(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(
        path, usecols=lambda c: c in POINT_COLUMNS + ["target_delay_s", "target_class"]
    )
    frame["T"] = pd.to_datetime(frame["T"], errors="raise")
    frame["target_time_begin"] = pd.to_datetime(frame["target_time_begin"], errors="raise")
    frame["tr_id"] = frame["tr_id"].astype("int64")
    frame["target_stop_id"] = frame["target_stop_id"].astype("int64")
    return frame


def read_telemetry(path: Path) -> pd.DataFrame:
    columns = [
        "packet_id",
        "tr_id",
        "event_time",
        "location_valid",
        "lon",
        "lat",
        "speed",
        "heading",
    ]
    frame = pd.read_csv(path, usecols=columns, low_memory=False)
    frame["event_time"] = pd.to_datetime(frame["event_time"], errors="coerce")
    for column in ("lon", "lat", "speed", "heading"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["location_valid"] = frame["location_valid"].astype("boolean").fillna(False)
    frame = frame.dropna(subset=["event_time", "tr_id"])
    frame["tr_id"] = frame["tr_id"].astype("int64")
    frame = frame.drop_duplicates(["tr_id", "event_time", "packet_id"], keep="first")
    return frame.sort_values(["tr_id", "event_time"], kind="stable").reset_index(drop=True)


def read_schedule(path: Path) -> pd.DataFrame:
    # Deliberate whitelist: factual arrival time is never loaded by feature code.
    frame = pd.read_csv(path, usecols=SCHEDULE_COLUMNS, low_memory=False)
    frame["time_begin"] = pd.to_datetime(frame["time_begin"], errors="raise")
    frame["tr_id"] = frame["tr_id"].astype("int64")
    frame["tt_action_item_id"] = frame["tt_action_item_id"].astype("int64")
    coords = frame["geom"].str.extract(r"POINT \(([-+0-9.eE]+) ([-+0-9.eE]+)\)")
    frame["stop_lon"] = pd.to_numeric(coords[0], errors="coerce")
    frame["stop_lat"] = pd.to_numeric(coords[1], errors="coerce")
    return frame.sort_values(
        ["tr_id", "time_begin", "tt_action_item_id"], kind="stable"
    ).reset_index(drop=True)
