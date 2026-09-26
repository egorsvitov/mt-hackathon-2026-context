from __future__ import annotations

import numpy as np
import pandas as pd

from route_delay_v2.model import DelayBundle, DelaySpec, fit_delay, predict_delay
from route_delay_v2.spatial import assert_no_temporal_catalog_features


def frame(count=40):
    values = np.linspace(-100, 100, count)
    return pd.DataFrame(
        {
            "cur_dev_s": values,
            "cur_dev_abs_s": np.abs(values),
            "horizon_s": 700.0,
            "time_sin": 0.0,
            "time_cos": 1.0,
            "hour": 10.0,
            "last_speed": 20.0,
            "last_heading": 90.0,
            "has_history": 1.0,
            "has_valid_gps": 1.0,
            "packet_age_s": 1.0,
            "gps_age_s": 1.0,
            "target_delay_s": values + np.linspace(5.0, 25.0, count),
        }
    )


def test_delay_bundle_roundtrip_and_residual(tmp_path):
    data = frame()
    bundle = fit_delay(data, DelaySpec("test", iterations=20))
    before = predict_delay(data, bundle)
    bundle.save(tmp_path)
    restored = DelayBundle.load(tmp_path)
    assert restored.categorical == []
    assert np.allclose(before, predict_delay(data, restored))


def test_temporal_catalog_fields_are_forbidden():
    try:
        assert_no_temporal_catalog_features(["mm_historical_segment_time_s"])
    except AssertionError:
        pass
    else:
        raise AssertionError("historical duration unexpectedly accepted")

