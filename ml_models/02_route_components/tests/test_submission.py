import pandas as pd
from transport_delay.workflow import validate_submission


def test_submission_contract():
    template = pd.DataFrame({"sample_id": ["a", "b"], "prediction": [0.0, 0.0]})
    candidate = pd.DataFrame({"sample_id": ["a", "b"], "prediction": [1.0, -2.0]})
    validate_submission(candidate, template)

