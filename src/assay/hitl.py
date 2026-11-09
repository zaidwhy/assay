"""Human-in-the-loop: which runs need approval, and the gate they wait at."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

Decision = Literal["approve", "reject"]

HIGH_IMPACT_TERMS: dict[str, tuple[str, ...]] = {
    "medical": ("dose", "dosage", "diagnos", "symptom", "treatment", "medication", "surgery", "prescri",
                "pregnan", "side effect", "overdose", "vaccine"),
    "legal": ("lawsuit", "sue ", "legal advice", "contract", "liability", "custody", "visa", "immigration",
              "court", "attorney", "lawyer"),
    "financial": ("invest", "mortgage", "loan", "retirement", "stock", "crypto", "bankrupt", "tax ",
                  "taxes", "pension", "savings"),
    "safety": ("dangerous", "toxic", "poison", "explosive", "weapon", "electrical wiring", "carbon monoxide"),
    "irreversible": ("irreversible", "permanent", "cannot be undone", "quit my job", "resign", "divorce",
                     "euthan", "delete all"),
}


class ImpactAssessment(BaseModel):
    high: bool
    reasons: list[str] = Field(default_factory=list)


def classify(question: str, requested: str | None, analyst_flag: bool, analyst_reason: str = "") -> ImpactAssessment:
    reasons = []
    if requested == "high":
        reasons.append("request set impact=high")
    lowered = f" {question.lower()} "
    for category, terms in HIGH_IMPACT_TERMS.items():
        hit = next((t for t in terms if re.search(re.escape(t), lowered)), None)
        if hit:
            reasons.append(f"{category} keyword {hit.strip()!r}")
    if analyst_flag:
        reasons.append(f"analyst flagged high impact: {analyst_reason}".rstrip(": "))
    return ImpactAssessment(high=bool(reasons), reasons=reasons)


class ApprovalDecision(BaseModel):
    decision: Decision
    note: str = Field(default="", max_length=2000)


class ApprovalGate:
    """One pending future per run waiting at `awaiting_approval`."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[ApprovalDecision]] = {}

    def open(self, run_id: str) -> asyncio.Future[ApprovalDecision]:
        future: asyncio.Future[ApprovalDecision] = asyncio.get_running_loop().create_future()
        self._pending[run_id] = future
        return future

    def is_waiting(self, run_id: str) -> bool:
        return run_id in self._pending and not self._pending[run_id].done()

    def decide(self, run_id: str, decision: ApprovalDecision) -> bool:
        future = self._pending.get(run_id)
        if future is None or future.done():
            return False
        future.set_result(decision)
        return True

    def close(self, run_id: str) -> None:
        self._pending.pop(run_id, None)
