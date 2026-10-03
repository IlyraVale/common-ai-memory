from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


log = logging.getLogger("memory_feedback")

VALID_VERDICTS = frozenset({"used", "ignored", "stale", "conflict", "helpful", "corrected"})
VALID_SOURCES = frozenset({"agent_report", "user_explicit", "system_test"})
MEMORY_VERDICTS = frozenset({"used", "ignored", "stale", "conflict"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class MemoryFeedbackStore:
    """Append-only shadow telemetry for memory retrieval and feedback."""

    def __init__(
        self,
        project_root: str | Path,
        agent_id: str,
        db_path: str | Path | None = None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.agent_id = str(agent_id).strip()
        if not self.agent_id:
            raise ValueError("agent_id is required")
        self.db_path = Path(db_path) if db_path is not None else self.project_root / "state" / "memory-feedback.sqlite3"
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.db_path), timeout=self.busy_timeout_ms / 1000)
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS retrieval_events (
                retrieval_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                query TEXT NOT NULL,
                owner_filter TEXT,
                result_ids_json TEXT NOT NULL,
                result_scores_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS retrieval_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                retrieval_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                memory_id TEXT,
                verdict TEXT NOT NULL,
                note TEXT,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (retrieval_id) REFERENCES retrieval_events(retrieval_id)
            );
            CREATE INDEX IF NOT EXISTS idx_feedback_retrieval_id ON retrieval_feedback(retrieval_id);
            CREATE INDEX IF NOT EXISTS idx_events_created_at ON retrieval_events(created_at);
            CREATE INDEX IF NOT EXISTS idx_feedback_created_at ON retrieval_feedback(created_at);
            """
        )
        return db

    def record_retrieval(
        self,
        query: str,
        owner_filter: str | None,
        result_ids: Iterable[str],
        result_scores: Iterable[float] | None = None,
    ) -> str:
        retrieval_id = str(uuid.uuid4())
        ids = [str(value) for value in result_ids]
        scores = None if result_scores is None else [float(value) for value in result_scores]
        with self._connect() as db:
            db.execute(
                """INSERT INTO retrieval_events
                   (retrieval_id, agent_id, query, owner_filter, result_ids_json, result_scores_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    retrieval_id,
                    self.agent_id,
                    str(query or ""),
                    owner_filter,
                    json.dumps(ids, ensure_ascii=False),
                    None if scores is None else json.dumps(scores),
                    _now_iso(),
                ),
            )
        return retrieval_id

    def add_feedback(
        self,
        retrieval_id: str,
        verdict: str,
        memory_ids: list[str] | None = None,
        note: str | None = None,
        source: str = "agent_report",
    ) -> dict[str, Any]:
        verdict = str(verdict).strip().lower()
        source = str(source).strip().lower()
        if verdict not in VALID_VERDICTS:
            raise ValueError(f"invalid verdict: {verdict!r}")
        if source not in VALID_SOURCES:
            raise ValueError(f"invalid source: {source!r}")
        if note is not None and len(note) > 500:
            raise ValueError("note must be at most 500 characters")
        ids = None if memory_ids is None else [str(value) for value in memory_ids]
        if verdict in MEMORY_VERDICTS and not ids:
            raise ValueError(f"{verdict} feedback requires at least one memory_id")

        with self._connect() as db:
            row = db.execute(
                "SELECT agent_id, result_ids_json FROM retrieval_events WHERE retrieval_id = ?",
                (retrieval_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown retrieval_id: {retrieval_id}")
            if row["agent_id"] != self.agent_id:
                raise ValueError("retrieval_id belongs to a different agent")
            result_ids = set(json.loads(row["result_ids_json"]))
            unknown = [memory_id for memory_id in ids or [] if memory_id not in result_ids]
            if unknown:
                raise ValueError(f"memory_ids not returned by this retrieval: {unknown}")

            targets: list[str | None] = ids or [None]
            created_at = _now_iso()
            db.executemany(
                """INSERT INTO retrieval_feedback
                   (retrieval_id, agent_id, memory_id, verdict, note, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (retrieval_id, self.agent_id, memory_id, verdict, note, source, created_at)
                    for memory_id in targets
                ],
            )
        return {"ok": True, "retrieval_id": retrieval_id, "added": len(targets)}

    def feedback_report(self, days: int = 30) -> dict[str, Any]:
        days = int(days)
        if days < 1:
            raise ValueError("days must be at least 1")
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
        with self._connect() as db:
            events = db.execute(
                "SELECT retrieval_id FROM retrieval_events WHERE created_at >= ?",
                (since,),
            ).fetchall()
            event_ids = {row["retrieval_id"] for row in events}
            if event_ids:
                placeholders = ",".join("?" for _ in event_ids)
                rows = db.execute(
                    f"SELECT * FROM retrieval_feedback WHERE retrieval_id IN ({placeholders})",
                    tuple(event_ids),
                ).fetchall()
            else:
                rows = []

        valid_rows = []
        for row in rows:
            if row["verdict"] not in VALID_VERDICTS or row["source"] not in VALID_SOURCES:
                log.warning(
                    "ignoring invalid feedback row id=%s verdict=%r source=%r",
                    row["id"], row["verdict"], row["source"],
                )
                continue
            valid_rows.append(row)

        verdict_counts = Counter(row["verdict"] for row in valid_rows)
        feedback_events = {row["retrieval_id"] for row in valid_rows}
        agent_events = {row["retrieval_id"] for row in valid_rows if row["source"] == "agent_report"}
        used = Counter(row["memory_id"] for row in valid_rows if row["verdict"] == "used" and row["memory_id"])
        corrected_or_stale = Counter(
            row["memory_id"] for row in valid_rows
            if row["verdict"] in {"corrected", "stale"} and row["memory_id"]
        )
        recall_count = len(event_ids)
        return {
            "days": days,
            "recall_count": recall_count,
            "feedback_recall_ratio": len(feedback_events) / recall_count if recall_count else 0.0,
            "verdict_counts": {name: verdict_counts[name] for name in sorted(VALID_VERDICTS)},
            "most_used_memories": [{"memory_id": key, "count": value} for key, value in used.most_common()],
            "most_corrected_or_stale_memories": [
                {"memory_id": key, "count": value} for key, value in corrected_or_stale.most_common()
            ],
            "agent_report_coverage": len(agent_events) / recall_count if recall_count else 0.0,
        }


def recall_result(
    feedback_store: MemoryFeedbackStore,
    query: str,
    owner_filter: str | None,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Attach shadow metadata without allowing telemetry failure to break recall."""
    retrieval_id = None
    try:
        retrieval_id = feedback_store.record_retrieval(
            query=query,
            owner_filter=owner_filter,
            result_ids=[str(item.get("id") or "") for item in items],
        )
    except Exception:
        log.warning("failed to record recall feedback event", exc_info=True)
    return {"result": items, "retrieval_id": retrieval_id}


def feedback_report(days: int = 30) -> dict[str, Any]:
    """Read feedback statistics for the configured project; not an MCP tool."""
    project_root = Path(os.getenv("AI_MEMORY_ROOT", Path(__file__).resolve().parent)).resolve()
    return MemoryFeedbackStore(project_root, "report").feedback_report(days=days)
