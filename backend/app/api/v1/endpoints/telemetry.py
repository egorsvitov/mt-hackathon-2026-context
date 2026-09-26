from fastapi import APIRouter

from app.schemas.telemetry import RawNDTPRecord
from app.services.pipeline import pipeline

router = APIRouter()


@router.post("/telemetry", response_model=dict)
async def process_telemetry(record: RawNDTPRecord):
    """Приём записи телеметрии (после парсинга NDTP или из внешнего replay).

    Запись обновляет состояние ТС и детектор прибытий; раз в минуту по времени событий
    для ТС строятся признаки на момент T, вызывается ML и обновляются прогноз и инциденты.
    """
    await pipeline.ingest(record)
    prediction = pipeline.predictions.get(record.tr_id)
    return {
        "status": "success",
        "processed_tr_id": record.tr_id,
        "prediction_delay_s": prediction["prediction_delay_s"] if prediction else None,
    }
