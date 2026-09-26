from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor
from transport_delay.features import feature_columns as v1_feature_columns

from .spatial import assert_no_temporal_catalog_features, spatial_feature_columns


@dataclass(frozen=True)
class DelaySpec:
    name: str
    use_map: bool = False
    use_components: bool = False
    use_experts: bool = False
    use_segment_peers: bool = False
    depth: int = 4
    l2_leaf_reg: float = 30.0
    iterations: int = 384


@dataclass
class DelayBundle:
    model: CatBoostRegressor
    features: list[str]
    categorical: list[str]
    spec: DelaySpec
    metadata: dict

    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.model.save_model(directory / "model.cbm")
        manifest = {
            "features": self.features,
            "categorical": self.categorical,
            "spec": asdict(self.spec),
            "metadata": self.metadata,
            "causal_policy": {
                "route_geometry": "static_prior",
                "telemetry": "event_time <= T",
                "forbidden_catalog_features": [
                    "historical_segment_time_s",
                    "estimated_remaining_time_s",
                    "segment_support",
                ],
            },
        }
        (directory / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, directory: Path) -> DelayBundle:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        model = CatBoostRegressor().load_model(directory / "model.cbm")
        return cls(
            model,
            manifest["features"],
            manifest["categorical"],
            DelaySpec(**manifest["spec"]),
            manifest["metadata"],
        )


def columns_for(frame: pd.DataFrame, spec: DelaySpec) -> tuple[list[str], list[str]]:
    features = v1_feature_columns(frame, ("geography",), include_vehicle=False)
    categorical: list[str] = []
    if spec.use_map:
        numeric, cats = spatial_feature_columns(frame)
        if not spec.use_segment_peers:
            numeric = [c for c in numeric if not c.startswith("segment_peers_")]
        features += numeric + cats
        categorical += cats
    if spec.use_components:
        features += [c for c in frame if c.startswith("component_")]
    features = list(dict.fromkeys(c for c in features if c in frame))
    assert_no_temporal_catalog_features(features)
    return features, categorical


def matrix(frame: pd.DataFrame, features: list[str], categorical: list[str]) -> pd.DataFrame:
    result = frame[features].copy()
    for name in categorical:
        result[name] = result[name].fillna("__unknown__").astype(str)
    return result


def fit_delay(
    frame: pd.DataFrame,
    spec: DelaySpec,
    valid: pd.DataFrame | None = None,
    metadata: dict | None = None,
) -> DelayBundle:
    features, categorical = columns_for(frame, spec)
    target = frame["target_delay_s"].to_numpy(float) - frame["cur_dev_s"].to_numpy(float)
    model = CatBoostRegressor(
        loss_function="MAE",
        eval_metric="MAE",
        iterations=spec.iterations,
        learning_rate=0.03,
        depth=spec.depth,
        l2_leaf_reg=spec.l2_leaf_reg,
        random_seed=42,
        task_type="CPU",
        thread_count=4,
        allow_writing_files=False,
        verbose=False,
    )
    kwargs = {}
    if valid is not None and len(valid):
        valid_target = valid["target_delay_s"].to_numpy(float) - valid["cur_dev_s"].to_numpy(float)
        kwargs = {
            "eval_set": (matrix(valid, features, categorical), valid_target),
            "early_stopping_rounds": 100,
            "use_best_model": True,
        }
    model.fit(
        matrix(frame, features, categorical),
        target,
        cat_features=categorical,
        **kwargs,
    )
    return DelayBundle(model, features, categorical, spec, metadata or {})


def predict_delay(frame: pd.DataFrame, bundle: DelayBundle) -> np.ndarray:
    residual = bundle.model.predict(matrix(frame, bundle.features, bundle.categorical))
    return np.asarray(residual, dtype=float) + frame["cur_dev_s"].to_numpy(float)


def mae(actual, predicted) -> float:
    return float(np.mean(np.abs(np.asarray(actual, dtype=float) - np.asarray(predicted))))

