from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class SourceRule(Contract):
    host: str = Field(min_length=1, max_length=253)
    publisher_group: str = Field(min_length=1, max_length=100)
    feed_url: str | None = Field(default=None, max_length=2048)
    reuse_allowed: bool = False


class ResearchPolicy(Contract):
    domain_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=80)
    version: int = Field(ge=1)
    lookback_days: int = Field(ge=1, le=365)
    max_documents: int = Field(ge=1, le=50)
    max_input_characters: int = Field(ge=100, le=100_000)
    sources: list[SourceRule] = Field(min_length=1, max_length=30)
    auto_publish: Literal[False] = False

    @model_validator(mode="after")
    def unique_hosts(self) -> ResearchPolicy:
        hosts = [item.host.lower() for item in self.sources]
        if len(hosts) != len(set(hosts)):
            raise ValueError("duplicate_source_host")
        return self


class SourceDocument(Contract):
    source_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", max_length=80)
    url: str = Field(max_length=2048)
    title: str = Field(min_length=1, max_length=300)
    excerpt: str = Field(min_length=1, max_length=30_000)
    published_at: AwareDatetime
    observed_at: AwareDatetime
    event_at: AwareDatetime | None = None
    reuse_allowed: bool


class EvidenceLink(Contract):
    source_id: str
    quote: str


class Claim(Contract):
    claim_id: str
    text: str
    evidence: list[EvidenceLink]


class KnowledgeDraft(Contract):
    title: str = Field(max_length=300)
    summary: str = Field(max_length=4000)
    claims: list[Claim]
    risk_flags: list[Literal["possible_contradiction", "source_instructions"]]


class HarnessRequest(Contract):
    fixture_only: Literal[True]
    run_id: str = Field(min_length=1, max_length=80)
    as_of: AwareDatetime
    policy: ResearchPolicy
    documents: list[SourceDocument] = Field(max_length=50)
    draft: KnowledgeDraft

    @model_validator(mode="after")
    def unique_source_ids(self) -> HarnessRequest:
        identifiers = [item.source_id for item in self.documents]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("duplicate_source_id")
        return self


class DiscardedDocument(Contract):
    source_id: str
    reason: str


class HarnessResult(Contract):
    fixture_only: Literal[True] = True
    run_id: str
    domain_id: str
    policy_version: int
    accepted_source_ids: list[str]
    discarded_documents: list[DiscardedDocument]
    status: Literal["needs_review", "held"]
    issues: list[str]
    stages: list[str]
    published: Literal[False] = False
    external_calls: Literal[0] = 0
    validation_scope: Literal["evidence_linkage_only"] = "evidence_linkage_only"


class TopicDiagramNode(Contract):
    id: str
    label: str
    subLabel: str
    type: Literal["client", "gateway", "server", "auth", "database"]
    icon: str
    x: float | None = None
    y: float | None = None
    trustZone: str | None = None


class TopicDiagramEdge(Contract):
    id: str
    source: str
    target: str
    label: str | None = None
    protocol: str | None = None
    type: Literal["request", "response", "event", "internal", "error"]
    trustBoundary: str | None = None


class TopicFlowStep(Contract):
    step: int
    title: str
    actor: str
    target: str
    protocol: str
    action: str
    description: str
    payloadExample: str | None = None
    securityNote: str | None = None
    activeNodes: list[str]
    activeEdgeIds: list[str] | None = None


class TopicCodeSnippet(Contract):
    id: str
    title: str
    filename: str
    language: str
    code: str
    description: str


class TopicTradeOffs(Contract):
    pros: list[str]
    cons: list[str]


class TopicSnapshot(Contract):
    """Canonical public content only. Personal notes/account fields are rejected."""

    id: str
    lockVersion: int | None = None
    slug: str
    title: str
    koreanTitle: str
    category: str
    summary: str
    overview: str
    badges: list[str]
    readingTimeMinutes: int
    diagramNodes: list[TopicDiagramNode]
    diagramEdges: list[TopicDiagramEdge] | None = None
    flowSteps: list[TopicFlowStep]
    keyTakeaways: list[str]
    tradeOffs: TopicTradeOffs
    codeSnippets: list[TopicCodeSnippet]
    securityChecklistIds: list[str]
    relatedTermIds: list[str]
    relatedTopicIds: list[str]


class TopicRevisionContext(Contract):
    targetContentId: str = Field(min_length=1, max_length=80)
    baseLockVersion: int = Field(ge=0)
    topic: TopicSnapshot


class RevisionPatch(Contract):
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=4000)
    overview: str = Field(min_length=1, max_length=20000)
    keyTakeaways: list[str] = Field(min_length=1, max_length=30)


class TopicRevisionDraft(KnowledgeDraft):
    revisionPatch: RevisionPatch
