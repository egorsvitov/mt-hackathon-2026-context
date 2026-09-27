"""Identical 15-minute causal resampling used to train the production encoder."""
import numpy as np
import pandas as pd

STEPS=45
STEP_S=20
WINDOW_S=900
CHANNELS=['speed', 'heading_sin', 'heading_cos', 'gps_valid', 'age', 'east_delta', 'north_delta']

def ns(series):
    return series.to_numpy(dtype="datetime64[ns]").astype(np.int64)

def sequences(points, traffic):
    arrays = {int(v): g for v, g in traffic.groupby('tr_id')}
    values = np.zeros((len(points), STEPS, len(CHANNELS)), np.float32)
    values[:, :, 4] = 1
    packet_sets = []
    for i, p in enumerate(points.itertuples(index=False)):
        rows = arrays.get(int(p.tr_id))
        used = set()
        if rows is not None and len(rows):
            times = ns(rows['event_time'])
            grid = p.T.value + np.arange(-STEPS + 1, 1) * STEP_S * 10**9
            ix = np.searchsorted(times, grid, side='right') - 1
            safe = np.maximum(ix, 0)
            age = (grid - times[safe]) / 1e9
            present = (ix >= 0) & (age <= 60) & (times[safe] >= p.T.value - WINDOW_S * 10**9)
            gps = present & rows['location_valid'].to_numpy(bool)[safe]
            lon = rows['lon'].to_numpy(float)[safe]
            lat = rows['lat'].to_numpy(float)[safe]
            gps &= np.isfinite(lon) & np.isfinite(lat) & (lon >= 30) & (lon <= 45) & (lat >= 50) & (lat <= 60)
            speed = rows['speed'].to_numpy(float)[safe]
            speed = np.where(present & np.isfinite(speed) & (speed >= 0) & (speed <= 150), speed, 0)
            heading = np.nan_to_num(rows['heading'].to_numpy(float)[safe])
            east = np.zeros(STEPS); north = np.zeros(STEPS)
            pair = gps[1:] & gps[:-1]
            east[1:] = np.where(pair, np.diff(lon) * 111320 * np.cos(np.radians(lat[1:])), 0)
            north[1:] = np.where(pair, np.diff(lat) * 110540, 0)
            values[i] = np.column_stack([
                speed / 50, np.sin(np.radians(heading)) * present,
                np.cos(np.radians(heading)) * present, gps,
                np.where(present, age, 60) / 60,
                np.nan_to_num(east) / 100, np.nan_to_num(north) / 100,
            ])
            selected = safe[present]
            assert (times[selected] <= p.T.value).all()
            used = set(rows['packet_id'].to_numpy()[selected].tolist())
        packet_sets.append(used)
    assert np.isfinite(values).all()
    return values, packet_sets
