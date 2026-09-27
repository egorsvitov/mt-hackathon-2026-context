"""Frozen route-v2 snapshot feature formulas; no telemetry-fitted catalog statistics."""
import numpy as np
from route_matching import Catalog
from route_matching.types import MatchState

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
