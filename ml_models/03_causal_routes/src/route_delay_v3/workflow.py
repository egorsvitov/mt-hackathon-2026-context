from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from route_delay_v2.model import DelaySpec, fit_delay, mae, predict_delay
from route_matching import Catalog
from transport_delay.workflow import validate_submission

from .components import add_components
from .global_components import add_global_components

SPECS = (
    ("persistence", False, False, False),
    ("v1", False, False, False),
    ("map", True, False, False),
    ("route_stats", True, True, False),
    ("global_components", True, True, False),
    ("edge_context", True, True, True),
)
WINDOWS = ((10, 14), (14, 18), (18, 22))


def _load(cache: Path):
    points = pd.read_parquet(cache / "points.parquet")
    runs = pd.read_parquet(cache / "run_events.parquet")
    dwells = pd.read_parquet(cache / "dwell_events.parquet")
    for frame in (points, runs, dwells):
        for col in ("T", "label_available_at", "event_time", "available_at"):
            if col in frame:
                frame[col] = pd.to_datetime(frame[col])
    return points, runs, dwells


def _real_ids(points: pd.DataFrame) -> set[int]:
    return set(points.loc[points.split == "test", "tr_id"].astype(int))


def _spec(name: str) -> DelaySpec | None:
    for value, use_map, use_components, use_peers in SPECS:
        if value == name:
            return (
                None
                if name == "persistence"
                else DelaySpec(
                    name,
                    use_map=use_map,
                    use_components=use_components,
                    use_segment_peers=use_peers,
                    iterations=400,
                    depth=4,
                    l2_leaf_reg=30,
                )
            )
    raise ValueError(f"Unknown model specification: {name}")


def _prepare_matrix(
    frame: pd.DataFrame,
    runs: pd.DataFrame,
    dwells: pd.DataFrame,
    catalog: Catalog,
    spec: DelaySpec | None,
    blocked: set[str],
) -> pd.DataFrame:
    result = frame.drop(columns=["mm_edge_id"], errors="ignore")
    if spec is not None and spec.use_components:
        if spec.name in {"global_components", "edge_context"}:
            result = add_global_components(result, runs, dwells, catalog, blocked)
        else:
            result = add_components(result, runs, dwells, catalog, blocked)
    return result


def _predict_rolling(
    source: pd.DataFrame,
    target: pd.DataFrame,
    runs: pd.DataFrame,
    dwells: pd.DataFrame,
    catalog: Catalog,
    name: str,
    blocked: set[str],
    save_dir: Path | None = None,
) -> pd.DataFrame:
    spec = _spec(name)
    source = source[~source.mm_trip_occurrence_id.isin(blocked)].copy()
    target = target.sort_values(["T", "sample_id"], kind="stable").copy()
    prepared_source = _prepare_matrix(source, runs, dwells, catalog, spec, blocked)
    prepared_target = _prepare_matrix(target, runs, dwells, catalog, spec, blocked)
    results = []
    for hour, group in prepared_target.groupby(prepared_target["T"].dt.floor("h"), sort=True):
        next_hour_trips = set(group.mm_trip_occurrence_id) - {"__unknown__"}
        cutoff = hour
        available = eligible_training_rows(prepared_source, cutoff, next_hour_trips)
        trips = available.mm_trip_occurrence_id.replace("__unknown__", np.nan).nunique()
        model = None
        if spec is not None and len(available) >= 200 and trips >= 5:
            model = fit_delay(
                available,
                spec,
                metadata={
                    "model_cutoff": cutoff.isoformat(),
                    "train_rows": len(available),
                    "train_trips": int(trips),
                    "catalog_version": catalog.version,
                },
            )
            if save_dir is not None:
                model.save(save_dir / cutoff.strftime("%Y%m%dT%H%M") / name)
        prediction = (
            predict_delay(group, model) if model is not None else group.cur_dev_s.to_numpy(float)
        )
        result = group[["sample_id", "T", "tr_id", "mm_trip_occurrence_id"]].copy()
        result["prediction"] = prediction
        result["model_cutoff"] = cutoff if model is not None else pd.NaT
        result["train_rows"] = len(available) if model is not None else 0
        result["train_trips"] = int(trips) if model is not None else 0
        result["max_dynamic_available_at"] = group["max_dynamic_available_at"].to_numpy()
        results.append(result)
    if not results:
        return pd.DataFrame(columns=["sample_id", "prediction"])
    result = pd.concat(results, ignore_index=True)
    if result.prediction.isna().any() or not np.isfinite(result.prediction).all():
        raise ValueError("Non-finite rolling predictions")
    return result


def _score(target: pd.DataFrame, prediction: pd.DataFrame) -> dict:
    scored = target[["sample_id", "target_delay_s", "cur_dev_s"]].merge(
        prediction[["sample_id", "prediction", "train_rows"]], on="sample_id", validate="one_to_one"
    )
    return {
        "rows": len(scored),
        "mae": mae(scored.target_delay_s, scored.prediction),
        "persistence_mae": mae(scored.target_delay_s, scored.cur_dev_s),
        "fallback_fraction": float((scored.train_rows == 0).mean()),
    }


def backtest(cache: Path, catalog_path: Path, artifacts: Path) -> dict:
    artifacts.mkdir(parents=True, exist_ok=True)
    catalog = Catalog.load(catalog_path)
    points, runs, dwells = _load(cache)
    train = points[points.split == "train"]
    real_ids = _real_ids(points)
    reports = []
    for name, *_ in SPECS:
        folds = []
        for begin, end in WINDOWS[:2]:
            start = pd.Timestamp(2026, 1, 6, begin)
            stop = pd.Timestamp(2026, 1, 6, end)
            valid = train[
                train.tr_id.isin(real_ids) & train["T"].between(start, stop, inclusive="left")
            ]
            blocked = set(valid.mm_trip_occurrence_id) - {"__unknown__"}
            prediction = _predict_rolling(train, valid, runs, dwells, catalog, name, blocked)
            folds.append({"start": start.isoformat(), **_score(valid, prediction)})
        joined = sum(fold["rows"] * fold["mae"] for fold in folds) / sum(
            fold["rows"] for fold in folds
        )
        reports.append({"name": name, "development_mae": joined, "folds": folds})
    selected = reports[0]
    for candidate in reports[1:]:
        gain = selected["development_mae"] - candidate["development_mae"]
        worsening = [candidate["folds"][i]["mae"] - selected["folds"][i]["mae"] for i in range(2)]
        if gain >= 1.0 and max(worsening) <= 3.0:
            selected = candidate
    start = pd.Timestamp(2026, 1, 6, 18)
    stop = pd.Timestamp(2026, 1, 6, 22)
    final = train[train.tr_id.isin(real_ids) & train["T"].between(start, stop, inclusive="left")]
    blocked = set(final.mm_trip_occurrence_id) - {"__unknown__"}
    final_prediction = _predict_rolling(
        train, final, runs, dwells, catalog, selected["name"], blocked
    )
    final_prediction.to_parquet(artifacts / "final_window_predictions.parquet", index=False)
    report = {
        "selected": selected["name"],
        "development": reports,
        "final_window": _score(final, final_prediction),
        "catalog_version": catalog.version,
        "causal_policy": {
            "telemetry": "available_at <= T",
            "labels": "target_fact_time + 60s < hourly_cutoff",
            "component_events": "available_at < T",
            "heldout_trips": "excluded from training and component history",
        },
    }
    (artifacts / "backtest_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n"
    )
    return report


def _selected(artifacts: Path) -> str:
    path = artifacts / "backtest_report.json"
    if not path.exists():
        raise ValueError("Run backtest and freeze a candidate before evaluate/predict")
    return json.loads(path.read_text())["selected"]


def evaluate(cache: Path, catalog_path: Path, artifacts: Path) -> dict:
    catalog = Catalog.load(catalog_path)
    points, runs, dwells = _load(cache)
    train = points[points.split == "train"]
    test = points[points.split == "test"]
    blocked = set(test.mm_trip_occurrence_id) - {"__unknown__"}
    prediction = _predict_rolling(
        train,
        test,
        runs,
        dwells,
        catalog,
        _selected(artifacts),
        blocked,
        artifacts / "test_models",
    )
    prediction.to_parquet(artifacts / "test_predictions.parquet", index=False)
    report = {"selected": _selected(artifacts), **_score(test, prediction)}
    synthetic = train[
        ~train.tr_id.isin(_real_ids(points))
        & train["T"].between(
            pd.Timestamp(2026, 1, 6, 18),
            pd.Timestamp(2026, 1, 6, 22),
            inclusive="left",
        )
    ]
    synthetic_blocked = set(synthetic.mm_trip_occurrence_id) - {"__unknown__"}
    synthetic_prediction = _predict_rolling(
        train, synthetic, runs, dwells, catalog, _selected(artifacts), synthetic_blocked
    )
    report["synthetic_final_window"] = _score(synthetic, synthetic_prediction)
    (artifacts / "test_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n"
    )
    return report


def predict(data_dir: Path, cache: Path, catalog_path: Path, artifacts: Path, output: Path) -> dict:
    catalog = Catalog.load(catalog_path)
    points, runs, dwells = _load(cache)
    source = points[points.split.isin(["train", "test"])]
    validate = points[points.split == "validate"]
    result = _predict_rolling(
        source,
        validate,
        runs,
        dwells,
        catalog,
        _selected(artifacts),
        set(),
        artifacts / "validate_models",
    )
    result.to_parquet(artifacts / "validate_predictions.parquet", index=False)
    template = pd.read_csv(data_dir / "sample_submission.csv", sep=";")
    submission = template[["sample_id"]].merge(
        result[["sample_id", "prediction"]], on="sample_id", how="left", validate="one_to_one"
    )
    validate_submission(submission, template)
    output.parent.mkdir(parents=True, exist_ok=True)
    submission.to_csv(output, sep=";", index=False)
    return {
        "selected": _selected(artifacts),
        "rows": len(submission),
        "output": str(output),
        "catalog_version": catalog.version,
    }


def eligible_training_rows(
    frame: pd.DataFrame, cutoff: pd.Timestamp, blocked: set[str]
) -> pd.DataFrame:
    return frame[(frame.label_available_at < cutoff) & ~frame.mm_trip_occurrence_id.isin(blocked)]
