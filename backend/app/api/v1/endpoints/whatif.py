"""Меры диспетчера: резервный автобус и сокращение стоянок."""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.pipeline import pipeline

router = APIRouter()


class ReserveRequest(BaseModel):
    """Запрос на выпуск резервного автобуса."""
    tr_id: str = Field(..., description="ТС, на линию которого выпускается резерв")
    ready_min: float = Field(10, ge=0, le=120, description="Через сколько минут резерв готов к выходу")
    start_visit_id: int | None = Field(
        None, description="Посещение, с которого резерв берёт рейс. По умолчанию ближайшее, к которому он успевает"
    )


class DwellRequest(BaseModel):
    """Запрос на сокращение стоянок."""
    tr_id: str
    scope: Literal["vehicle", "route"] = Field("vehicle", description="Только это ТС или вся линия")
    cut_s: float = Field(10, ge=0, le=60, description="На сколько секунд короче стоянка на каждой остановке")
    short_layover: bool = Field(True, description="Сократить отстой на конечной до 2 мин")


def _whatif():
    """Сервис мер или 503, если backend ещё не запустился."""
    if pipeline.whatif is None:
        raise HTTPException(503, "Меры недоступны: backend ещё не запущен")
    return pipeline.whatif


@router.get("")
async def list_measures():
    """Действующие меры и их эффект, пересчитанный по свежим прогнозам."""
    return _whatif().list_out()


@router.post("/reserve")
async def release_reserve(req: ReserveRequest):
    """Выпускает резервный автобус на рейс опаздывающего ТС."""
    try:
        return _whatif().add_reserve(str(req.tr_id), req.ready_min, req.start_visit_id)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'")) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.post("/dwell")
async def shorten_dwell(req: DwellRequest):
    """Сокращает стоянки и отстой на конечной для ТС или всей линии."""
    try:
        return _whatif().add_dwell(str(req.tr_id), req.scope, req.cut_s, req.short_layover)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'")) from e


@router.delete("/{measure_id}")
async def cancel_measure(measure_id: int):
    """Отменяет меру. Резервный автобус снимается с линии."""
    if not _whatif().remove(measure_id):
        raise HTTPException(404, "Мера не найдена")
    return {"status": "cancelled", "id": measure_id}
