from pathlib import Path

import pandas as pd

from transport_delay.data import read_schedule


def test_factual_schedule_time_is_never_loaded(tmp_path: Path):
    common = {
        "tt_action_item_id": [1],
        "time_begin": ["2026-01-06 10:00:00"],
        "order_date": ["2026-01-06"],
        "manual_fill": [True],
        "tr_id": [7],
        "geom": ["POINT (37.6 55.7)"],
        "building_address": ["stop"],
    }
    first = pd.DataFrame({**common, "time_fact_begin": ["2026-01-06 10:01:00"]})
    second = pd.DataFrame({**common, "time_fact_begin": ["2026-01-06 23:59:00"]})
    first.to_csv(tmp_path / "a.csv", index=False)
    second.to_csv(tmp_path / "b.csv", index=False)
    pd.testing.assert_frame_equal(read_schedule(tmp_path / "a.csv"), read_schedule(tmp_path / "b.csv"))
