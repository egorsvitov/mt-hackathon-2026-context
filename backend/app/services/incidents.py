"""Инциденты — эпизоды риска по ТС (а не карточка на каждый прогноз).

* ``critical`` открывает инцидент сразу, ``warning``/``early`` — со второго прогноза подряд.
* Инцидент закрывается после ``COOLDOWN`` прогнозов подряд без риска.
* Причина и рекомендация берутся из последнего прогноза с риском.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.incident_rules import SEVERITY_ORDER

ALERT_KINDS = {"warning": "late", "critical": "late", "early": "early"}
COOLDOWN = 3
KEEP_RESOLVED_SEC = 900


@dataclass
class Episode:
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
    history: list = field(default_factory=list)


class IncidentTracker:
    def __init__(self):
        self.active: dict[str, Episode] = {}
        self.resolved: list[Episode] = []
        self.prev_kind: dict[str, str | None] = {}

    def reset(self) -> None:
        self.active.clear()
        self.resolved.clear()
        self.prev_kind.clear()

    def update(self, tr: str, pred: dict) -> None:
        """pred — прогноз во внутреннем виде (времена — epoch)."""
        sev = pred["severity"]
        kind = ALERT_KINDS.get(sev)
        ep = self.active.get(tr)
        if ep and kind and ep.kind != kind:
            self._close(ep, pred["as_of"])
            ep = None
        if kind:
            if ep is None and (sev == "critical" or self.prev_kind.get(tr) == kind):
                ep = Episode(f"INC-{tr}-{int(pred['as_of'])}", tr, kind, pred["as_of"], pred, pred, sev)
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
        """ТС ушло с линии — закрыть его инцидент."""
        ep = self.active.get(tr)
        if ep:
            self._close(ep, t)

    def _close(self, ep: Episode, t: float) -> None:
        ep.closed_at = t
        self.active.pop(ep.tr_id, None)
        self.resolved.append(ep)

    def set_outcome(self, tr: str, target_stop_id: int, outcome: float) -> None:
        """Исход (факт) для цели, по которой шла тревога — для строки «прогноз → факт»."""
        for ep in list(self.active.values()) + self.resolved[-50:]:
            if ep.tr_id == tr and ep.alert["target_stop_id"] == target_stop_id:
                ep.outcome_delay_s = outcome

    def visible(self, now: float) -> list[Episode]:
        self.resolved = [e for e in self.resolved if e.closed_at > now - KEEP_RESOLVED_SEC]
        return list(self.active.values()) + self.resolved
