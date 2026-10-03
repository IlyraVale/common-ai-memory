from __future__ import annotations

import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_TTL_HOURS = 72
MIN_TTL_HOURS = 24
MAX_TTL_HOURS = 168
_SECRET = re.compile(
    r"(?i)(password|passwd|api[_-]?key|oauth|access[_-]?token|refresh[_-]?token|cookie|secret)\s*[:=]|"
    r"\bsk-[A-Za-z0-9_-]{12,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scrap time must be timezone-aware")
    return value.astimezone(timezone.utc)


class DreamScrapStore:
    _INIT_LOCK_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16)

    def __init__(self, project_root: str | Path, db_path: str | Path | None = None, busy_timeout_ms: int = 5000) -> None:
        self.root = Path(project_root).resolve()
        self.db_path = Path(db_path) if db_path else self.root / "state" / "dream-scraps.sqlite3"
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))

    @staticmethod
    def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
        text = str(exc).lower()
        return "database is locked" in text or "database is busy" in text

    def _connect_once(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000)
        try:
            db.row_factory = sqlite3.Row
            db.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS dream_scraps (
            scrap_id TEXT PRIMARY KEY, owner TEXT NOT NULL, created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL, content TEXT NOT NULL, source_type TEXT NOT NULL,
            source_ref TEXT, eligible_for_dream INTEGER NOT NULL,
            category TEXT, tags TEXT, origin_reason TEXT,
            schema_version INTEGER NOT NULL, derived INTEGER NOT NULL,
            ephemeral INTEGER NOT NULL, factual_authority INTEGER NOT NULL
        )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_dream_scraps_owner_expiry ON dream_scraps(owner, expires_at)")
            db.commit()
            return db
        except Exception:
            db.close()
            raise

    def _connect(self) -> sqlite3.Connection:
        for delay in (*self._INIT_LOCK_RETRY_DELAYS, None):
            try:
                return self._connect_once()
            except sqlite3.OperationalError as exc:
                if delay is None or not self._is_locked_error(exc):
                    raise
                time.sleep(delay)
        raise AssertionError("unreachable")

    def add(self, owner: str, content: str, *, source_type: str, source_ref: str | None = None,
            ttl_hours: int = DEFAULT_TTL_HOURS, now: datetime | None = None,
            eligible_for_dream: bool = True, invalidated: bool = False,
            category: str | None = None, tags: str | None = None, origin_reason: str | None = None) -> dict[str, Any]:
        owner = str(owner).strip().lower()
        content = str(content).strip()
        if not owner or not content or len(content) > 800:
            raise ValueError("scrap owner/content is invalid")
        if source_type in {"delete", "forget", "remove", "corrected", "stale", "superseded"} or invalidated:
            raise ValueError("deleted or invalidated content cannot become a scrap")
        if _SECRET.search(content):
            raise ValueError("secret-like content is not allowed in scraps")
        if isinstance(ttl_hours, bool) or not MIN_TTL_HOURS <= int(ttl_hours) <= MAX_TTL_HOURS:
            raise ValueError("scrap TTL must be between 24 and 168 hours")
        created = _utc(now); expires = created + timedelta(hours=int(ttl_hours))
        row = {"schema_version": SCHEMA_VERSION, "scrap_id": secrets.token_urlsafe(18), "owner": owner,
               "created_at": created.isoformat().replace("+00:00", "Z"),
               "expires_at": expires.isoformat().replace("+00:00", "Z"), "content": content,
               "source_type": source_type, "source_ref": source_ref,
               "eligible_for_dream": bool(eligible_for_dream), "category": category, "tags": tags,
               "origin_reason": origin_reason, "derived": True, "ephemeral": True,
               "factual_authority": False}
        db = self._connect()
        try:
            db.execute("INSERT INTO dream_scraps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                row["scrap_id"], owner, row["created_at"], row["expires_at"], content, source_type,
                source_ref, int(row["eligible_for_dream"]), category, tags, origin_reason,
                SCHEMA_VERSION, 1, 1, 0)); db.commit()
        finally: db.close()
        return row

    def cleanup(self, now: datetime | None = None) -> int:
        stamp = _utc(now).isoformat().replace("+00:00", "Z")
        db = self._connect()
        try:
            cur = db.execute("DELETE FROM dream_scraps WHERE expires_at<=?", (stamp,)); db.commit(); return cur.rowcount
        finally: db.close()

    def eligible(self, owner: str, *, cutoff: datetime, now: datetime | None = None) -> list[dict[str, Any]]:
        self.cleanup(now)
        stamp = _utc(now).isoformat().replace("+00:00", "Z")
        cutoff_iso = _utc(cutoff).isoformat().replace("+00:00", "Z")
        db = self._connect()
        try:
            rows = db.execute("SELECT * FROM dream_scraps WHERE owner=? AND eligible_for_dream=1 AND expires_at>? AND created_at<=? ORDER BY scrap_id", (owner, stamp, cutoff_iso)).fetchall()
            return [dict(row) for row in rows]
        finally: db.close()

    def delete_by_source_ref(self, owner: str, source_ref: str) -> int:
        db = self._connect()
        try:
            cur = db.execute("DELETE FROM dream_scraps WHERE owner=? AND source_ref=?", (owner, source_ref)); db.commit(); return cur.rowcount
        finally: db.close()
