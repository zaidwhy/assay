from __future__ import annotations

from assay.agents.critic import CriticInput, judge, passage_for, quote_in_chunk
from assay.deadline import Deadline
from assay.domain import Chunk
from assay.events import EventBus
from assay.run_context import RunContext
from assay.settings import Settings
from assay.store import Store
from assay.telemetry import Tracer
from tests.fakes import CANBERRA_TEXT, ScriptedLLM

CHUNK = Chunk(id="s1:c0", source_id=1, start=0, end=len(CANBERRA_TEXT), text=CANBERRA_TEXT)


def make_ctx(settings: Settings, llm: ScriptedLLM) -> RunContext:
    store = Store(settings.db_path)
    store.create_run("abc123abc123", "What is the capital of Australia?", None, "normal", 30)
    bus = EventBus(store)
    return RunContext(run_id="abc123abc123", question="What is the capital of Australia?", session_id=None,
                      settings=settings, deadline=Deadline.for_run(30, settings),
                      tracer=Tracer("abc123abc123", lambda span: None), llm=llm, tools={}, bus=bus)


def test_exact_and_reformatted_quotes_are_found() -> None:
    assert quote_in_chunk("Canberra is the capital city of Australia.", CANBERRA_TEXT, 0.6)
    # Line breaks and case differ between page and quote; the span is still the same span.
    assert quote_in_chunk("SELECTED as the site of the capital in 1908 as a compromise", CANBERRA_TEXT, 0.6)


def test_fabricated_quote_is_not_found() -> None:
    assert not quote_in_chunk("Sydney has been the capital of Australia since 1901", CANBERRA_TEXT, 0.6)
    assert not quote_in_chunk("", CANBERRA_TEXT, 0.6)


def test_one_word_slip_in_a_copied_span_is_still_found() -> None:
    # 10 of 11 tokens sit in one contiguous window: the model misquoted a word, not the fact.
    assert quote_in_chunk("the city was chosen as the site of the capital in 1908", CANBERRA_TEXT, 0.6)


def test_words_gathered_from_across_the_chunk_are_not_a_span() -> None:
    # Every word occurs in the chunk, but never together; a stitched quote proves nothing.
    assert not quote_in_chunk("Canberra Griffin 1913 Melbourne compromise", CANBERRA_TEXT, 0.6)


def test_short_quote_must_match_exactly() -> None:
    # Under three tokens there is no window to judge overlap on, so only a verbatim match counts.
    assert quote_in_chunk("Construction began", CANBERRA_TEXT, 0.6)
    assert not quote_in_chunk("capital 1908", CANBERRA_TEXT, 0.6)


async def test_supported_claim_with_quote_absent_from_chunk_is_struck(settings: Settings) -> None:
    claim = "Sydney is the capital of Australia."
    llm = ScriptedLLM(claims=[(claim, "Sydney was made the national capital by the 1901 constitution")])
    verdict = await judge(make_ctx(settings, llm), CriticInput(claim_id="k1", claim=claim, chunks=[CHUNK]),
                          timeout_s=5)
    assert verdict.verdict == "supported"  # the model said so
    assert verdict.span_found is False
    assert verdict.struck is True
    assert verdict.reason == "quoted span does not occur in the cited passage"


async def test_supported_claim_with_real_quote_survives(settings: Settings) -> None:
    claim = "Parliament House opened in 1988."
    llm = ScriptedLLM(claims=[(claim, "Parliament House in Canberra opened in 1988")])
    verdict = await judge(make_ctx(settings, llm), CriticInput(claim_id="k1", claim=claim, chunks=[CHUNK]),
                          timeout_s=5)
    assert verdict.struck is False and verdict.span_found is True


async def test_claim_citing_no_retrieved_chunk_is_struck_without_a_model_call(settings: Settings) -> None:
    llm = ScriptedLLM()
    verdict = await judge(make_ctx(settings, llm), CriticInput(claim_id="k1", claim="Anything.", chunks=[]),
                          timeout_s=5)
    assert verdict.struck and verdict.reason == "cites no retrieved passage"
    assert llm.calls == []


def test_passage_for_points_at_the_sentence_the_quote_came_from() -> None:
    text = ("Street lighting is a large municipal cost. Converting to LED fixtures typically uses about "
            "50 percent less energy than high-pressure sodium. Fixtures are rated for 15 years.")
    chunk = Chunk(id="s1:c0", source_id=1, start=0, end=len(text), text=text)
    passage = passage_for("uses about 50 percent less energy", [chunk])
    assert passage is not None
    assert passage["chunk_id"] == "s1:c0"
    assert "50 percent less energy" in passage["match"]
    assert passage["before"].endswith("large municipal cost.")
    assert passage_for("", [chunk]) is None


class RateLimitError(Exception):
    """Stands in for litellm's rate-limit error; only the class name is read."""


async def test_a_claim_the_model_never_saw_is_not_checked_rather_than_invalid(settings: Settings) -> None:
    llm = ScriptedLLM(failures={"critic": RateLimitError("429 tokens per day")})
    request = CriticInput(claim_id="k1", claim="Canberra is the capital city of Australia.", chunks=[CHUNK])
    verdict = await judge(make_ctx(settings, llm), request, timeout_s=5)
    assert verdict.struck and not verdict.checked
    assert verdict.reason.startswith("not checked: model capacity ran out")
