import pandas as pd

from transport_delay.model import ModelSpec, _fit_one


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
