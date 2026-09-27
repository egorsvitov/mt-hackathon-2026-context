"""Инциденты: эпизоды риска по ТС, а не отдельная карточка на каждый прогноз."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.incident_rules import SEVERITY_ORDER

ALERT_KINDS = {"warning": "late", "critical": "late", "early": "early"}
COOLDOWN = 3
KEEP_RESOLVED_SEC = 900


@dataclass
class Episode:
    """Один инцидент по ТС: от первой тревоги до закрытия."""
    incident_id: str
    tr_id: str
    kind: str
    opened_at: float
    current: dict
    alert: dict
    peak: str
    closed_at: float | None = None
    calm: int = 0
    outcome_delay_s: float | None = None


class IncidentTracker:
    """Превращает поток прогнозов в инциденты, чтобы не заводить карточку на каждый прогноз."""
    def __init__(self):
        self.active: dict[str, Episode] = {}
        self.resolved: list[Episode] = []
        self.prev_kind: dict[str, str | None] = {}

    def reset(self) -> None:
        """Удаляет все инциденты."""
        self.active.clear()
        self.resolved.clear()
        self.prev_kind.clear()

    def update(self, tr: str, pred: dict) -> None:
        """Учитывает новый прогноз по ТС: открывает, обновляет или закрывает инцидент.

        Опоздание больше 2 минут открывает инцидент сразу, риск и опережение со второго прогноза
        подряд. Закрываем после нескольких спокойных прогнозов подряд.
        """
        sev = pred["severity"]
        kind = ALERT_KINDS.get(sev)
        ep = self.active.get(tr)
        if ep and kind and ep.kind != kind:
            self._close(ep, pred["as_of"])
            ep = None
        if kind:
            if ep is None and (sev == "critical" or self.prev_kind.get(tr) == kind):
                ep = Episode(
                    f"INC-{tr}-{int(pred['as_of'])}",
                    tr,
                    kind,
                    pred["as_of"],
                    pred,
                    pred,
                    sev,
                )
                self.active[tr] = ep
            if ep:
                ep.current = ep.alert = pred
                ep.calm = 0
                if SEVERITY_ORDER[sev] > SEVERITY_ORDER[ep.peak]:
                    ep.peak = sev
        elif ep:
            ep.current = pred
            ep.calm += 1
            if ep.calm >= COOLDOWN:
                self._close(ep, pred["as_of"])
        self.prev_kind[tr] = kind

    def drop(self, tr: str, t: float) -> None:
        """ТС ушло с линии, закрываем его инцидент."""
        ep = self.active.get(tr)
        if ep:
            self._close(ep, t)

    def _close(self, ep: Episode, t: float) -> None:
        """Закрывает инцидент и переносит его в недавно закрытые."""
        ep.closed_at = t
        self.active.pop(ep.tr_id, None)
        self.resolved.append(ep)

    def set_outcome(self, tr: str, target_stop_id: int, outcome: float) -> None:
        """Запоминает фактическое отклонение по остановке, о которой была тревога."""
        for ep in list(self.active.values()) + self.resolved[-50:]:
            if ep.tr_id == tr and ep.alert["target_stop_id"] == target_stop_id:
                ep.outcome_delay_s = outcome

    def visible(self, now: float) -> list[Episode]:
        """Активные инциденты и закрытые за последние 15 минут."""
        self.resolved = [
            e for e in self.resolved if e.closed_at > now - KEEP_RESOLVED_SEC
        ]
        return list(self.active.values()) + self.resolved
