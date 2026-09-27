from fastapi import APIRouter

from app.schemas.dashboard import Incident
from app.services.pipeline import pipeline

router = APIRouter()


@router.get("", response_model=list[Incident])
async def get_incidents():
    """Активные инциденты и закрытые за последние 15 минут.

    Инцидент это эпизод риска по ТС: прогноз опоздания или опережения на ближайшие 10-15 минут,
    участок маршрута, вероятная причина и что посоветовать диспетчеру.
    """
    return pipeline.incidents_out()
