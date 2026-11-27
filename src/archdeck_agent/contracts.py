from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class SourceRule(Contract):
    host: str = Field(min_length=1, max_length=253)
    publisher_group: str = Field(min_length=1, max_length=100)


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
