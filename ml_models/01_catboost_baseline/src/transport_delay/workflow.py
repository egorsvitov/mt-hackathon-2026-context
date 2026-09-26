from __future__ import annotations

import json
import platform
from dataclasses import asdict, replace
from pathlib import Path
from time import perf_counter

import catboost
import numpy as np
import pandas as pd

from .config import data_dir
from .data import read_points, read_schedule, read_telemetry
from .features import build_features
from .model import ModelSpec, cross_validate, diagnostics, mae, predict, train_bundle


def prepare(data_path: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    started = perf_counter()
    definitions = {
        "train": (
            data_path / "labels/labels_train.csv",
            data_path / "train/traffic.csv",
            data_path / "train/schedule.csv",
        ),
        "test": (
            data_path / "labels/labels_test.csv",
            data_path / "test/traffic.csv",
            data_path / "test/schedule.csv",
        ),
        "validate": (
            data_path / "validate/points.csv",
            data_path / "validate/traffic.csv",
            data_path / "validate/schedule_plan.csv",
        ),
    }
    test_ids = set(read_points(data_path / "labels/labels_test.csv")["tr_id"])
    summary = {}
    for split, (points_path, traffic_path, schedule_path) in definitions.items():
        split_started = perf_counter()
        points = read_points(points_path)
        labels = points[
            [c for c in ("sample_id", "target_delay_s", "target_class") if c in points]
        ].copy()
        feature_input = points.drop(columns=["target_delay_s", "target_class"], errors="ignore")
        frame = build_features(
            feature_input, read_telemetry(traffic_path), read_schedule(schedule_path)
        )
        frame = frame.merge(labels, on="sample_id", how="left", validate="one_to_one")
        frame["is_real_train_vehicle"] = frame["tr_id"].isin(test_ids)
        if "target_delay_s" in frame:
            frame["actual_event_time"] = frame["target_time_begin"] + pd.to_timedelta(
                frame["target_delay_s"], unit="s"
            )
        frame.to_parquet(output / f"{split}.parquet", index=False)
        summary[split] = {
            "rows": len(frame),
            "columns": len(frame.columns),
            "seconds": perf_counter() - split_started,
        }
    summary["total_seconds"] = perf_counter() - started
    (output / "prepare_report.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def _reportable(result: dict) -> dict:
    return {key: value for key, value in result.items() if key != "predictions"}


def _prefer(candidate: dict, incumbent: dict, tolerance: float = 1.0) -> bool:
    if candidate["mae"] < incumbent["mae"] - tolerance:
        return True
    if abs(candidate["mae"] - incumbent["mae"]) <= tolerance:
        cs, old = candidate["spec"], incumbent["spec"]
        candidate_complexity = (
            len(cs["groups"]),
            cs["include_vehicle"],
            cs["depth"],
            len(cs["seeds"]),
        )
        incumbent_complexity = (
            len(old["groups"]),
            old["include_vehicle"],
            old["depth"],
            len(old["seeds"]),
        )
        if candidate_complexity != incumbent_complexity:
            return candidate_complexity < incumbent_complexity
        return candidate["mae"] < incumbent["mae"]
    return False


def run_selection(cache: Path, artifacts: Path) -> dict:
    artifacts.mkdir(parents=True, exist_ok=True)
    train = pd.read_parquet(cache / "train.parquet")
    real_train = train[train["is_real_train_vehicle"]].reset_index(drop=True)
    history: list[dict] = []

    # Stage 1: target formulation and optimization loss on the compact base feature set.
    for formulation in ("direct", "residual"):
        for loss in ("MAE", "RMSE"):
            result = cross_validate(
                real_train, ModelSpec(formulation=formulation, loss_function=loss)
            )
            history.append(_reportable(result))
    best = min(history, key=lambda item: item["mae"])
    for candidate in history:
        if _prefer(candidate, best):
            best = candidate

    # Stage 2: add feature groups only when they give a material improvement.
    spec = ModelSpec(
        **{
            **best["spec"],
            "groups": tuple(best["spec"]["groups"]),
            "seeds": tuple(best["spec"]["seeds"]),
        }
    )
    for group in ("dynamics", "geography", "peers"):
        candidate = cross_validate(real_train, replace(spec, groups=spec.groups + (group,)))
        history.append(_reportable(candidate))
        if _prefer(candidate, best):
            best, spec = candidate, replace(spec, groups=spec.groups + (group,))
    vehicle_candidate = cross_validate(real_train, replace(spec, include_vehicle=True))
    history.append(_reportable(vehicle_candidate))
    if _prefer(vehicle_candidate, best):
        best, spec = vehicle_candidate, replace(spec, include_vehicle=True)

    # Stage 3: shallow CPU-friendly parameter sweep around the selected representation.
    for depth in (4, 6, 8):
        for l2 in (3.0, 10.0, 30.0):
            candidate = cross_validate(real_train, replace(spec, depth=depth, l2_leaf_reg=l2))
            history.append(_reportable(candidate))
            if _prefer(candidate, best):
                best, spec = candidate, replace(spec, depth=depth, l2_leaf_reg=l2)

    # Stage 4: accept a three-seed average only for a material improvement.
    ensemble_spec = replace(spec, seeds=(42, 17, 73))
    ensemble = cross_validate(real_train, ensemble_spec)
    history.append(_reportable(ensemble))
    if _prefer(ensemble, best):
        best, spec = ensemble, ensemble_spec

    # Use the robust median early-stopped tree count for the final fit on all real train data.
    best_iterations = [i for fold in best["folds"] for i in fold["best_iterations"] if i >= 0]
    if best_iterations:
        spec = replace(spec, iterations=max(50, int(np.median(best_iterations)) + 1))

    test = pd.read_parquet(cache / "test.parquet")
    bundle = train_bundle(
        real_train,
        spec,
        metadata={
            "selection_mae": best["mae"],
            "selection_persistence_mae": best["persistence_mae"],
            "real_train_rows": len(real_train),
            "python": platform.python_version(),
            "catboost": catboost.__version__,
        },
    )
    test_started = perf_counter()
    test_prediction = predict(test, bundle)
    inference_seconds = perf_counter() - test_started
    test_report = diagnostics(test, test_prediction)
    test_report["persistence_mae"] = mae(test["target_delay_s"], test["cur_dev_s"])
    test_report["inference_seconds"] = inference_seconds
    test_report["rows_per_second"] = len(test) / max(inference_seconds, 1e-9)

    recommended = (
        best["mae"] < best["persistence_mae"]
        and test_report["mae"] < test_report["persistence_mae"]
    )
    bundle.metadata.update({"test": test_report, "recommended_for_submission": recommended})
    bundle.save(artifacts / "model")

    validate = pd.read_parquet(cache / "validate.parquet")
    model_prediction = predict(validate, bundle)
    submission_prediction = (
        model_prediction if recommended else validate["cur_dev_s"].to_numpy(float)
    )
    submission = pd.DataFrame(
        {"sample_id": validate["sample_id"], "prediction": submission_prediction}
    )
    template = pd.read_csv(data_dir() / "sample_submission.csv", sep=";")
    submission = template[["sample_id"]].merge(
        submission, on="sample_id", how="left", validate="one_to_one"
    )
    validate_submission(submission, template)
    submission.to_csv(artifacts / "submission.csv", sep=";", index=False)
    model_submission = template[["sample_id"]].merge(
        pd.DataFrame({"sample_id": validate["sample_id"], "prediction": model_prediction}),
        on="sample_id",
        how="left",
        validate="one_to_one",
    )
    validate_submission(model_submission, template)
    model_submission.to_csv(artifacts / "submission_model_candidate.csv", sep=";", index=False)

    final_report = {
        "selected_spec": {**asdict(spec), "groups": list(spec.groups), "seeds": list(spec.seeds)},
        "selection": _reportable(best),
        "test": test_report,
        "recommended_for_submission": recommended,
        "submission_strategy": "catboost" if recommended else "cur_dev_s persistence",
        "experiments": history,
    }
    (artifacts / "experiment_report.json").write_text(
        json.dumps(final_report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
    )
    return final_report


def validate_submission(submission: pd.DataFrame, template: pd.DataFrame) -> None:
    if list(submission.columns) != ["sample_id", "prediction"]:
        raise ValueError("Submission must contain exactly sample_id;prediction")
    if len(submission) != len(template) or submission["sample_id"].duplicated().any():
        raise ValueError("Submission does not cover template exactly once")
    if submission["prediction"].isna().any() or not np.isfinite(submission["prediction"]).all():
        raise ValueError("Submission has missing or non-finite predictions")
