import pandas as pd
import pytest

from transport_delay.workflow import validate_submission


def test_submission_validation():
    template = pd.DataFrame({"sample_id": ["a", "b"], "prediction": [0.0, 0.0]})
    submission = pd.DataFrame({"sample_id": ["a", "b"], "prediction": [1.0, -2.0]})
    validate_submission(submission, template)


def test_submission_rejects_missing_value():
    template = pd.DataFrame({"sample_id": ["a"], "prediction": [0.0]})
    submission = pd.DataFrame({"sample_id": ["a"], "prediction": [float("nan")]})
    with pytest.raises(ValueError):
        validate_submission(submission, template)
