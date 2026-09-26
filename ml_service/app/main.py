"""Autonomous ML inference microservice for transport delay prediction.

Receives the model's 24 features from the backend, runs the trained CatBoost
bundle (residual: pred = cur_dev_s + catboost) and returns
{prediction_delay_s, late_probability, model_version}.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor
from fastapi import FastAPI
from pydantic import BaseModel

MODEL_DIR = Path(os.environ.get("MODEL_DIR", "artifacts/modeling/model"))
MANIFEST_PATH = MODEL_DIR / "manifest.json"


def _build_request() -> type[BaseModel]:
    """Схема запроса — ровно те 24 признака, что ожидает модель (из manifest.json)."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    annotations = {name: Optional[float] for name in manifest["feature_names"]}
    defaults = {name: None for name in manifest["feature_names"]}
    return type("PredictionRequest", (BaseModel,), {"__annotations__": annotations, **defaults})


PredictionRequest = _build_request()


class _Model:
    def __init__(self):
        self.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.feature_names = self.manifest["feature_names"]
        self.residual = self.manifest["spec"]["formulation"] == "residual"
        self.model = CatBoostRegressor()
        self.model.load_model(str(MODEL_DIR / "model_0.cbm"))
        self.version = f"catboost:{os.environ.get('MODEL_VERSION', '01_baseline')}"

    def predict(self, req) -> float:
        row = {name: getattr(req, name) for name in self.feature_names}
        df = pd.DataFrame([{k: (None if pd.isna(v) else v) for k, v in row.items()}])
        pred = float(self.model.predict(df)[0])
        return (req.cur_dev_s or 0.0) + pred if self.residual else pred


model = _Model()
app = FastAPI(title="ML Service", version="1.0.0")


@app.get("/health")
def health():
    return {"status": "ok", "model_version": model.version}


@app.post("/predict")
def predict(req: PredictionRequest):
    return {
        "prediction_delay_s": round(model.predict(req), 3),
        "late_probability": None,
        "model_version": model.version,
    }