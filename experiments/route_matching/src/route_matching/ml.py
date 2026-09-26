"""Measured CatBoost ablation: same rows/split/model, spatial columns are the only change."""
from __future__ import annotations

import json
from pathlib import Path


def catboost_ablation(cache: Path, spatial: Path, output: Path) -> dict:
    try:
        import numpy as np
        import pandas as pd
        from catboost import CatBoostRegressor
    except ImportError as exc:
        raise RuntimeError("Install the project with the 'ml' extra") from exc
    from .replay import read_feature_rows

    train = pd.read_parquet(cache / "train.parquet")
    test = pd.read_parquet(cache / "test.parquet")
    test_ids = set(test["tr_id"])
    train = train[train["tr_id"].isin(test_ids)].copy()
    mm_train = pd.DataFrame(read_feature_rows(spatial / "train.jsonl"))
    mm_test = pd.DataFrame(read_feature_rows(spatial / "test.jsonl"))
    train = train.merge(mm_train, on="sample_id", validate="one_to_one")
    test = test.merge(mm_test, on="sample_id", validate="one_to_one")
    base = [c for c in ("cur_dev_s", "cur_dev_abs_s", "horizon_s", "time_sin", "time_cos",
                         "hour", "last_speed", "last_heading", "has_history", "has_valid_gps",
                         "packet_age_s", "gps_age_s") if c in train]
    spatial_columns = [c for c in train if c.startswith("mm_")]
    if not spatial_columns:
        raise ValueError("No map-matching features")
    # The final 20% in time is a causal local gate; the supplied test remains untouched.
    train = train.sort_values("T")
    split = int(len(train) * .8)
    fit, valid = train.iloc[:split], train.iloc[split:]

    def run(features):
        model = CatBoostRegressor(loss_function="MAE", eval_metric="MAE", iterations=800,
                                  learning_rate=.03, depth=4, l2_leaf_reg=30, random_seed=42,
                                  allow_writing_files=False, verbose=False, thread_count=4)
        target = fit["target_delay_s"] - fit["cur_dev_s"]
        valid_target = valid["target_delay_s"] - valid["cur_dev_s"]
        model.fit(fit[features], target, eval_set=(valid[features], valid_target),
                  early_stopping_rounds=100, use_best_model=True)
        local = valid["cur_dev_s"].to_numpy() + model.predict(valid[features])
        # Refit the exact selected iteration on all causal training rows before test.
        iterations = max(1, model.get_best_iteration() + 1)
        final = CatBoostRegressor(loss_function="MAE", iterations=iterations, learning_rate=.03,
                                  depth=4, l2_leaf_reg=30, random_seed=42,
                                  allow_writing_files=False, verbose=False, thread_count=4)
        final.fit(train[features], train["target_delay_s"] - train["cur_dev_s"])
        predicted = test["cur_dev_s"].to_numpy() + final.predict(test[features])
        return {"features": features, "iterations": iterations,
                "local_mae": float(np.mean(np.abs(valid["target_delay_s"] - local))),
                "test_mae": float(np.mean(np.abs(test["target_delay_s"] - predicted)))}

    baseline, candidate = run(base), run(base + spatial_columns)
    report = {"rows": {"train": len(train), "test": len(test)}, "baseline": baseline,
              "spatial": candidate, "local_improvement_s": baseline["local_mae"] - candidate["local_mae"],
              "test_improvement_s": baseline["test_mae"] - candidate["test_mae"]}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    return report
