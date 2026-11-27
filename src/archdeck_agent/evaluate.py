from __future__ import annotations

import argparse
from pathlib import Path

from .contracts import HarnessRequest
from .workflow import evaluate_fixture


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run an offline synthetic research fixture; no network or model calls"
    )
    parser.add_argument("fixture", type=Path)
    arguments = parser.parse_args()
    if arguments.fixture.stat().st_size > 1_000_000:
        parser.error("fixture exceeds 1 MB")
    request = HarnessRequest.model_validate_json(arguments.fixture.read_bytes())
    print(evaluate_fixture(request).model_dump_json(indent=2))


if __name__ == "__main__":
    main()
