import json
import sys
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
from fastapi.testclient import TestClient

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'ml_service'),str(ROOT/'map_matching/src')]
from runtime.inference import Predictor
from runtime.schema import PredictionRequest
from runtime.stream import FeatureBuilder
from app.main import app, model
BUNDLE=ROOT/'ml_service/model'

def request():
    fields={k:None for k in model.manifest['input_feature_names']}
    fields.update(cur_dev_s=40,cur_dev_abs_s=40,horizon_s=700,hour=8)
    return PredictionRequest(sample_id='test',tr_id=123,t_timestamp=1000,target_stop_id=2,
        planned_arrival_time=1700,cur_dev_s=40,route_features=fields,
        telemetry_window=[[0,0,0,0,1,0,0] for _ in range(45)])

def test_http_contract():
    with TestClient(app) as client:
        assert client.get('/health').json()['active_mode']=='catboost'
        response=client.post('/predict',json=request().model_dump())
        assert response.status_code==200
        assert response.json()['model_version']=='catboost:v2-seed42'
        assert np.isfinite(response.json()['prediction_delay_s'])
        invalid=request().model_dump();invalid['telemetry_window']=[]
        assert client.post('/predict',json=invalid).status_code==422

def test_contract_rejects_mismatched_features():
    req=request();req.route_features.pop('hour')
    with pytest.raises(ValueError):model.predict(req)
    req=request();req.route_features['cur_dev_s']=41
    with pytest.raises(ValueError):model.predict(req)

def test_encoder_failure_degrades(monkeypatch):
    import builtins
    original=builtins.__import__
    def unavailable(name,*args,**kwargs):
        if name in ('encoder','torch'):raise ImportError('simulated unavailable encoder')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',unavailable)
    degraded=Predictor(BUNDLE,'ensemble')
    assert degraded.mode=='catboost' and degraded.degraded_reason
    assert degraded.predict(request())==model.predict(request())

def test_bundle_checksums():
    for name in model.manifest['artifacts']:model._verify(name)

def test_checksum_failure(monkeypatch):
    monkeypatch.setitem(model.manifest['artifacts'],'baseline_42.cbm','invalid')
    with pytest.raises(ValueError,match='checksum'):model._verify('baseline_42.cbm')

def test_encoder_inference_failure_degrades():
    degraded=Predictor(BUNDLE)
    class BrokenEncoder:
        def encode(self,raw):raise RuntimeError('simulated GPU failure')
    degraded.mode='ensemble'
    degraded.encoders={42:BrokenEncoder()}
    assert degraded.predict(request())==model.predict(request())
    assert degraded.mode=='catboost' and 'simulated GPU failure' in degraded.degraded_reason

def test_stream_causality_invalid_duplicates_reset():
    builder=FeatureBuilder(BUNDLE)
    builder.register_plan(123,[1,2],[900,1700],[37.4,37.5],[55.4,55.5])
    def ingest(t,packet,valid=True):
        builder.ingest(SimpleNamespace(tr_id=123,timestamp=t,packet_id=packet,location_valid=valid,
            lon=37.4 if valid else None,lat=55.4 if valid else None,speed=20,heading=90))
    ingest(980.25,'a');ingest(999.5,'b',False);ingest(999.5,'b',False)
    req=builder.request(123,1000,2,1700,40)
    assert len(builder.rows[123])==2
    assert req.route_features['packet_age_s']==pytest.approx(.5)
    assert req.route_features['gps_age_s']==pytest.approx(19.75)
    assert req.route_features['previous_stop_lon']==37.4
    assert req.route_features['previous_stop_lat']==55.4
    assert req.telemetry_window[-1][3]==0
    ingest(2000,'future')
    assert builder.request(123,1000,2,1700,40)==req
    ingest(990,'late',False)
    assert builder.request(123,1000,2,1700,40).route_features['packet_age_s']==.5
    builder.reset();assert not builder.rows and not builder.plans
    builder.close()

def test_empty_history_missing_route():
    builder=FeatureBuilder(BUNDLE)
    builder.register_plan(123,[1,2],[900,1700],[37.4,37.5],[55.4,55.5])
    req=builder.request(123,1000,2,1700,40)
    assert req.sequence_id is None and req.route_features['has_history']==0
    assert all(row[4]==1 for row in req.telemetry_window)
    assert np.isfinite(model.predict(req));builder.close()

def test_saved_catboost_fixture():
    fixture=json.loads((Path(__file__).parent/'fixtures/prediction.json').read_text())
    assert model.predict(PredictionRequest.model_validate(fixture['request']))==pytest.approx(fixture['prediction'],rel=1e-6,abs=1e-6)
