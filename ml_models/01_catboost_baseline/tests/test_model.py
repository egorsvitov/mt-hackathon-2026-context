import numpy as np
import pandas as pd

from transport_delay.model import ModelBundle, ModelSpec, _fit_one, predict, train_bundle


def test_residual_fit_does_not_mutate_targets():
    train = pd.DataFrame(
        {
            "cur_dev_s": [1.0, 2.0, 3.0, 4.0],
            "target_delay_s": [5.0, 8.0, 7.0, 12.0],
        }
    )
    valid = train.copy()
    expected_train = train["target_delay_s"].copy()
    expected_valid = valid["target_delay_s"].copy()
    _fit_one(
        train,
        valid,
        ["cur_dev_s"],
        ModelSpec(formulation="residual", iterations=2),
        seed=42,
    )
    pd.testing.assert_series_equal(train["target_delay_s"], expected_train)
    pd.testing.assert_series_equal(valid["target_delay_s"], expected_valid)


def test_spatial_categories_are_saved_in_bundle_and_predictable(tmp_path):
    frame = pd.DataFrame(
        {
            "cur_dev_s": [1.0, 2.0, 3.0, 4.0],
            "target_delay_s": [5.0, 8.0, 7.0, 12.0],
            "mm_segment_progress": [0.1, 0.2, 0.3, 0.4],
            "mm_route_pattern_id": ["a", "a", "b", None],
        }
    )
    bundle = train_bundle(frame, ModelSpec(groups=("spatial",), iterations=2))
    assert bundle.categorical_feature_names == ["mm_route_pattern_id"]
    expected = predict(frame, bundle)
    assert np.isfinite(expected).all()
    bundle.save(tmp_path)
    loaded = ModelBundle.load(tmp_path)
    np.testing.assert_allclose(predict(frame, loaded), expected)
