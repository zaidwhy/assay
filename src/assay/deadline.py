"""One wall-clock budget per run, split into stage budgets.

The deadline is created when the request arrives, so time spent queued counts against it.
A stage never gets more than what is left of the whole run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from assay.settings import Settings

STAGES = ("plan", "research", "analyze", "critique", "report")


@dataclass
class Deadline:
    total_s: float
    stage_budgets: dict[str, float]
    started: float = field(default_factory=time.monotonic)

    @classmethod
    def for_run(cls, total_s: float, settings: Settings) -> Deadline:
        base = {
            "plan": settings.budget_plan_s,
            "research": settings.budget_research_s,
            "analyze": settings.budget_analyze_s,
            "critique": settings.budget_critique_s,
            "report": settings.budget_report_s,
        }
        # Budgets are tuned for a 30 s run; a caller who asks for more time gets every stage scaled.
        scale = total_s / settings.default_deadline_s
        return cls(total_s=total_s, stage_budgets={name: secs * scale for name, secs in base.items()})

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def remaining(self) -> float:
        return max(0.0, self.total_s - self.elapsed())

    def expired(self) -> bool:
        return self.remaining() <= 0

    def stage(self, name: str) -> float:
        """Seconds the named stage may use.

        Its own budget, plus any slack earlier stages left unused (time beyond what the later
        stages are reserved), never more than what the run has left.
        """
        if name not in self.stage_budgets:
            raise KeyError(f"unknown stage {name!r}; stages are {STAGES}")
        remaining = self.remaining()
        later = sum(self.stage_budgets[s] for s in STAGES[STAGES.index(name) + 1 :])
        return min(remaining, max(self.stage_budgets[name], remaining - later))

    def fresh_stage(self, name: str) -> float:
        """Budget for a stage that runs after the approval gate, which the run deadline does not cover."""
        return self.stage_budgets[name]
