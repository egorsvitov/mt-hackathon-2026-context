"""Меры диспетчера (what-if): дополнительный автобус на линии и сокращение стоянок."""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.pipeline import pipeline

router = APIRouter()


class ReserveRequest(BaseModel):
    tr_id: str = Field(..., description="ТС, на линию которого выпускается резерв")
    ready_min: float = Field(10, ge=0, le=120, description="Через сколько минут резерв готов к выходу")
    start_visit_id: int | None = Field(
        None, description="Посещение, с которого резерв берёт рейс; по умолчанию — ближайшее, к которому успевает"
    )


class DwellRequest(BaseModel):
    tr_id: str
    scope: Literal["vehicle", "route"] = Field("vehicle", description="Только это ТС или вся линия")
    cut_s: float = Field(10, ge=0, le=60, description="На сколько секунд короче стоянка на каждой остановке")
    short_layover: bool = Field(True, description="Сократить отстой на конечной до 2 мин")


def _whatif():
    if pipeline.whatif is None:
        raise HTTPException(503, "Меры недоступны: backend ещё не запущен")
    return pipeline.whatif


@router.get("")
async def list_measures():
    """Действующие меры и их эффект на текущий момент (пересчитывается по свежим прогнозам)."""
    return _whatif().list_out()


@router.post("/reserve")
async def release_reserve(req: ReserveRequest):
    """Выпустить дополнительный автобус: берёт рейс с остановки, к которой успевает, и идёт по графику."""
    try:
        return _whatif().add_reserve(str(req.tr_id), req.ready_min, req.start_visit_id)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'")) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/dwell")
async def shorten_dwell(req: DwellRequest):
    """Сократить стоянки на остановках (и отстой на конечной) для ТС или всей линии."""
    try:
        return _whatif().add_dwell(str(req.tr_id), req.scope, req.cut_s, req.short_layover)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'")) from e


@router.delete("/{measure_id}")
async def cancel_measure(measure_id: int):
    """Отменить меру; резервный автобус снимается с линии."""
    if not _whatif().remove(measure_id):
        raise HTTPException(404, "Мера не найдена")
    return {"status": "cancelled", "id": measure_id}
