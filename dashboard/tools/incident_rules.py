"""Правила риска и причин живут в backend/app/services/incident_rules.py.

Здесь только импорт оттуда, чтобы инструменты дашборда и backend считали одинаково.
"""

import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[2] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.services.incident_rules import (  # noqa: E402,F401
    EVIDENCE_SPEC,
    REASONS,
    SEVERITY_ORDER,
    STOP_ZONE_M,
    THRESHOLDS,
    diagnose,
    evidence,
    explain,
    severity_of,
)
