"""The real litellm Router with mocked deployments: no network, real fallback logic."""

from __future__ import annotations

from assay.deadline import Deadline
from assay.events import EventBus
from assay.router import Deployment, RouterLLM, build_chain, parse_json_object
from assay.run_context import RunContext
from assay.settings import Settings
from assay.store import Store
from assay.telemetry import Span, Tracer


def mock_chain(primary_error: str | None) -> dict:
    primary = {"model": "openai/primary-model", "api_key": "unused", "mock_response": primary_error or "from primary"}
    backup = {"model": "openai/backup-model", "api_key": "unused", "mock_response": "from backup"}
    return {tier: [Deployment(f"{tier}:primary", primary), Deployment(f"{tier}:backup", backup)]
            for tier in ("fast", "strong")}


async def call_through_context(settings: Settings, llm: RouterLLM) -> tuple[str, list[Span]]:
    spans: list[Span] = []
    store = Store(settings.db_path)
    ctx = RunContext(run_id="r0", question="q", session_id=None, settings=settings,
                     deadline=Deadline.for_run(30, settings), tracer=Tracer("r0", spans.append), llm=llm, tools={},
                     bus=EventBus(store))
    reply = await ctx.llm_call("analyst.draft", "strong", [{"role": "user", "content": "hi"}], timeout_s=10)
    return reply.text, spans


async def test_provider_failure_falls_back_and_span_records_it(settings: Settings) -> None:
    no_retry = settings.model_copy(update={"llm_num_retries": 0})
    text, spans = await call_through_context(no_retry, RouterLLM(no_retry, chain=mock_chain("litellm.RateLimitError")))
    assert text == "from backup"
    (span,) = spans
    assert span.kind == "llm"
    assert span.fallback_used is True
    assert span.attrs["deployment"] == "strong:backup"
    assert span.retries >= 1
    assert span.status == "ok"


async def test_healthy_primary_is_not_a_fallback(settings: Settings) -> None:
    text, spans = await call_through_context(settings, RouterLLM(settings, chain=mock_chain(None)))
    assert text == "from primary"
    assert spans[0].fallback_used is False and spans[0].retries == 0


def test_chain_skips_providers_without_keys(settings: Settings) -> None:
    only_ollama = build_chain(settings, environ={})
    assert [d.name for d in only_ollama["strong"]] == ["strong:ollama"]
    full = build_chain(settings, environ={"GROQ_API_KEY": "x", "OPENROUTER_API_KEY": "y"})
    assert [d.name for d in full["fast"]] == ["fast:groq", "fast:groq-backup", "fast:openrouter", "fast:ollama"]
    # Keys are referenced by variable name, never copied into the deployment config.
    assert full["fast"][0].litellm_params["api_key"] == "os.environ/GROQ_API_KEY"


def test_json_object_is_found_inside_prose() -> None:
    assert parse_json_object('Sure! ```json\n{"verdict": "supported"}\n```') == {"verdict": "supported"}
