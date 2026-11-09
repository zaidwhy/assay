from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from assay.api import create_app
from assay.domain import RunState
from assay.retrieval import HybridRetriever
from assay.settings import Settings
from tests.fakes import LexicalEmbedder, OverlapReranker, ScriptedLLM, canberra_tools


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(db_path=tmp_path / "assay.db", use_ollama=True, api_key=None)


def offline_retriever(settings: Settings) -> HybridRetriever:
    return HybridRetriever(LexicalEmbedder(), OverlapReranker(), rrf_k=settings.rrf_k, fuse_top=settings.fuse_top,
                           rerank_top=settings.rerank_top)


@asynccontextmanager
async def running_app(settings: Settings, llm: ScriptedLLM | None = None,
                      tools: dict | None = None) -> AsyncIterator[tuple[httpx.AsyncClient, object]]:
    app = create_app(settings, llm=llm or ScriptedLLM(), tools=tools or canberra_tools(),
                     retriever=offline_retriever(settings), warm_models=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://assay.test") as client:
            yield client, app


async def wait_for_state(app, run_id: str, states: set[RunState], timeout_s: float = 10.0) -> str:  # noqa: ANN001
    """Block until the run reaches one of `states`, woken by its own events rather than a fixed sleep."""
    store, bus = app.state.store, app.state.bus
    async with asyncio.timeout(timeout_s):
        while True:
            waiter = bus.subscribe(run_id)
            try:
                state = store.get_run(run_id)["state"]
                if state in states:
                    return state
                await waiter.wait()
            finally:
                bus.unsubscribe(run_id, waiter)
