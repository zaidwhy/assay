"""Analyst: drafts atomic claims from the top reranked chunks, each citing the chunk ids it rests on."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field, ValidationError

from assay.router import parse_json_object
from assay.run_context import RunContext

log = logging.getLogger(__name__)

MAX_CLAIMS = 10
MAX_CHUNK_CHARS = 2400

SYSTEM_PROMPT = """ROLE: analyst
You answer a question using only the numbered passages given. Write 3 to 8 claims. Each claim is one
short sentence stating one fact taken from a passage, and lists the ids of the passages that state it.
One fact means one: never join two facts with "and", "which", "while" or a comma; write them as two
claims. Stay close to the passage's own wording and add no qualifier it does not state, because each
claim is checked against a single quoted sentence and a half-proven claim is removed.
Do not use outside knowledge. Write a claim for every fact in the passages that bears on the question,
even when the passages only answer part of it; write no claims only when no passage is relevant.
Set recommendation to one sentence that answers the question from your claims alone ("" when there are
no claims), and options to the courses of action the passages describe.
Set high_impact to true if acting on the answer could affect health, legal standing, money, safety,
or cannot be undone.
Return JSON only:
{"claims": [{"text": "...", "chunk_ids": ["s1:c0"]}],
 "options": ["..."], "recommendation": "...", "high_impact": false, "impact_reason": ""}"""


class PassageRef(BaseModel):
    id: str
    title: str
    text: str


class AnalystInput(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    passages: list[PassageRef] = Field(max_length=40)


class DraftClaim(BaseModel):
    text: str = Field(min_length=1, max_length=600)
    chunk_ids: list[str] = Field(default_factory=list, max_length=6)


class AnalystDraft(BaseModel):
    claims: list[DraftClaim] = Field(default_factory=list)
    options: list[str] = Field(default_factory=list)
    recommendation: str = ""
    high_impact: bool = False
    impact_reason: str = ""


def _coerce(raw: dict) -> AnalystDraft:
    """Accept the shapes small models produce (chunk_ids as a string, claims as plain strings)."""
    claims = []
    for item in raw.get("claims") or []:
        if isinstance(item, str):
            item = {"text": item, "chunk_ids": []}
        if not isinstance(item, dict):
            continue
        ids = item.get("chunk_ids") or item.get("citations") or []
        if isinstance(ids, str):
            ids = [ids]
        text = str(item.get("text", "")).strip()[:600]
        if text:
            claims.append({"text": text, "chunk_ids": [str(i).strip("[] ") for i in ids][:6]})
    options = raw.get("options") or []
    return AnalystDraft.model_validate(
        {
            "claims": claims[:MAX_CLAIMS],
            "options": [str(o)[:300] for o in options][:6] if isinstance(options, list) else [],
            "recommendation": str(raw.get("recommendation") or "")[:600],
            "high_impact": bool(raw.get("high_impact", False)),
            "impact_reason": str(raw.get("impact_reason") or "")[:300],
        }
    )


async def analyze(ctx: RunContext, request: AnalystInput, *, timeout_s: float) -> AnalystDraft:
    passages = "\n\n".join(f"[{p.id}] ({p.title})\n{p.text[:MAX_CHUNK_CHARS]}" for p in request.passages)
    reply = await ctx.llm_call(
        "analyst.draft", "strong",
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": f"Question: {request.question}\n\nPassages:\n{passages}"}],
        json_mode=True, timeout_s=timeout_s,
    )
    try:
        return _coerce(parse_json_object(reply.text))
    except (ValueError, ValidationError) as exc:
        log.warning("analyst reply in run %s was not usable JSON: %s", ctx.run_id, exc)
        ctx.scratchpad.notes.append("analyst reply was not valid JSON; no claims drafted")
        return AnalystDraft()
