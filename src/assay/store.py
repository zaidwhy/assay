"""SQLite persistence. One connection, WAL mode, every statement parameterized.

All access goes through `with self._lock, self._conn:` so writes are serialized and
each call is its own transaction.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER NOT NULL,
        applied_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        session_id TEXT,
        question TEXT NOT NULL,
        impact TEXT NOT NULL,
        state TEXT NOT NULL,
        deadline_s REAL NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        latency_ms REAL,
        draft_json TEXT,
        report_json TEXT,
        evaluation_json TEXT,
        error TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS events (
        run_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (run_id, seq)
    )""",
    """CREATE TABLE IF NOT EXISTS spans (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        parent_id TEXT,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        start_ts TEXT NOT NULL,
        latency_ms REAL NOT NULL,
        tokens_in INTEGER NOT NULL,
        tokens_out INTEGER NOT NULL,
        model TEXT,
        fallback_used INTEGER NOT NULL,
        retries INTEGER NOT NULL,
        status TEXT NOT NULL,
        error TEXT,
        attrs_json TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS spans_by_run ON spans (run_id, start_ts)",
    """CREATE TABLE IF NOT EXISTS verdicts (
        run_id TEXT NOT NULL,
        claim_id TEXT NOT NULL,
        text TEXT NOT NULL,
        chunk_ids_json TEXT NOT NULL,
        verdict TEXT NOT NULL,
        quote TEXT NOT NULL,
        reason TEXT NOT NULL,
        span_found INTEGER NOT NULL,
        struck INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (run_id, claim_id)
    )""",
    """CREATE TABLE IF NOT EXISTS session_findings (
        session_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        question TEXT NOT NULL,
        verdict TEXT NOT NULL,
        claim_text TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS findings_by_session ON session_findings (session_id)",
    """CREATE TABLE IF NOT EXISTS session_documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        name TEXT NOT NULL,
        media_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS documents_by_session ON session_documents (session_id)",
)

# One fixed statement per updatable column, so no SQL is ever assembled at runtime.
_RUN_UPDATES = {
    "state": "UPDATE runs SET state = ?, updated_at = ? WHERE id = ?",
    "latency_ms": "UPDATE runs SET latency_ms = ?, updated_at = ? WHERE id = ?",
    "draft_json": "UPDATE runs SET draft_json = ?, updated_at = ? WHERE id = ?",
    "report_json": "UPDATE runs SET report_json = ?, updated_at = ? WHERE id = ?",
    "evaluation_json": "UPDATE runs SET evaluation_json = ?, updated_at = ? WHERE id = ?",
    "error": "UPDATE runs SET error = ?, updated_at = ? WHERE id = ?",
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path: Path | str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            for statement in _SCHEMA:
                self._conn.execute(statement)
            row = self._conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            if row["v"] is None:
                self._conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (SCHEMA_VERSION, _now())
                )
            elif row["v"] != SCHEMA_VERSION:
                raise RuntimeError(f"database schema_version is {row['v']}, this build expects {SCHEMA_VERSION}")
        log.info("store ready at %s (schema_version %d)", path, SCHEMA_VERSION)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ping(self) -> bool:
        with self._lock, self._conn:
            return self._conn.execute("SELECT 1").fetchone()[0] == 1

    # runs ---------------------------------------------------------------------------------

    def create_run(self, run_id: str, question: str, session_id: str | None, impact: str, deadline_s: float) -> None:
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO runs (id, session_id, question, impact, state, deadline_s, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)",
                (run_id, session_id, question, impact, deadline_s, now, now),
            )

    def update_run(self, run_id: str, **fields: Any) -> None:
        unknown = set(fields) - set(_RUN_UPDATES)
        if unknown:
            raise ValueError(f"update_run got unknown fields {sorted(unknown)}; allowed are {sorted(_RUN_UPDATES)}")
        now = _now()
        with self._lock, self._conn:
            for name, value in fields.items():
                self._conn.execute(_RUN_UPDATES[name], (value, now, run_id))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    # events -------------------------------------------------------------------------------

    def append_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> tuple[int, str]:
        created = _now()
        with self._lock, self._conn:
            seq = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            self._conn.execute(
                "INSERT INTO events (run_id, seq, type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, seq, event_type, json.dumps(payload), created),
            )
        return seq, created

    def events_after(self, run_id: str, after_seq: int) -> list[dict[str, Any]]:
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT seq, type, payload_json, created_at FROM events WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
        return [
            {"seq": r["seq"], "type": r["type"], "data": json.loads(r["payload_json"]), "ts": r["created_at"]}
            for r in rows
        ]

    # spans --------------------------------------------------------------------------------

    def insert_span(self, span: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO spans (id, run_id, parent_id, name, kind, start_ts, latency_ms, tokens_in, tokens_out,"
                " model, fallback_used, retries, status, error, attrs_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    span["span_id"], span["run_id"], span["parent_id"], span["name"], span["kind"], span["start"],
                    span["latency_ms"], span["tokens_in"], span["tokens_out"], span["model"],
                    int(span["fallback_used"]), span["retries"], span["status"], span["error"],
                    json.dumps(span["attrs"]),
                ),
            )

    def spans_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT * FROM spans WHERE run_id = ? ORDER BY start_ts", (run_id,)
            ).fetchall()
        spans = []
        for r in rows:
            span = dict(r)
            span["span_id"] = span.pop("id")
            span["start"] = span.pop("start_ts")
            span["fallback_used"] = bool(span["fallback_used"])
            span["attrs"] = json.loads(span.pop("attrs_json"))
            spans.append(span)
        return spans

    # verdicts and session memory ----------------------------------------------------------

    def insert_verdicts(self, run_id: str, verdicts: list[dict[str, Any]]) -> None:
        now = _now()
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO verdicts (run_id, claim_id, text, chunk_ids_json, verdict, quote, reason,"
                " span_found, struck, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id, v["claim_id"], v["text"], json.dumps(v["chunk_ids"]), v["verdict"], v["quote"],
                        v["reason"], int(v["span_found"]), int(v["struck"]), now,
                    )
                    for v in verdicts
                ],
            )

    def verdicts_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT claim_id, text, verdict, quote, reason, span_found, struck FROM verdicts WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def add_session_findings(self, session_id: str, run_id: str, question: str, verdict: str, claims: list[str]) -> None:
        now = _now()
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO session_findings (session_id, run_id, question, verdict, claim_text, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(session_id, run_id, question, verdict, claim, now) for claim in claims],
            )

    def session_findings(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT run_id, question, verdict, claim_text FROM session_findings WHERE session_id = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def add_document(self, session_id: str, name: str, media_type: str, size_bytes: int, text: str) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO session_documents (session_id, name, media_type, size_bytes, text, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, name, media_type, size_bytes, text, _now()),
            )
        return int(cursor.lastrowid)

    def session_documents(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT id, name, media_type, text FROM session_documents WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]
