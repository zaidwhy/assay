"""Per-run evaluation, computed for every run after its report is written."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field, ValidationError

from assay.agents.critic import quote_in_chunk
from assay.domain import Chunk, ClaimVerdict, Report, Source
from assay.retrieval import HybridRetriever
from assay.router import parse_json_object
from assay.run_context import RunContext

log = logging.getLogger(__name__)

GRADE_PROMPT = """ROLE: grader
Grade how well the report answers the question, from 1 (irrelevant) to 5 (directly and fully answers).
Return JSON only: {"grade": <1-5>, "reason": "<one line on what is missing or present>"}"""

REPORT_SECTIONS = ("verdict", "recommendation", "key_findings", "risks", "open_questions", "bibliography")


class RelevanceGrade(BaseModel):
    grade: int = Field(ge=1, le=5)
    reason: str = Field(default="", max_length=300)


class RunEvaluation(BaseModel):
    faithfulness: float | None
    citation_precision: float | None
    relevance_similarity: float | None
    relevance_grade: int | None = None
    relevance_reason: str = ""
    structure_completeness: float
    source_diversity: int
    reliability_weighted_evidence: float
    claims_total: int
    claims_supported: int


def evaluate(report: Report, verdicts: list[ClaimVerdict], chunks: dict[str, Chunk], sources: dict[int, Source],
             reliability: dict[int, float], retriever: HybridRetriever | None, *,
             span_threshold: float) -> RunEvaluation:
    total = len(verdicts)
    supported = [v for v in verdicts if not v.struck]
    citations = [(v, c) for v in verdicts for c in v.chunk_ids]
    # A citation holds when its claim survived and the quoted span was found in that chunk.
    held = sum(
        1 for v, c in citations
        if not v.struck and c in chunks and v.quote and quote_in_chunk(v.quote, chunks[c].text, span_threshold)
    )
    similarity = None
    if retriever is not None and report.key_findings:
        text = report.verdict + " " + " ".join(f.text for f in report.key_findings)
        similarity = round(retriever.similarity(report.question, text), 4)
    present = sum(1 for section in REPORT_SECTIONS if getattr(report, section) not in (None, "", []))
    cited_sources = {chunks[c].source_id for v in supported for c in v.chunk_ids if c in chunks}
    weighted = sum(
        max((reliability.get(chunks[c].source_id, 0.0) for c in v.chunk_ids if c in chunks), default=0.0)
        for v in supported
    )
    return RunEvaluation(
        faithfulness=round(len(supported) / total, 4) if total else None,
        citation_precision=round(held / len(citations), 4) if citations else None,
        relevance_similarity=similarity,
        structure_completeness=round(present / len(REPORT_SECTIONS), 4),
        source_diversity=len({sources[s].domain for s in cited_sources if s in sources}),
        reliability_weighted_evidence=round(weighted, 4),
        claims_total=total,
        claims_supported=len(supported),
    )


async def grade_relevance(ctx: RunContext, report: Report, *, timeout_s: float) -> RelevanceGrade | None:
    try:
        reply = await ctx.llm_call(
            "evaluation.grade", "fast",
            [{"role": "system", "content": GRADE_PROMPT},
             {"role": "user", "content": f"Question: {report.question}\n\nReport:\n{report.markdown[:6000]}"}],
            json_mode=True, timeout_s=timeout_s,
        )
        return RelevanceGrade.model_validate(parse_json_object(reply.text))
    except TimeoutError:
        log.info("relevance grade timed out for run %s", ctx.run_id)
    except (ValueError, ValidationError) as exc:
        log.info("relevance grade for run %s unusable: %s", ctx.run_id, exc)
    return None
