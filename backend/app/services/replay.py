"""CSV replay: исторический день подаётся в онлайн-контур так, будто это поток NDTP.

Виртуальные часы идут со скоростью ``speed``; все записи telemetry с ``event_time <= часы``
передаются в ``pipeline.ingest`` в порядке времени. Перед стартом прогоняются 20 минут
истории, чтобы у ТС было состояние и подтверждённые прибытия.

Имитация обрыва (``link_down``): часы идут, записи не подаются; после восстановления
накопленные записи догружаются — так проверяется деградация и восстановление (критерий 5).
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
        # start() ждёт предпрогон до того, как запомнит задачу: без блокировки два старта
        # (автоперезапуск дня и перемотка) шли бы параллельно по общему self.idx.
        self.lock = asyncio.Lock()
        self.gen = 0  # поколение: stop() во время предпрогона отменяет начатый старт

    def load(self, split: str) -> None:
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
        return float(self.t[0]), float(self.t[-1])

    def clock(self) -> float:
        if not self.active:
            return self.t0
        return self.t0 + (time.monotonic() - self.w0) * self.speed

    def parse_start(self, hhmm: str | None) -> float:
        """'08:30' -> epoch этого времени в день воспроизведения."""
        first = self.t[0]
        midnight = first - ((first + settings.TZ_OFFSET_HOURS * 3600) % 86400)
        h, m = (hhmm or settings.REPLAY_START).split(":")
        return midnight + int(h) * 3600 + int(m) * 60

    async def start(self, speed: float | None = None, start: str | None = None) -> None:
        if self.t is None:
            self.load(settings.REPLAY_SPLIT)
        async with self.lock:
            await self.stop()
            gen = self.gen
            self.speed = settings.REPLAY_SPEED if speed is None else max(0.0, float(speed))
            begin = self.parse_start(start)
            self.p.reset()
            self.link_down = False
            # Предпрогон: 20 минут истории до старта — состояние ТС и прибытия.
            self.idx = int(np.searchsorted(self.t, begin - PREROLL_SEC))
            self.active = True
            self.t0, self.w0 = begin, time.monotonic()
            await self._feed_until(begin)
            if gen != self.gen:  # за время предпрогона воспроизведение остановили
                return
            self.task = asyncio.create_task(self._run())
            log.info("Replay: старт %s, x%s", start or settings.REPLAY_START, self.speed)

    async def stop(self) -> None:
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
        """Сменить скорость часов без сброса состояния; 0 — пауза."""
        self.t0, self.w0 = self.clock(), time.monotonic()
        self.speed = max(0.0, float(speed))

    def set_link(self, down: bool) -> None:
        if self.link_down and not down:
            self.p.reconnects += 1
        self.link_down = down

    async def _feed_until(self, until: float) -> None:
        end = int(np.searchsorted(self.t, until, side="right"))
        while self.idx < end:
            batch_end = min(end, self.idx + 500)
            for rec in self.records[self.idx : batch_end]:
                await self.p.ingest(rec)
            self.idx = batch_end
            await asyncio.sleep(0)  # не блокировать API при догрузке

    async def _run(self) -> None:
        try:
            while self.active:
                now = self.clock()
                if now > self.t[-1]:
                    # День закончился — по кругу; перезапуск отдельной задачей (start отменяет текущую).
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
