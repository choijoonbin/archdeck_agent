"""Explicit test-only HTTP integration server with synthetic source and mock model.

Launch with uvicorn integration_server:app --app-dir scripts --host 127.0.0.1 --port 4010.
Uses real internal authentication and durable runtime; never configure for a shared deployment.
"""

import json
import os

from archdeck_agent.contracts import KnowledgeDraft
from archdeck_agent.main import app
from archdeck_agent.runtime import ResearchRuntime

FEED = b"""<rss><channel><item><title>SYNTHETIC integration fixture</title>
<link>https://openai.com/test-only-archdeck-fixture</link>
<pubDate>Fri, 27 Nov 2026 00:00:00 GMT</pubDate>
<description>Synthetic test fixture: retry controls are configurable.</description>
</item></channel></rss>"""


class FixtureFetcher:
    def __init__(self, **kwargs):
        pass

    def feed(self, url, hosts):
        return FEED


class FixtureModel:
    last_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def generate(self, context, run_id):
        source = json.loads(context)["sources"][0]
        return KnowledgeDraft(
            title="SYNTHETIC integration draft — not real news",
            summary="This is an end-to-end verification fixture, not a published technical claim.",
            claims=[
                {
                    "claim_id": "c1",
                    "text": "The synthetic fixture mentions configurable retries.",
                    "evidence": [{"source_id": source["source_id"], "quote": source["excerpt"]}],
                }
            ],
            risk_flags=[],
        )


os.environ.setdefault("ARCHDECK_AGENT_STATE_PATH", "artifacts/integration-server.sqlite")
app.dependency_overrides = {}
# main.execute resolves runtime() directly, so replace its cached instance factory explicitly.
import archdeck_agent.main as api  # noqa: E402

fixture_runtime = ResearchRuntime(
    os.environ["ARCHDECK_AGENT_STATE_PATH"], FixtureFetcher, FixtureModel
)
api.runtime = lambda: fixture_runtime
