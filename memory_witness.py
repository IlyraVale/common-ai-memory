from __future__ import annotations

import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _iso(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("provenance time must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class MemoryWitnessStore:
    _INIT_LOCK_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16)

    def __init__(self, project_root: str | Path, db_path: str | Path | None = None, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(db_path) if db_path else Path(project_root).resolve() / "state" / "memory-witness.sqlite3"
        self.busy_timeout_ms = busy_timeout_ms

    @staticmethod
    def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
        text = str(exc).lower()
        return "database is locked" in text or "database is busy" in text

    def _connect_once(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
        try:
            db.row_factory = sqlite3.Row; db.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}"); db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS memory_exposures (
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, memory_id TEXT NOT NULL,
            episode_id TEXT NOT NULL, source TEXT NOT NULL, context_kind TEXT NOT NULL,
            exposed_at TEXT NOT NULL, retrieval_id TEXT
        )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_exposure_episode ON memory_exposures(owner,episode_id,memory_id)")
            db.execute("""CREATE TABLE IF NOT EXISTS memory_witnesses (
            witness_id TEXT PRIMARY KEY, owner TEXT NOT NULL, memory_id TEXT NOT NULL,
            evidence_ref TEXT NOT NULL, evidence_at TEXT NOT NULL, episode_id TEXT NOT NULL,
            independent INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
        )""")
            db.commit(); return db
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

    def expose(self, owner: str, memory_ids: list[str], episode_id: str, *, source: str,
               context_kind: str, retrieval_id: str | None = None, now: datetime | None = None) -> int:
        stamp = _iso(now); ids = list(dict.fromkeys(str(x) for x in memory_ids if x))
        db = self._connect()
        try:
            db.executemany("INSERT INTO memory_exposures(owner,memory_id,episode_id,source,context_kind,exposed_at,retrieval_id) VALUES(?,?,?,?,?,?,?)",
                           [(owner, mid, episode_id, source, context_kind, stamp, retrieval_id) for mid in ids]); db.commit(); return len(ids)
        finally: db.close()

    def record_witness(self, owner: str, memory_id: str, evidence_ref: str, episode_id: str,
                       *, evidence_at: datetime | None = None) -> dict:
        stamp = _iso(evidence_at); db = self._connect()
        try:
            rows = db.execute("SELECT context_kind FROM memory_exposures WHERE owner=? AND memory_id=? AND episode_id=? ORDER BY id", (owner,memory_id,episode_id)).fetchall()
            independent = not rows
            reason = "independent:no_prior_exposure" if independent else f"not_independent:{rows[0]['context_kind']}"
            wid = secrets.token_urlsafe(18)
            db.execute("INSERT INTO memory_witnesses VALUES(?,?,?,?,?,?,?,?,?)", (wid,owner,memory_id,evidence_ref,stamp,episode_id,int(independent),reason,_iso()))
            db.commit(); return {"witness_id":wid,"independent":independent,"reason":reason}
        finally: db.close()

    def has_independent_witness(self, owner: str, memory_id: str) -> bool:
        db=self._connect()
        try: return db.execute("SELECT 1 FROM memory_witnesses WHERE owner=? AND memory_id=? AND independent=1 LIMIT 1",(owner,memory_id)).fetchone() is not None
        finally: db.close()

    def delete_evidence_ref(self, owner: str, evidence_ref: str) -> int:
        db=self._connect()
        try:
            cur=db.execute("DELETE FROM memory_witnesses WHERE owner=? AND evidence_ref=?",(owner,evidence_ref));db.commit();return cur.rowcount
        finally: db.close()

    def cleanup_exposures(self, *, before: datetime) -> int:
        db=self._connect()
        try:
            cur=db.execute("DELETE FROM memory_exposures WHERE exposed_at<?",(_iso(before),));db.commit();return cur.rowcount
        finally: db.close()
