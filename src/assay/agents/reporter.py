"""Report Generator: builds the decision report from surviving claims only.

Key findings are the verified claims verbatim. The model writes the verdict, recommendation and
risks, and each of those must name the findings it rests on; a line that cites no surviving
finding is dropped, so no uncited sentence reaches the reader.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field, ValidationError

from assay.domain import BibEntry, Chunk, CitedLine, ClaimVerdict, Finding, Report, Source
from assay.router import parse_json_object
from assay.run_context import RunContext

log = logging.getLogger(__name__)

INSUFFICIENT = "insufficient evidence"

SYSTEM_PROMPT = """ROLE: reporter
You write the decision part of a research report from numbered findings that have already been
verified. Use only those findings. Every statement must list the finding numbers it rests on.
Return JSON only:
{"verdict": "one sentence answering the question", "verdict_findings": [1],
 "recommendation": "one sentence", "recommendation_findings": [1, 2],
 "risks": [{"text": "...", "findings": [2]}],
 "open_questions": ["what the findings leave unanswered"]}"""

CHECK_PROMPT = """ROLE: conclusion check
You are given a question, a proposed one-sentence answer, and verified findings. Decide whether the
answer follows from the findings alone. It does not follow if it names a person, place, thing, number
or date the findings do not state, treats one thing as another (for example a real place as a legendary
one), or answers a different question than the one asked. Return JSON only:
{"follows": true, "reason": "one short sentence"}"""


class CheckOutput(BaseModel):
    follows: bool
    reason: str = Field(default="", max_length=400)


async def check_verdict(ctx: RunContext, verdict: str, findings: list[Finding], timeout_s: float) -> CheckOutput | None:
    """Each finding was proven against its passage; this proves the sentence built from them.
    None means the check could not run, which leaves the verdict standing but marked unchecked."""
    try:
        reply = await ctx.llm_call(
            "reporter.check", "fast",
            [{"role": "system", "content": CHECK_PROMPT},
             {"role": "user", "content": f"Question: {ctx.question}\n\nProposed answer: {verdict}\n\nVerified findings:\n"
              + "\n".join(f"{f.n}. {f.text}" for f in findings)}],
            json_mode=True, timeout_s=timeout_s,
        )
        return CheckOutput.model_validate(parse_json_object(reply.text))
    except Exception as exc:
        log.info("conclusion check in run %s did not run: %s", ctx.run_id, type(exc).__name__)
        return None


def _why_abstained(ctx: RunContext, verdicts: list[ClaimVerdict]) -> str:
    """Name the actual cause: an empty draft from a rate-limited model is not thin evidence."""
    error = ctx.scratchpad.analyst_error
    if not verdicts and error == "RateLimitError":
        return ("Model capacity ran out before any claim could be drafted (the provider's rate limit), so this "
                "says nothing about the evidence. Try again in a minute.")
    if not verdicts and error:
        return f"The Analyst could not draft claims ({error}), so no decision was made. Try again."
    if not verdicts and not ctx.sources:
        return "No sources were found for this question, so there is nothing to base a decision on."
    if not verdicts:
        return "The sources found do not state anything that bears on this question."
    supported = sum(1 for v in verdicts if not v.struck)
    return (f"Only {supported} of {len(verdicts)} claims held up against their cited passages, too few to "
            "support a decision. The removed claims and their reasons are below.")


class ReporterInput(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    findings: list[str] = Field(max_length=40)


class RiskLine(BaseModel):
    text: str = Field(max_length=600)
    findings: list[int] = Field(default_factory=list)


class ReporterOutput(BaseModel):
    verdict: str = Field(default="", max_length=600)
    verdict_findings: list[int] = Field(default_factory=list)
    recommendation: str = Field(default="", max_length=600)
    recommendation_findings: list[int] = Field(default_factory=list)
    risks: list[RiskLine] = Field(default_factory=list, max_length=6)
    open_questions: list[str] = Field(default_factory=list, max_length=6)


def _bibliography(sources: dict[int, Source], chunks: dict[str, Chunk], claims: list[ClaimVerdict],
                  reliability: dict[int, float]) -> tuple[list[BibEntry], dict[int, int]]:
    order: list[int] = []
    for claim in claims:
        for chunk_id in claim.chunk_ids:
            chunk = chunks.get(chunk_id)
            if chunk and chunk.source_id not in order:
                order.append(chunk.source_id)
    number_of = {source_id: n for n, source_id in enumerate(order, start=1)}
    entries = [
        BibEntry(n=number_of[sid], source_id=sid, title=sources[sid].title, url=sources[sid].url,
                 domain=sources[sid].domain, reliability=reliability.get(sid, sources[sid].reliability))
        for sid in order
    ]
    return entries, number_of


def _cited_line(text: str, finding_numbers: list[int], findings: list[Finding]) -> CitedLine | None:
    by_n = {f.n: f for f in findings}
    citations = sorted({c for n in finding_numbers if n in by_n for c in by_n[n].citations})
    if not text.strip() or not citations:
        return None
    return CitedLine(text=text.strip(), citations=citations)


def render_markdown(report: Report) -> str:
    def marks(numbers: list[int]) -> str:
        return "".join(f"[{n}]" for n in numbers)

    lines = [f"# {report.question}", "", f"**Verdict:** {report.verdict} {marks(report.verdict_citations)}".rstrip(), ""]
    if report.recommendation:
        lines += [f"**Recommendation:** {report.recommendation.text} {marks(report.recommendation.citations)}", ""]
    lines.append("## Key findings")
    lines += [f"{f.n}. {f.text} {marks(f.citations)}" for f in report.key_findings] or ["None survived the check."]
    if report.risks:
        lines += ["", "## Risks"] + [f"- {r.text} {marks(r.citations)}" for r in report.risks]
    if report.open_questions:
        lines += ["", "## Open questions"] + [f"- {q}" for q in report.open_questions]
    lines += ["", "## Struck claims"]
    lines += [f"- ~~{c.text}~~ ({c.verdict}: {c.reason})" for c in report.struck_claims] or ["None."]
    if report.reused_prior_findings:
        lines += ["", "## Reused from earlier in this session"] + [f"- {r}" for r in report.reused_prior_findings]
    lines += ["", "## Sources"]
    lines += [f"[{b.n}] {b.title} - {b.url} (reliability {b.reliability:.2f})" for b in report.bibliography]
    return "\n".join(lines) + "\n"


async def compose(
    ctx: RunContext,
    verdicts: list[ClaimVerdict],
    *,
    abstained: bool,
    reliability: dict[int, float],
    timeout_s: float,
) -> Report:
    supported = [v for v in verdicts if not v.struck]
    struck = [v for v in verdicts if v.struck]
    bibliography, number_of = _bibliography(ctx.sources, ctx.chunks, supported, reliability)
    findings = [
        Finding(n=i, claim_id=v.claim_id, text=v.text,
                citations=sorted({number_of[ctx.chunks[c].source_id] for c in v.chunk_ids if c in ctx.chunks}))
        for i, v in enumerate(supported, start=1)
    ]
    reused = sorted({ctx.sources[ctx.chunks[c].source_id].title for v in supported for c in v.chunk_ids
                     if c in ctx.chunks and ctx.sources[ctx.chunks[c].source_id].origin == "memory"})

    verdict_text, verdict_citations = INSUFFICIENT, []
    recommendation, risks, open_questions = None, [], []
    verdict_check, abstain_reason = "skipped", ""
    if abstained:
        open_questions = [f"Which sources would settle: {ctx.question}"]
        abstain_reason = _why_abstained(ctx, verdicts)
    elif findings:
        verdict_text = "See key findings; the report writer did not finish within its budget."
        try:
            reply = await ctx.llm_call(
                "reporter.compose", "strong",
                [{"role": "system", "content": SYSTEM_PROMPT},
                 {"role": "user", "content": f"Question: {ctx.question}\n\nVerified findings:\n"
                  + "\n".join(f"{f.n}. {f.text}" for f in findings)}],
                json_mode=True, timeout_s=timeout_s,
            )
            written = ReporterOutput.model_validate(parse_json_object(reply.text))
            verdict_line = _cited_line(written.verdict, written.verdict_findings or [1], findings)
            if verdict_line:
                verdict_text, verdict_citations = verdict_line.text, verdict_line.citations
            recommendation = _cited_line(written.recommendation, written.recommendation_findings, findings)
            risks = [line for r in written.risks if (line := _cited_line(r.text, r.findings, findings))]
            open_questions = [q.strip() for q in written.open_questions if q.strip()]
            if verdict_line:
                checked = await check_verdict(ctx, verdict_text, findings, timeout_s)
                if checked is not None and not checked.follows:
                    verdict_check, abstained = "failed", True
                    abstain_reason = "The verified findings do not answer the question as asked: " + checked.reason
                    verdict_text, verdict_citations, recommendation, risks = INSUFFICIENT, [], None, []
                elif checked is not None:
                    verdict_check = "passed"
        except TimeoutError:
            ctx.scratchpad.notes.append("report writer ran out of budget; findings shown without a written verdict")
        except (ValueError, ValidationError) as exc:
            log.info("reporter reply in run %s unusable: %s", ctx.run_id, exc)
            ctx.scratchpad.notes.append("report writer reply was not valid JSON; findings shown as verified")
        except Exception as exc:
            # Findings are already verified; losing the written verdict must not lose the report.
            log.warning("reporter call in run %s failed: %s", ctx.run_id, exc)
            ctx.scratchpad.notes.append(f"report writer failed ({type(exc).__name__}); findings shown as verified")

    report = Report(
        question=ctx.question,
        verdict=verdict_text,
        verdict_citations=verdict_citations,
        abstained=abstained,
        recommendation=recommendation,
        key_findings=findings,
        risks=risks,
        open_questions=open_questions,
        struck_claims=struck,
        bibliography=bibliography,
        reused_prior_findings=reused,
        verdict_check=verdict_check,
        abstain_reason=abstain_reason,
    )
    report.markdown = render_markdown(report)
    return report
