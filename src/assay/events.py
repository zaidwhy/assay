"""Run events: persisted in order, then pushed to any live SSE readers.

Readers subscribe before they read the backlog, so an event published between the read and
the wait is never missed; it only wakes the reader early.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

from assay.store import Store

log = logging.getLogger(__name__)

EVENT_TYPES = frozenset(
    {"run.state", "query.planned", "source.found", "chunk.ranked", "claim.drafted", "claim.verdict",
     "report.ready", "span"}
)


class EventBus:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._waiters: dict[str, set[asyncio.Event]] = defaultdict(set)

    def publish(self, run_id: str, event_type: str, payload: dict[str, Any]) -> int:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"event type {event_type!r} is not one of {sorted(EVENT_TYPES)}")
        seq, _ = self._store.append_event(run_id, event_type, payload)
        for waiter in list(self._waiters.get(run_id, ())):
            waiter.set()
        return seq

    def subscribe(self, run_id: str) -> asyncio.Event:
        waiter = asyncio.Event()
        self._waiters[run_id].add(waiter)
        return waiter

    def unsubscribe(self, run_id: str, waiter: asyncio.Event) -> None:
        waiters = self._waiters.get(run_id)
        if waiters is not None:
            waiters.discard(waiter)
            if not waiters:
                self._waiters.pop(run_id, None)
