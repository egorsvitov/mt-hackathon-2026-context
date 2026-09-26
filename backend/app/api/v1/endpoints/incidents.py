from fastapi import APIRouter

from app.schemas.dashboard import Incident
from app.services.pipeline import pipeline

router = APIRouter()


@router.get("", response_model=list[Incident])
async def get_incidents():
    """Инциденты: активные и закрытые за последние 15 минут.

    Инцидент — эпизод риска по ТС: прогноз опоздания/опережения в горизонте 10–15 минут,
    участок маршрута, предполагаемая причина и рекомендация диспетчеру.
    """
    return pipeline.incidents_out()
