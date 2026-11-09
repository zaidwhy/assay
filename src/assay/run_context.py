"""What one run carries between agents: budget, tracer, model access, tools, sources and scratchpad."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from assay.deadline import Deadline
from assay.domain import Chunk, Source
from assay.events import EventBus
from assay.router import LLMBackend, LLMReply, Tier
from assay.settings import Settings
from assay.telemetry import Tracer
from assay.tools import SourceDoc, Tool, domain_of, prior_reliability

log = logging.getLogger(__name__)

TOOL_ATTEMPTS = 2  # one try plus one retry


class Scratchpad(BaseModel):
    """Working memory for one run, passed from agent to agent."""

    sub_queries_tried: list[str] = Field(default_factory=list)
    sources_seen: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


@dataclass
class RunContext:
    run_id: str
    question: str
    session_id: str | None
    settings: Settings
    deadline: Deadline
    tracer: Tracer
    llm: LLMBackend
    tools: dict[str, Tool]
    bus: EventBus
    scratchpad: Scratchpad = field(default_factory=Scratchpad)
    sources: dict[int, Source] = field(default_factory=dict)
    chunks: dict[str, Chunk] = field(default_factory=dict)

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.bus.publish(self.run_id, event_type, payload)

    async def llm_call(
        self,
        name: str,
        tier: Tier,
        messages: list[dict[str, Any]],
        *,
        timeout_s: float,
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
    ) -> LLMReply:
        if timeout_s <= 0:
            raise TimeoutError(f"{name}: no time left in the run budget")
        async with self.tracer.span(name, "llm", tier=tier) as span:
            reply = await asyncio.wait_for(
                self.llm.complete(tier, messages, tools=tools, json_mode=json_mode, timeout_s=timeout_s),
                timeout=timeout_s,
            )
            span.model = reply.model
            span.fallback_used = reply.fallback_used
            span.retries = reply.retries
            span.tokens_in = reply.tokens_in
            span.tokens_out = reply.tokens_out
            span.attrs["deployment"] = reply.deployment
            return reply

    async def tool_call(self, name: str, arguments: dict[str, Any], *, timeout_s: float) -> list[SourceDoc]:
        """Run a tool with a timeout and one retry. A failure is recorded on the span, never raised."""
        tool = self.tools.get(name)
        async with self.tracer.span(f"tool.{name}", "tool", arguments=arguments) as span:
            if tool is None:
                span.status = "error"
                span.error = f"unknown tool {name!r}; available: {sorted(self.tools)}"
                return []
            last_error = ""
            for attempt in range(TOOL_ATTEMPTS):
                try:
                    docs = await asyncio.wait_for(tool.run(arguments), timeout=timeout_s)
                # Any tool fault (network, parser, third-party library) costs this source, never the run.
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"[:300]
                    log.info("tool %s attempt %d failed in run %s: %s", name, attempt + 1, self.run_id, last_error)
                    continue
                span.retries = attempt
                span.attrs["documents"] = len(docs)
                return docs
            span.retries = TOOL_ATTEMPTS - 1
            span.status = "error"
            span.error = last_error
            return []

    def add_source(self, doc: SourceDoc) -> Source | None:
        """Register a document as a source, once per URL; emits `source.found`."""
        if doc.url in self.scratchpad.sources_seen or not doc.text.strip():
            return None
        source = Source(
            id=len(self.sources) + 1,
            url=doc.url,
            title=doc.title[:300] or doc.url,
            domain=domain_of(doc.url) if doc.origin != "upload" else "upload",
            origin=doc.origin,
            reliability=prior_reliability(doc.url, doc.origin),
            text=doc.text,
        )
        self.sources[source.id] = source
        self.scratchpad.sources_seen.append(doc.url)
        self.emit(
            "source.found",
            {"source_id": source.id, "url": source.url, "title": source.title, "domain": source.domain,
             "reliability": source.reliability, "origin": source.origin},
        )
        return source
