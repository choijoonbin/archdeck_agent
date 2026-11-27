from pathlib import Path

import pytest

from archdeck_agent.contracts import HarnessRequest


@pytest.fixture
def request_fixture():
    return HarnessRequest.model_validate_json(
        (Path(__file__).parents[1] / "fixtures/research-request.json").read_bytes()
    )
