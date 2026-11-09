"""arXiv search through the public export API; returns titles and abstracts."""

from __future__ import annotations

from typing import Any

import httpx
from defusedxml import ElementTree

from assay.tools import SourceDoc, Tool
from assay.tools.fetch import USER_AGENT

API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
MAX_RESULTS = 5


def parse_feed(xml_text: str) -> list[SourceDoc]:
    root = ElementTree.fromstring(xml_text)
    docs = []
    for entry in root.findall(f"{ATOM}entry"):
        title = " ".join((entry.findtext(f"{ATOM}title") or "").split())
        summary = " ".join((entry.findtext(f"{ATOM}summary") or "").split())
        url = (entry.findtext(f"{ATOM}id") or "").strip()
        if title and summary and url:
            docs.append(SourceDoc(url=url, title=title, text=f"{title}\n\n{summary}", origin="arxiv"))
    return docs


def make_arxiv_tool(*, timeout_s: float) -> Tool:
    async def run(args: dict[str, Any]) -> list[SourceDoc]:
        query = str(args.get("query", "")).strip()
        if not 2 <= len(query) <= 300:
            raise ValueError("arxiv.query must be 2-300 characters")
        async with httpx.AsyncClient(timeout=timeout_s, headers={"User-Agent": USER_AGENT}) as client:
            response = await client.get(
                API, params={"search_query": f"all:{query}", "start": 0, "max_results": MAX_RESULTS}
            )
            response.raise_for_status()
        return parse_feed(response.text)

    return Tool(
        name="arxiv",
        description="Search arXiv for research papers; returns titles and abstracts. Use for scientific topics.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "paper topic or keywords"}},
            "required": ["query"],
        },
        run=run,
    )
