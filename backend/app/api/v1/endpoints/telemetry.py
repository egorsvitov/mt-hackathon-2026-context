from fastapi import APIRouter
from app.api.websocket.dashboard_ws import manager
from app.schemas.incident import IncidentResponse
from app.schemas.telemetry import RawNDTPRecord
from app.services.feature_extractor import feature_extractor
from app.services.ml_client import ml_client

router = APIRouter()


@router.post("/telemetry", response_model=dict)
async def process_telemetry(record: RawNDTPRecord):
    # 1. Извлекаем признаки строго до T
    features = feature_extractor.extract_features(record)

    prediction: IncidentResponse | None = None
    if features:
        # 2. Получаем предикт от ML-модуля
        prediction = await ml_client.get_prediction(features)

        # 3. Шлем обновление в сокет дашборда Макса
        await manager.broadcast(
            {
                "type": "telemetry_update",
                "tr_id": record.tr_id,
                "lat": record.lat,
                "lon": record.lon,
                "speed": record.speed,
                "prediction": prediction.model_dump(),
            }
        )

    return {"status": "success", "processed_tr_id": record.tr_id}