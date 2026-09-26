"""Spatial feature adapter and measured CatBoost ablation."""

from __future__ import annotations

import json
import math
from pathlib import Path

from .catalog import Catalog
from .data import normalize_events
from .replay import read_feature_rows, replay
from .types import Event, timestamp


def _optional_ml():
    try:
        import numpy as np
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError("Install the project with the 'ml' extra") from exc
    return np, pd


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def build_spatial_features(
    points,
    telemetry,
    schedule_plan,
    route_catalog: Catalog,
    timezone_name: str = "Europe/Moscow",
):
    """Build one causal, label-free map-matching feature row per prediction point."""
    _, pd = _optional_ml()
    required_points = {"sample_id", "tr_id", "T", "target_stop_id", "cur_dev_s"}
    if missing := required_points - set(points.columns):
        raise ValueError(f"points missing columns: {sorted(missing)}")
    required_schedule = {"tt_action_item_id", "tr_id", "time_begin", "geom"}
    if missing := required_schedule - set(schedule_plan.columns):
        raise ValueError(f"schedule_plan missing columns: {sorted(missing)}")
    known_visits = set(schedule_plan["tt_action_item_id"].astype(str))

    events = []
    for row in telemetry.to_dict("records"):
        receive_time = row.get("receive_time")
        receive = None if pd.isna(receive_time) else timestamp(receive_time, timezone_name)
        speed, heading = _number(row.get("speed")), _number(row.get("heading"))
        valid = row.get("location_valid", False)
        if isinstance(valid, str):
            valid = valid.lower() == "true"
        packet = row.get("packet_id")
        events.append(
            Event(
                int(row["tr_id"]),
                timestamp(row["event_time"], timezone_name),
                _number(row.get("lon")),
                _number(row.get("lat")),
                speed if speed is not None and 0 <= speed <= 150 else None,
                heading % 360 if heading is not None else None,
                bool(valid),
                "" if pd.isna(packet) else str(packet),
                receive,
            )
        )
    prediction_points, target_times = [], {}
    for row in points.to_dict("records"):
        target = str(row["target_stop_id"])
        if target not in known_visits:
            raise ValueError(f"Unknown target_stop_id: {target}")
        sample_id = str(row["sample_id"])
        prediction_points.append(
            {
                "sample_id": sample_id,
                "tr_id": int(row["tr_id"]),
                "T": timestamp(row["T"], timezone_name),
                "target_visit_id": target,
                "cur_dev_s": float(row["cur_dev_s"]),
            }
        )
        if "target_time_begin" in row and not pd.isna(row["target_time_begin"]):
            target_times[sample_id] = timestamp(row["target_time_begin"], timezone_name)

    results, _ = replay(normalize_events(events), prediction_points, route_catalog)
    rows = []
    for point, state in results:
        row = {
            "sample_id": point["sample_id"],
            **state.feature_row(),
            "mm_catalog_version": route_catalog.version,
        }
        target_time = target_times.get(point["sample_id"])
        row["mm_route_eta_prediction_s"] = (
            point["T"] + state.estimated_remaining_time_s - target_time
            if target_time is not None and state.estimated_remaining_time_s is not None
            else None
        )
        rows.append(row)
    return pd.DataFrame(rows).sort_values("sample_id", kind="stable").reset_index(drop=True)


def wai_zhou_predictions(frame):
    """Return deterministic segment-time predictions with persistence fallback."""
    np, _ = _optional_ml()
    route = frame["mm_route_eta_prediction_s"].to_numpy(float)
    fallback = frame["cur_dev_s"].to_numpy(float)
    return np.where(np.isfinite(route), route, fallback)


def _baseline_columns(cache: Path, frame) -> list[str]:
    report = cache.parent / "modeling" / "experiment_report.json"
    if report.exists():
        selected = json.loads(report.read_text())["selection"]["features"]
        return [column for column in selected if column in frame]
    return [
        column
        for column in (
            "cur_dev_s",
            "cur_dev_abs_s",
            "horizon_s",
            "time_sin",
            "time_cos",
            "hour",
            "last_speed",
            "last_heading",
            "has_history",
            "has_valid_gps",
            "packet_age_s",
            "gps_age_s",
            "last_lon",
            "last_lat",
            "target_lon",
            "target_lat",
            "distance_to_target_m",
            "previous_stop_lon",
            "previous_stop_lat",
            "target_leg_planned_s",
            "remaining_visits",
            "schedule_progress",
            "geo_lon_cell",
            "geo_lat_cell",
        )
        if column in frame
    ]


def catboost_ablation(cache: Path, spatial: Path, output: Path) -> dict:
    np, pd = _optional_ml()
    try:
        from catboost import CatBoostRegressor
    except ImportError as exc:
        raise RuntimeError("Install the project with the 'ml' extra") from exc

    train = pd.read_parquet(cache / "train.parquet")
    test = pd.read_parquet(cache / "test.parquet")
    train = train[train["tr_id"].isin(set(test["tr_id"]))].copy()
    train = train.merge(
        pd.DataFrame(read_feature_rows(spatial / "train.jsonl")),
        on="sample_id",
        validate="one_to_one",
    )
    test = test.merge(
        pd.DataFrame(read_feature_rows(spatial / "test.jsonl")),
        on="sample_id",
        validate="one_to_one",
    )
    base = _baseline_columns(cache, train)
    position = [
        column
        for column in (
            "mm_segment_progress",
            "mm_route_progress",
            "mm_remaining_distance_m",
            "mm_remaining_segments",
            "mm_remaining_stops",
            "mm_route_distance_m",
            "mm_confidence_margin",
            "mm_gps_age_s",
            "mm_matched",
            "mm_confirmed",
            "mm_fallback",
            "mm_off_route",
        )
        if column in train
    ]
    wai = [
        column
        for column in (
            "mm_planned_segment_time_s",
            "mm_planned_remaining_time_s",
            "mm_historical_segment_time_s",
            "mm_estimated_remaining_time_s",
            "mm_segment_support",
            "mm_speed_mps",
            "mm_speed_1m_mps",
            "mm_speed_3m_mps",
            "mm_speed_5m_mps",
        )
        if column in train
    ]
    categories = [
        column
        for column in (
            "mm_route_pattern_id",
            "mm_direction_id",
            "mm_current_segment_id",
            "mm_target_segment_id",
            "mm_mode",
            "mm_position_quality",
            "mm_route_quality",
            "mm_route_source",
        )
        if column in train
    ]
    missing_masks = [
        column for column in train if column.startswith("mm_") and column.endswith("_missing")
    ]

    def prepared(frame, features):
        result = frame[features].copy()
        cats = [column for column in features if column in categories]
        for column in cats:
            result[column] = result[column].fillna("__unknown__").astype(str)
        return result, cats

    def folds(frame):
        for hour in (10, 14, 18):
            cutoff = pd.Timestamp(2026, 1, 6, hour)
            end = cutoff + pd.Timedelta(hours=4)
            fit = frame[frame["actual_event_time"] < cutoff]
            valid = frame[(frame["T"] >= cutoff) & (frame["T"] < end)]
            overlap = set(fit["target_stop_id"]) & set(valid["target_stop_id"])
            yield cutoff.isoformat(), fit[~fit["target_stop_id"].isin(overlap)], valid

    def evaluate(features):
        predictions, actuals, fold_rows, iterations = [], [], [], []
        for name, fit, valid in folds(train):
            fit_x, cats = prepared(fit, features)
            valid_x, _ = prepared(valid, features)
            model = CatBoostRegressor(
                loss_function="MAE",
                eval_metric="MAE",
                iterations=1500,
                learning_rate=0.03,
                depth=4,
                l2_leaf_reg=30,
                random_seed=42,
                allow_writing_files=False,
                verbose=False,
                thread_count=4,
            )
            model.fit(
                fit_x,
                fit["target_delay_s"] - fit["cur_dev_s"],
                cat_features=cats,
                eval_set=(valid_x, valid["target_delay_s"] - valid["cur_dev_s"]),
                early_stopping_rounds=100,
                use_best_model=True,
            )
            predicted = valid["cur_dev_s"].to_numpy() + model.predict(valid_x)
            error = np.abs(valid["target_delay_s"].to_numpy() - predicted)
            predictions.extend(predicted)
            actuals.extend(valid["target_delay_s"])
            best = max(1, model.get_best_iteration() + 1)
            iterations.append(best)
            fold_rows.append(
                {
                    "fold": name,
                    "rows": len(valid),
                    "mae": float(error.mean()),
                    "best_iterations": best,
                }
            )
        return {
            "features": features,
            "categorical_features": [c for c in features if c in categories],
            "folds": fold_rows,
            "mae": float(np.mean(np.abs(np.asarray(actuals) - predictions))),
            "iterations": int(np.median(iterations)),
        }

    feature_sets = {
        "baseline": base,
        "position": list(dict.fromkeys(base + position + missing_masks)),
        "wai_zhou": list(dict.fromkeys(base + position + wai + missing_masks)),
        "categorical": list(dict.fromkeys(base + position + wai + categories + missing_masks)),
    }
    experiments = {name: evaluate(features) for name, features in feature_sets.items()}
    selected_name, selected = min(experiments.items(), key=lambda item: item[1]["mae"])
    selected_x, cats = prepared(train, selected["features"])
    model = CatBoostRegressor(
        loss_function="MAE",
        iterations=selected["iterations"],
        learning_rate=0.03,
        depth=4,
        l2_leaf_reg=30,
        random_seed=42,
        allow_writing_files=False,
        verbose=False,
        thread_count=4,
    )
    model.fit(selected_x, train["target_delay_s"] - train["cur_dev_s"], cat_features=cats)
    test_x, _ = prepared(test, selected["features"])
    predicted = test["cur_dev_s"].to_numpy() + model.predict(test_x)
    test_error = np.abs(test["target_delay_s"].to_numpy() - predicted)
    baseline_mae = experiments["baseline"]["mae"]
    report = {
        "rows": {"train": len(train), "test": len(test)},
        "experiments": experiments,
        "selected": selected_name,
        "validation_improvement_s": baseline_mae - selected["mae"],
        "recommended": selected_name if baseline_mae - selected["mae"] > 1 else "baseline",
        "test": {
            "mae": float(test_error.mean()),
            "p90_absolute_error": float(np.quantile(test_error, 0.9)),
        },
    }
    if "mm_route_eta_prediction_s" in test:
        route_eta = wai_zhou_predictions(test)
        route_error = np.abs(test["target_delay_s"].to_numpy() - route_eta)
        report["route_eta_baseline"] = {
            "mae": float(route_error.mean()),
            "coverage": float(test["mm_route_eta_prediction_s"].notna().mean()),
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report
