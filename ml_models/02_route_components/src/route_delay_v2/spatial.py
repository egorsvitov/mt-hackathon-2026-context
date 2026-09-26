from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from route_matching import Catalog
from route_matching.data import read_events
from route_matching.matcher import Matcher
from route_matching.types import MatchState, timestamp

from .config import PEER_MAX_AGE_S, TIMEZONE

# Explicit whitelist: no full-day duration, support or confirmation statistic is exposed.
SPATIAL_NUMERIC = (
    "segment_progress",
    "route_progress",
    "remaining_distance_m",
    "remaining_segments",
    "remaining_stops",
    "speed_mps",
    "speed_1m_mps",
    "speed_3m_mps",
    "speed_5m_mps",
    "route_distance_m",
    "confidence_margin",
    "gps_age_s",
    "planned_segment_time_s",
)
SPATIAL_CATEGORICAL = (
    "route_pattern_id",
    "direction_id",
    "current_segment_id",
    "target_segment_id",
    "mode",
    "position_quality",
)
FORBIDDEN_CATALOG_FEATURES = frozenset(
    {"historical_segment_time_s", "estimated_remaining_time_s", "segment_support"}
)


def _epoch(value: pd.Timestamp) -> float:
    return timestamp(value.to_pydatetime(), TIMEZONE)


def _static_row(state: MatchState, point, catalog: Catalog) -> dict:
    row = {f"mm_{name}": getattr(state, name) for name in SPATIAL_NUMERIC}
    row.update(
        {
            f"mm_{name}": getattr(state, name) or "__unknown__"
            for name in SPATIAL_CATEGORICAL
        }
    )
    row.update(
        mm_matched=float(state.mode != "unmatched"),
        mm_off_route=np.nan if state.off_route is None else float(state.off_route),
        mm_sequence_id=state.sequence_id or "__unknown__",
        mm_trip_occurrence_id=state.trip_occurrence_id or "__unknown__",
        mm_current_leg_index=(state.visit_index - 1) if state.visit_index is not None else np.nan,
        mm_position_delay_s=np.nan,
        mm_progress_deviation=np.nan,
    )
    if state.sequence_id and state.segment_progress is not None:
        sequence = next((s for s in catalog.sequences if s.sequence_id == state.sequence_id), None)
        leg_index = (state.visit_index - 1) if state.visit_index is not None else None
        if sequence is not None and leg_index is not None and 0 <= leg_index < len(sequence.legs):
            leg = sequence.legs[leg_index]
            scheduled_position = leg.start.time + state.segment_progress * (
                leg.end.time - leg.start.time
            )
            row["mm_position_delay_s"] = state.T - scheduled_position
            planned_now = state.T - float(point.cur_dev_s)
            duration = max(leg.end.time - leg.start.time, 1.0)
            planned_fraction = np.clip((planned_now - leg.start.time) / duration, 0.0, 1.0)
            row["mm_progress_deviation"] = state.segment_progress - planned_fraction
    for name in SPATIAL_NUMERIC:
        row[f"mm_{name}_missing"] = float(row[f"mm_{name}"] is None)
    return row


def _peer_row(state: MatchState, latest: dict[int, MatchState]) -> dict[str, float]:
    peers = [
        other
        for tr_id, other in latest.items()
        if tr_id != state.tr_id
        and state.current_segment_id
        and other.current_segment_id == state.current_segment_id
        and other.max_event_time is not None
        and state.T - other.max_event_time <= PEER_MAX_AGE_S
    ]
    speeds = np.asarray(
        [p.speed_3m_mps for p in peers if p.speed_3m_mps is not None], dtype=float
    )
    return {
        "segment_peers_count": float(len(peers)),
        "segment_peers_speed_median_mps": float(np.median(speeds)) if len(speeds) else np.nan,
        "segment_peers_speed_std_mps": float(np.std(speeds)) if len(speeds) else np.nan,
        "segment_peers_age_max_s": (
            float(max(state.T - p.max_event_time for p in peers)) if peers else np.nan
        ),
    }


def build_spatial_features(
    points: pd.DataFrame, traffic_path: Path, catalog: Catalog
) -> pd.DataFrame:
    """Replay the prefix available at every T and expose only static/current-state fields."""
    wanted = set(points["tr_id"].astype(int))
    events = [e for e in read_events(traffic_path, TIMEZONE) if e.tr_id in wanted]
    events.sort(key=lambda event: (event.event_time, event.packet_id))
    ordered = points.sort_values(["T", "sample_id"], kind="stable")
    matcher = Matcher(catalog)
    latest: dict[int, MatchState] = {}
    rows = []
    event_index = 0
    try:
        for point in ordered.itertuples(index=False):
            T = _epoch(point.T)
            while event_index < len(events) and events[event_index].event_time <= T:
                event = events[event_index]
                latest[event.tr_id] = matcher.update(event)
                event_index += 1
            state = matcher.snapshot(
                int(point.tr_id), T, str(point.target_stop_id), float(point.cur_dev_s)
            )
            row = {"sample_id": point.sample_id, **_static_row(state, point, catalog)}
            row.update(_peer_row(state, latest))
            assignment = catalog.assignment_by_visit.get(str(point.target_stop_id))
            row["mm_target_stop_index"] = assignment.stop_index if assignment else np.nan
            rows.append(row)
    finally:
        matcher.close()
    result = pd.DataFrame(rows)
    return points[["sample_id"]].merge(result, on="sample_id", how="left", validate="one_to_one")


def spatial_feature_columns(frame: pd.DataFrame) -> tuple[list[str], list[str]]:
    categorical = [f"mm_{name}" for name in SPATIAL_CATEGORICAL]
    numeric = [
        c
        for c in frame
        if c.startswith(("mm_", "segment_peers_"))
        and c not in categorical
        and c not in {"mm_sequence_id", "mm_trip_occurrence_id"}
    ]
    return numeric, [c for c in categorical if c in frame]


def assert_no_temporal_catalog_features(columns) -> None:
    leaked = {c.removeprefix("mm_") for c in columns} & FORBIDDEN_CATALOG_FEATURES
    if leaked:
        raise AssertionError(f"Temporal catalog features are forbidden: {sorted(leaked)}")

