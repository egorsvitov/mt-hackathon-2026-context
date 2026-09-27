from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from route_delay_v2.spatial import _static_row
from route_matching import Catalog
from route_matching.matcher import Matcher
from route_matching.types import Event, timestamp
from transport_delay.data import read_points, read_schedule
from transport_delay.features import META_COLUMNS, build_features, feature_columns

TIMEZONE = "Europe/Moscow"
KEY = ["tr_id", "event_time", "packet_id"]
TRAFFIC_COLUMNS = (
    "packet_id",
    "tr_id",
    "event_time",
    "receive_time",
    "location_valid",
    "lon",
    "lat",
    "speed",
    "heading",
)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_traffic_union(data_dir: Path) -> tuple[pd.DataFrame, dict]:
    parts = []
    sources = {}
    for split in ("train", "test", "validate"):
        path = data_dir / split / "traffic.csv"
        frame = pd.read_csv(
            path,
            usecols=lambda name: name in TRAFFIC_COLUMNS,
            dtype={"packet_id": "string"},
            low_memory=False,
        )
        frame["source_split"] = split
        parts.append(frame)
        sources[str(path)] = file_hash(path)
    traffic = pd.concat(parts, ignore_index=True)
    traffic["tr_id"] = pd.to_numeric(traffic["tr_id"], errors="raise").astype("int64")
    traffic["packet_id"] = traffic["packet_id"].fillna("").astype(str)
    for name in ("event_time", "receive_time"):
        if name not in traffic:
            traffic[name] = pd.NaT
        traffic[name] = pd.to_datetime(traffic[name], errors="coerce")
    if traffic["event_time"].isna().any():
        raise ValueError("Telemetry contains an invalid event_time")
    for name in ("lon", "lat", "speed", "heading"):
        traffic[name] = pd.to_numeric(traffic[name], errors="coerce")
    traffic["location_valid"] = traffic["location_valid"].astype(str).str.lower() == "true"
    payload = ["receive_time", "location_valid", "lon", "lat", "speed", "heading"]
    duplicates = traffic.duplicated(KEY, keep=False)
    if duplicates.any():
        counts = traffic.loc[duplicates].groupby(KEY, dropna=False)[payload].nunique(dropna=False)
        conflicting = counts.gt(1).any(axis=1)
        if conflicting.any():
            raise ValueError(
                f"Conflicting telemetry payloads for {int(conflicting.sum())} duplicate keys"
            )
    before = len(traffic)
    traffic = traffic.drop_duplicates(KEY, keep="first").copy()
    traffic["available_at"] = traffic[["event_time", "receive_time"]].max(axis=1)
    traffic = traffic.sort_values(
        ["available_at", "event_time", "tr_id", "packet_id"], kind="stable"
    ).reset_index(drop=True)
    return traffic, {
        "input_rows": before,
        "unique_rows": len(traffic),
        "duplicates_removed": before - len(traffic),
        "conflicting_keys": 0,
        "source_sha256": sources,
    }


def read_public_schedule(data_dir: Path) -> tuple[pd.DataFrame, dict]:
    paths = [
        data_dir / "train/schedule.csv",
        data_dir / "test/schedule.csv",
        data_dir / "validate/schedule_plan.csv",
    ]
    parts = [read_schedule(path) for path in paths]
    full = pd.concat(parts, ignore_index=True)
    key = "tt_action_item_id"
    payload = ["tr_id", "time_begin", "geom", "building_address"]
    duplicated = full.duplicated(key, keep=False)
    if duplicated.any():
        inconsistent = (
            full.loc[duplicated].groupby(key)[payload].nunique(dropna=False).gt(1).any(axis=1)
        )
        if inconsistent.any():
            raise ValueError("Conflicting public schedule records")
    return full.drop_duplicates(key).reset_index(drop=True), {
        str(path): file_hash(path) for path in paths
    }


def _epoch(value: pd.Timestamp) -> float:
    return timestamp(value.to_pydatetime(), TIMEZONE)


def _event(row) -> Event:
    return Event(
        int(row.tr_id),
        _epoch(row.event_time),
        None if pd.isna(row.lon) else float(row.lon),
        None if pd.isna(row.lat) else float(row.lat),
        None if pd.isna(row.speed) else float(row.speed),
        None if pd.isna(row.heading) else float(row.heading),
        bool(row.location_valid),
        str(row.packet_id),
        _epoch(row.available_at),
    )


def build_point_features(
    points: pd.DataFrame, traffic: pd.DataFrame, schedule: pd.DataFrame, catalog: Catalog
) -> pd.DataFrame:
    # The baseline engine uses event_time as its censoring clock. Replacing it with
    # availability is conservative for delayed packets and never admits future data.
    visible = traffic[
        ["packet_id", "tr_id", "available_at", "location_valid", "lon", "lat", "speed", "heading"]
    ].rename(columns={"available_at": "event_time"})
    base = build_features(
        points.drop(columns=["target_delay_s", "target_class"], errors="ignore"), visible, schedule
    )
    base = base[list(dict.fromkeys(META_COLUMNS + feature_columns(base, ("geography",))))]
    known = {s.tr_id for s in catalog.sequences}
    gps = traffic[traffic.tr_id.isin(known)].copy()
    gps["available_at"] = pd.to_datetime(gps["available_at"])
    gps["bucket"] = gps.available_at.dt.floor("15s")
    selected = set(
        gps.sort_values(["tr_id", "available_at", "event_time"])
        .groupby(["tr_id", "bucket"], sort=False)
        .tail(1)
        .index
    )
    for vehicle, group in gps.groupby("tr_id"):
        times = points.loc[points.tr_id == vehicle, "T"].to_numpy(dtype="datetime64[ns]")
        if not len(times):
            continue
        ordered_gps = group.sort_values("available_at", kind="stable")
        available = ordered_gps.available_at.to_numpy(dtype="datetime64[ns]")
        indices = np.searchsorted(available, times, side="right") - 1
        selected.update(ordered_gps.index[indices[indices >= 0]].tolist())
    gps = gps.loc[sorted(selected)]
    events = [_event(row) for row in gps.itertuples(index=False)]
    events.sort(key=lambda e: (e.available_time, e.event_time, e.tr_id, e.packet_id))
    ordered = points.sort_values(["T", "sample_id"], kind="stable")
    matcher = Matcher(catalog)
    latest = {}
    last_seen = {}
    stopped_since = {}
    sequence_by_id = {s.sequence_id: s for s in catalog.sequences}
    availability = traffic.available_at.to_numpy(dtype="datetime64[ns]")
    availability.sort()
    rows = []
    cursor = 0
    try:
        for point in ordered.itertuples(index=False):
            now = _epoch(point.T)
            while cursor < len(events) and events[cursor].available_time <= now:
                event = events[cursor]
                previous = last_seen.get(event.tr_id)
                if event.event_time <= now and (previous is None or event.event_time > previous):
                    state_at_event = matcher.update(event)
                    latest[event.tr_id] = state_at_event
                    sequence = sequence_by_id.get(state_at_event.sequence_id)
                    if previous is not None and event.event_time - previous > 45:
                        stopped_since.pop(event.tr_id, None)
                    last_seen[event.tr_id] = event.event_time
                    if (
                        sequence is not None
                        and state_at_event.visit_index is not None
                        and event.valid
                        and event.speed is not None
                        and event.speed <= 3
                    ):
                        anchor = sequence.stops[max(0, state_at_event.visit_index - 1)]
                        from route_matching.geo import distance

                        if distance(event.coord, anchor.coord) <= 40:
                            stopped_since.setdefault(event.tr_id, event.event_time)
                        else:
                            stopped_since.pop(event.tr_id, None)
                    else:
                        stopped_since.pop(event.tr_id, None)
                cursor += 1
            state = matcher.snapshot(
                int(point.tr_id), now, str(point.target_stop_id), float(point.cur_dev_s)
            )
            row = _static_row(state, point, catalog)
            assignment = catalog.assignment_by_visit.get(str(point.target_stop_id))
            row["mm_target_stop_index"] = assignment.stop_index if assignment else np.nan
            row["mm_trip_occurrence_id"] = (
                assignment.trip_occurrence_id if assignment else "__unknown__"
            )
            row["mm_edge_id"] = state.edge_id or "__unknown__"
            last = last_seen.get(int(point.tr_id))
            start = stopped_since.get(int(point.tr_id))
            row["mm_current_dwell_elapsed_s"] = (
                min(900.0, now - start)
                if start is not None and last is not None and now - last <= 45
                else np.nan
            )
            peers = [
                other
                for vehicle, other in latest.items()
                if vehicle != int(point.tr_id)
                and state.edge_id is not None
                and other.edge_id == state.edge_id
                and other.max_event_time is not None
                and now - other.max_event_time <= 900
            ]
            speeds = [p.speed_3m_mps for p in peers if p.speed_3m_mps is not None]
            row["segment_peers_count"] = float(len(peers))
            row["segment_peers_speed_mps"] = float(np.median(speeds)) if speeds else np.nan
            visible_index = int(np.searchsorted(availability, np.datetime64(point.T), side="right"))
            row["max_dynamic_available_at"] = (
                pd.Timestamp(availability[visible_index - 1]) if visible_index else pd.NaT
            )
            rows.append({"sample_id": point.sample_id, **row})
    finally:
        matcher.close()
    spatial = pd.DataFrame(rows)
    return base.merge(spatial, on="sample_id", how="left", validate="one_to_one")


def event_coverage(events: pd.DataFrame) -> dict:
    if events.empty:
        return {"rows": 0, "patterns": 0, "trips": 0, "pass_through_fraction": None}
    return {
        "rows": len(events),
        "patterns": int(events.route_pattern_id.nunique()),
        "trips": int(events.trip_occurrence_id.nunique()),
        "pass_through_fraction": (
            float(events.pass_through.mean()) if "pass_through" in events else None
        ),
        "max_gap_s_quantiles": {
            str(q): float(events.max_gap_s.quantile(q)) for q in (0.5, 0.9, 0.99)
        },
        "boundary_sources": events.boundary_source.value_counts().to_dict(),
    }


def prepare(data_dir: Path, catalog_path: Path, output: Path) -> dict:
    from .events import build_events

    catalog = Catalog.load(catalog_path)
    if catalog.provenance.get("policy") != "static_plan_graph":
        raise ValueError("v3 requires a static_plan_graph catalog")
    output.mkdir(parents=True, exist_ok=True)
    traffic, traffic_report = read_traffic_union(data_dir)
    schedule, schedule_hashes = read_public_schedule(data_dir)
    definitions = {
        "train": data_dir / "labels/labels_train.csv",
        "test": data_dir / "labels/labels_test.csv",
        "validate": data_dir / "validate/points.csv",
    }
    points = []
    for split, path in definitions.items():
        frame = read_points(path)
        frame["split"] = split
        points.append(frame)
    all_points = pd.concat(points, ignore_index=True)
    if all_points.sample_id.duplicated().any():
        raise ValueError("Duplicate sample_id across point splits")
    features = build_point_features(all_points, traffic, schedule, catalog)
    features = features.merge(
        all_points[
            ["sample_id", "split"] + (["target_delay_s"] if "target_delay_s" in all_points else [])
        ],
        on="sample_id",
        how="left",
        validate="one_to_one",
    )
    if "target_delay_s_x" in features:
        features["target_delay_s"] = features.pop("target_delay_s_y")
        features = features.drop(columns=["target_delay_s_x"])
    features["label_available_at"] = (
        features["target_time_begin"]
        + pd.to_timedelta(features["target_delay_s"], unit="s")
        + pd.Timedelta(seconds=60)
    )
    features.loc[features.split == "validate", "label_available_at"] = pd.NaT
    runs, dwells = build_events(traffic, catalog)
    features.to_parquet(output / "points.parquet", index=False)
    features.loc[
        features.split.isin(["train", "test"]),
        ["sample_id", "split", "T", "mm_trip_occurrence_id", "label_available_at"],
    ].sort_values(["label_available_at", "sample_id"], kind="stable").to_parquet(
        output / "label_disclosure.parquet", index=False
    )
    runs.to_parquet(output / "run_events.parquet", index=False)
    dwells.to_parquet(output / "dwell_events.parquet", index=False)
    report = {
        "catalog_version": catalog.version,
        "graph_version": catalog.graph_version,
        "traffic": traffic_report,
        "schedule_sha256": schedule_hashes,
        "point_sha256": {split: file_hash(path) for split, path in definitions.items()},
        "points": {split: int((features.split == split).sum()) for split in definitions},
        "matched": {
            split: int(features.loc[features.split == split, "mm_matched"].sum())
            for split in definitions
        },
        "run_events": len(runs),
        "dwell_events": len(dwells),
        "event_coverage": {"run": event_coverage(runs), "dwell": event_coverage(dwells)},
        "matcher_sampling_s": 15,
        "late_packet_policy": "ignore packets older than latest HMM event_time",
    }
    (output / "prepare_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n"
    )
    manifest = {
        "catalog_sha256": file_hash(catalog_path),
        "catalog_version": catalog.version,
        "graph_version": catalog.graph_version,
        "source_sha256": {
            **traffic_report["source_sha256"],
            **schedule_hashes,
            **report["point_sha256"],
        },
        "schemas": {
            "points": {name: str(dtype) for name, dtype in features.dtypes.items()},
            "run_events": {name: str(dtype) for name, dtype in runs.dtypes.items()},
            "dwell_events": {name: str(dtype) for name, dtype in dwells.dtypes.items()},
        },
        "causal_policy": {
            "catalog": "planned stops and pinned road graph only",
            "forbidden_catalog_fields": [
                "median_duration_s",
                "estimated_remaining_time_s",
                "segment_support",
                "historical GPS and trace support",
            ],
            "telemetry": "available_at <= T",
            "label": "actual target arrival + 60 seconds",
            "component_event": "all confirming GPS packets received",
            "validate_facts": "never loaded",
            "matcher_sampling_s": 15,
        },
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return report
