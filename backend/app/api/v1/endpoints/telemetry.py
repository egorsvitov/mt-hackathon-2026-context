from fastapi import APIRouter

from app.schemas.telemetry import RawNDTPRecord
from app.services.pipeline import pipeline

router = APIRouter()


@router.post("/telemetry", response_model=dict)
async def process_telemetry(record: RawNDTPRecord):
    """Принимает одну точку телеметрии от NDTP-парсера.

    Точка обновляет трек ТС и детектор прибытий. Раз в минуту по времени событий для ТС
    считаются признаки, вызывается модель и обновляются прогноз и инциденты.
    """
    await pipeline.ingest(record)
    prediction = pipeline.predictions.get(record.tr_id)
    return {
        "status": "success",
        "processed_tr_id": record.tr_id,
        "prediction_delay_s": prediction["prediction_delay_s"] if prediction else None,
    }
