"""Правила уровня риска, гипотез о причине и рекомендаций для диспетчера.

Модуль не зависит от pandas и может быть перенесён в backend как есть:
на вход — признаки на момент прогноза ``T`` (только данные с ``event_time <= T``)
и прогноз модели, на выход — поля ``severity``, ``reason``, ``evidence``,
``recommendation`` контракта ``Prediction``/``Incident`` (см. ``dashboard/CONTRACT.md``).

Причины формулируются как гипотезы, подкреплённые наблюдаемыми признаками:
в данных нет дверей, пробок и ДТП, поэтому утверждать их нельзя.
"""

from __future__ import annotations

import math

# Пороги уровня риска, секунды. Граница «опоздание» совпадает с target_class
# организаторов (late > +120 c, early < −60 c).
THRESHOLDS = {
    "early": -60.0,  # prediction <= early    -> опережение
    "warning": 60.0,  # prediction >= warning  -> риск (жёлтый)
    "critical": 120.0,  # prediction >= critical -> опоздание (красный)
}

STOP_ZONE_M = 150  # дальше от остановки — стоянка «вне остановочного пункта» (с запасом на погрешность GPS)

SEVERITY_ORDER = {"critical": 3, "warning": 2, "early": 1, "ok": 0, "unknown": -1}

# Какие признаки показываются в карточке как доказательства.
# feature/norm_feature — имена признаков на момент T.
EVIDENCE_SPEC = [
    {"code": "speed_5m", "label": "Средняя скорость за 5 мин", "unit": "км/ч", "feature": "speed_5m_kmh", "norm_feature": "speed_norm_kmh"},
    {"code": "stationary", "label": "Непрерывный простой", "unit": "с", "feature": "stationary_s", "norm_feature": None},
    {"code": "dev_trend_15m", "label": "Изменение отклонения за 15 мин", "unit": "с", "feature": "dev_trend_15m_s", "norm_feature": None},
    {"code": "cur_dev", "label": "Отклонение на последней остановке", "unit": "с", "feature": "cur_dev_s", "norm_feature": None},
    {"code": "required_speed", "label": "Нужная скорость до цели", "unit": "км/ч", "feature": "required_speed_kmh", "norm_feature": "speed_norm_kmh"},
    {"code": "gps_age", "label": "Возраст GPS-отметки", "unit": "с", "feature": "gps_age_s", "norm_feature": None},
]

# Справочник причин: заголовок, какие доказательства подсвечивать, рекомендация.
REASONS = {
    "NO_GPS": {"title": "Нет свежих данных GPS", "flags": ["gps_age"],
               "recommendation": "Проверить связь с бортовым терминалом; уточнить положение ТС у водителя"},
    "LONG_STOP": {"title": "Длительная остановка вне остановочного пункта", "flags": ["stationary"],
                  "recommendation": "Связаться с водителем и выяснить причину остановки (затор, ДТП, неисправность)"},
    "LONG_DWELL": {"title": "Затянувшаяся стоянка у остановки", "flags": ["stationary"],
                   "recommendation": "Уточнить причину стоянки (посадка, отстой); при необходимости сократить стоянку"},
    "SLOW": {"title": "Скорость ниже нормы маршрута", "flags": ["speed_5m"],
             "recommendation": "Проверить дорожную обстановку на участке; рассмотреть светофорный приоритет или объезд"},
    "GROWING": {"title": "Опоздание нарастает", "flags": ["dev_trend_15m"],
                "recommendation": "Предупредить водителя; подготовить выравнивание интервала (удержание следующего рейса)"},
    "CARRYOVER": {"title": "Накопленное опоздание", "flags": ["cur_dev"],
                  "recommendation": "Сократить стоянку на конечной для возврата в график; при повторении — выпуск резерва"},
    "TIGHT": {"title": "Плотный график на участке", "flags": ["required_speed"],
              "recommendation": "Учесть участок при корректировке расписания"},
    "MODEL": {"title": "Совокупность факторов", "flags": [],
              "recommendation": "Наблюдать; при росте прогноза связаться с водителем"},
    "EARLY": {"title": "Опережение графика", "flags": ["cur_dev"],
              "recommendation": "Предупредить водителя: выдерживать время на остановках, чтобы не оставить пассажиров"},
}


def severity_of(prediction_s: float | None) -> str:
    """Уровень риска по прогнозу задержки: ok / warning / critical / early / unknown."""
    p = _num(prediction_s)
    if p is None:
        return "unknown"
    if p >= THRESHOLDS["critical"]:
        return "critical"
    if p >= THRESHOLDS["warning"]:
        return "warning"
    if p <= THRESHOLDS["early"]:
        return "early"
    return "ok"


def _num(x):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def _mmss(seconds: float) -> str:
    seconds = int(round(abs(seconds)))
    m, s = divmod(seconds, 60)
    return f"{m} мин {s:02d} с" if m else f"{s} с"


def diagnose(features: dict, prediction_s: float, severity: str) -> tuple[str | None, str | None]:
    """Код причины и текст с цифрами, либо ``(None, None)`` для ТС в графике.

    ``features`` — признаки на момент T: ``cur_dev_s``, ``dev_trend_15m_s``,
    ``speed_5m_kmh``, ``speed_norm_kmh``, ``stationary_s``, ``near_stop_m``,
    ``gps_age_s``, ``required_speed_kmh``. Пропуски допустимы (``None``/NaN).
    """
    if severity in ("ok", "unknown"):
        return None, None
    if severity == "early":
        return "EARLY", f"Ожидается прибытие на {_mmss(prediction_s)} раньше плана"

    cur = _num(features.get("cur_dev_s")) or 0.0
    trend = _num(features.get("dev_trend_15m_s"))
    v5 = _num(features.get("speed_5m_kmh"))
    norm = _num(features.get("speed_norm_kmh"))
    still = _num(features.get("stationary_s")) or 0.0
    near = _num(features.get("near_stop_m"))
    age = _num(features.get("gps_age_s"))
    req = _num(features.get("required_speed_kmh"))

    candidates = []
    if age is not None and age > 180:
        candidates.append((100, "NO_GPS", f"Последняя отметка {_mmss(age)} назад — прогноз по последнему известному состоянию"))
    if still >= 150 and near is not None and near > STOP_ZONE_M:
        candidates.append((90, "LONG_STOP", f"ТС стоит {_mmss(still)}, до ближайшей остановки {near:.0f} м"))
    if still >= 120 and near is not None and near <= STOP_ZONE_M:
        candidates.append((85, "LONG_DWELL", f"ТС стоит у остановки {_mmss(still)}"))
    if v5 is not None and norm and v5 < 0.6 * norm:
        ratio = v5 / norm
        candidates.append((60 + (1 - ratio) * 15, "SLOW",
                           f"{v5:.0f} км/ч за 5 мин при норме {norm:.0f} км/ч (−{(1 - ratio) * 100:.0f}%)"))
    if trend is not None and trend >= 60:
        candidates.append((55 + min(trend / 10, 15), "GROWING", f"Отклонение выросло на {trend:.0f} с за последние 15 мин"))
    if cur >= 120:
        candidates.append((50, "CARRYOVER", f"ТС уже отстаёт на {_mmss(cur)} и не нагоняет график"))
    if req is not None and norm and req > 1.3 * norm:
        candidates.append((40, "TIGHT", f"Чтобы успеть, нужна скорость {req:.0f} км/ч при норме {norm:.0f} км/ч"))
    if not candidates:
        candidates.append((0, "MODEL", "Модель ожидает опоздание по сочетанию признаков, явного лидера нет"))
    _, code, detail = max(candidates, key=lambda c: c[0])
    return code, detail


def evidence(features: dict, reason_code: str | None) -> list[dict]:
    """Список доказательств для карточки; ``flag`` — признак, на котором основана гипотеза."""
    flags = set(REASONS[reason_code]["flags"]) if reason_code else set()
    out = []
    for spec in EVIDENCE_SPEC:
        out.append({
            "code": spec["code"],
            "label": spec["label"],
            "value": _num(features.get(spec["feature"])),
            "norm": _num(features.get(spec["norm_feature"])) if spec["norm_feature"] else None,
            "unit": spec["unit"],
            "flag": spec["code"] in flags,
        })
    return out


def explain(features: dict, prediction_s: float, severity: str) -> dict:
    """Поля ``reason``/``evidence``/``recommendation`` контракта для одного прогноза."""
    code, detail = diagnose(features, prediction_s, severity)
    reason = None
    recommendation = None
    if code:
        reason = {"code": code, "title": REASONS[code]["title"], "detail": detail}
        recommendation = REASONS[code]["recommendation"]
    return {"reason": reason, "evidence": evidence(features, code), "recommendation": recommendation}
