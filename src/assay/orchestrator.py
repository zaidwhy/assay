"""The run state machine.

planning -> researching -> analyzing -> critiquing -> (awaiting_approval) -> reporting
-> done | abstained | rejected | failed

Every transition is persisted and emitted as a `run.state` event. Each stage is bounded by the
run's Deadline; a stage that runs out of time hands the next stage whatever it produced.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field
from rank_bm25 import BM25Okapi

from assay import hitl
from assay.agents import analyst, critic, reporter, researcher
from assay.deadline import Deadline
from assay.domain import TERMINAL_STATES, ClaimVerdict, Report, RunState
from assay.evaluation import evaluate, grade_relevance
from assay.events import EventBus
from assay.ingest import chunk_text
from assay.retrieval import HybridRetriever, tokenize
from assay.router import LLMBackend
from assay.run_context import RunContext
from assay.settings import Settings
from assay.store import Store
from assay.telemetry import Metrics, Span, Tracer
from assay.tools import SourceDoc, Tool

log = logging.getLogger(__name__)

MEMORY_FINDINGS = 5
GRADE_TIMEOUT_S = 10.0


class ResearchRequest(BaseModel):
    question: str = Field(min_length=8, max_length=1000)
    session_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    impact: Literal["normal", "high"] | None = None
    max_seconds: float | None = Field(default=None, gt=0, le=60)


class QueueFull(RuntimeError):
    """Every run slot and every queue place is taken."""


class Admission:
    """At most `concurrency` runs execute; at most `queue_cap` more wait for a slot."""

    def __init__(self, concurrency: int, queue_cap: int) -> None:
        self.concurrency = concurrency
        self.queue_cap = queue_cap
        self._semaphore = asyncio.Semaphore(concurrency)
        self.waiting = 0
        self.running = 0

    def reserve(self) -> None:
        if self.running + self.waiting >= self.concurrency + self.queue_cap:
            raise QueueFull(
                f"{self.running} runs executing and {self.waiting} queued; the limits are "
                f"{self.concurrency} concurrent runs and a queue of {self.queue_cap}"
            )
        self.waiting += 1

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        try:
            await self._semaphore.acquire()
        finally:
            self.waiting -= 1
        self.running += 1
        try:
            yield
        finally:
            self.running -= 1
            self._semaphore.release()


@dataclass
class Assessment:
    verdicts: list[ClaimVerdict]
    reliability: dict[int, float]
    abstain: bool
    impact: hitl.ImpactAssessment
    draft: analyst.AnalystDraft


class Orchestrator:
    def __init__(self, *, store: Store, bus: EventBus, settings: Settings, llm: LLMBackend, tools: dict[str, Tool],
                 retriever: HybridRetriever, metrics: Metrics, gate: hitl.ApprovalGate,
                 grade_with_llm: bool = True) -> None:
        self.store = store
        self.bus = bus
        self.settings = settings
        self.llm = llm
        self.tools = tools
        self.retriever = retriever
        self.metrics = metrics
        self.gate = gate
        self.grade_with_llm = grade_with_llm
        self.admission = Admission(settings.max_concurrent_runs, settings.queue_cap)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    # admission --------------------------------------------------------------------------

    def submit(self, request: ResearchRequest) -> str:
        """Admit a run and start it. Raises QueueFull when there is no room."""
        try:
            self.admission.reserve()
        except QueueFull:
            self.metrics.rejected.inc()
            raise
        total_s = min(request.max_seconds or self.settings.default_deadline_s, self.settings.max_deadline_s)
        deadline = Deadline.for_run(total_s, self.settings)
        run_id = uuid.uuid4().hex[:12]
        self.store.create_run(run_id, request.question, request.session_id, request.impact or "normal", total_s)
        self.bus.publish(run_id, "run.state", {"state": RunState.queued, "previous": None, "elapsed_ms": 0})
        task = asyncio.create_task(self._execute(run_id, request, deadline), name=f"run-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run_id, None))
        return run_id

    async def wait(self, run_id: str) -> None:
        task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.shield(task)

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    # state machine ----------------------------------------------------------------------

    def _persist_span(self, span: Span) -> None:
        record = span.as_record()
        self.store.insert_span(record)
        self.bus.publish(span.run_id, "span", record)

    def _transition(self, ctx: RunContext, state: RunState, previous: RunState | None, **detail: object) -> RunState:
        self.store.update_run(ctx.run_id, state=state)
        payload: dict[str, object] = {"state": state, "previous": previous,
                                      "elapsed_ms": round(ctx.deadline.elapsed() * 1000)}
        if detail:
            payload["detail"] = detail
        self.bus.publish(ctx.run_id, "run.state", payload)
        log.info("run %s: %s -> %s", ctx.run_id, previous, state)
        if state in TERMINAL_STATES:
            self.metrics.runs.labels(state=state).inc()
        return state

    async def _execute(self, run_id: str, request: ResearchRequest, deadline: Deadline) -> None:
        tracer = Tracer(run_id, self._persist_span, self.metrics)
        ctx = RunContext(run_id=run_id, question=request.question, session_id=request.session_id,
                         settings=self.settings, deadline=deadline, tracer=tracer, llm=self.llm, tools=self.tools,
                         bus=self.bus)
        state: RunState = RunState.queued
        try:
            async with tracer.span("run", "run", question=request.question):
                async with self.admission.slot():
                    state, assessment = await self._assess(ctx, request, state)
                state = await self._finish(ctx, assessment, state)
        except asyncio.CancelledError:
            self.store.update_run(run_id, error="cancelled at shutdown")
            self._transition(ctx, RunState.failed, state, reason="cancelled at shutdown")
            raise
        except Exception as exc:
            log.exception("run %s failed in state %s", run_id, state)
            self.store.update_run(run_id, error=f"{type(exc).__name__}: {exc}"[:1000])
            self._transition(ctx, RunState.failed, state, reason=f"{type(exc).__name__}: {exc}"[:300])
        finally:
            self.gate.close(run_id)

    async def _assess(self, ctx: RunContext, request: ResearchRequest,
                      state: RunState) -> tuple[RunState, Assessment]:
        """Everything the 30 s budget covers: plan, research, analyze, critique."""
        state = self._transition(ctx, RunState.planning, state)
        prior = self._relevant_prior_findings(request)
        async with ctx.tracer.span("stage.planning", "stage"):
            try:
                research_plan = await researcher.plan(
                    ctx, researcher.ResearcherInput(question=request.question, prior_findings=prior),
                    timeout_s=ctx.deadline.stage("plan"),
                )
            except Exception as exc:
                # The plan is an optimization; searching the question itself is always a valid plan.
                log.warning("planner failed in run %s, using the baseline plan: %s %s", ctx.run_id, type(exc).__name__, exc)
                ctx.scratchpad.notes.append(f"planner failed ({type(exc).__name__}); searched the question directly")
                research_plan = researcher.baseline_plan(request.question)
        ctx.emit("query.planned", {
            "sub_queries": research_plan.sub_queries,
            "tool_calls": [{"tool": c.name, "arguments": c.arguments} for c in research_plan.tool_calls],
            "planned_by_model": research_plan.planned_by_model,
            "reused_findings": len(prior),
        })

        state = self._transition(ctx, RunState.researching, state)
        async with ctx.tracer.span("stage.researching", "stage") as span:
            if prior:
                ctx.add_source(SourceDoc(url=f"memory://session/{request.session_id}",
                                         title="Earlier findings in this session", text="\n\n".join(prior),
                                         origin="memory"))
            for document in self.store.session_documents(request.session_id) if request.session_id else []:
                ctx.add_source(SourceDoc(url=f"upload://{document['id']}/{document['name']}", title=document["name"],
                                         text=document["text"], origin="upload"))
            await researcher.gather(ctx, research_plan, budget_s=ctx.deadline.stage("research"))
            for source in ctx.sources.values():
                for chunk in chunk_text(source.id, source.text, chunk_tokens=self.settings.chunk_tokens,
                                        overlap=self.settings.chunk_overlap):
                    ctx.chunks[chunk.id] = chunk
            span.attrs.update(sources=len(ctx.sources), chunks=len(ctx.chunks))

        state = self._transition(ctx, RunState.analyzing, state)
        async with ctx.tracer.span("stage.analyzing", "stage"):
            async with ctx.tracer.span("retrieval.rank", "agent", candidates=len(ctx.chunks)):
                ranked = await self.retriever.rank_async(request.question, list(ctx.chunks.values()))
            for item in ranked:
                ctx.emit("chunk.ranked", {
                    "chunk_id": item.chunk.id, "source_id": item.chunk.source_id, "rank": item.rank,
                    "fused_score": item.fused_score, "rerank_score": item.rerank_score,
                    "heading": item.chunk.heading, "start": item.chunk.start, "end": item.chunk.end,
                    "preview": item.chunk.text[:240],
                })
            draft = analyst.AnalystDraft()
            if ranked:
                passages = [analyst.PassageRef(id=r.chunk.id, title=ctx.sources[r.chunk.source_id].title,
                                               text=r.chunk.text) for r in ranked]
                try:
                    draft = await analyst.analyze(ctx, analyst.AnalystInput(question=request.question,
                                                                            passages=passages),
                                                  timeout_s=ctx.deadline.stage("analyze"))
                except Exception as exc:
                    log.warning("analyst failed in run %s: %s %s", ctx.run_id, type(exc).__name__, exc)
                    ctx.scratchpad.notes.append(f"analyst failed ({type(exc).__name__}); no claims drafted")
        ranked_ids = {r.chunk.id for r in ranked}
        requests = []
        for i, claim in enumerate(draft.claims, start=1):
            claim_id = f"k{i}"
            ctx.emit("claim.drafted", {"id": claim_id, "text": claim.text, "chunk_ids": claim.chunk_ids})
            # Only passages the Analyst was shown can prove a claim; any other id is invented.
            cited = [ctx.chunks[c] for c in dict.fromkeys(claim.chunk_ids) if c in ranked_ids]
            requests.append(critic.CriticInput(claim_id=claim_id, claim=claim.text, chunks=cited[:6]))

        state = self._transition(ctx, RunState.critiquing, state)
        async with ctx.tracer.span("stage.critiquing", "stage", claims=len(requests)):
            verdicts = await critic.judge_all(ctx, requests, budget_s=ctx.deadline.stage("critique"))
        self.store.insert_verdicts(ctx.run_id, [v.model_dump() for v in verdicts])
        reliability = critic.source_reliability(ctx.sources, ctx.chunks, verdicts)

        supported = sum(1 for v in verdicts if not v.struck)
        ratio = supported / len(verdicts) if verdicts else 0.0
        abstain = supported < self.settings.min_supported_claims or ratio < self.settings.min_supported_ratio
        impact = hitl.classify(request.question, request.impact, draft.high_impact, draft.impact_reason)
        return state, Assessment(verdicts=verdicts, reliability=reliability, abstain=abstain, impact=impact,
                                 draft=draft)

    async def _finish(self, ctx: RunContext, assessment: Assessment, state: RunState) -> RunState:
        gated = not assessment.abstain and assessment.impact.high
        report_budget = ctx.deadline.stage("report")
        if gated:
            draft_view = {
                "claims": [v.model_dump() for v in assessment.verdicts],
                "options": assessment.draft.options,
                "recommendation": assessment.draft.recommendation,
                "impact_reasons": assessment.impact.reasons,
            }
            self.store.update_run(ctx.run_id, draft_json=json.dumps(draft_view),
                                  latency_ms=round(ctx.deadline.elapsed() * 1000, 1))
            self.metrics.run_latency.observe(ctx.deadline.elapsed())
            waiting = self.gate.open(ctx.run_id)
            state = self._transition(ctx, RunState.awaiting_approval, state, reasons=assessment.impact.reasons)
            decision = await waiting
            if decision.decision == "reject":
                self.store.update_run(ctx.run_id, error=f"rejected at approval: {decision.note}"[:1000])
                return self._transition(ctx, RunState.rejected, state, note=decision.note)
            # The run deadline covers everything up to the gate; the report after approval gets its own budget.
            report_budget = ctx.deadline.fresh_stage("report")

        state = self._transition(ctx, RunState.reporting, state)
        async with ctx.tracer.span("stage.reporting", "stage"):
            report = await reporter.compose(ctx, assessment.verdicts, abstained=assessment.abstain,
                                            reliability=assessment.reliability, timeout_s=report_budget)
        self.store.update_run(ctx.run_id, report_json=report.model_dump_json())
        if not gated:
            self.store.update_run(ctx.run_id, latency_ms=round(ctx.deadline.elapsed() * 1000, 1))
            self.metrics.run_latency.observe(ctx.deadline.elapsed())
        ctx.emit("report.ready", {"verdict": report.verdict, "abstained": report.abstained,
                                  "report": report.model_dump(mode="json"), "markdown": report.markdown})
        if ctx.session_id:
            self.store.add_session_findings(ctx.session_id, ctx.run_id, ctx.question, report.verdict,
                                            [f.text for f in report.key_findings])
        state = self._transition(ctx, RunState.abstained if report.abstained else RunState.done, state)
        await self._evaluate(ctx, report, assessment)
        return state

    async def _evaluate(self, ctx: RunContext, report: Report, assessment: Assessment) -> None:
        evaluation = await asyncio.to_thread(
            evaluate, report, assessment.verdicts, ctx.chunks, ctx.sources, assessment.reliability, self.retriever,
            span_threshold=self.settings.span_overlap_threshold,
        )
        if self.grade_with_llm and report.key_findings:
            grade = await grade_relevance(ctx, report, timeout_s=GRADE_TIMEOUT_S)
            if grade:
                evaluation.relevance_grade = grade.grade
                evaluation.relevance_reason = grade.reason
        self.store.update_run(ctx.run_id, evaluation_json=evaluation.model_dump_json())

    def _relevant_prior_findings(self, request: ResearchRequest) -> list[str]:
        if not request.session_id:
            return []
        rows = self.store.session_findings(request.session_id)
        claims = list(dict.fromkeys(r["claim_text"] for r in rows))
        if not claims:
            return []
        scores = BM25Okapi([tokenize(c) or ["_"] for c in claims]).get_scores(tokenize(request.question))
        ranked = sorted(zip(scores, claims, strict=True), reverse=True)
        return [claim for score, claim in ranked[:MEMORY_FINDINGS] if score > 0]


def run_view(store: Store, run_id: str) -> dict | None:
    """The public shape of a run for GET /runs/{id}."""
    row = store.get_run(run_id)
    if row is None:
        return None
    return {
        "run_id": row["id"],
        "question": row["question"],
        "session_id": row["session_id"],
        "impact": row["impact"],
        "state": row["state"],
        "deadline_s": row["deadline_s"],
        "latency_ms": row["latency_ms"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "draft": json.loads(row["draft_json"]) if row["draft_json"] else None,
        "report": json.loads(row["report_json"]) if row["report_json"] else None,
        "evaluation": json.loads(row["evaluation_json"]) if row["evaluation_json"] else None,
        "error": row["error"],
        "verdicts": store.verdicts_for_run(run_id),
    }
