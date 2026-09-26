from __future__ import annotations

import numpy as np
import pandas as pd

from route_delay_v2.components import fit_component


def run_events(count=20):
    return pd.DataFrame(
        {
            "route_pattern_id": ["rare"] * count,
            "direction_id": ["d"] * count,
            "segment_id": [f"s{i % 3}" for i in range(count)],
            "segment_length_m": np.linspace(100, 400, count),
            "planned_interval_s": np.linspace(60, 180, count),
            "hour": np.linspace(6, 20, count),
            "recent_speed_kmh": 20.0,
            "endpoint_distance_m": 5.0,
            "max_gap_s": 15.0,
            "run_time_s": np.linspace(65, 190, count),
            "event_time": pd.date_range("2026-01-06", periods=count, freq="10min"),
            "available_at": pd.date_range("2026-01-06", periods=count, freq="10min")
            + pd.Timedelta(minutes=2),
            "trip_occurrence_id": [f"t{i // 3}" for i in range(count)],
        }
    )


def test_rare_route_uses_global_fallback():
    frame = run_events()
    bundle = fit_component(frame, "run", enable_experts=True)
    prediction, expert = bundle.predict(frame.iloc[:2])
    assert np.isfinite(prediction).all()
    assert not expert.any()


def test_component_bundle_roundtrip(tmp_path):
    bundle = fit_component(run_events(), "run", enable_experts=False)
    before, _ = bundle.predict(run_events().iloc[:3])
    bundle.save(tmp_path)
    restored = type(bundle).load(tmp_path)
    predicted, _ = restored.predict(run_events().iloc[:3])
    assert np.allclose(before, predicted)

