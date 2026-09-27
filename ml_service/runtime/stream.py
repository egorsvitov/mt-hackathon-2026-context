"""Bounded streaming ML state, separate from dashboard/arrival-tracker state."""
from collections import defaultdict
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
from route_matching import Catalog, Event, Matcher
from route_matching.data import normalize_events

from .features import build_features
from .schema import PredictionRequest
from .sequences import sequences
from .spatial import _static_row


def local_time(epoch):
    return pd.Timestamp(epoch, unit='s', tz='UTC').tz_convert('Europe/Moscow').tz_localize(None).as_unit('ns')


def finite(value):
    return None if value is None or (isinstance(value, (float, np.floating)) and not np.isfinite(value)) else value


class FeatureBuilder:
    def __init__(self, directory):
        directory = Path(directory)
        self.manifest = json.loads((directory/'manifest.json').read_text())
        self.catalog = Catalog.load(directory/'catalog.json')
        self.matcher = Matcher(self.catalog)
        self.reset()

    def reset(self):
        self.matcher.close()
        self.matcher = Matcher(self.catalog)
        self.rows = defaultdict(list)
        self.seen = defaultdict(set)
        self.plans = {}
        self.matched_packets = set()
        self.matched_navigation = set()
        self.counter = 0
        self.watermarks = {}

    def close(self):
        self.matcher.close()

    def register_plan(self, tr_id, visit_ids, epochs, lon, lat):
        self.plans[int(tr_id)] = pd.DataFrame({'tr_id':int(tr_id), 'tt_action_item_id':visit_ids,
            'time_begin':pd.to_datetime([local_time(t) for t in epochs]).astype('datetime64[ns]'),
            'stop_lon':lon, 'stop_lat':lat})

    def ingest(self, record, visible_until=None):
        tr = int(record.tr_id); self.counter += 1
        packet = str(getattr(record, 'packet_id', None) or f'live:{self.counter}')
        key = packet
        if key in self.seen[tr]:
            return
        self.seen[tr].add(key)
        # Keep invalid GPS events: they still contain speed, heading and availability.
        row = {'tr_id':tr, 'packet_id':packet, 'epoch':float(record.timestamp),
            'receive_time':getattr(record, 'receive_time', None),
            'lon':np.nan if record.lon is None else record.lon,
            'lat':np.nan if record.lat is None else record.lat,
            'speed':np.nan if record.speed is None else record.speed,
            'heading':np.nan if record.heading is None else record.heading,
            'location_valid':record.location_valid}
        self.rows[tr].append(row)
        if visible_until is not None and visible_until > self.watermarks.get(tr, -float('inf')):
            self._advance(tr, visible_until)
            self._prune(tr, visible_until)
            self.watermarks[tr] = visible_until

    def request(self, tr_id, T, target_stop_id, target_time, cur_dev_s):
        tr = int(tr_id)
        point = SimpleNamespace(sample_id=f'{tr}_{int(T+10800)}', tr_id=tr, T=local_time(T),
            target_stop_id=int(target_stop_id), target_time_begin=local_time(target_time), cur_dev_s=float(cur_dev_s))
        visible = sorted((row for row in self.rows[tr] if row['epoch'] <= T), key=lambda r:r['epoch'])
        self._advance(tr, T)
        fields = ['tr_id', 'packet_id', 'lon', 'lat', 'speed', 'heading', 'location_valid']
        traffic = pd.DataFrame(visible, columns=fields+['epoch'])
        traffic['event_time'] = pd.to_datetime([local_time(row['epoch']) for row in visible]).astype('datetime64[ns]')
        points = pd.DataFrame([vars(point)])
        frame = build_features(points, traffic, self.plans[tr])
        state = self.matcher.snapshot(tr, T, str(target_stop_id), float(cur_dev_s))
        spatial = _static_row(state, point, self.catalog)
        assignment = self.catalog.assignment_by_visit.get(str(target_stop_id))
        spatial['mm_target_stop_index'] = assignment.stop_index if assignment else np.nan
        row = {**frame.iloc[0].to_dict(), **spatial}
        window, _ = sequences(points, traffic)
        request = PredictionRequest(sample_id=point.sample_id, tr_id=tr, t_timestamp=T,
            target_stop_id=int(target_stop_id), planned_arrival_time=target_time, cur_dev_s=cur_dev_s,
            sequence_id=state.sequence_id,
            route_features={name:finite(row[name]) for name in self.manifest['input_feature_names']},
            telemetry_window=window[0].tolist())
        # Retain 20 minutes plus last old packet/valid GPS for age features; preserve pending events.
        self._prune(tr, T)
        return request

    def _advance(self, tr, T):
        visible = sorted((row for row in self.rows[tr] if row['epoch'] <= T), key=lambda r:r['epoch'])
        # Future events must not advance/prune the model matcher before a forecast.
        pending = []
        for event in visible:
            if (tr, event['packet_id']) in self.matched_packets:
                continue
            speed = finite(event['speed'])
            speed = speed if speed is not None and 0 <= speed <= 150 else None
            heading = finite(event['heading'])
            pending.append(Event(tr, event['epoch'], finite(event['lon']), finite(event['lat']),
                speed, heading % 360 if heading is not None else None,
                bool(event['location_valid']), event['packet_id'], event['receive_time'] or event['epoch']))
        for event in sorted(normalize_events(pending), key=lambda e:(e.event_time,e.packet_id)):
            key = (tr, event.packet_id)
            if key in self.matched_packets:
                continue
            self.matched_packets.add(key)
            if event.key not in self.matched_navigation:
                self.matcher.update(event)
                self.matched_navigation.add(event.key)

    def _prune(self, tr, T):
        older = sorted((r for r in self.rows[tr] if r['epoch'] < T-1200), key=lambda r:r['epoch'])
        keep = [r for r in self.rows[tr] if r['epoch'] >= T-1200]
        if older:
            keep.append(older[-1])
            gps = [r for r in older if r['location_valid'] and np.isfinite(r['lon']) and np.isfinite(r['lat'])
                   and 30 <= r['lon'] <= 45 and 50 <= r['lat'] <= 60]
            if gps and gps[-1]['packet_id'] != older[-1]['packet_id']:
                keep.append(gps[-1])
        self.rows[tr] = keep
        self.seen[tr] = {r['packet_id'] for r in keep}
