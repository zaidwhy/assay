"""The tool set a run is given. Tests swap in offline tools with the same names."""

from __future__ import annotations

from assay.settings import Settings
from assay.tools import Tool
from assay.tools.arxiv import make_arxiv_tool
from assay.tools.fetch import make_fetch_tool
from assay.tools.web_search import make_web_search_tool
from assay.tools.wikipedia import make_wikipedia_tool


def default_tools(settings: Settings) -> dict[str, Tool]:
    tools = [
        make_web_search_tool(settings.search_backend),
        make_wikipedia_tool(timeout_s=settings.tool_timeout_s),
        make_arxiv_tool(timeout_s=settings.tool_timeout_s),
        make_fetch_tool(max_bytes=settings.max_fetch_bytes, timeout_s=settings.fetch_timeout_s),
    ]
    return {tool.name: tool for tool in tools}
