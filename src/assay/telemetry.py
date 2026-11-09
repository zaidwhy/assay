"""Spans for every agent step, LLM call and tool call, plus Prometheus metrics.

A span is persisted and streamed as a `span` event when it closes. The parent link comes
from a context variable so nested `async with tracer.span(...)` blocks form a tree without
threading ids through every call.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

log = logging.getLogger(__name__)

_current_span: contextvars.ContextVar[str | None] = contextvars.ContextVar("assay_current_span", default=None)

SPAN_KINDS = frozenset({"run", "stage", "agent", "llm", "tool"})


@dataclass
class Span:
    run_id: str
    name: str
    kind: str
    parent_id: str | None
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    start: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="milliseconds"))
    latency_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    model: str | None = None
    fallback_used: bool = False
    retries: int = 0
    status: str = "ok"
    error: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)

    def as_record(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "run_id": self.run_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "kind": self.kind,
            "start": self.start,
            "latency_ms": round(self.latency_ms, 1),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "model": self.model,
            "fallback_used": self.fallback_used,
            "retries": self.retries,
            "status": self.status,
            "error": self.error,
            "attrs": self.attrs,
        }


class Metrics:
    """Process-wide counters. A private registry keeps tests from colliding on global state."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "assay_requests_total", "HTTP requests by route and status", ["route", "status"], registry=self.registry
        )
        self.runs = Counter("assay_runs_total", "Runs by terminal state", ["state"], registry=self.registry)
        self.run_latency = Histogram(
            "assay_run_latency_seconds", "Run latency up to the report or approval gate",
            buckets=(1, 2, 5, 10, 15, 20, 30, 45, 60, 120), registry=self.registry,
        )
        self.span_latency = Histogram(
            "assay_span_latency_seconds", "Span latency by kind", ["kind"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20), registry=self.registry,
        )
        self.tokens = Counter("assay_llm_tokens_total", "LLM tokens", ["direction", "model"], registry=self.registry)
        self.fallbacks = Counter("assay_llm_fallbacks_total", "LLM calls answered by a fallback deployment",
                                 registry=self.registry)
        self.failures = Counter("assay_span_failures_total", "Spans that ended in error", ["kind"],
                                registry=self.registry)
        self.rejected = Counter("assay_admission_rejected_total", "Runs refused because the queue was full",
                                registry=self.registry)

    def observe_span(self, span: Span) -> None:
        self.span_latency.labels(kind=span.kind).observe(span.latency_ms / 1000)
        if span.kind == "llm":
            model = span.model or "unknown"
            self.tokens.labels(direction="in", model=model).inc(span.tokens_in)
            self.tokens.labels(direction="out", model=model).inc(span.tokens_out)
            if span.fallback_used:
                self.fallbacks.inc()
        if span.status != "ok":
            self.failures.labels(kind=span.kind).inc()

    def render(self) -> bytes:
        return generate_latest(self.registry)


SpanSink = Callable[[Span], None]


class Tracer:
    """Per-run span factory. `sink` persists and streams each closed span."""

    def __init__(self, run_id: str, sink: SpanSink, metrics: Metrics | None = None) -> None:
        self.run_id = run_id
        self._sink = sink
        self._metrics = metrics
        self.closed: list[Span] = []

    @asynccontextmanager
    async def span(self, name: str, kind: str, **attrs: Any) -> AsyncIterator[Span]:
        if kind not in SPAN_KINDS:
            raise ValueError(f"span kind {kind!r} is not one of {sorted(SPAN_KINDS)}")
        span = Span(run_id=self.run_id, name=name, kind=kind, parent_id=_current_span.get(), attrs=dict(attrs))
        token = _current_span.set(span.span_id)
        began = time.perf_counter()
        try:
            yield span
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                span.status = "cancelled"
            else:
                span.status = "timeout" if isinstance(exc, TimeoutError) else "error"
            span.error = span.error or f"{type(exc).__name__}: {exc}"[:500]
            raise
        finally:
            _current_span.reset(token)
            span.latency_ms = (time.perf_counter() - began) * 1000
            self.closed.append(span)
            if self._metrics:
                self._metrics.observe_span(span)
            try:
                self._sink(span)
            except Exception:
                log.exception("span sink failed for %s in run %s", span.name, self.run_id)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # litellm and httpx log every request at INFO; spans already record those calls.
    for noisy in ("LiteLLM", "litellm", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
