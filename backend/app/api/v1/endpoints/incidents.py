from fastapi import APIRouter
from app.schemas.incident import IncidentResponse

router = APIRouter()


@router.get("/incidents", response_model=list[IncidentResponse])
async def get_active_incidents():
    """Возвращает список ТС с высоким риском задержки в горизонте 10-15 минут."""
    # Заглушка для первичной отрисовки дашборда Максом
    return [
        IncidentResponse(
            tr_id="BUS_104",
            target_stop_id="STOP_12",
            target_stop_name="Улица Новый Арбат",
            predicted_delay_sec=340.0,
            delay_probability=0.88,
            risk_level="RED",
            predicted_cause="Аномальное снижение скорости на перегоне",
            forecast_horizon_min=12.5,
        )
    ]