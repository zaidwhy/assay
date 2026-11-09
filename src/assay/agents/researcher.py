"""Researcher: plans sub-queries as tool calls, then gathers sources until its budget ends."""

from __future__ import annotations

import asyncio
import logging
import re

from pydantic import BaseModel, Field

from assay.router import ToolCall, parse_json_object
from assay.run_context import RunContext
from assay.tools import SourceDoc

log = logging.getLogger(__name__)

MAX_TOOL_CALLS = 8
MAX_SUB_QUERIES = 5
QUERY_TOOLS = ("web_search", "wikipedia", "arxiv")

SYSTEM_PROMPT = """ROLE: researcher
You plan research for a question. Break it into 3 to 5 short, distinct search sub-queries and call
the tools to run them: web_search for general facts and news, wikipedia for encyclopedic topics,
arxiv only for scientific research papers. Make every sub-query different. Respond only with tool calls."""


class ResearcherInput(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    prior_findings: list[str] = Field(default_factory=list, max_length=20)


class ResearchPlan(BaseModel):
    sub_queries: list[str] = Field(min_length=1, max_length=MAX_SUB_QUERIES * 2)
    tool_calls: list[ToolCall] = Field(min_length=1, max_length=MAX_TOOL_CALLS)
    planned_by_model: bool


def _normalize(query: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", query.lower()))


def baseline_plan(question: str) -> ResearchPlan:
    """The plan used when the model plans nothing usable or runs out of time."""
    query = question[:300]
    return ResearchPlan(
        sub_queries=[query],
        tool_calls=[ToolCall(name="web_search", arguments={"query": query}),
                    ToolCall(name="wikipedia", arguments={"query": query})],
        planned_by_model=False,
    )


def build_plan(question: str, proposed: list[ToolCall], available: set[str]) -> ResearchPlan:
    """Keep valid, distinct tool calls, and make sure the question itself is always searched."""
    calls: list[ToolCall] = []
    seen: set[tuple[str, str]] = set()
    baseline = baseline_plan(question).tool_calls
    for call in [*proposed, *baseline]:
        query = str(call.arguments.get("query", "")).strip()
        if call.name not in QUERY_TOOLS or call.name not in available or not 2 <= len(query) <= 300:
            continue
        key = (call.name, _normalize(query))
        if key in seen:
            continue
        seen.add(key)
        calls.append(ToolCall(name=call.name, arguments={"query": query}))
        if len(calls) >= MAX_TOOL_CALLS:
            break
    if not calls:
        return baseline_plan(question)
    sub_queries = list(dict.fromkeys(str(c.arguments["query"]) for c in calls))
    return ResearchPlan(sub_queries=sub_queries, tool_calls=calls, planned_by_model=bool(proposed))


async def plan(ctx: RunContext, request: ResearcherInput, *, timeout_s: float) -> ResearchPlan:
    specs = [tool.spec() for name, tool in ctx.tools.items() if name in QUERY_TOOLS]
    user = f"Question: {request.question}"
    if request.prior_findings:
        user += "\n\nFindings from earlier questions in this session:\n" + "\n".join(
            f"- {f}" for f in request.prior_findings
        )
    reply = await ctx.llm_call(
        "researcher.plan", "fast",
        [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
        tools=specs, timeout_s=timeout_s,
    )
    proposed = list(reply.tool_calls)
    if not proposed and reply.text:
        # Some small models answer with a JSON list of queries instead of tool calls.
        try:
            parsed = parse_json_object(reply.text)
        except ValueError:
            parsed = {}
        for query in parsed.get("sub_queries", [])[:MAX_SUB_QUERIES]:
            proposed.append(ToolCall(name="web_search", arguments={"query": str(query)}))
    return build_plan(request.question, proposed, set(ctx.tools))


def _pick_pages_to_fetch(snippets: list[SourceDoc], limit: int) -> list[SourceDoc]:
    """Top search hits, one per domain first, so fetch time buys diversity."""
    chosen: list[SourceDoc] = []
    domains: set[str] = set()
    for doc in snippets:
        domain = doc.url.split("/")[2] if doc.url.count("/") >= 2 else doc.url
        if domain not in domains:
            chosen.append(doc)
            domains.add(domain)
        if len(chosen) >= limit:
            break
    return chosen


async def gather(ctx: RunContext, research_plan: ResearchPlan, *, budget_s: float) -> int:
    """Run the plan's tool calls, then fetch the best pages, stopping when the budget ends.

    Returns the number of sources registered. Work still pending at the budget is cancelled and
    the run continues with what arrived.
    """
    loop = asyncio.get_running_loop()
    stop_at = loop.time() + budget_s
    ctx.scratchpad.sub_queries_tried.extend(research_plan.sub_queries)
    tool_timeout = min(ctx.settings.tool_timeout_s, budget_s)
    searches = {
        asyncio.create_task(ctx.tool_call(call.name, call.arguments, timeout_s=tool_timeout)): call
        for call in research_plan.tool_calls
    }
    snippets: list[SourceDoc] = []
    pending = set(searches)
    while pending and len(ctx.sources) < ctx.settings.max_sources_per_run:
        done, pending = await asyncio.wait(pending, timeout=max(0.0, stop_at - loop.time()),
                                           return_when=asyncio.FIRST_COMPLETED)
        if not done:
            break
        for task in done:
            for doc in task.result():
                if doc.snippet_only:
                    snippets.append(doc)
                else:
                    ctx.add_source(doc)
    for task in pending:
        task.cancel()
    if pending:
        ctx.scratchpad.notes.append(f"research budget ended with {len(pending)} tool calls unfinished")

    room = ctx.settings.max_sources_per_run - len(ctx.sources)
    fetch_targets = _pick_pages_to_fetch(snippets, min(ctx.settings.pages_to_fetch, max(room, 0)))
    remaining = stop_at - loop.time()
    if fetch_targets and remaining > 0.5 and "fetch" in ctx.tools:
        fetch_timeout = min(ctx.settings.fetch_timeout_s, remaining)
        fetches = {
            asyncio.create_task(ctx.tool_call("fetch", {"url": doc.url}, timeout_s=fetch_timeout)): doc
            for doc in fetch_targets
        }
        done, still_pending = await asyncio.wait(fetches, timeout=remaining)
        for task in still_pending:
            task.cancel()
        fetched_urls: set[str] = set()
        for task in done:
            for page in task.result():
                fetched_urls.add(fetches[task].url)
                ctx.add_source(page.model_copy(update={"title": fetches[task].title or page.title}))
        if still_pending:
            ctx.scratchpad.notes.append(f"{len(still_pending)} page fetches cut off by the research budget")
    else:
        fetched_urls = set()
    # A search snippet is still evidence when its page could not be fetched in time.
    for doc in snippets:
        if len(ctx.sources) >= ctx.settings.max_sources_per_run:
            break
        if doc.url not in fetched_urls:
            ctx.add_source(doc)
    return len(ctx.sources)
