import json

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_provider import environment
from test_runtime import Fetcher, Provider, request_value

from archdeck_agent.contracts import TopicRevisionDraft
from archdeck_agent.main import app
from archdeck_agent.provider import ProviderConfiguration, ResponsesDraftProvider
from archdeck_agent.runtime import ExecutionRequest, ResearchRuntime


def target():
    return {
        "targetContentId": "topic-1",
        "baseLockVersion": 4,
        "topic": {
            "id": "topic-1",
            "slug": "topic",
            "lockVersion": 4,
            "title": "Existing topic",
            "koreanTitle": "기존 토픽",
            "category": "Security",
            "summary": "Old summary",
            "overview": "Old overview",
            "badges": [],
            "readingTimeMinutes": 3,
            "diagramNodes": [],
            "flowSteps": [],
            "keyTakeaways": ["Old takeaway"],
            "tradeOffs": {"pros": [], "cons": []},
            "codeSnippets": [],
            "securityChecklistIds": [],
            "relatedTermIds": [],
            "relatedTopicIds": [],
        },
    }


def test_revision_runtime_supplies_canonical_base_and_keeps_patch(tmp_path):
    class RevisionProvider(Provider):
        def generate(self, context, run_id):
            assert json.loads(context)["targetTopic"] == target()
            base = super().generate(context, run_id)
            return TopicRevisionDraft(
                **base.model_dump(),
                revisionPatch={
                    "title": "Updated title",
                    "summary": "Updated summary",
                    "overview": "Updated overview",
                    "keyTakeaways": ["Updated takeaway"],
                },
            )

    request = ExecutionRequest.model_validate(
        {**request_value().model_dump(), "targetTopic": target()}
    )
    result = ResearchRuntime(tmp_path / "r.sqlite", Fetcher, RevisionProvider).execute(request)
    assert result["status"] == "needs_review"
    assert result["draft"]["revisionPatch"]["overview"] == "Updated overview"
    assert result["draft"]["claims"] and not result["published"]


def test_private_note_is_not_an_allowed_revision_context():
    value = target()
    value["topic"]["privateMemo"] = "private"
    with pytest.raises(ValidationError):
        ExecutionRequest.model_validate({**request_value().model_dump(), "targetTopic": value})


def test_revision_azure_schema_is_strict_and_patch_only(request_fixture):
    draft = TopicRevisionDraft(
        **request_fixture.draft.model_dump(),
        revisionPatch={
            "title": "Updated",
            "summary": "Summary",
            "overview": "Overview",
            "keyTakeaways": ["One"],
        },
    )

    def responder(request):
        schema = json.loads(request.content)["text"]["format"]["schema"]
        assert "revisionPatch" in schema["required"]
        assert schema["$defs"]["RevisionPatch"]["additionalProperties"] is False
        assert set(schema["$defs"]["RevisionPatch"]["required"]) == {
            "title",
            "summary",
            "overview",
            "keyTakeaways",
        }
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [{"content": [{"type": "output_text", "text": draft.model_dump_json()}]}],
            },
        )

    provider = ResponsesDraftProvider(
        ProviderConfiguration.from_environment(environment(ARCHDECK_AGENT_LIVE_ENABLED="true")),
        httpx.MockTransport(responder),
    )
    assert provider.generate(json.dumps({"targetTopic": target()}), "revision-1") == draft


def test_readyz_probes_distributed_database_and_hides_failure(monkeypatch):
    import archdeck_agent.main as api

    for key, value in environment(ARCHDECK_AGENT_LIVE_ENABLED="true").items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ARCHDECK_AGENT_DISTRIBUTED", "true")
    monkeypatch.setenv("ARCHDECK_AGENT_SERVICE_TOKEN", "x" * 40)
    calls = []

    class Registry:
        def probe(self):
            calls.append(True)
            raise psycopg.OperationalError("secret-dsn-password")

    class Runtime:
        distributed = True
        registry = Registry()

    monkeypatch.setattr(api, "runtime", lambda: Runtime())
    with TestClient(app) as client:
        response = client.get("/readyz")
    assert response.status_code == 503 and calls == [True]
    assert "secret" not in response.text


def test_private_note_nested_in_code_is_also_rejected():
    value = target()
    value["topic"]["codeSnippets"] = [
        {
            "id": "c1",
            "title": "Config",
            "filename": "file",
            "language": "json",
            "code": "{}",
            "description": "Config",
            "privateMemo": "not public content",
        }
    ]
    with pytest.raises(ValidationError):
        ExecutionRequest.model_validate({**request_value().model_dump(), "targetTopic": value})


def test_new_diagram_fields_are_accepted_for_revision_snapshot():
    value = target()
    value["topic"]["diagramNodes"] = [
        {
            "id": "client",
            "label": "Client",
            "subLabel": "SPA",
            "type": "client",
            "icon": "Monitor",
            "x": 10,
            "y": 20,
            "trustZone": "browser",
        }
    ]
    value["topic"]["diagramEdges"] = [
        {
            "id": "loop",
            "source": "client",
            "target": "client",
            "type": "internal",
            "label": "PKCE",
            "trustBoundary": "browser",
        }
    ]
    request = ExecutionRequest.model_validate(
        {**request_value().model_dump(), "targetTopic": value}
    )
    assert request.targetTopic.topic.diagramEdges[0].id == "loop"
