"""End-to-end runs through the HTTP API with the scripted LLM and in-memory tools."""

from __future__ import annotations

import asyncio
import json

from assay.domain import RunState
from assay.settings import Settings
from tests.conftest import running_app, wait_for_state
from tests.fakes import GOOD_CLAIMS, ScriptedLLM, canberra_tools

QUESTION = "What is the capital city of Australia?"
TERMINAL = {RunState.done, RunState.abstained, RunState.failed, RunState.rejected}


async def test_supported_run_produces_cited_report(settings: Settings) -> None:
    async with running_app(settings) as (client, app):
        response = await client.post("/research", json={"question": QUESTION})
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        assert await wait_for_state(app, run_id, TERMINAL) == RunState.done
        await app.state.orchestrator.wait(run_id)
        run = (await client.get(f"/runs/{run_id}")).json()
    report = run["report"]
    assert report["abstained"] is False
    assert len(report["key_findings"]) == len(GOOD_CLAIMS)
    assert all(f["citations"] for f in report["key_findings"])
    assert report["recommendation"]["citations"]
    # The scripted reporter offers a risk with no finding behind it; it must not reach the report.
    assert report["risks"] == []
    assert run["evaluation"]["faithfulness"] == 1.0
    assert run["latency_ms"] < 30_000


async def test_abstains_when_supported_claims_fall_under_the_floor(settings: Settings) -> None:
    fabricated = [(text, "a sentence that appears in no passage at all") for text, _ in GOOD_CLAIMS[1:4]]
    llm = ScriptedLLM(claims=[GOOD_CLAIMS[0], *fabricated])
    async with running_app(settings, llm=llm) as (client, app):
        run_id = (await client.post("/research", json={"question": QUESTION})).json()["run_id"]
        assert await wait_for_state(app, run_id, TERMINAL) == RunState.abstained
        await app.state.orchestrator.wait(run_id)
        report = (await client.get(f"/runs/{run_id}")).json()["report"]
    assert report["verdict"] == "insufficient evidence"
    assert report["abstained"] is True
    assert len(report["struck_claims"]) == 3
    assert "reporter" not in llm.calls


async def test_one_proven_claim_answers_a_one_fact_question(settings: Settings) -> None:
    llm = ScriptedLLM(claims=[GOOD_CLAIMS[0]])
    async with running_app(settings, llm=llm) as (client, app):
        run_id = (await client.post("/research", json={"question": QUESTION})).json()["run_id"]
        assert await wait_for_state(app, run_id, TERMINAL) == RunState.done
        await app.state.orchestrator.wait(run_id)
        report = (await client.get(f"/runs/{run_id}")).json()["report"]
    assert report["abstained"] is False
    assert [f["text"] for f in report["key_findings"]] == [GOOD_CLAIMS[0][0]]


async def test_abstains_when_no_claim_survives(settings: Settings) -> None:
    llm = ScriptedLLM(claims=[(GOOD_CLAIMS[0][0], "a sentence that appears in no passage at all")])
    async with running_app(settings, llm=llm) as (client, app):
        run_id = (await client.post("/research", json={"question": QUESTION})).json()["run_id"]
        assert await wait_for_state(app, run_id, TERMINAL) == RunState.abstained
        await app.state.orchestrator.wait(run_id)
        report = (await client.get(f"/runs/{run_id}")).json()["report"]
    assert report["verdict"] == "insufficient evidence"
    assert len(report["struck_claims"]) == 1


async def test_deadline_expiry_mid_research_still_reports_from_what_arrived(settings: Settings) -> None:
    # The wikipedia tool never returns; research must stop at its budget and use the web result.
    async with running_app(settings, tools=canberra_tools(hang_wikipedia=True)) as (client, app):
        run_id = (await client.post("/research", json={"question": QUESTION, "max_seconds": 3})).json()["run_id"]
        assert await wait_for_state(app, run_id, TERMINAL) == RunState.done
        await app.state.orchestrator.wait(run_id)
        run = (await client.get(f"/runs/{run_id}")).json()
        trace = (await client.get(f"/runs/{run_id}/trace")).json()["spans"]
    assert run["report"]["key_findings"]
    assert {b["domain"] for b in run["report"]["bibliography"]} == {"example.org"}
    wiki_spans = [s for s in trace if s["name"] == "tool.wikipedia"]
    assert wiki_spans and wiki_spans[0]["status"] == "cancelled"
    assert run["latency_ms"] < 3_000 + 500


async def test_research_stops_early_once_enough_sources_are_in(settings: Settings) -> None:
    # A hung tool must not hold research to its full budget when the evidence is already in hand.
    eager = settings.model_copy(update={"research_enough_sources": 1, "research_settle_s": 0.5})
    async with running_app(eager, tools=canberra_tools(hang_wikipedia=True)) as (client, app):
        run_id = (await client.post("/research", json={"question": QUESTION, "max_seconds": 20})).json()["run_id"]
        assert await wait_for_state(app, run_id, TERMINAL) == RunState.done
        await app.state.orchestrator.wait(run_id)
        trace = (await client.get(f"/runs/{run_id}/trace")).json()["spans"]
    research = next(s for s in trace if s["name"] == "stage.researching")
    assert research["latency_ms"] < 2_000


async def test_high_impact_run_waits_for_approval_then_reports(settings: Settings) -> None:
    async with running_app(settings) as (client, app):
        run_id = (await client.post("/research", json={"question": QUESTION, "impact": "high"})).json()["run_id"]
        assert await wait_for_state(app, run_id, TERMINAL | {RunState.awaiting_approval}) == RunState.awaiting_approval
        run = (await client.get(f"/runs/{run_id}")).json()
        assert run["report"] is None
        assert run["draft"]["claims"] and "request set impact=high" in run["draft"]["impact_reasons"]

        response = await client.post(f"/runs/{run_id}/approval", json={"decision": "approve", "note": "ok"})
        assert response.status_code == 200
        assert await wait_for_state(app, run_id, TERMINAL) == RunState.done
        await app.state.orchestrator.wait(run_id)
        assert (await client.get(f"/runs/{run_id}")).json()["report"]["key_findings"]
        again = await client.post(f"/runs/{run_id}/approval", json={"decision": "approve"})
        assert again.status_code == 409


async def test_rejected_run_ends_rejected_without_report(settings: Settings) -> None:
    async with running_app(settings) as (client, app):
        question = "Should I change my medication dose for blood pressure?"
        run_id = (await client.post("/research", json={"question": question})).json()["run_id"]
        await wait_for_state(app, run_id, TERMINAL | {RunState.awaiting_approval})
        await client.post(f"/runs/{run_id}/approval", json={"decision": "reject", "note": "needs a doctor"})
        assert await wait_for_state(app, run_id, TERMINAL) == RunState.rejected
        run = (await client.get(f"/runs/{run_id}")).json()
    assert run["report"] is None
    assert "needs a doctor" in run["error"]


async def test_queue_cap_returns_429_with_retry_after(settings: Settings) -> None:
    capped = settings.model_copy(update={"max_concurrent_runs": 1, "queue_cap": 1})
    blocker = asyncio.Event()
    llm = ScriptedLLM(blockers={"researcher": blocker})
    async with running_app(capped, llm=llm) as (client, app):
        first = await client.post("/research", json={"question": QUESTION})
        second = await client.post("/research", json={"question": QUESTION})
        third = await client.post("/research", json={"question": QUESTION})
        assert (first.status_code, second.status_code, third.status_code) == (202, 202, 429)
        assert third.headers["Retry-After"] == str(capped.queue_retry_after_s)
        assert "a queue of 1" in third.json()["detail"]
        blocker.set()
        for run_id in (first.json()["run_id"], second.json()["run_id"]):
            await wait_for_state(app, run_id, TERMINAL)


async def test_request_bounds_are_enforced(settings: Settings) -> None:
    async with running_app(settings) as (client, _):
        short = await client.post("/research", json={"question": "short"})
        long_deadline = await client.post("/research", json={"question": QUESTION, "max_seconds": 61})
    assert short.status_code == 422 and "question" in short.text
    assert long_deadline.status_code == 422 and "max_seconds" in long_deadline.text


async def test_event_stream_emits_typed_events_in_order(settings: Settings) -> None:
    async with running_app(settings) as (client, app):
        run_id = (await client.post("/research", json={"question": QUESTION})).json()["run_id"]
        await wait_for_state(app, run_id, TERMINAL)
        await app.state.orchestrator.wait(run_id)
        body = (await client.get(f"/runs/{run_id}/events")).text
    frames = [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]
    types = [f["type"] for f in frames]
    for expected in ("run.state", "query.planned", "source.found", "chunk.ranked", "claim.drafted",
                     "claim.verdict", "report.ready", "span"):
        assert expected in types, expected
    assert [f["seq"] for f in frames] == sorted(f["seq"] for f in frames)
    source = next(f for f in frames if f["type"] == "source.found")["data"]
    assert {"url", "title", "domain", "reliability"} <= set(source)
    verdict = next(f for f in frames if f["type"] == "claim.verdict")["data"]
    assert {"id", "verdict", "quote", "reason"} <= set(verdict)
    states = [f["data"]["state"] for f in frames if f["type"] == "run.state"]
    assert states[:6] == ["queued", "planning", "researching", "analyzing", "critiquing", "reporting"]


async def test_api_key_guards_writes_when_set(settings: Settings) -> None:
    guarded = settings.model_copy(update={"api_key": "local-test-key-value"})
    async with running_app(guarded) as (client, _):
        refused = await client.post("/research", json={"question": QUESTION})
        allowed = await client.post("/research", json={"question": QUESTION},
                                    headers={"X-API-Key": "local-test-key-value"})
        health = await client.get("/health")
    assert refused.status_code == 401
    assert allowed.status_code == 202
    assert health.status_code == 200 and health.json()["schema_version"] == 1


async def test_session_memory_is_reused_by_a_later_run(settings: Settings) -> None:
    async with running_app(settings) as (client, app):
        first = (await client.post("/research", json={"question": QUESTION, "session_id": "s-1"})).json()["run_id"]
        await wait_for_state(app, first, TERMINAL)
        await app.state.orchestrator.wait(first)
        second = (await client.post("/research", json={"question": "When did Canberra become the capital city?",
                                                       "session_id": "s-1"})).json()["run_id"]
        await wait_for_state(app, second, TERMINAL)
        body = (await client.get(f"/runs/{second}/events")).text
    planned = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    plan = next(f for f in planned if f["type"] == "query.planned")["data"]
    assert plan["reused_findings"] > 0
    assert any(f["type"] == "source.found" and f["data"]["origin"] == "memory" for f in planned)


async def test_verdict_that_does_not_follow_from_findings_abstains(settings: Settings) -> None:
    # Every finding was proven, but the sentence built from them claims more; the run must not ship it.
    llm = ScriptedLLM(verdict_follows=False)
    async with running_app(settings, llm=llm) as (client, app):
        run_id = (await client.post("/research", json={"question": QUESTION})).json()["run_id"]
        assert await wait_for_state(app, run_id, TERMINAL) == RunState.abstained
        await app.state.orchestrator.wait(run_id)
        report = (await client.get(f"/runs/{run_id}")).json()["report"]
    assert report["verdict"] == "insufficient evidence"
    assert report["verdict_check"] == "failed"
    assert "Thera, not Atlantis" in report["abstain_reason"]
    assert report["key_findings"], "verified findings are still shown"


async def test_verdict_that_follows_is_marked_checked(settings: Settings) -> None:
    async with running_app(settings) as (client, app):
        run_id = (await client.post("/research", json={"question": QUESTION})).json()["run_id"]
        assert await wait_for_state(app, run_id, TERMINAL) == RunState.done
        await app.state.orchestrator.wait(run_id)
        report = (await client.get(f"/runs/{run_id}")).json()["report"]
    assert report["verdict_check"] == "passed"
    assert report["verdict"] == "Canberra is the capital of Australia."
