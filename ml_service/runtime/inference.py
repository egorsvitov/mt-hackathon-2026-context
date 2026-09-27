"""Self-contained frozen models; CatBoost mode does not import torch."""
import hashlib
import json
import logging
from pathlib import Path
import numpy as np
import pandas as pd
from catboost import CatBoostRegressor
from route_matching import Catalog
from .components import ComponentBundle, add_component_predictions

log = logging.getLogger(__name__)


def matrix(frame, columns, categorical):
    result = frame[columns].copy()
    for column in categorical:
        result[column] = result[column].fillna('__unknown__').astype(str)
    for column in set(columns)-set(categorical):
        result[column] = pd.to_numeric(result[column], errors='coerce')
    return result


class Predictor:
    def __init__(self, directory: Path, mode='catboost'):
        if mode not in {'catboost', 'ensemble'}:
            raise ValueError('ML_MODE must be catboost or ensemble')
        self.directory = Path(directory)
        self.manifest = json.loads((self.directory/'manifest.json').read_text())
        self.requested_mode = mode
        self.mode = 'catboost'
        self.degraded_reason = None
        for name in ['catalog.json', 'baseline_42.cbm', 'run/global.cbm', 'dwell/global.cbm',
                     'run/manifest.json', 'dwell/manifest.json']:
            self._verify(name)
        self.catalog = Catalog.load(self.directory/'catalog.json')
        self.components = [ComponentBundle.load(self.directory/name) for name in ['run', 'dwell']]
        self.baseline = {42:self._cat('baseline_42.cbm')}
        self.hybrid = {}; self.encoders = {}
        if mode == 'ensemble':
            try:
                from .encoder import FrozenEncoder
                for seed in self.manifest['seeds']:
                    for name in [f'baseline_{seed}.cbm', f'hybrid_{seed}.cbm', f'encoder_{seed}.pt']:
                        self._verify(name)
                    self.baseline[seed] = self._cat(f'baseline_{seed}.cbm')
                    self.hybrid[seed] = self._cat(f'hybrid_{seed}.cbm')
                    self.encoders[seed] = FrozenEncoder(self.directory/f'encoder_{seed}.pt',
                        self.manifest['encoder_config'], self.manifest['normalization'])
                self.mode = 'ensemble'
            except Exception as exc:
                self._degrade(exc)

    def _verify(self, name):
        expected = self.manifest['artifacts'][name]
        if hashlib.sha256((self.directory/name).read_bytes()).hexdigest() != expected:
            raise ValueError(f'Model bundle checksum mismatch: {name}')

    def _cat(self, name):
        return CatBoostRegressor().load_model(str(self.directory/name))

    def _degrade(self, exc):
        self.mode = 'catboost'
        self.degraded_reason = f'{type(exc).__name__}: {exc}'
        self.encoders.clear(); self.hybrid.clear()
        log.warning('TS2Vec unavailable; using CatBoost v2 seed42: %s', self.degraded_reason)

    @property
    def version(self):
        return 'ensemble:v2-ts2vec-40-66-34' if self.mode == 'ensemble' else 'catboost:v2-seed42'

    def enrich(self, requests):
        rows = []
        expected = set(self.manifest['input_feature_names'])
        for req in requests:
            if set(req.route_features) != expected:
                raise ValueError('route_features must match the 62-field production manifest')
            row = dict(req.route_features)
            try:
                feature_cur = float(row['cur_dev_s'])
            except (TypeError, ValueError) as exc:
                raise ValueError('cur_dev_s must be a finite numeric feature') from exc
            if feature_cur != req.cur_dev_s:
                raise ValueError('cur_dev_s context and features disagree')
            row.update(sample_id=req.sample_id, mm_sequence_id=req.sequence_id or '__unknown__')
            rows.append(row)
        frame = pd.DataFrame(rows)
        for name in expected-set(self.manifest['categorical']):
            frame[name] = pd.to_numeric(frame[name], errors='coerce').astype(float)
        return add_component_predictions(frame, self.catalog, *self.components)

    def predict_many(self, requests):
        frame = self.enrich(requests)
        x = matrix(frame, self.manifest['feature_names'], self.manifest['categorical'])
        cur = frame.cur_dev_s.to_numpy(float)
        if self.mode == 'ensemble':
            try:
                raw = np.asarray([req.telemetry_window for req in requests], np.float32)
                base = []; hybrid = []
                for seed in self.manifest['seeds']:
                    base.append(self.baseline[seed].predict(x, thread_count=1)+cur)
                    z = self.encoders[seed].encode(raw)
                    extras = pd.DataFrame(z, index=frame.index, columns=[f'ts2vec_{i}' for i in range(32)])
                    enriched = pd.concat([frame, extras], axis=1)
                    xh = matrix(enriched, self.manifest['hybrid_feature_names'], self.manifest['categorical'])
                    hybrid.append(self.hybrid[seed].predict(xh, thread_count=1)+cur)
                weights = self.manifest['weights']
                result = weights['v2']*np.mean(base, axis=0)+weights['hybrid']*np.mean(hybrid, axis=0)
                if not np.isfinite(result).all():
                    raise FloatingPointError('Non-finite ensemble prediction')
                return result
            except Exception as exc:
                self._degrade(exc)
        result = self.baseline[42].predict(x, thread_count=1)+cur
        if not np.isfinite(result).all():
            raise FloatingPointError('Non-finite CatBoost prediction')
        return result

    def predict(self, request):
        return float(self.predict_many([request])[0])
