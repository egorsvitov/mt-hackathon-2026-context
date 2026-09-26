from __future__ import annotations

import pandas as pd
from route_matching.types import StopGroup, Visit, timestamp

from route_delay_v2.events import _event_for_stop


def stop() -> StopGroup:
    visit = Visit("v1", 1, timestamp("2026-01-06 10:00", "Europe/Moscow"), "s1", (37.0, 55.0))
    return StopGroup("s1", (37.0, 55.0), (visit,))


def telemetry(speeds, seconds=None):
    seconds = seconds or list(range(0, len(speeds) * 15, 15))
    return pd.DataFrame(
        {
            "event_time": [pd.Timestamp("2026-01-06 10:00") + pd.Timedelta(seconds=s) for s in seconds],
            "location_valid": True,
            "lon": [37.0] * len(speeds),
            "lat": [55.0] * len(speeds),
            "speed": speeds,
        }
    )


def test_detector_distinguishes_dwell_and_pass_through():
    dwell = _event_for_stop(telemetry([10, 0, 0, 10]), stop(), 0)
    passing = _event_for_stop(telemetry([15, 15, 15]), stop(), 0)
    assert dwell is not None and not dwell.pass_through
    assert (dwell.departure - dwell.arrival).total_seconds() > 0
    assert passing is not None and passing.pass_through
    assert passing.arrival == passing.departure


def test_detector_rejects_large_gap():
    event = _event_for_stop(telemetry([0, 0], [0, 60]), stop(), 0)
    assert event is None

