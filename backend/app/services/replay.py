"""Воспроизведение исторического дня из CSV, как будто это поток NDTP.

Часы идут со скоростью speed, записи с event_time не позже часов уходят в pipeline.ingest.
При имитации обрыва часы идут, а записи копятся и догружаются после восстановления.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd

from app.core.config import settings
from app.schemas.telemetry import RawNDTPRecord
from app.services.network import naive_to_epoch

log = logging.getLogger(__name__)
PREROLL_SEC = 1200
TICK_SEC = 0.2


class ReplayFeeder:
    """Проигрывает исторический день из traffic.csv через тот же ingest, что и поток NDTP."""
    def __init__(self, pipeline):
        self.p = pipeline
        self.active = False
        self.link_down = False
        self.speed = 1.0
        self.t0 = self.w0 = 0.0
        self.idx = 0
        self.task: asyncio.Task | None = None
        self.t: np.ndarray | None = None
        self.records: list[RawNDTPRecord] = []
        # без блокировки перемотка и автоперезапуск дня могли запустить две подачи сразу
        self.lock = asyncio.Lock()
        self.gen = 0

    def load(self, split: str) -> None:
        """Читает телеметрию дня и готовит записи, отсортированные по времени."""
        path = Path(settings.DATA_DIR) / split / "traffic.csv"
        df = pd.read_csv(
            path,
            usecols=[
                "tr_id",
                "unit_id",
                "event_time",
                "location_valid",
                "lon",
                "lat",
                "speed",
                "heading",
            ],
        )
        df["t"] = naive_to_epoch(df["event_time"])
        df = df.sort_values("t", kind="stable")
        valid = (
            df["location_valid"].astype(str).str.lower().eq("true")
            & df["lat"].notna()
            & df["lon"].notna()
        )
        self.t = df["t"].to_numpy(float)
        self.records = [
            RawNDTPRecord(
                tr_id=str(tr),
                timestamp=int(round(t)),
                unit_id=None if pd.isna(u) else int(u),
                lat=float(la) if ok else None,
                lon=float(lo) if ok else None,
                speed=None if pd.isna(s) else float(s),
                heading=None if pd.isna(h) else float(h),
                location_valid=bool(ok),
                source="replay",
            )
            for tr, t, u, la, lo, s, h, ok in zip(
                df["tr_id"],
                df["t"],
                df["unit_id"],
                df["lat"],
                df["lon"],
                df["speed"],
                df["heading"],
                valid,
            )
        ]
        log.info("Replay: %d записей из %s", len(self.records), path)

    @property
    def day_bounds(self) -> tuple[float, float]:
        """Первая и последняя отметка дня."""
        return float(self.t[0]), float(self.t[-1])

    def clock(self) -> float:
        """Текущее время воспроизведения с учётом скорости."""
        if not self.active:
            return self.t0
        return self.t0 + (time.monotonic() - self.w0) * self.speed

    def parse_start(self, hhmm: str | None) -> float:
        """Переводит время вида 08:30 в метку этого момента в воспроизводимом дне."""
        first = self.t[0]
        midnight = first - ((first + settings.TZ_OFFSET_HOURS * 3600) % 86400)
        h, m = (hhmm or settings.REPLAY_START).split(":")
        return midnight + int(h) * 3600 + int(m) * 60

    async def start(self, speed: float | None = None, start: str | None = None) -> None:
        """Запускает воспроизведение с нуля с указанного времени.

        Перед стартом прогоняет 20 минут истории, чтобы у ТС уже были трек и прибытия.
        """
        if self.t is None:
            self.load(settings.REPLAY_SPLIT)
        async with self.lock:
            await self.stop()
            gen = self.gen
            self.speed = settings.REPLAY_SPEED if speed is None else max(0.0, float(speed))
            begin = self.parse_start(start)
            self.p.reset()
            self.link_down = False
            self.idx = int(np.searchsorted(self.t, begin - PREROLL_SEC))
            self.active = True
            self.t0, self.w0 = begin, time.monotonic()
            await self._feed_until(begin)
            if gen != self.gen:  # пока шёл предпрогон, воспроизведение остановили
                return
            self.task = asyncio.create_task(self._run())
            log.info("Replay: старт %s, x%s", start or settings.REPLAY_START, self.speed)

    async def stop(self) -> None:
        """Останавливает воспроизведение, часы замирают на текущем моменте."""
        self.gen += 1
        if self.task:
            self.t0 = self.clock()
            self.active = False
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        self.active = False

    def set_speed(self, speed: float) -> None:
        """Меняет скорость без сброса состояния, 0 ставит на паузу."""
        self.t0, self.w0 = self.clock(), time.monotonic()
        self.speed = max(0.0, float(speed))

    def set_link(self, down: bool) -> None:
        """Имитирует обрыв связи: часы идут, а записи не подаются до восстановления."""
        if self.link_down and not down:
            self.p.reconnects += 1
        self.link_down = down

    async def _feed_until(self, until: float) -> None:
        """Подаёт в pipeline все записи до момента until порциями по 500."""
        end = int(np.searchsorted(self.t, until, side="right"))
        while self.idx < end:
            batch_end = min(end, self.idx + 500)
            for rec in self.records[self.idx : batch_end]:
                await self.p.ingest(rec)
            self.idx = batch_end
            await asyncio.sleep(0)

    async def _run(self) -> None:
        """Основной цикл: подаёт новые записи и сверяет прогнозы с фактом."""
        try:
            while self.active:
                now = self.clock()
                if now > self.t[-1]:
                    # день закончился, начинаем заново отдельной задачей
                    self.active = False
                    asyncio.get_running_loop().create_task(self.start(self.speed, None))
                    return
                if not self.link_down:
                    await self._feed_until(now)
                if self.p.evaluator:
                    self.p.evaluator.advance(now, self.p.incidents)
                await asyncio.sleep(TICK_SEC)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Replay остановлен из-за ошибки")
            self.active = False
