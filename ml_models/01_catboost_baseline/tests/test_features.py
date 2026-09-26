from __future__ import annotations

import pandas as pd
import pandas.testing as pdt

from transport_delay.features import build_features


def _inputs():
    points = pd.DataFrame(
        {
            "sample_id": ["1_1"],
            "tr_id": [1],
            "T": pd.to_datetime(["2026-01-06 10:00:00"]),
            "target_stop_id": [12],
            "target_time_begin": pd.to_datetime(["2026-01-06 10:12:00"]),
            "cur_dev_s": [30.0],
        }
    )
    traffic = pd.DataFrame(
        {
            "packet_id": [1, 2],
            "tr_id": [1, 1],
            "event_time": pd.Series(
                ["2026-01-06 09:59:00", "2026-01-06 10:00:00"], dtype="datetime64[ns]"
            ),
            "location_valid": pd.array([True, True], dtype="boolean"),
            "lon": [37.60, 37.601],
            "lat": [55.70, 55.701],
            "speed": [20.0, 15.0],
            "heading": [45.0, 45.0],
        }
    )
    schedule = pd.DataFrame(
        {
            "tt_action_item_id": [11, 12],
            "time_begin": pd.to_datetime(["2026-01-06 10:08:00", "2026-01-06 10:12:00"]),
            "tr_id": [1, 1],
            "geom": ["POINT (37.61 55.71)", "POINT (37.62 55.72)"],
            "building_address": ["a", "b"],
            "stop_lon": [37.61, 37.62],
            "stop_lat": [55.71, 55.72],
        }
    )
    return points, traffic, schedule


def test_future_events_do_not_change_snapshot():
    points, traffic, schedule = _inputs()
    before = build_features(points, traffic, schedule)
    future = traffic.iloc[[-1]].copy()
    future["packet_id"] = 3
    future["event_time"] = pd.Series(
        ["2026-01-06 10:01:00"], index=future.index, dtype="datetime64[ns]"
    )
    future["speed"] = 140.0
    after = build_features(points, pd.concat([traffic, future], ignore_index=True), schedule)
    pdt.assert_frame_equal(before, after)


def test_order_and_duplicate_do_not_change_snapshot():
    points, traffic, schedule = _inputs()
    expected = build_features(points, traffic, schedule)
    changed = pd.concat([traffic.iloc[::-1], traffic.iloc[[0]]], ignore_index=True)
    actual = build_features(points, changed, schedule)
    pdt.assert_frame_equal(expected, actual)


def test_missing_history_is_supported():
    points, traffic, schedule = _inputs()
    points["tr_id"] = 99
    schedule["tr_id"] = 99
    result = build_features(points, traffic.iloc[0:0], schedule)
    assert result.loc[0, "has_history"] == 0
    assert pd.isna(result.loc[0, "last_lon"])
