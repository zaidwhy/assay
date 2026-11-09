"""Web search: Tavily when TAVILY_API_KEY is set, keyless DuckDuckGo (ddgs) otherwise or when Tavily fails.

Keyless search measured about 10 s per call on this project's machine, with 3 of 5 calls failing;
a 30 s run cannot carry that, so a keyed search API leads and ddgs is the floor that needs no key.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from ddgs import DDGS

from assay.tools import SourceDoc, Tool

log = logging.getLogger(__name__)

MAX_RESULTS = 8
TAVILY_URL = "https://api.tavily.com/search"
# A page body shorter than this is treated as a snippet, so the fetch tool may still read the page.
MIN_PAGE_CHARS = 600


def _ddgs_search(query: str, max_results: int, backend: str, timeout_s: float) -> list[dict[str, Any]]:
    return DDGS(timeout=max(1, int(timeout_s))).text(query, max_results=max_results, backend=backend) or []


async def _tavily_search(query: str, max_results: int, api_key: str, timeout_s: float,
                         max_page_chars: int) -> list[SourceDoc]:
    payload = {"query": query, "max_results": max_results, "search_depth": "basic", "include_raw_content": True}
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.post(TAVILY_URL, json=payload, headers={"Authorization": f"Bearer {api_key}"})
        response.raise_for_status()
    docs = []
    for hit in response.json().get("results", []):
        url = str(hit.get("url", ""))
        if not url.startswith(("http://", "https://")):
            continue
        # Tavily returns the page body with the hit, which saves a fetch that often 403s or times out.
        page = (hit.get("raw_content") or "")[:max_page_chars]
        full = len(page) >= MIN_PAGE_CHARS
        docs.append(SourceDoc(url=url, title=str(hit.get("title") or url), text=page if full else str(hit.get("content", "")),
                              origin="web", snippet_only=not full))
    return docs


def make_web_search_tool(backend: str, *, tavily_api_key: str | None = None, timeout_s: float = 8.0,
                         max_page_chars: int = 200_000) -> Tool:
    async def run(args: dict[str, Any]) -> list[SourceDoc]:
        query = str(args.get("query", "")).strip()
        if not 2 <= len(query) <= 300:
            raise ValueError("web_search.query must be 2-300 characters")
        max_results = min(int(args.get("max_results", 5) or 5), MAX_RESULTS)
        if tavily_api_key:
            try:
                return await _tavily_search(query, max_results, tavily_api_key, timeout_s, max_page_chars)
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("tavily search failed, falling back to ddgs: %s", type(exc).__name__)
        # ddgs is synchronous; keep it off the event loop.
        hits = await asyncio.to_thread(_ddgs_search, query, max_results, backend, timeout_s)
        return [
            SourceDoc(
                url=hit.get("href", ""),
                title=hit.get("title", "") or hit.get("href", ""),
                text=hit.get("body", ""),
                origin="web",
                snippet_only=True,
            )
            for hit in hits
            if hit.get("href", "").startswith(("http://", "https://"))
        ]

    return Tool(
        name="web_search",
        description="Search the web. Returns result titles, URLs and page text or snippets.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "a focused search query"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS},
            },
            "required": ["query"],
        },
        run=run,
    )
