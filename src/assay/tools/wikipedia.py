"""Wikipedia through the MediaWiki API: search, then plain-text extracts of the top pages."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from assay.tools import SourceDoc, Tool
from assay.tools.fetch import USER_AGENT

API = "https://en.wikipedia.org/w/api.php"
MAX_PAGES = 3
MAX_EXTRACT_CHARS = 60_000


def make_wikipedia_tool(*, timeout_s: float) -> Tool:
    async def run(args: dict[str, Any]) -> list[SourceDoc]:
        query = str(args.get("query", "")).strip()
        if not 2 <= len(query) <= 300:
            raise ValueError("wikipedia.query must be 2-300 characters")
        async with httpx.AsyncClient(timeout=timeout_s, headers={"User-Agent": USER_AGENT}) as client:
            search = await client.get(
                API,
                params={"action": "query", "list": "search", "srsearch": query, "srlimit": MAX_PAGES,
                        "format": "json"},
            )
            search.raise_for_status()
            titles = [hit["title"] for hit in search.json().get("query", {}).get("search", [])]
            if not titles:
                return []
            pages = await client.get(
                API,
                params={"action": "query", "prop": "extracts", "explaintext": 1, "exsectionformat": "wiki",
                        "titles": "|".join(titles), "format": "json", "redirects": 1},
            )
            pages.raise_for_status()
        docs = []
        for page in pages.json().get("query", {}).get("pages", {}).values():
            text = (page.get("extract") or "")[:MAX_EXTRACT_CHARS]
            if text.strip():
                title = page.get("title", "")
                docs.append(
                    SourceDoc(
                        url=f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
                        title=title,
                        text=text,
                        origin="wikipedia",
                    )
                )
        return docs

    return Tool(
        name="wikipedia",
        description="Search English Wikipedia and return the text of the best matching articles.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "article topic or search terms"}},
            "required": ["query"],
        },
        run=run,
    )
