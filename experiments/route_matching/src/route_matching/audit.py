from __future__ import annotations

import csv
import hashlib
from pathlib import Path

from .types import timestamp


def dataset_audit(data: Path, timezone_name: str) -> dict:
    train_schedule = list(csv.DictReader((data / "train/schedule.csv").open()))
    test_schedule = list(csv.DictReader((data / "test/schedule.csv").open()))
    train_visits = {(r["tt_action_item_id"], r["tr_id"], r["time_begin"][:19], r["geom"])
                    for r in train_schedule}
    test_visits = {(r["tt_action_item_id"], r["tr_id"], r["time_begin"][:19], r["geom"])
                   for r in test_schedule}
    with (data / "test/traffic.csv").open("rb") as stream:
        test_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    with (data / "validate/traffic.csv").open("rb") as stream:
        validate_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    point_paths = (data / "labels/labels_test.csv", data / "validate/points.csv")
    points = [r for path in point_paths for r in csv.DictReader(path.open())]
    cutoff_text = min(r["T"] for r in points)
    cutoff = timestamp(cutoff_text, timezone_name)
    prior_plan = sum(timestamp(r["time_begin"], timezone_name) < cutoff for r in train_schedule
                     if int(r["tr_id"]) < 9_000_000)
    return {
        "test_schedule_rows": len(test_schedule),
        "test_visits_already_in_train": len(test_visits & train_visits),
        "test_schedule_fully_in_train": test_visits <= train_visits,
        "test_validate_traffic_identical": test_hash == validate_hash,
        "test_traffic_sha256": test_hash,
        "cutoff_text": cutoff_text,
        "cutoff_epoch": cutoff,
        "real_planned_visits_before_cutoff": prior_plan,
    }
