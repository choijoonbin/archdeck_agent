import json
import socket
from datetime import UTC, datetime

import pytest
from defusedxml.common import EntitiesForbidden
from fastapi.testclient import TestClient

from archdeck_agent.collector import CollectionError, parse_feed, public_addresses
from archdeck_agent.contracts import KnowledgeDraft
from archdeck_agent.main import app
from archdeck_agent.runtime import ExecutionRequest, ResearchRuntime, RuntimeErrorCode

FEED = (
    b"<rss><channel><item><title>Test release</title>"
    b"<link>https://example.org/release</link>"
    b"<pubDate>Fri, 27 Nov 2026 00:00:00 GMT</pubDate>"
    b"<description>A new release added retry controls.</description>"
    b"</item></channel></rss>"
)


def request_value():
    return ExecutionRequest.model_validate(
        {
            "runId": "test-live-run",
            "leaseFence": 1,
            "asOf": "2026-11-27T03:00:00Z",
            "policy": {
                "domain_id": "arbitrary-domain",
                "version": 1,
                "lookback_days": 7,
                "max_documents": 2,
                "max_input_characters": 5000,
                "sources": [
                    {
                        "host": "example.org",
                        "publisher_group": "fixture",
                        "feed_url": "https://example.org/feed.xml",
                        "reuse_allowed": True,
                    }
                ],
                "auto_publish": False,
            },
        }
    )


class Fetcher:
    def __init__(self, **kwargs):
        pass

    def feed(self, url, hosts):
        return FEED


class Provider:
    calls = 0

    def generate(self, context, run_id):
        self.calls += 1
        source = json.loads(context)["sources"][0]
        return KnowledgeDraft(
            title="Release",
            summary="Retry controls were added.",
            claims=[
                {
                    "claim_id": "c1",
                    "text": "Retry controls added",
                    "evidence": [
                        {
                            "source_id": source["source_id"],
                            "quote": "A new release added retry controls.",
                        }
                    ],
                }
            ],
            risk_flags=[],
        )


def test_runtime_persists_result_and_does_not_repeat_model_on_replay(tmp_path):
    provider = Provider()
    path = tmp_path / "runtime.sqlite"
    runtime = ResearchRuntime(path, Fetcher, lambda: provider)
    first = runtime.execute(request_value())
    assert first["status"] == "needs_review" and not first["published"]
    restarted = ResearchRuntime(path, Fetcher, lambda: provider)
    assert restarted.execute(request_value()) == first
    assert provider.calls == 1


def test_resume_completed_collection_after_model_failure(tmp_path):
    class FailingProvider:
        def generate(self, *args):
            raise RuntimeError("temporary failure")

    path = tmp_path / "runtime.sqlite"
    runtime = ResearchRuntime(path, Fetcher, lambda: FailingProvider())
    with pytest.raises(RuntimeError, match="temporary"):
        runtime.execute(request_value())

    class ForbiddenFetcher:
        def __init__(self, **kwargs):
            raise AssertionError("collection was already checkpointed")

    restarted = ResearchRuntime(path, ForbiddenFetcher, Provider)
    assert restarted.execute(request_value())["status"] == "needs_review"


def test_conflicting_replay_and_cancel_are_rejected(tmp_path):
    runtime = ResearchRuntime(tmp_path / "runtime.sqlite", Fetcher, Provider)
    runtime.execute(request_value())
    different = request_value().model_copy(update={"leaseFence": 2})
    with pytest.raises(RuntimeErrorCode, match="identity_conflict"):
        runtime.execute(different)
    runtime.cancel("test-live-run")
    with pytest.raises(RuntimeErrorCode, match="cancelled"):
        runtime.execute(request_value())


def test_xml_entities_do_not_expand():
    with pytest.raises(EntitiesForbidden):
        parse_feed(
            b'<!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><rss><channel><item><title>&xxe;</title></item></channel></rss>',
            "example.org",
            datetime.now(UTC),
            3,
            True,
        )


def test_unknown_publication_is_not_invented():
    documents, rejected = parse_feed(
        FEED.replace(b"<pubDate>Fri, 27 Nov 2026 00:00:00 GMT</pubDate>", b""),
        "example.org",
        datetime.now(UTC),
        3,
        True,
    )
    assert not documents and rejected[0]["reason"] == "invalid_or_undated_entry"


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "192.168.1.1"])
def test_private_dns_and_mixed_public_private_records_rejected(monkeypatch, ip):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", (ip, 443)), (2, 1, 6, "", ("8.8.8.8", 443))],
    )
    with pytest.raises(CollectionError, match="non_public_dns"):
        public_addresses("example.org")


def test_internal_api_requires_machine_token_before_validation(monkeypatch):
    monkeypatch.setenv("ARCHDECK_AGENT_SERVICE_TOKEN", "x" * 40)
    with TestClient(app) as client:
        assert client.post("/internal/runs/a/execute", json={"bad": "secret"}).status_code == 401
        authenticated = client.post(
            "/internal/runs/a/execute",
            json={"bad": "secret"},
            headers={"Authorization": "Bearer " + "x" * 40},
        )
        assert authenticated.status_code == 422 and "secret" not in authenticated.text
        too_large = client.post(
            "/internal/runs/a/execute",
            content=b"x" * 100001,
            headers={"Authorization": "Bearer " + "x" * 40},
        )
        assert too_large.status_code == 413


def test_checkpoint_hash_prevents_different_input_after_local_cache_loss(tmp_path):
    path = tmp_path / "runtime.sqlite"
    first = ResearchRuntime(path, Fetcher, Provider)
    first.execute(request_value())
    path.unlink()
    altered = request_value().model_copy(update={"asOf": datetime(2026, 11, 28, tzinfo=UTC)})
    with pytest.raises(RuntimeErrorCode, match="checkpoint_identity_conflict"):
        ResearchRuntime(path, Fetcher, Provider).execute(altered)


def test_model_attempt_budget_bounds_resumed_failures(tmp_path):
    class FailingProvider:
        def generate(self, *args):
            raise RuntimeError("failed model")

    path = tmp_path / "runtime.sqlite"
    runtime = ResearchRuntime(path, Fetcher, FailingProvider)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="failed model"):
            runtime.execute(request_value())
    with pytest.raises(RuntimeErrorCode, match="model_attempt_budget_exhausted"):
        runtime.execute(request_value())


def test_configured_checkpoint_factory_is_used_by_graph(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from langgraph.checkpoint.memory import InMemorySaver

    calls = []

    @contextmanager
    def configured_checkpoint(self):
        calls.append("selected")
        yield InMemorySaver()

    monkeypatch.setattr(ResearchRuntime, "checkpointer", configured_checkpoint)
    result = ResearchRuntime(tmp_path / "runtime.sqlite", Fetcher, Provider).execute(
        request_value()
    )
    assert result["status"] == "needs_review" and calls == ["selected"]


@pytest.mark.parametrize(
    "status,mime,encoding,body,error",
    [
        (302, "application/rss+xml", "identity", b"", "source_http_302"),
        (200, "text/html", "identity", b"<html/>", "unsupported_source_mime"),
        (200, "application/rss+xml", "gzip", b"bad", "compressed_content_not_allowed"),
        (200, "application/rss+xml", "identity", b"x" * 101, "source_size_limit"),
    ],
)
def test_fetcher_rejects_redirects_mime_compression_and_size(
    monkeypatch, status, mime, encoding, body, error
):
    import archdeck_agent.collector as collector

    class Response:
        def __init__(self):
            self.status = status
            self.offset = 0

        def getheader(self, name, default=""):
            return {"Content-Type": mime, "Content-Encoding": encoding}.get(name, default)

        def read(self, count):
            result = body[self.offset : self.offset + count]
            self.offset += len(result)
            return result

    class Connection:
        def __init__(self, host, address, timeout):
            assert host == "example.org" and address == "8.8.8.8"

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(collector, "public_addresses", lambda host: ["8.8.8.8"])
    monkeypatch.setattr(collector, "PinnedHTTPS", Connection)
    with pytest.raises(CollectionError, match=error):
        collector.SafeFetcher(max_bytes=100).fetch("https://example.org/rss", {"example.org"})
