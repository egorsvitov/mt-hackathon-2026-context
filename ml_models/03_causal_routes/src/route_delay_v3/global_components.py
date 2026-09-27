from __future__ import annotations

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor
from route_matching import Catalog

from .components import add_components

RUN_COLUMNS = ("route_pattern_id", "segment_id", "planned_interval_s", "segment_length_m", "hour")
DWELL_COLUMNS = ("route_pattern_id", "stop_id", "stop_index", "hour")
CACHE: dict[tuple, tuple[CatBoostRegressor | None, CatBoostRegressor | None]] = {}


def _matrix(frame: pd.DataFrame, columns: tuple[str, ...]) -> pd.DataFrame:
    result = frame.reindex(columns=columns).copy()
    for column in columns:
        if column.endswith("_id"):
            result[column] = result[column].fillna("__unknown__").astype(str)
        else:
            result[column] = pd.to_numeric(result[column], errors="coerce")
    return result


def _fit(frame: pd.DataFrame, columns: tuple[str, ...], target: str):
    if len(frame) < 100 or frame.trip_occurrence_id.nunique() < 5:
        return None
    train = frame.copy()
    train["hour"] = train.event_time.dt.hour
    categorical = [column for column in columns if column.endswith("_id")]
    model = CatBoostRegressor(
        loss_function="MAE",
        iterations=150,
        learning_rate=0.03,
        depth=4,
        l2_leaf_reg=30,
        random_seed=42,
        thread_count=4,
        allow_writing_files=False,
        verbose=False,
    )
    model.fit(_matrix(train, columns), train[target].to_numpy(float), cat_features=categorical)
    return model


def _models(
    runs: pd.DataFrame,
    dwells: pd.DataFrame,
    hour: pd.Timestamp,
    excluded_trips: set[str],
):
    key = (id(runs), id(dwells), frozenset(excluded_trips), hour)
    if key not in CACHE:
        past_run = runs[(runs.available_at < hour) & ~runs.trip_occurrence_id.isin(excluded_trips)]
        past_dwell = dwells[
            (dwells.available_at < hour) & ~dwells.trip_occurrence_id.isin(excluded_trips)
        ]
        CACHE[key] = (
            _fit(past_run, RUN_COLUMNS, "run_time_s"),
            _fit(past_dwell, DWELL_COLUMNS, "dwell_time_s"),
        )
    return CACHE[key]


def add_global_components(
    frame: pd.DataFrame,
    runs: pd.DataFrame,
    dwells: pd.DataFrame,
    catalog: Catalog,
    excluded_trips: set[str] | None = None,
) -> pd.DataFrame:
    excluded_trips = excluded_trips or set()
    result = add_components(frame, runs, dwells, catalog, excluded_trips)
    result["component_global_model"] = 0.0
    sequences = {sequence.sequence_id: sequence for sequence in catalog.sequences}
    assignments = catalog.assignment_by_visit
    for hour, index in result.groupby(result["T"].dt.floor("h")).groups.items():
        run_model, dwell_model = _models(runs, dwells, hour, excluded_trips)
        if run_model is None or dwell_model is None:
            continue
        for i in index:
            point = result.loc[i]
            sequence = sequences.get(point.mm_sequence_id)
            assignment = assignments.get(str(point.target_stop_id))
            if sequence is None or assignment is None:
                continue
            try:
                current = int(point.mm_current_leg_index)
                target = assignment.stop_index
            except (TypeError, ValueError):
                continue
            if (
                current < 0
                or target <= current
                or target >= len(sequence.stops)
                or assignment.trip_occurrence_id != sequence.trip_occurrence_id
            ):
                continue
            run_rows = []
            dwell_rows = []
            for leg in sequence.legs[current:target]:
                variants = catalog.variants.get(leg.segment_id, ())
                run_rows.append(
                    {
                        "route_pattern_id": sequence.route_pattern_id,
                        "segment_id": leg.segment_id,
                        "planned_interval_s": max(0.0, leg.end.time - leg.start.time),
                        "segment_length_m": variants[0].path.length_m if variants else np.nan,
                        "hour": point["T"].hour,
                    }
                )
                if leg.index + 1 < target:
                    stop = sequence.stops[leg.index + 1]
                    dwell_rows.append(
                        {
                            "route_pattern_id": sequence.route_pattern_id,
                            "stop_id": stop.stop_id,
                            "stop_index": leg.index + 1,
                            "hour": point["T"].hour,
                        }
                    )
            runs_pred = np.clip(
                run_model.predict(_matrix(pd.DataFrame(run_rows), RUN_COLUMNS)), 0, 3600
            )
            if pd.notna(point.mm_segment_progress):
                runs_pred[0] *= max(0.0, 1 - min(1.0, float(point.mm_segment_progress)))
            dwell_sum = 0.0
            if dwell_rows:
                dwell_sum = float(
                    np.clip(
                        dwell_model.predict(_matrix(pd.DataFrame(dwell_rows), DWELL_COLUMNS)),
                        0,
                        900,
                    ).sum()
                )
            elapsed = point.get("mm_current_dwell_elapsed_s", np.nan)
            if pd.notna(elapsed):
                stop_id = sequence.stops[current].stop_id
                past = dwells[
                    (dwells.available_at < point["T"])
                    & ~dwells.trip_occurrence_id.isin(
                        excluded_trips | {sequence.trip_occurrence_id}
                    )
                    & (dwells.stop_id == stop_id)
                    & (dwells.dwell_time_s >= elapsed)
                ]
                if len(past):
                    dwell_sum += float(np.median(past.dwell_time_s - elapsed))
            eta = float(runs_pred.sum()) + dwell_sum
            result.loc[i, "component_eta_s"] = eta
            result.loc[i, "component_delay_s"] = eta - float(point.horizon_s)
            result.loc[i, "component_run_s"] = float(runs_pred.sum())
            result.loc[i, "component_dwell_s"] = dwell_sum
            result.loc[i, "component_global_model"] = 1.0
            result.loc[i, "component_fallback"] = 0.0
    return result
