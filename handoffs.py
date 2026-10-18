"""Handoff capsules: short-lived working context so a new window can continue where another stopped.

A capsule is not a memory. It lives in ``state/handoffs.sqlite3`` (never under ``memory/``), so it is
never part of recall, the search or vector indexes, or Dream material. Capsules expire; several can be
active per owner at once (one per window or project), and nothing here calls a model.
"""
from __future__ import annotations

import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HANDOFF_DB = Path("state") / "handoffs.sqlite3"
FIELD_LIMITS = {"topic": 120, "summary": 600, "next_steps": 400, "temporary_context": 600}
NEXT_HINT_CHARS = 120
CLOSED_RETENTION_DAYS = 30


@dataclass(frozen=True)
class HandoffConfig:
    max_active: int = 5
    default_ttl_hours: int = 48
    max_ttl_hours: int = 168
    wake_limit: int = 3


DEFAULT_HANDOFF_CONFIG = HandoffConfig()


class HandoffError(ValueError):
    """A rejected handoff request; the message is safe to show to the caller."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean(name: str, value: Any, *, required: bool = False) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise HandoffError(f"{name} is required")
    limit = FIELD_LIMITS[name]
    if len(text) > limit:
        raise HandoffError(f"{name} is {len(text)} characters; the limit is {limit}. Shorten it.")
    return text


def next_hint(row: dict[str, Any]) -> str:
    text = (row.get("next_steps") or row.get("summary") or "").replace("\n", " ").strip()
    return text if len(text) <= NEXT_HINT_CHARS else text[: NEXT_HINT_CHARS - 1] + "…"


class HandoffStore:
    def __init__(self, project_root: str | Path, owner: str, *, config: HandoffConfig = DEFAULT_HANDOFF_CONFIG,
                 clock=_now) -> None:
        self.project_root = Path(project_root).resolve()
        self.owner = str(owner or "").strip().lower()
        if not self.owner:
            raise HandoffError("an owner identity is required")
        self.config = config
        self.clock = clock
        self.db_path = self.project_root / HANDOFF_DB

    # --- storage -----------------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS handoffs (
                handoff_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                topic TEXT NOT NULL,
                summary TEXT NOT NULL,
                next_steps TEXT NOT NULL,
                temporary_context TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','closed')),
                closed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_handoffs_owner ON handoffs(owner, status, updated_at);
            CREATE TABLE IF NOT EXISTS handoff_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                handoff_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('created','updated','closed')),
                topic TEXT NOT NULL,
                at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_handoff_events_at ON handoff_events(at);
            """
        )
        return db

    def _is_active(self, row: sqlite3.Row | dict[str, Any], now: datetime) -> bool:
        return row["status"] == "active" and str(row["expires_at"]) > _iso(now)

    def _public(self, row: sqlite3.Row, now: datetime, *, full: bool) -> dict[str, Any]:
        item = {"handoff_id": row["handoff_id"], "owner": row["owner"], "topic": row["topic"],
                "updated_at": row["updated_at"], "expires_at": row["expires_at"],
                "status": "active" if self._is_active(row, now) else ("closed" if row["status"] == "closed" else "expired")}
        if full:
            item.update(summary=row["summary"], next_steps=row["next_steps"],
                        temporary_context=row["temporary_context"], created_at=row["created_at"],
                        closed_at=row["closed_at"])
        else:
            item["next_hint"] = next_hint(dict(row))
        return item

    def _purge(self, db: sqlite3.Connection, now: datetime) -> None:
        """Lazy cleanup: closed or expired capsules older than the retention window disappear."""
        cutoff = _iso(now - timedelta(days=CLOSED_RETENTION_DAYS))
        db.execute("DELETE FROM handoffs WHERE (status='closed' AND closed_at<?) OR expires_at<?", (cutoff, cutoff))

    # --- operations ------------------------------------------------------------------------------
    def set(self, *, topic: str, summary: str = "", next_steps: str = "", temporary_context: str = "",
            handoff_id: str | None = None, ttl_hours: int | None = None) -> dict[str, Any]:
        fields = {"topic": _clean("topic", topic, required=True), "summary": _clean("summary", summary),
                  "next_steps": _clean("next_steps", next_steps),
                  "temporary_context": _clean("temporary_context", temporary_context)}
        if not (fields["summary"] or fields["next_steps"]):
            raise HandoffError("give at least a summary or next_steps")
        ttl = self.config.default_ttl_hours if ttl_hours is None else int(ttl_hours)
        if not 1 <= ttl <= self.config.max_ttl_hours:
            raise HandoffError(f"ttl_hours must be between 1 and {self.config.max_ttl_hours}")
        now = self.clock()
        expires = _iso(now + timedelta(hours=ttl))
        with closing(self._connect()) as db, db:
            self._purge(db, now)
            if handoff_id:
                row = db.execute("SELECT * FROM handoffs WHERE handoff_id=?", (handoff_id,)).fetchone()
                if row is None:
                    raise HandoffError(f"no handoff {handoff_id}")
                if row["owner"] != self.owner:
                    raise HandoffError("only the owner of a handoff can update it")
                if row["status"] == "closed":
                    raise HandoffError("this handoff is closed; create a new one")
                db.execute(
                    "UPDATE handoffs SET topic=?, summary=?, next_steps=?, temporary_context=?, updated_at=?, "
                    "expires_at=? WHERE handoff_id=?",
                    (*fields.values(), _iso(now), expires, handoff_id))
                kind = "updated"
            else:
                active = [r for r in db.execute(
                    "SELECT * FROM handoffs WHERE owner=? AND status='active' ORDER BY updated_at DESC", (self.owner,))
                    if self._is_active(r, now)]
                if len(active) >= self.config.max_active:
                    listing = "; ".join(f"{r['handoff_id']} ({r['topic']})" for r in active)
                    raise HandoffError(f"{len(active)} handoffs are already active. Close one first: {listing}")
                handoff_id = "h-" + secrets.token_hex(6)
                db.execute(
                    "INSERT INTO handoffs VALUES (?,?,?,?,?,?,?,?,?, 'active', NULL)",
                    (handoff_id, self.owner, *fields.values(), _iso(now), _iso(now), expires))
                kind = "created"
            db.execute("INSERT INTO handoff_events(handoff_id, owner, kind, topic, at) VALUES (?,?,?,?,?)",
                       (handoff_id, self.owner, kind, fields["topic"], _iso(now)))
            row = db.execute("SELECT * FROM handoffs WHERE handoff_id=?", (handoff_id,)).fetchone()
            return {"ok": True, "action": kind, "handoff": self._public(row, now, full=False)}

    def list(self, *, owner: str | None = None, include_inactive: bool = False, limit: int = 20) -> dict[str, Any]:
        """Compact index. ``owner`` defaults to the caller; ``"all"`` lists every owner."""
        now = self.clock()
        who = (owner or self.owner).strip().lower()
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM handoffs" + ("" if who == "all" else " WHERE owner=?") + " ORDER BY updated_at DESC",
                () if who == "all" else (who,)).fetchall()
        items = [self._public(r, now, full=False) for r in rows if include_inactive or self._is_active(r, now)]
        return {"ok": True, "owner": who, "handoffs": items[: max(1, min(int(limit), 50))]}

    def get(self, handoff_id: str) -> dict[str, Any]:
        now = self.clock()
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM handoffs WHERE handoff_id=?", (str(handoff_id),)).fetchone()
        if row is None:
            return {"ok": False, "error": f"no handoff {handoff_id}"}
        return {"ok": True, "handoff": self._public(row, now, full=True)}

    def close(self, handoff_id: str) -> dict[str, Any]:
        now = self.clock()
        with closing(self._connect()) as db, db:
            row = db.execute("SELECT * FROM handoffs WHERE handoff_id=?", (str(handoff_id),)).fetchone()
            if row is None:
                return {"ok": False, "error": f"no handoff {handoff_id}"}
            if row["owner"] != self.owner:
                return {"ok": False, "error": "only the owner of a handoff can close it"}
            if row["status"] == "closed":
                return {"ok": True, "action": "already_closed", "handoff_id": handoff_id}
            db.execute("UPDATE handoffs SET status='closed', closed_at=? WHERE handoff_id=?", (_iso(now), handoff_id))
            db.execute("INSERT INTO handoff_events(handoff_id, owner, kind, topic, at) VALUES (?,?,?,?,?)",
                       (handoff_id, self.owner, "closed", row["topic"], _iso(now)))
        return {"ok": True, "action": "closed", "handoff_id": handoff_id}

    def wake_index(self) -> list[dict[str, Any]]:
        """At most ``wake_limit`` of the owner's active capsules, compact (no bodies). Empty when none."""
        if not self.db_path.is_file():
            return []
        now = self.clock()
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM handoffs WHERE owner=? AND status='active' ORDER BY updated_at DESC",
                              (self.owner,)).fetchall()
        active = [r for r in rows if self._is_active(r, now)][: self.config.wake_limit]
        return [{"handoff_id": r["handoff_id"], "topic": r["topic"], "updated_at": r["updated_at"],
                 "next_hint": next_hint(dict(r))} for r in active]


def handoff_events(project_root: str | Path, *, since: str | None = None, until: str | None = None) -> list[dict[str, Any]]:
    """Read-only event rows for the timeline (empty when the store does not exist)."""
    path = Path(project_root).resolve() / HANDOFF_DB
    if not path.is_file():
        return []
    query, params = "SELECT * FROM handoff_events WHERE 1=1", []
    if since:
        query += " AND at>=?"
        params.append(since)
    if until:
        query += " AND at<?"
        params.append(until)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in db.execute(query + " ORDER BY at, event_id", params)]
        except sqlite3.OperationalError:
            return []
