from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context
from pydantic import AwareDatetime, Field

from .collector import CollectionError, SafeFetcher, parse_feed
from .contracts import Contract, HarnessRequest, KnowledgeDraft, ResearchPolicy, SourceDocument
from .provider import ProviderConfiguration, ResponsesDraftProvider
from .workflow import evaluate_fixture


class ExecutionRequest(Contract):
    runId: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    leaseFence: int = Field(ge=1)
    policy: ResearchPolicy
    asOf: AwareDatetime


class RuntimeErrorCode(ValueError):
    pass


class LiveState(TypedDict, total=False):
    documents: list[dict]
    discarded: list[dict]
    draft: dict
    result: dict
    usage: dict
    request_hash: str


class ResearchRuntime:
    def __init__(self, path=None, fetcher_factory=SafeFetcher, provider_factory=None):
        self.path = Path(path or os.getenv("ARCHDECK_AGENT_STATE_PATH", "artifacts/runtime.sqlite"))
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fetcher_factory = fetcher_factory
        self.provider_factory = provider_factory or (
            lambda: ResponsesDraftProvider(ProviderConfiguration.from_environment())
        )
        self.lock = threading.Lock()
        with self.database() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS executions (run_id TEXT PRIMARY KEY, "
                "fence INTEGER, request_hash TEXT, cancelled INTEGER DEFAULT 0, result TEXT)"
            )
        with self.database() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS model_reservations (run_id TEXT, "
                "fence INTEGER, attempts INTEGER NOT NULL, PRIMARY KEY(run_id,fence))"
            )
        os.chmod(self.path, 0o600)

    @contextmanager
    def checkpointer(self):
        dsn = os.getenv("ARCHDECK_AGENT_CHECKPOINT_DSN")
        if dsn:
            from langgraph.checkpoint.postgres import PostgresSaver

            with PostgresSaver.from_conn_string(dsn) as saver:
                yield saver
        else:
            with SqliteSaver.from_conn_string(str(self.path) + ".checkpoints") as saver:
                yield saver

    @contextmanager
    def database(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def cancelled(self, run_id, fence):
        with self.database() as db:
            row = db.execute(
                "SELECT fence,cancelled FROM executions WHERE run_id=?", (run_id,)
            ).fetchone()
        return row is None or row[0] != fence or bool(row[1])

    def cancel(self, run_id):
        with self.database() as db:
            db.execute("UPDATE executions SET cancelled=1 WHERE run_id=?", (run_id,))

    def execute(self, request):
        if not self.lock.acquire(blocking=False):
            raise RuntimeErrorCode("worker_busy")
        try:
            return self._execute(request)
        finally:
            self.lock.release()

    def _execute(self, request):
        request_hash = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        with self.database() as db:
            previous = db.execute(
                "SELECT fence,request_hash,cancelled,result FROM executions WHERE run_id=?",
                (request.runId,),
            ).fetchone()
            if previous:
                if previous[0] != request.leaseFence or previous[1] != request_hash:
                    raise RuntimeErrorCode("run_identity_conflict")
                if previous[2]:
                    raise RuntimeErrorCode("run_cancelled")
                if previous[3]:
                    return json.loads(previous[3])
            else:
                db.execute(
                    "INSERT INTO executions(run_id,fence,request_hash) VALUES (?,?,?)",
                    (request.runId, request.leaseFence, request_hash),
                )
        started = time.monotonic()

        def guard():
            if self.cancelled(request.runId, request.leaseFence):
                raise RuntimeErrorCode("run_cancelled")
            if time.monotonic() - started > 110:
                raise RuntimeErrorCode("execution_deadline")

        def collect(state):
            guard()
            fetcher = self.fetcher_factory(deadline=started + 70)
            documents, discarded = [], []
            for source in request.policy.sources:
                guard()
                if not source.feed_url or not source.reuse_allowed:
                    discarded.append({"host": source.host, "reason": "source_not_approved"})
                    continue
                try:
                    body = fetcher.feed(source.feed_url, {source.host})
                    found, invalid = parse_feed(
                        body,
                        source.host,
                        request.asOf,
                        request.policy.max_documents,
                        source.reuse_allowed,
                    )
                    documents.extend(found)
                    discarded.extend(invalid)
                except CollectionError as error:
                    discarded.append({"host": source.host, "reason": str(error)})
                except (ValueError, DefusedXmlException, ParseError):
                    discarded.append({"host": source.host, "reason": "invalid_source_feed"})
                if len(documents) >= request.policy.max_documents:
                    break
            unique = {item.source_id: item for item in documents}
            empty = KnowledgeDraft(
                title="Collection validation",
                summary="Eligibility filtering",
                claims=[],
                risk_flags=[],
            )
            harness = HarnessRequest(
                fixture_only=True,
                run_id=request.runId,
                as_of=request.asOf,
                policy=request.policy,
                documents=list(unique.values()),
                draft=empty,
            )
            eligible = evaluate_fixture(harness)
            accepted = set(eligible.accepted_source_ids)
            discarded.extend(item.model_dump() for item in eligible.discarded_documents)
            return {
                "documents": [
                    item.model_dump(mode="json")
                    for item in unique.values()
                    if item.source_id in accepted
                ],
                "discarded": discarded,
            }

        def synthesize(state):
            guard()
            if not state["documents"]:
                return {
                    "draft": {
                        "title": "수집 자료 없음",
                        "summary": "규칙에 맞는 자료가 없습니다.",
                        "claims": [],
                        "risk_flags": [],
                    }
                }
            with self.database() as db:
                db.execute(
                    "INSERT OR IGNORE INTO model_reservations(run_id,fence,attempts) "
                    "VALUES (?,?,0)",
                    (request.runId, request.leaseFence),
                )
                changed = db.execute(
                    "UPDATE model_reservations SET attempts=attempts+1 "
                    "WHERE run_id=? AND fence=? AND attempts<2",
                    (request.runId, request.leaseFence),
                ).rowcount
                if not changed:
                    raise RuntimeErrorCode("model_attempt_budget_exhausted")
            provider = self.provider_factory()
            draft = provider.generate(
                json.dumps(
                    {"domain": request.policy.domain_id, "sources": state["documents"]},
                    ensure_ascii=False,
                ),
                request.runId,
            )
            guard()
            return {"draft": draft.model_dump(), "usage": getattr(provider, "last_usage", {})}

        def validate(state):
            guard()
            harness = HarnessRequest(
                fixture_only=True,
                run_id=request.runId,
                as_of=request.asOf,
                policy=request.policy,
                documents=[SourceDocument.model_validate(item) for item in state["documents"]],
                draft=KnowledgeDraft.model_validate(state["draft"]),
            )
            evaluated = evaluate_fixture(harness)
            return {
                "result": {
                    "runId": request.runId,
                    "leaseFence": request.leaseFence,
                    "status": evaluated.status,
                    "draft": state["draft"],
                    "documents": state["documents"],
                    "discarded": state["discarded"],
                    "issues": evaluated.issues,
                    "published": False,
                    "usage": {
                        "modelCalls": 1 if state["documents"] else 0,
                        "costStatus": "not_priced",
                        "tokens": state.get("usage", {}),
                    },
                }
            }

        graph = StateGraph(LiveState)
        graph.add_node("collect", collect)
        graph.add_node("synthesize", synthesize)
        graph.add_node("validate", validate)
        graph.add_edge(START, "collect")
        graph.add_edge("collect", "synthesize")
        graph.add_edge("synthesize", "validate")
        graph.add_edge("validate", END)
        configuration = {
            "configurable": {"thread_id": request.runId + ":" + str(request.leaseFence)}
        }
        # primitive state only; strict deserialization prevents unexpected object restoration.
        os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"
        with (
            tracing_context(enabled=False),
            self.checkpointer() as saver,
        ):
            compiled = graph.compile(checkpointer=saver)
            existing = compiled.get_state(configuration)
            if existing.values and existing.values.get("request_hash") != request_hash:
                raise RuntimeErrorCode("checkpoint_identity_conflict")
            state = (
                compiled.invoke(
                    None if existing.next else {"request_hash": request_hash}, configuration
                )
                if existing.next or not existing.values
                else existing.values
            )
        guard()
        result = state["result"]
        with self.database() as db:
            db.execute(
                "UPDATE executions SET result=? WHERE run_id=? AND fence=? AND cancelled=0",
                (json.dumps(result), request.runId, request.leaseFence),
            )
        return result
