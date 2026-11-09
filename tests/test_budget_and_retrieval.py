from __future__ import annotations

from assay.deadline import Deadline
from assay.domain import Chunk
from assay.hitl import classify
from assay.retrieval import reciprocal_rank_fusion
from assay.settings import Settings
from tests.conftest import offline_retriever


def test_stage_budgets_scale_with_the_run_deadline(settings: Settings) -> None:
    default = Deadline.for_run(30, settings)
    longer = Deadline.for_run(60, settings)
    assert default.stage_budgets["research"] == 10
    assert longer.stage_budgets["research"] == 20
    # Planning may use slack beyond its own 3 s only when later stages keep their reservations.
    assert default.stage("plan") == min(default.remaining(), max(3, default.remaining() - 26))


def test_stage_budget_never_exceeds_what_is_left(settings: Settings) -> None:
    deadline = Deadline.for_run(30, settings)
    deadline.started -= 29.5
    assert deadline.stage("research") <= 0.5
    deadline.started -= 1
    assert deadline.expired() and deadline.stage("report") == 0


def test_rrf_rewards_agreement_between_rankings() -> None:
    fused = reciprocal_rank_fusion([[0, 1, 2], [1, 0, 2]], k=60)
    assert fused[0] == fused[1] > fused[2]


def test_hybrid_rank_puts_the_matching_chunk_first(settings: Settings) -> None:
    chunks = [
        Chunk(id="s1:c0", source_id=1, start=0, end=10, text="Bananas are yellow fruit grown in the tropics."),
        Chunk(id="s1:c1", source_id=1, start=10, end=20, text="Canberra is the capital city of Australia."),
        Chunk(id="s2:c0", source_id=2, start=0, end=10, text="Ottawa is the capital of Canada."),
    ]
    ranked = offline_retriever(settings).rank("capital city of Australia", chunks)
    assert ranked[0].chunk.id == "s1:c1"
    assert [r.rank for r in ranked] == [1, 2, 3]


def test_impact_classifier_flags_keywords_and_explicit_requests() -> None:
    assert classify("What dose of ibuprofen is safe?", None, False).high
    assert classify("Is intermittent fasting better than calorie restriction for weight loss?", None, False).high
    assert classify("What is the capital of Australia?", "high", False).reasons == ["request set impact=high"]
    assert not classify("What is the capital of Australia?", None, False).high
    assert classify("Is this plan sound?", None, True, "affects savings").high


def test_arxiv_is_only_planned_for_scholarly_questions() -> None:
    from assay.agents.researcher import build_plan
    from assay.router import ToolCall

    proposed = [ToolCall(name="arxiv", arguments={"query": "street lights"}),
                ToolCall(name="web_search", arguments={"query": "street lights"})]
    tools = {"web_search", "wikipedia", "arxiv", "fetch"}
    everyday = build_plan("Do LED street lights reduce crime at night?", proposed, tools)
    scholarly = build_plan("What does recent research say about LED street lights and crime?", proposed, tools)
    assert "arxiv" not in {c.name for c in everyday.tool_calls}
    assert "arxiv" in {c.name for c in scholarly.tool_calls}


def test_sources_below_the_rerank_floor_are_dropped(settings: Settings) -> None:
    chunks = [
        Chunk(id="s1:c0", source_id=1, start=0, end=10, text="Canberra is the capital city of Australia."),
        Chunk(id="s2:c0", source_id=2, start=0, end=10, text="Dark energy survey cosmology results."),
    ]
    retriever = offline_retriever(settings)
    retriever.min_source_rerank = 1.0  # the overlap reranker scores the off-topic chunk 0
    ranked = retriever.rank("capital city of Australia", chunks)
    assert [r.chunk.source_id for r in ranked] == [1]
