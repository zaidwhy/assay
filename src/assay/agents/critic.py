"""Critic: checks each claim against the exact chunks it cites, then verifies the quoted span mechanically.

Two layers. The model gives a verdict and the quote it says proves the claim; then the quote must
actually occur in a cited chunk (normalized substring, or contiguous token overlap at or above the
threshold). A claim the model calls supported with a quote that is not in the chunk is struck.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections import Counter

from pydantic import BaseModel, Field, ValidationError

from assay.domain import Chunk, ClaimVerdict, Source, Verdict
from assay.router import parse_json_object
from assay.run_context import RunContext

log = logging.getLogger(__name__)

MIN_QUOTE_TOKENS = 3

SYSTEM_PROMPT = """ROLE: critic
You check one claim against the passages it cites, and nothing else. Copy, word for word, the shortest
span from a passage that proves the claim. Verdict "supported" if the span states the claim,
"partial" if it states only part of it, "unsupported" if no span states it (then quote "").
Return JSON only: {"verdict": "supported" | "partial" | "unsupported", "quote": "...", "reason": "..."}"""

# Curly quotes, long dashes and no-break spaces differ between the page and the model's copy of it.
_TRANSLATE = str.maketrans({chr(0x2018): "'", chr(0x2019): "'", chr(0x201C): '"', chr(0x201D): '"',
                            chr(0x2013): "-", chr(0x2014): "-", chr(0x00A0): " "})
_TOKEN = re.compile(r"[a-z0-9]+")


class CriticInput(BaseModel):
    claim_id: str
    claim: str = Field(min_length=1, max_length=600)
    chunks: list[Chunk] = Field(max_length=6)


class CriticOutput(BaseModel):
    verdict: Verdict
    quote: str = Field(default="", max_length=1200)
    reason: str = Field(default="", max_length=600)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_TRANSLATE).lower()
    return " ".join(text.split()).strip(" .,;:\"'")


def quote_in_chunk(quote: str, chunk_text: str, threshold: float) -> bool:
    """True when the quote occurs in the chunk: as a normalized substring, or as a contiguous
    window of chunk tokens sharing at least `threshold` of the quote's tokens."""
    quote_norm = _normalize(quote)
    if not quote_norm:
        return False
    if quote_norm in _normalize(chunk_text):
        return True
    quote_tokens = _TOKEN.findall(quote_norm)
    if len(quote_tokens) < MIN_QUOTE_TOKENS:
        return False
    chunk_tokens = _TOKEN.findall(_normalize(chunk_text))
    width = len(quote_tokens)
    wanted = Counter(quote_tokens)
    best = 0
    for start in range(max(1, len(chunk_tokens) - width + 1)):
        window = Counter(chunk_tokens[start : start + width])
        best = max(best, sum((wanted & window).values()))
        if best == width:
            break
    return best / width >= threshold


def _struck(claim_id: str, text: str, chunk_ids: list[str], reason: str) -> ClaimVerdict:
    return ClaimVerdict(claim_id=claim_id, text=text, chunk_ids=chunk_ids, verdict="unsupported", quote="",
                        reason=reason, span_found=False, struck=True)


async def judge(ctx: RunContext, request: CriticInput, *, timeout_s: float) -> ClaimVerdict:
    chunk_ids = [c.id for c in request.chunks]
    if not request.chunks:
        return _struck(request.claim_id, request.claim, chunk_ids, "cites no retrieved passage")
    passages = "\n\n".join(f"[{c.id}]\n{c.text}" for c in request.chunks)
    try:
        reply = await ctx.llm_call(
            # Many small parallel checks: the fast tier has its own rate limit, so the critique does
            # not queue behind the strong model's tokens-per-minute cap; the span check guards quality.
            f"critic.{request.claim_id}", "fast",
            [{"role": "system", "content": SYSTEM_PROMPT},
             {"role": "user", "content": f"Claim: {request.claim}\n\nCited passages:\n{passages}"}],
            json_mode=True, timeout_s=timeout_s,
        )
        judged = CriticOutput.model_validate(parse_json_object(reply.text))
    except TimeoutError:
        return _struck(request.claim_id, request.claim, chunk_ids, "not checked before the critique deadline")
    except (ValueError, ValidationError) as exc:
        log.info("critic reply for %s in run %s unusable: %s", request.claim_id, ctx.run_id, exc)
        return _struck(request.claim_id, request.claim, chunk_ids, "critic reply was not a valid verdict")
    except Exception as exc:
        # A provider outage after every fallback strikes this claim; it must not take down the run.
        log.warning("critic call for %s in run %s failed: %s", request.claim_id, ctx.run_id, exc)
        return _struck(request.claim_id, request.claim, chunk_ids, f"critic call failed ({type(exc).__name__})")
    span_found = any(
        quote_in_chunk(judged.quote, c.text, ctx.settings.span_overlap_threshold) for c in request.chunks
    )
    struck = not (judged.verdict == "supported" and span_found)
    reason = judged.reason.strip() or judged.verdict
    if judged.verdict == "supported" and not span_found:
        reason = "quoted span does not occur in the cited passage"
    return ClaimVerdict(claim_id=request.claim_id, text=request.claim, chunk_ids=chunk_ids, verdict=judged.verdict,
                        quote=judged.quote, reason=reason[:600], span_found=span_found, struck=struck)


def _emit_verdict(ctx: RunContext, verdict: ClaimVerdict) -> None:
    ctx.emit("claim.verdict", {"id": verdict.claim_id, "verdict": verdict.verdict, "quote": verdict.quote,
                               "reason": verdict.reason, "span_found": verdict.span_found,
                               "struck": verdict.struck})


async def _judge_and_emit(ctx: RunContext, request: CriticInput, timeout_s: float) -> ClaimVerdict:
    verdict = await judge(ctx, request, timeout_s=timeout_s)
    # Emitted as each claim lands so a watcher sees verdicts arrive, not one burst at the end.
    _emit_verdict(ctx, verdict)
    return verdict


async def judge_all(ctx: RunContext, requests: list[CriticInput], *, budget_s: float) -> list[ClaimVerdict]:
    """Judge every claim in parallel. Claims still unjudged at the budget are struck, not dropped."""
    tasks = [asyncio.create_task(_judge_and_emit(ctx, r, budget_s)) for r in requests]
    if tasks:
        await asyncio.wait(tasks, timeout=budget_s)
    verdicts = []
    for request, task in zip(requests, tasks, strict=True):
        if task.done() and not task.cancelled() and task.exception() is None:
            verdicts.append(task.result())
            continue
        if task.done() and not task.cancelled():
            log.error("critic failed on %s in run %s", request.claim_id, ctx.run_id, exc_info=task.exception())
        task.cancel()
        verdict = _struck(request.claim_id, request.claim, [c.id for c in request.chunks],
                          "not checked before the critique deadline")
        _emit_verdict(ctx, verdict)
        verdicts.append(verdict)
    return verdicts


def source_reliability(sources: dict[int, Source], chunks: dict[str, Chunk],
                       verdicts: list[ClaimVerdict]) -> dict[int, float]:
    """Half the domain prior, half the share of this source's citations that held up."""
    cited: Counter[int] = Counter()
    held: Counter[int] = Counter()
    for verdict in verdicts:
        for chunk_id in verdict.chunk_ids:
            chunk = chunks.get(chunk_id)
            if chunk is None:
                continue
            cited[chunk.source_id] += 1
            if not verdict.struck:
                held[chunk.source_id] += 1
    scores = {}
    for source_id, source in sources.items():
        if cited[source_id]:
            scores[source_id] = round(0.5 * source.reliability + 0.5 * held[source_id] / cited[source_id], 3)
        else:
            scores[source_id] = source.reliability
    return scores
