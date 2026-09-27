"""Production CatBoost v2 / GPU TS2Vec ensemble inference service."""
import os
from pathlib import Path
from fastapi import FastAPI, HTTPException
from runtime.inference import Predictor
from runtime.schema import PredictionRequest

MODEL_DIR = Path(os.environ.get('MODEL_DIR', str(Path(__file__).resolve().parents[1]/'model')))
model = Predictor(MODEL_DIR, os.environ.get('ML_MODE', 'catboost'))
app = FastAPI(title='Transport ML Service', version='2.0.0')


@app.get('/health')
def health():
    return {'status':'ok', 'model_version':model.version, 'requested_mode':model.requested_mode,
            'active_mode':model.mode, 'degraded_reason':model.degraded_reason}


@app.post('/predict')
def predict(req: PredictionRequest):
    try:
        value = model.predict(req)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {'prediction_delay_s':round(value, 3), 'late_probability':None, 'model_version':model.version}
