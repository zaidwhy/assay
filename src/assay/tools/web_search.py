"""Keyless web search through DuckDuckGo (ddgs)."""

from __future__ import annotations

import asyncio
from typing import Any

from ddgs import DDGS

from assay.tools import SourceDoc, Tool

MAX_RESULTS = 8


def _search(query: str, max_results: int) -> list[dict[str, Any]]:
    return DDGS().text(query, max_results=max_results) or []


def make_web_search_tool() -> Tool:
    async def run(args: dict[str, Any]) -> list[SourceDoc]:
        query = str(args.get("query", "")).strip()
        if not 2 <= len(query) <= 300:
            raise ValueError("web_search.query must be 2-300 characters")
        max_results = min(int(args.get("max_results", 5) or 5), MAX_RESULTS)
        # ddgs is synchronous; keep it off the event loop.
        hits = await asyncio.to_thread(_search, query, max_results)
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
        description="Search the web. Returns result titles, URLs and snippets.",
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
