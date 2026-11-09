"""Researcher: plans sub-queries as tool calls, then gathers sources until its budget ends."""

from __future__ import annotations

import asyncio
import logging
import re

from pydantic import BaseModel, Field

from assay.router import ToolCall, parse_json_object
from assay.run_context import RunContext
from assay.tools import SourceDoc, domain_of

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


_STOPWORDS = frozenset(
    "a an and are as at be by did do does for from how in is it its of on or the this to was were what when "
    "where which who whom why will with".split()
)


def keyword_query(question: str) -> str:
    """The question without question words; search engines and Wikipedia match on the content words."""
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'.-]*", question)
    kept = [w for w in words if w.lower() not in _STOPWORDS]
    return " ".join(kept or words)[:300]


def baseline_plan(question: str) -> ResearchPlan:
    """The plan used when the model plans nothing usable or runs out of time."""
    query = keyword_query(question)
    return ResearchPlan(
        sub_queries=[query],
        tool_calls=[ToolCall(name="web_search", arguments={"query": query}),
                    ToolCall(name="wikipedia", arguments={"query": query})],
        planned_by_model=False,
    )


_SCHOLARLY = re.compile(
    r"\b(paper|papers|study|studies|research|researchers|arxiv|preprint|peer[- ]reviewed|journal|theorem|"
    r"proof|algorithm|neural|transformer|machine learning|deep learning|dataset|benchmark|quantum|"
    r"physics|cosmolog\w*|genom\w*|protein|molecul\w*|clinical trial|meta-analysis|state of research|"
    r"literature|citation|hypothesis|empirical)\b",
    re.I,
)


def is_scholarly(question: str) -> bool:
    """Whether arXiv is worth asking. For everyday questions it returns keyword-matched papers that crowd out
    relevant sources."""
    return bool(_SCHOLARLY.search(question))


def build_plan(question: str, proposed: list[ToolCall], available: set[str]) -> ResearchPlan:
    """Keep valid, distinct tool calls, and make sure the question itself is always searched."""
    if not is_scholarly(question):
        available = available - {"arxiv"}
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
    offered = set(QUERY_TOOLS) if is_scholarly(request.question) else set(QUERY_TOOLS) - {"arxiv"}
    specs = [tool.spec() for name, tool in ctx.tools.items() if name in offered]
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


async def gather(ctx: RunContext, research_plan: ResearchPlan, *, budget_s: float) -> int:
    """Run the plan's tool calls and fetch promising pages as soon as search hits arrive.

    Stops when the budget ends; work still pending is cancelled and the run continues with what
    arrived. Returns the number of sources registered.
    """
    loop = asyncio.get_running_loop()
    stop_at = loop.time() + budget_s
    settle_at = loop.time() + min(ctx.settings.research_settle_s, budget_s)
    ctx.scratchpad.sub_queries_tried.extend(research_plan.sub_queries)
    tool_timeout = min(ctx.settings.tool_timeout_s, budget_s)
    pending: dict[asyncio.Task[list[SourceDoc]], SourceDoc | None] = {
        asyncio.create_task(ctx.tool_call(call.name, call.arguments, timeout_s=tool_timeout)): None
        for call in research_plan.tool_calls
    }
    snippets: list[SourceDoc] = []
    fetched_from: set[str] = set()
    fetch_domains: set[str] = set()
    fetches_started = 0

    def start_fetches(hits: list[SourceDoc]) -> None:
        # Hits from distinct domains first, so the fetch budget buys diversity; pages already in hand are skipped.
        nonlocal fetches_started
        if "fetch" not in ctx.tools:
            return
        for hit in hits:
            if fetches_started >= ctx.settings.pages_to_fetch:
                return
            domain = domain_of(hit.url)
            if domain in fetch_domains or hit.url in ctx.scratchpad.sources_seen:
                continue
            remaining = stop_at - loop.time()
            if remaining < 0.5:
                return
            fetch_domains.add(domain)
            fetches_started += 1
            task = asyncio.create_task(
                ctx.tool_call("fetch", {"url": hit.url}, timeout_s=min(ctx.settings.fetch_timeout_s, remaining))
            )
            pending[task] = hit

    while pending and len(ctx.sources) < ctx.settings.max_sources_per_run:
        horizon = settle_at if len(ctx.sources) >= ctx.settings.research_enough_sources else stop_at
        done, _ = await asyncio.wait(pending, timeout=max(0.0, horizon - loop.time()),
                                     return_when=asyncio.FIRST_COMPLETED)
        if not done:
            break
        for task in done:
            hit = pending.pop(task)
            docs = task.result()
            if hit is not None:
                for page in docs:
                    fetched_from.add(hit.url)
                    ctx.add_source(page.model_copy(update={"title": hit.title or page.title}))
                continue
            hits = [d for d in docs if d.snippet_only]
            snippets.extend(hits)
            for doc in docs:
                if not doc.snippet_only:
                    ctx.add_source(doc)
            start_fetches(hits)
    for task in pending:
        task.cancel()
    if pending:
        ctx.scratchpad.notes.append(f"research budget ended with {len(pending)} tool calls unfinished")
    # A search snippet is still evidence when its page could not be fetched in time.
    for doc in snippets:
        if len(ctx.sources) >= ctx.settings.max_sources_per_run:
            break
        if doc.url not in fetched_from:
            ctx.add_source(doc)
    return len(ctx.sources)
