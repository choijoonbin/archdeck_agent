from __future__ import annotations

from datetime import timedelta
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from .contracts import DiscardedDocument, HarnessRequest, HarnessResult
from .policy import canonicalize_url, contains_instruction_marker, excerpt_digest, normalize_text


class ResearchState(TypedDict):
    request: HarnessRequest
    accepted: list[str]
    discarded: list[DiscardedDocument]
    issues: list[str]
    stages: list[str]
    status: str


def filter_documents(state: ResearchState) -> dict:
    request = state["request"]
    policy = request.policy
    hosts = {source.host.lower() for source in policy.sources}
    earliest = request.as_of - timedelta(days=policy.lookback_days)
    accepted, discarded = [], []
    seen_urls, seen_excerpts = set(), set()
    input_size = 0
    for document in request.documents:
        reason = None
        try:
            canonical_url = canonicalize_url(document.url, hosts)
        except ValueError:
            canonical_url = ""
            reason = "unapproved_url"
        digest = excerpt_digest(document.excerpt)
        if reason is None:
            if not document.reuse_allowed:
                reason = "reuse_not_allowed"
            elif document.published_at > request.as_of:
                reason = "future_publication"
            elif document.observed_at > request.as_of:
                reason = "future_observation"
            elif document.observed_at < document.published_at:
                reason = "invalid_date_order"
            elif document.published_at < earliest:
                reason = "outside_collection_window"
            elif canonical_url in seen_urls or digest in seen_excerpts:
                reason = "duplicate"
            elif contains_instruction_marker(document.excerpt):
                reason = "instruction_marker_triage"
            elif len(accepted) >= policy.max_documents:
                reason = "document_budget"
            elif input_size + len(document.excerpt) > policy.max_input_characters:
                reason = "input_budget"
        if reason:
            discarded.append(DiscardedDocument(source_id=document.source_id, reason=reason))
        else:
            accepted.append(document.source_id)
            seen_urls.add(canonical_url)
            seen_excerpts.add(digest)
            input_size += len(document.excerpt)
    return {"accepted": accepted, "discarded": discarded, "stages": ["filter_documents"]}


def validate_evidence(state: ResearchState) -> dict:
    request = state["request"]
    sources = {
        item.source_id: item for item in request.documents if item.source_id in state["accepted"]
    }
    issues = []
    if not request.draft.title.strip():
        issues.append("empty_title")
    if not request.draft.summary.strip():
        issues.append("empty_summary")
    if not sources:
        issues.append("no_eligible_sources")
    if not request.draft.claims:
        issues.append("no_claims")
    claim_ids = [claim.claim_id for claim in request.draft.claims]
    if len(set(claim_ids)) != len(claim_ids):
        issues.append("duplicate_claim_id")
    for claim in request.draft.claims:
        if not claim.text.strip() or not claim.evidence:
            issues.append("unsupported_claim")
        for link in claim.evidence:
            source = sources.get(link.source_id)
            if source is None:
                issues.append("unknown_or_rejected_evidence")
            elif not link.quote.strip() or normalize_text(link.quote) not in normalize_text(
                source.excerpt
            ):
                issues.append("evidence_quote_not_found")
    issues.extend(request.draft.risk_flags)
    return {"issues": sorted(set(issues)), "stages": [*state["stages"], "validate_evidence"]}


def review_gate(state: ResearchState) -> dict:
    return {
        "status": "held" if state["issues"] else "needs_review",
        "stages": [*state["stages"], "review_gate"],
    }


def build_fixture_graph():
    builder = StateGraph(ResearchState)
    builder.add_node("filter_documents", filter_documents)
    builder.add_node("validate_evidence", validate_evidence)
    builder.add_node("review_gate", review_gate)
    builder.add_edge(START, "filter_documents")
    builder.add_edge("filter_documents", "validate_evidence")
    builder.add_edge("validate_evidence", "review_gate")
    builder.add_edge("review_gate", END)
    return builder.compile(checkpointer=InMemorySaver())


def evaluate_fixture(request: HarnessRequest) -> HarnessResult:
    # Even an inherited tracing environment must not export synthetic evaluation data.
    with tracing_context(enabled=False):
        graph = build_fixture_graph()
        state = graph.invoke(
            {
                "request": request,
                "accepted": [],
                "discarded": [],
                "issues": [],
                "stages": [],
                "status": "held",
            },
            config={"configurable": {"thread_id": request.run_id}},
        )
    return HarnessResult(
        run_id=request.run_id,
        domain_id=request.policy.domain_id,
        policy_version=request.policy.version,
        accepted_source_ids=state["accepted"],
        discarded_documents=state["discarded"],
        status=state["status"],
        issues=state["issues"],
        stages=state["stages"],
    )
