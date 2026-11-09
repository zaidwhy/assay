"""Types shared across stages: the run tree of question, sources, chunks, claims and verdicts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class RunState(StrEnum):
    queued = "queued"
    planning = "planning"
    researching = "researching"
    analyzing = "analyzing"
    critiquing = "critiquing"
    awaiting_approval = "awaiting_approval"
    reporting = "reporting"
    done = "done"
    abstained = "abstained"
    rejected = "rejected"
    failed = "failed"


TERMINAL_STATES = frozenset({RunState.done, RunState.abstained, RunState.rejected, RunState.failed})

SourceOrigin = Literal["web", "wikipedia", "arxiv", "upload", "memory"]


class Source(BaseModel):
    id: int
    url: str
    title: str
    domain: str
    origin: SourceOrigin
    reliability: float = Field(ge=0, le=1)
    text: str = Field(exclude=True, default="")


class Chunk(BaseModel):
    id: str  # s{source}:c{n}
    source_id: int
    start: int  # char offset into the source text, inclusive
    end: int  # exclusive
    heading: str = ""
    text: str


class RankedChunk(BaseModel):
    chunk: Chunk
    rank: int
    fused_score: float
    rerank_score: float | None = None


Verdict = Literal["supported", "partial", "unsupported"]


class ClaimVerdict(BaseModel):
    claim_id: str
    text: str
    chunk_ids: list[str]
    verdict: Verdict
    quote: str
    reason: str
    span_found: bool
    struck: bool


class Finding(BaseModel):
    n: int
    claim_id: str
    text: str
    citations: list[int]  # bibliography numbers


class BibEntry(BaseModel):
    n: int
    source_id: int
    title: str
    url: str
    domain: str
    reliability: float


class CitedLine(BaseModel):
    text: str
    citations: list[int]


class Report(BaseModel):
    question: str
    verdict: str
    verdict_citations: list[int] = Field(default_factory=list)
    abstained: bool
    recommendation: CitedLine | None
    key_findings: list[Finding]
    risks: list[CitedLine]
    open_questions: list[str]
    struck_claims: list[ClaimVerdict]
    bibliography: list[BibEntry]
    reused_prior_findings: list[str]
    markdown: str = ""
