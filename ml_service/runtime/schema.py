"""Versioned internal backend-to-model request; no labels or fitted statistics."""
import math
from typing import Literal
from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class PredictionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    schema_version: Literal[2] = 2
    sample_id: str
    tr_id: int
    t_timestamp: float
    target_stop_id: int
    planned_arrival_time: float
    cur_dev_s: float
    route_features: dict[str, float | str | None]
    sequence_id: str | None = None
    telemetry_window: list[list[float]]

    @field_validator('telemetry_window')
    @classmethod
    def window_shape(cls, value):
        if len(value) != 45 or any(len(row) != 7 for row in value):
            raise ValueError('telemetry_window must be 45 x 7')
        if not all(math.isfinite(x) for row in value for x in row):
            raise ValueError('Use age=1 for missing events, not NaN/Infinity')
        return value

    @model_validator(mode='after')
    def horizon(self):
        if not 600 < self.planned_arrival_time-self.t_timestamp <= 900:
            raise ValueError('Forecast horizon must be (600, 900] seconds')
        for value in self.route_features.values():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError('Use null for missing route features')
        return self
