"""Web search: Tavily results become sources with page text, and a Tavily failure falls back to ddgs."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from assay.tools import web_search

PAGE = "LED fixtures typically use about 50 percent less energy than high-pressure sodium. " * 12


def tavily_returning(status: int, body: dict) -> type[httpx.AsyncClient]:
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(status, json=body)

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(answer), **kwargs)

    return Client


async def test_tavily_hits_carry_page_text_or_stay_snippets(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"results": [
        {"url": "https://energy.example/led", "title": "LED street lights", "content": "short", "raw_content": PAGE},
        {"url": "https://news.example/led", "title": "City news", "content": "a short snippet", "raw_content": None},
        {"url": "ftp://bad.example/x", "title": "skipped", "content": "x"},
    ]}
    monkeypatch.setattr(web_search.httpx, "AsyncClient", tavily_returning(200, body))
    tool = web_search.make_web_search_tool("auto", tavily_api_key="test-key")
    docs = await tool.run({"query": "LED street lights energy"})
    assert [(d.url, d.snippet_only) for d in docs] == [("https://energy.example/led", False), ("https://news.example/led", True)]
    assert docs[0].text == PAGE
    assert docs[1].text == "a short snippet"


async def test_tavily_failure_falls_back_to_keyless_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_search.httpx, "AsyncClient", tavily_returning(429, {"detail": "rate limited"}))
    monkeypatch.setattr(web_search, "_ddgs_search", lambda *args: [{"href": "https://fallback.example/a", "title": "A", "body": "snippet"}])
    tool = web_search.make_web_search_tool("auto", tavily_api_key="test-key")
    docs = await tool.run({"query": "LED street lights energy"})
    assert [d.url for d in docs] == ["https://fallback.example/a"]


async def test_query_length_is_bounded() -> None:
    tool = web_search.make_web_search_tool("auto")
    with pytest.raises(ValueError, match="2-300 characters"):
        await tool.run({"query": "x" * 301})
