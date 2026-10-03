from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import time
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from config import data_root, load_dotenv


SCHEMA_VERSION = "3"
DB_RELATIVE_PATH = Path("state/memory-search.sqlite3")
DIRTY_RELATIVE_PATH = Path("state/memory-search.dirty")
TOKENIZER = "trigram"
_QUERY_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[㐀-鿿豈-﫿]+")
_REPLACE_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16)
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}
log = logging.getLogger("memory_search")


class SearchIndexUnavailable(RuntimeError):
    pass


def fts5_available() -> bool:
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE VIRTUAL TABLE probe USING fts5(content, tokenize='trigram')")
        return True
    except sqlite3.Error:
        return False
    finally:
        db.close()


def normalize_match_query(query: str) -> str | None:
    tokens = []
    for token in _QUERY_TOKEN_RE.findall((query or "").lower()):
        # FTS5 trigram cannot match shorter terms.  Falling back preserves the
        # existing one/two-character and emoji behavior.
        if len(token) < 3:
            return None
        tokens.append('"' + token.replace('"', '""') + '"')
    if not tokens:
        return None
    return " AND ".join(dict.fromkeys(tokens))


def record_hash(record: dict[str, Any]) -> str:
    payload = {
        key: str(record.get(key) or "")
        for key in (
            "id", "owner", "scope", "category", "content", "created_at",
            "updated_at", "source", "status",
        )
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class MemorySearchIndex:
    def __init__(self, project_root: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self.root = Path(project_root).resolve()
        self.db_path = self.root / DB_RELATIVE_PATH
        self.dirty_path = self.root / DIRTY_RELATIVE_PATH
        self.lock_path = self.root / "state/.memory-search.lock"
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))

    @contextmanager
    def _lock(self, timeout: float = 15.0):
        key = os.path.normcase(str(self.lock_path.resolve()))
        with _PROCESS_LOCKS_GUARD:
            local = _PROCESS_LOCKS.setdefault(key, threading.RLock())
        if not local.acquire(timeout=timeout):
            raise TimeoutError("memory search lock timed out")
        stream = None
        locked = False
        deadline = time.monotonic() + timeout
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            stream = self.lock_path.open("a+b", buffering=0)
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"0"); stream.flush()
            while True:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("memory search lock timed out")
                    time.sleep(0.02)
            yield
        finally:
            if locked and stream is not None:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            if stream is not None:
                stream.close()
            local.release()

    def _connect(self, path: Path | None = None, *, require_schema: bool = True) -> sqlite3.Connection:
        target = path or self.db_path
        db = sqlite3.connect(target, timeout=self.busy_timeout_ms / 1000)
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        if path is None:
            db.execute("PRAGMA journal_mode=WAL")
        if require_schema:
            version = db.execute(
                "SELECT value FROM search_meta WHERE key='schema_version'"
            ).fetchone()
            if version is None or str(version[0]) != SCHEMA_VERSION:
                db.close()
                raise SearchIndexUnavailable("search index schema mismatch")
        return db

    def _connect_readonly(self) -> sqlite3.Connection:
        uri = "file:" + self.db_path.resolve().as_posix() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=self.busy_timeout_ms / 1000)
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        version = db.execute("SELECT value FROM search_meta WHERE key='schema_version'").fetchone()
        if version is None or str(version[0]) != SCHEMA_VERSION:
            db.close()
            raise SearchIndexUnavailable("search index schema mismatch")
        return db

    @staticmethod
    def _create_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE search_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE memories (
                memory_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                scope TEXT NOT NULL,
                category TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                source TEXT,
                status TEXT,
                lifecycle TEXT NOT NULL,
                superseded_by TEXT,
                verification TEXT NOT NULL,
                content_hash TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE memory_fts USING fts5(
                memory_id UNINDEXED,
                content,
                category,
                tokenize='trigram'
            );
            """
        )
        db.execute("INSERT INTO search_meta(key,value) VALUES('schema_version',?)", (SCHEMA_VERSION,))

    @staticmethod
    def _insert_record(db: sqlite3.Connection, record: dict[str, Any]) -> None:
        memory_id = str(record["id"])
        db.execute(
            """INSERT INTO memories(
                   memory_id,owner,scope,category,created_at,updated_at,source,status,lifecycle,superseded_by,verification,content_hash
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                memory_id, str(record["owner"]), str(record["scope"]), str(record["category"]),
                str(record["created_at"]), str(record["updated_at"]),
                record.get("source"), record.get("status"),
                str(record.get("lifecycle") or "active"), record.get("superseded_by"),
                str(record.get("verification") or "unknown"),
                record_hash(record),
            ),
        )
        db.execute(
            "INSERT INTO memory_fts(memory_id,content,category) VALUES(?,?,?)",
            (memory_id, str(record.get("content") or ""), str(record.get("category") or "")),
        )

    def mark_dirty(self, reason: str) -> None:
        self.dirty_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.dirty_path.with_name(self.dirty_path.name + f".tmp-{os.getpid()}-{secrets.token_hex(3)}")
        tmp.write_text(type(reason).__name__ if not isinstance(reason, str) else reason[:120], encoding="utf-8")
        os.replace(tmp, self.dirty_path)

    def is_dirty(self) -> bool:
        return self.dirty_path.exists()

    def rebuild(self, records: Iterable[dict[str, Any]]) -> dict[str, Any]:
        if not fts5_available():
            raise SearchIndexUnavailable("SQLite FTS5 trigram tokenizer is unavailable")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.db_path.with_name(self.db_path.name + f".rebuild-{os.getpid()}-{secrets.token_hex(4)}")
        count = 0
        try:
            db = sqlite3.connect(tmp)
            try:
                self._create_schema(db)
                db.commit()
                db.execute("BEGIN")
                for record in records:
                    self._insert_record(db, record)
                    count += 1
                db.execute(
                    "INSERT INTO search_meta(key,value) VALUES('last_rebuild_at',?)",
                    (datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),),
                )
                db.commit()
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("rebuilt search index failed integrity_check")
            finally:
                db.close()
            with self._lock():
                for suffix in ("-wal", "-shm"):
                    self.db_path.with_name(self.db_path.name + suffix).unlink(missing_ok=True)
                for delay in (*_REPLACE_RETRY_DELAYS, None):
                    try:
                        os.replace(tmp, self.db_path)
                        break
                    except PermissionError:
                        if delay is None:
                            raise
                        time.sleep(delay)
                # Open once to establish production WAL mode before declaring clean.
                db = self._connect()
                db.close()
                self.dirty_path.unlink(missing_ok=True)
            return {"ok": True, "indexed": count, "schema_version": SCHEMA_VERSION}
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def _require_usable(self) -> None:
        if not self.db_path.is_file():
            raise SearchIndexUnavailable("search index is missing")
        if self.is_dirty():
            raise SearchIndexUnavailable("search index needs rebuild")

    def search(self, query: str, *, owner: str = "all", limit: int = 50) -> list[dict[str, Any]] | None:
        match = normalize_match_query(query)
        if match is None:
            return None
        self._require_usable()
        with self._lock():
            db = self._connect()
            try:
                where = ["memory_fts MATCH ?"]
                params: list[Any] = [match]
                owner = (owner or "all").strip().lower()
                if owner == "shared":
                    where.append("m.scope='shared'")
                elif owner == "human":
                    where.append("m.owner='human'")
                elif owner != "all":
                    where.append("m.owner=?")
                    params.append(owner)
                params.append(max(1, min(int(limit), 1000)))
                rows = db.execute(
                    """SELECT m.memory_id, m.updated_at, m.source, m.lifecycle, m.superseded_by, m.verification,
                              bm25(memory_fts, 0.0, 1.0, 0.25) AS rank
                         FROM memory_fts JOIN memories m USING(memory_id)
                        WHERE """ + " AND ".join(where) + " ORDER BY rank LIMIT ?",
                    params,
                ).fetchall()
                result = [dict(row) for row in rows]
                # Stable secondary ordering: inferred loses only an exact BM25 tie;
                # observed/user_statement/legacy remain peers, then recency decides.
                result.sort(key=lambda row: str(row["updated_at"]), reverse=True)
                result.sort(key=lambda row: 1 if row["source"] == "inferred" else 0)
                result.sort(key=lambda row: float(row["rank"]))
                return result
            finally:
                db.close()

    def upsert(self, record: dict[str, Any]) -> None:
        self._require_usable()
        with self._lock():
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                memory_id = str(record["id"])
                db.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
                db.execute("DELETE FROM memories WHERE memory_id=?", (memory_id,))
                self._insert_record(db, record)
                db.commit()
            finally:
                db.close()

    def delete(self, memory_id: str) -> None:
        self._require_usable()
        with self._lock():
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
                db.execute("DELETE FROM memories WHERE memory_id=?", (memory_id,))
                db.commit()
            finally:
                db.close()

    def diagnostics(self, records: Iterable[dict[str, Any]]) -> dict[str, Any]:
        source = {str(record["id"]): record for record in records}
        if not self.db_path.is_file():
            return {"status": "STALE", "reason": "missing", "indexed": 0,
                    "missing": sorted(source), "orphan": [], "hash_mismatch": []}
        try:
            with self._lock():
                db = self._connect_readonly()
                try:
                    integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
                    rows = db.execute("SELECT memory_id,content_hash,lifecycle,superseded_by,verification FROM memories").fetchall()
                    fts_count = int(db.execute("SELECT count(*) FROM memory_fts").fetchone()[0])
                    smoke = db.execute(
                        "SELECT count(*) FROM memory_fts WHERE memory_fts MATCH ?", ('"doctorprobe"',)
                    ).fetchone()[0]
                finally:
                    db.close()
        except (sqlite3.Error, SearchIndexUnavailable) as exc:
            return {"status": "STALE", "reason": type(exc).__name__, "indexed": 0,
                    "missing": sorted(source), "orphan": [], "hash_mismatch": []}
        indexed = {str(row["memory_id"]): str(row["content_hash"]) for row in rows}
        missing = sorted(set(source) - set(indexed))
        orphan = sorted(set(indexed) - set(source))
        mismatch = sorted(mid for mid in set(source) & set(indexed) if indexed[mid] != record_hash(source[mid]))
        by_id = {str(row["memory_id"]): row for row in rows}
        lifecycle_mismatch = sorted(
            mid for mid in set(source) & set(indexed)
            if str(by_id[mid]["lifecycle"] or "active") != str(source[mid].get("lifecycle") or "active")
            or str(by_id[mid]["superseded_by"] or "") != str(source[mid].get("superseded_by") or "")
        )
        verification_mismatch = sorted(
            mid for mid in set(source) & set(indexed)
            if str(by_id[mid]["verification"] or "unknown")
            != str(source[mid].get("verification") or "unknown")
        )
        stale = bool(missing or orphan or mismatch or lifecycle_mismatch or verification_mismatch or len(indexed) != fts_count or integrity != "ok" or self.is_dirty())
        return {
            "status": "STALE" if stale else "PASS", "reason": "mismatch" if stale else None,
            "indexed": len(indexed), "fts_rows": fts_count, "missing": missing,
            "orphan": orphan, "hash_mismatch": mismatch, "integrity": integrity,
            "lifecycle_mismatch": lifecycle_mismatch,
            "verification_mismatch": verification_mismatch,
            "bm25_smoke": isinstance(smoke, int), "schema_version": SCHEMA_VERSION,
        }


def rebuild_project(project_root: str | Path) -> dict[str, Any]:
    from memory_store import MemoryStore

    store = MemoryStore(project_root, "search-rebuild")
    index = MemorySearchIndex(project_root)
    with store._write_lock():
        records = [row for row in store._read_all() if not str(row.get("id") or "").startswith("human:_house/")]
        return index.rebuild(records)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Rebuild the disposable Common AI Memory FTS5 index")
    parser.add_argument("command", choices=("rebuild",))
    parser.add_argument("--root", default=str(data_root()))
    args = parser.parse_args(argv)
    try:
        result = rebuild_project(args.root)
    except Exception as exc:
        print(f"memory search rebuild failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
