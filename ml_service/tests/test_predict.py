"""Unit tests for the ML service: contract of the 24 model features and residual output."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.main import app, model, PredictionRequest


@pytest.fixture
def client():
    return TestClient(app)


def make_req(**overrides):
    req = {name: None for name in model.feature_names}
    req.update({
        "cur_dev_s": 40.0,
        "cur_dev_abs_s": 40.0,
        "horizon_s": 700.0,
        "hour": 8.0,
        "last_speed": 25.0,
        "last_lon": 37.4,
        "last_lat": 55.5,
        "target_lon": 37.5,
        "target_lat": 55.6,
        "distance_to_target_m": 5000.0,
        "remaining_visits": 3,
        "target_leg_planned_s": 300.0,
        "schedule_progress": 0.5,
    })
    req.update(overrides)
    return req


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_request_schema_matches_model_features():
    assert set(PredictionRequest.model_fields) == set(model.feature_names)


def test_predict_contract(client):
    r = client.post("/predict", json=make_req())
    assert r.status_code == 200
    body = r.json()
    assert "prediction_delay_s" in body
    assert "model_version" in body
    assert isinstance(body["prediction_delay_s"], float)


def test_residual_adds_cur_dev(client):
    # Residual: pred = cur_dev_s + catboost(...). Higher cur_dev raises the prediction.
    low = client.post("/predict", json=make_req(cur_dev_s=0.0)).json()["prediction_delay_s"]
    high = client.post("/predict", json=make_req(cur_dev_s=100.0)).json()["prediction_delay_s"]
    assert high >= low


def test_missing_optional_features_ok(client):
    r = client.post("/predict", json=make_req())
    assert r.status_code == 200
    assert isinstance(r.json()["prediction_delay_s"], float)