"""Offline evaluation harness: runs every labelled question through the real pipeline.

Usage: uv run python -m eval.run [--max-seconds 30] [--limit N] [--ids a01,u02]

An answerable item counts as correct when the run does not abstain and a known answer appears in
the verdict or a surviving finding. An unanswerable or false-premise item counts as correct when
the run abstains. Results go to stdout and eval/results.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from assay.api import create_app
from assay.domain import TERMINAL_STATES, RunState
from assay.hitl import ApprovalDecision
from assay.orchestrator import ResearchRequest, run_view
from assay.settings import Settings

log = logging.getLogger("eval.run")

HERE = Path(__file__).parent


class LabelledQuestion(BaseModel):
    id: str
    question: str = Field(min_length=8, max_length=1000)
    answerable: bool
    answers: list[str]


def load_questions(path: Path) -> list[LabelledQuestion]:
    with path.open(encoding="utf-8") as handle:
        return [LabelledQuestion.model_validate_json(line) for line in handle if line.strip()]


def judge_item(item: LabelledQuestion, run: dict[str, Any]) -> dict[str, Any]:
    report = run.get("report") or {}
    abstained = run["state"] != "done"
    answer_text = " ".join([report.get("verdict", ""), *(f["text"] for f in report.get("key_findings", []))]).lower()
    found = any(answer.lower() in answer_text for answer in item.answers)
    correct = (not abstained and found) if item.answerable else abstained
    evaluation = run.get("evaluation") or {}
    return {
        "id": item.id,
        "answerable": item.answerable,
        "state": run["state"],
        "abstained": abstained,
        "answer_found": found,
        "correct": correct,
        "latency_ms": run.get("latency_ms"),
        "claims_total": evaluation.get("claims_total", 0),
        "claims_supported": evaluation.get("claims_supported", 0),
        "faithfulness": evaluation.get("faithfulness"),
        "verdict": report.get("verdict"),
    }


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    abstained = [i for i in items if i["abstained"]]
    should_abstain = [i for i in items if not i["answerable"]]
    true_abstentions = [i for i in abstained if not i["answerable"]]
    faithfulness = [i["faithfulness"] for i in items if i["faithfulness"] is not None]
    latencies = [i["latency_ms"] for i in items if i["latency_ms"] is not None]
    return {
        "items": len(items),
        "accuracy": round(sum(i["correct"] for i in items) / len(items), 4) if items else None,
        "answerable_accuracy": _share([i["correct"] for i in items if i["answerable"]]),
        # Separates "found and verified the answer, then abstained on the floor" from "never found it".
        "answerable_answer_in_findings": _share([i["answer_found"] for i in items if i["answerable"]]),
        "unanswerable_with_supported_claims": sum(
            1 for i in items if not i["answerable"] and i["claims_supported"]
        ),
        "abstention_precision": round(len(true_abstentions) / len(abstained), 4) if abstained else None,
        "abstention_recall": round(len(true_abstentions) / len(should_abstain), 4) if should_abstain else None,
        "faithfulness_mean": round(statistics.fmean(faithfulness), 4) if faithfulness else None,
        "latency_p50_ms": percentile(latencies, 0.5),
        "latency_p95_ms": percentile(latencies, 0.95),
        "runs_within_30s": sum(1 for v in latencies if v <= 30_000),
    }


def _share(flags: list[bool]) -> float | None:
    return round(sum(flags) / len(flags), 4) if flags else None


async def finish_run(app: Any, run_id: str) -> None:
    """Wait for the run to end. The harness has no reviewer, so a run held for approval is approved."""
    store, bus, orchestrator = app.state.store, app.state.bus, app.state.orchestrator
    while True:
        waiter = bus.subscribe(run_id)
        try:
            state = store.get_run(run_id)["state"]
            if state in {s.value for s in TERMINAL_STATES}:
                break
            if state == RunState.awaiting_approval:
                orchestrator.gate.decide(run_id, ApprovalDecision(decision="approve", note="eval harness"))
            await waiter.wait()
        finally:
            bus.unsubscribe(run_id, waiter)
    await orchestrator.wait(run_id)


async def run_all(questions: list[LabelledQuestion], max_seconds: float, db_path: Path,
                  pause_s: float = 0.0) -> dict[str, Any]:
    settings = Settings(db_path=db_path)
    app = create_app(settings, grade_with_llm=False)
    items = []
    async with app.router.lifespan_context(app):
        orchestrator = app.state.orchestrator
        chain = app.state.llm.describe()
        for position, item in enumerate(questions):
            if position and pause_s:
                # Free hosted tiers meter tokens per minute; pacing keeps every run on the same deployment.
                await asyncio.sleep(pause_s)
            run_id = orchestrator.submit(ResearchRequest(question=item.question, max_seconds=max_seconds))
            await finish_run(app, run_id)
            run = run_view(app.state.store, run_id)
            judged = judge_item(item, run)
            items.append(judged)
            print(f"{item.id} {judged['state']:<17} correct={judged['correct']!s:<5} "
                  f"latency={judged['latency_ms']}ms claims={judged['claims_supported']}/{judged['claims_total']}",
                  flush=True)
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "max_seconds": max_seconds,
        "pause_seconds": pause_s,
        "llm_chain": chain,
        "summary": summarize(items),
        "items": items,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the labelled question set through assay.")
    parser.add_argument("--max-seconds", type=float, default=30.0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--pause-seconds", type=float, default=0.0)
    parser.add_argument("--ids", type=str, default="")
    parser.add_argument("--db", type=Path, default=HERE / "eval.db")
    parser.add_argument("--out", type=Path, default=HERE / "results.json")
    args = parser.parse_args()
    if not 0 < args.max_seconds <= 60:
        parser.error("--max-seconds must be in (0, 60], the API's own limit")
    logging.basicConfig(level=logging.WARNING)
    questions = load_questions(HERE / "questions.jsonl")
    if args.ids:
        wanted = set(args.ids.split(","))
        questions = [q for q in questions if q.id in wanted]
    questions = questions[: args.limit] if args.limit else questions
    results = asyncio.run(run_all(questions, args.max_seconds, args.db, args.pause_seconds))
    args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps({"llm_chain": results["llm_chain"], **results["summary"]}, indent=2))


if __name__ == "__main__":
    main()
