from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor
from route_matching import Catalog

EXPERT_SHRINKAGE = 100.0

RUN_NUMERIC = (
    "segment_length_m",
    "planned_interval_s",
    "hour",
    "recent_speed_kmh",
    "endpoint_distance_m",
    "max_gap_s",
)
RUN_CATEGORICAL = ("route_pattern_id", "direction_id", "segment_id")
DWELL_NUMERIC = ("hour", "stop_index", "distance_m", "max_gap_s")
DWELL_CATEGORICAL = ("route_pattern_id", "direction_id", "stop_id")


def _matrix(frame: pd.DataFrame, numeric, categorical) -> pd.DataFrame:
    result = pd.DataFrame(index=frame.index)
    for name in numeric:
        result[name] = pd.to_numeric(frame.get(name), errors="coerce")
    for name in categorical:
        values = frame[name] if name in frame else pd.Series("__unknown__", index=frame.index)
        result[name] = values.fillna("__unknown__").astype(str)
    return result


@dataclass
class ComponentBundle:
    kind: str
    target: str
    numeric: tuple[str, ...]
    categorical: tuple[str, ...]
    global_model: CatBoostRegressor
    experts: dict[str, CatBoostRegressor]
    expert_counts: dict[str, int]

    def predict(self, frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        if frame.empty:
            return np.empty(0), np.empty(0, dtype=bool)
        matrix = _matrix(frame, self.numeric, self.categorical)
        raw = np.asarray(
            self.global_model.predict(matrix), dtype=float
        )
        used = np.zeros(len(frame), dtype=bool)
        routes = frame["route_pattern_id"].fillna("__unknown__").astype(str).to_numpy()
        for route, model in self.experts.items():
            mask = routes == route
            if not mask.any():
                continue
            n = self.expert_counts[route]
            weight = n / (n + EXPERT_SHRINKAGE)
            raw[mask] += weight * np.asarray(model.predict(matrix.loc[mask]), dtype=float)
            used[mask] = True
        upper = 3600.0 if self.kind == "run" else 900.0
        return np.clip(raw, 0.0, upper), used

    @classmethod
    def load(cls, directory: Path) -> ComponentBundle:
        payload = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        global_model = CatBoostRegressor().load_model(directory / "global.cbm")
        experts = {
            route: CatBoostRegressor().load_model(directory / filename)
            for route, filename in payload["experts"].items()
        }
        return cls(
            payload["kind"],
            payload["target"],
            tuple(payload["numeric"]),
            tuple(payload["categorical"]),
            global_model,
            experts,
            {str(k): int(v) for k, v in payload["expert_counts"].items()},
        )


def _run_row(point, sequence, leg, catalog: Catalog, current: bool) -> dict:
    variants = catalog.variants.get(leg.segment_id, ())
    length = variants[0].path.length_m if variants else np.nan
    speed_mps = getattr(point, "mm_speed_3m_mps", np.nan) if current else np.nan
    return {
        "route_pattern_id": sequence.route_pattern_id or "__unknown__",
        "direction_id": sequence.direction_id or "__unknown__",
        "segment_id": leg.segment_id,
        "segment_length_m": length,
        "planned_interval_s": max(0.0, leg.end.time - leg.start.time),
        "hour": point.hour,
        "recent_speed_kmh": speed_mps * 3.6 if np.isfinite(speed_mps) else np.nan,
        "endpoint_distance_m": getattr(point, "mm_route_distance_m", np.nan),
        "max_gap_s": getattr(point, "mm_gps_age_s", np.nan),
    }


def _dwell_row(point, sequence, stop, index: int, current: bool) -> dict:
    return {
        "route_pattern_id": sequence.route_pattern_id or "__unknown__",
        "direction_id": sequence.direction_id or "__unknown__",
        "stop_id": stop.stop_id,
        "hour": point.hour,
        "stop_index": index,
        "distance_m": getattr(point, "mm_route_distance_m", np.nan) if current else np.nan,
        "max_gap_s": getattr(point, "mm_gps_age_s", np.nan) if current else np.nan,
    }


def add_component_predictions(
    frame: pd.DataFrame,
    catalog: Catalog,
    run_bundle: ComponentBundle,
    dwell_bundle: ComponentBundle,
) -> pd.DataFrame:
    sequences = {s.sequence_id: s for s in catalog.sequences}
    run_rows, dwell_rows, specs = [], [], []
    for point in frame.itertuples(index=False):
        sequence = sequences.get(str(getattr(point, "mm_sequence_id", "")))
        try:
            current = int(point.mm_current_leg_index)
            target = int(point.mm_target_stop_index)
        except (TypeError, ValueError):
            sequence = None
        if sequence is None or current < 0 or target <= current or target >= len(sequence.stops):
            specs.append((point.sample_id, [], [], float(point.horizon_s)))
            continue
        point_runs, point_dwells = [], []
        for leg_index in range(current, target):
            point_runs.append(len(run_rows))
            run_rows.append(_run_row(point, sequence, sequence.legs[leg_index], catalog, leg_index == current))
        for stop_index in range(current + 1, target):
            point_dwells.append(len(dwell_rows))
            dwell_rows.append(
                _dwell_row(point, sequence, sequence.stops[stop_index], stop_index, False)
            )
        specs.append((point.sample_id, point_runs, point_dwells, float(point.horizon_s)))
    run_frame, dwell_frame = pd.DataFrame(run_rows), pd.DataFrame(dwell_rows)
    run_pred, run_expert = run_bundle.predict(run_frame)
    dwell_pred, dwell_expert = dwell_bundle.predict(dwell_frame)
    rows = []
    lookup = frame.set_index("sample_id")
    for sample_id, run_idx, dwell_idx, horizon in specs:
        point = lookup.loc[sample_id]
        if not run_idx:
            rows.append(
                {
                    "sample_id": sample_id,
                    "component_eta_s": horizon,
                    "component_delay_s": float(point.cur_dev_s),
                    "component_run_s": horizon,
                    "component_dwell_s": 0.0,
                    "component_expert_fraction": 0.0,
                    "component_fallback": 1.0,
                }
            )
            continue
        run_values = run_pred[run_idx].copy()
        progress = point.get("mm_segment_progress")
        if pd.notna(progress):
            run_values[0] *= max(0.0, 1.0 - float(progress))
        dwell_values = dwell_pred[dwell_idx] if dwell_idx else np.empty(0)
        eta = float(run_values.sum() + dwell_values.sum())
        flags = np.concatenate((run_expert[run_idx], dwell_expert[dwell_idx]))
        rows.append(
            {
                "sample_id": sample_id,
                "component_eta_s": eta,
                "component_delay_s": eta - horizon,
                "component_run_s": float(run_values.sum()),
                "component_dwell_s": float(dwell_values.sum()),
                "component_expert_fraction": float(flags.mean()) if len(flags) else 0.0,
                "component_fallback": 0.0,
            }
        )
    return frame.merge(pd.DataFrame(rows), on="sample_id", how="left", validate="one_to_one")
