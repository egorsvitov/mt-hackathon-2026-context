"""Правила риска, причин и рекомендаций живут в backend: backend/app/services/incident_rules.py.

Этот модуль — прокладка для инструментов дашборда (build_fixtures, mock_backend), чтобы
REPLAY и backend использовали одни и те же правила.
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
