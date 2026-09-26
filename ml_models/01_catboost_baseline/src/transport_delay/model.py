from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor

from .features import feature_columns


@dataclass(frozen=True)
class ModelSpec:
    formulation: str = "residual"
    loss_function: str = "MAE"
    groups: tuple[str, ...] = ()
    include_vehicle: bool = False
    depth: int = 6
    l2_leaf_reg: float = 10.0
    iterations: int = 1500
    learning_rate: float = 0.03
    seeds: tuple[int, ...] = (42,)


@dataclass
class ModelBundle:
    models: list[CatBoostRegressor]
    feature_names: list[str]
    spec: ModelSpec
    metadata: dict[str, Any]
    categorical_feature_names: list[str] | None = None

    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        files = []
        for index, model in enumerate(self.models):
            name = f"model_{index}.cbm"
            model.save_model(directory / name)
            files.append(name)
        manifest = {
            "model_files": files,
            "feature_names": self.feature_names,
            "spec": {
                **asdict(self.spec),
                "groups": list(self.spec.groups),
                "seeds": list(self.spec.seeds),
            },
            "metadata": self.metadata,
            "categorical_feature_names": self.categorical_feature_names or [],
        }
        (directory / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
        )

    @classmethod
    def load(cls, directory: Path) -> ModelBundle:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        raw_spec = manifest["spec"]
        raw_spec["groups"] = tuple(raw_spec["groups"])
        raw_spec["seeds"] = tuple(raw_spec["seeds"])
        models = []
        for name in manifest["model_files"]:
            model = CatBoostRegressor()
            model.load_model(directory / name)
            models.append(model)
        return cls(
            models,
            manifest["feature_names"],
            ModelSpec(**raw_spec),
            manifest["metadata"],
            manifest.get("categorical_feature_names", []),
        )


def _categorical_features(frame: pd.DataFrame, features: list[str]) -> list[str]:
    return [
        name
        for name in features
        if name in frame
        and (frame[name].dtype == "object" or str(frame[name].dtype).startswith("string"))
    ]


def _model_frame(frame: pd.DataFrame, features: list[str], categorical: list[str]) -> pd.DataFrame:
    values = frame[features].copy()
    for name in categorical:
        values[name] = values[name].fillna("__unknown__").astype(str)
    return values


def _fit_one(
    train: pd.DataFrame,
    valid: pd.DataFrame | None,
    features: list[str],
    spec: ModelSpec,
    seed: int,
) -> CatBoostRegressor:
    target = train["target_delay_s"].to_numpy(dtype=float, copy=True)
    if spec.formulation == "residual":
        target = target - train["cur_dev_s"].to_numpy(float)
    model = CatBoostRegressor(
        loss_function=spec.loss_function,
        eval_metric="MAE",
        iterations=spec.iterations,
        learning_rate=spec.learning_rate,
        depth=spec.depth,
        l2_leaf_reg=spec.l2_leaf_reg,
        random_seed=seed,
        task_type="CPU",
        thread_count=4,
        allow_writing_files=False,
        verbose=False,
    )
    kwargs: dict[str, Any] = {}
    categorical = _categorical_features(train, features)
    if valid is not None and len(valid):
        valid_target = valid["target_delay_s"].to_numpy(dtype=float, copy=True)
        if spec.formulation == "residual":
            valid_target -= valid["cur_dev_s"].to_numpy(float)
        kwargs.update(
            eval_set=(_model_frame(valid, features, categorical), valid_target),
            early_stopping_rounds=100,
            use_best_model=True,
        )
    model.fit(
        _model_frame(train, features, categorical), target, cat_features=categorical, **kwargs
    )
    return model


def train_bundle(
    frame: pd.DataFrame, spec: ModelSpec, metadata: dict[str, Any] | None = None
) -> ModelBundle:
    features = feature_columns(frame, spec.groups, spec.include_vehicle)
    metadata = dict(metadata or {})
    if "mm_catalog_version" in frame:
        metadata["route_catalog_versions"] = sorted(
            str(value) for value in frame["mm_catalog_version"].dropna().unique()
        )
    models = [_fit_one(frame, None, features, spec, seed) for seed in spec.seeds]
    return ModelBundle(models, features, spec, metadata, _categorical_features(frame, features))


def predict(features: pd.DataFrame, model_bundle: ModelBundle) -> np.ndarray:
    values = np.mean(
        [
            model.predict(
                _model_frame(
                    features,
                    model_bundle.feature_names,
                    model_bundle.categorical_feature_names or [],
                )
            )
            for model in model_bundle.models
        ],
        axis=0,
    )
    if model_bundle.spec.formulation == "residual":
        values = values + features["cur_dev_s"].to_numpy(float)
    return np.asarray(values, dtype=float)


def mae(actual: np.ndarray, predicted: np.ndarray) -> float:
    return float(np.mean(np.abs(np.asarray(actual) - np.asarray(predicted))))


def temporal_folds(frame: pd.DataFrame) -> list[tuple[str, pd.DataFrame, pd.DataFrame]]:
    folds = []
    for hour in (10, 14, 18):
        cutoff = pd.Timestamp(2026, 1, 6, hour)
        end = cutoff + pd.Timedelta(hours=4)
        train = frame[frame["actual_event_time"] < cutoff]
        valid = frame[(frame["T"] >= cutoff) & (frame["T"] < end)]
        # Guard against correlated samples for the same planned visit crossing the boundary.
        overlap = set(train["target_stop_id"]) & set(valid["target_stop_id"])
        if overlap:
            train = train[~train["target_stop_id"].isin(overlap)]
        folds.append((cutoff.isoformat(), train, valid))
    return folds


def cross_validate(frame: pd.DataFrame, spec: ModelSpec) -> dict[str, Any]:
    features = feature_columns(frame, spec.groups, spec.include_vehicle)
    fold_metrics, predictions = [], []
    for name, train, valid in temporal_folds(frame):
        if train.empty or valid.empty:
            raise ValueError(f"Empty temporal fold {name}")
        seed_predictions = []
        best_iterations = []
        for seed in spec.seeds:
            model = _fit_one(train, valid, features, spec, seed)
            raw = model.predict(valid[features])
            if spec.formulation == "residual":
                raw += valid["cur_dev_s"].to_numpy(float)
            seed_predictions.append(raw)
            best_iterations.append(int(model.get_best_iteration()))
        predicted = np.mean(seed_predictions, axis=0)
        actual = valid["target_delay_s"].to_numpy(float)
        fold_metrics.append(
            {
                "fold": name,
                "train_rows": len(train),
                "valid_rows": len(valid),
                "mae": mae(actual, predicted),
                "persistence_mae": mae(actual, valid["cur_dev_s"].to_numpy(float)),
                "best_iterations": best_iterations,
            }
        )
        predictions.append(
            pd.DataFrame(
                {"sample_id": valid["sample_id"], "actual": actual, "prediction": predicted}
            )
        )
    all_predictions = pd.concat(predictions, ignore_index=True)
    return {
        "spec": {**asdict(spec), "groups": list(spec.groups), "seeds": list(spec.seeds)},
        "features": features,
        "folds": fold_metrics,
        "mae": mae(all_predictions["actual"], all_predictions["prediction"]),
        "persistence_mae": mae(
            frame.set_index("sample_id")
            .loc[all_predictions["sample_id"], "target_delay_s"]
            .to_numpy(float),
            frame.set_index("sample_id")
            .loc[all_predictions["sample_id"], "cur_dev_s"]
            .to_numpy(float),
        ),
        "predictions": all_predictions,
    }


def diagnostics(frame: pd.DataFrame, predicted: np.ndarray) -> dict[str, Any]:
    error = np.abs(frame["target_delay_s"].to_numpy(float) - predicted)
    report: dict[str, Any] = {
        "rows": len(frame),
        "mae": float(np.mean(error)),
        "p90_absolute_error": float(np.quantile(error, 0.9)),
        "within_30s": float(np.mean(error <= 30)),
        "within_60s": float(np.mean(error <= 60)),
        "within_120s": float(np.mean(error <= 120)),
    }
    report["by_vehicle"] = {
        str(tr_id): float(np.mean(error[index]))
        for tr_id, index in frame.groupby("tr_id", sort=True).indices.items()
    }
    fresh = frame["gps_age_s"].fillna(np.inf).to_numpy() <= 120
    report["gps_fresh_mae"] = float(np.mean(error[fresh])) if fresh.any() else None
    report["gps_stale_mae"] = float(np.mean(error[~fresh])) if (~fresh).any() else None
    peer_columns = [c for c in frame if c.endswith("_count") and c.startswith("peers_")]
    if peer_columns:
        has_peers = frame[peer_columns].fillna(0).max(axis=1).to_numpy() > 0
        report["with_peers_mae"] = float(np.mean(error[has_peers])) if has_peers.any() else None
        report["without_peers_mae"] = (
            float(np.mean(error[~has_peers])) if (~has_peers).any() else None
        )
    return report
