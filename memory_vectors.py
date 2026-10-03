from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import sqlite3
import struct
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

from memory_search import _PROCESS_LOCKS, _PROCESS_LOCKS_GUARD, record_hash
from config import data_root, load_dotenv


SCHEMA_VERSION = "1"
DEFAULT_MODEL_ID = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_DIMENSION = 384
DEFAULT_MIN_SIMILARITY = 0.25
DB_RELATIVE_PATH = Path("state/memory-vectors.sqlite3")
DIRTY_RELATIVE_PATH = Path("state/memory-vectors.dirty")
MODEL_CACHE_RELATIVE_PATH = Path("state/models/fastembed")
_REPLACE_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16)
_BACKEND_CACHE_LOCK = threading.Lock()
_BACKEND_CACHE: dict[tuple[str, str], "EmbeddingBackend"] = {}


class VectorIndexUnavailable(RuntimeError):
    pass


class EmbeddingBackend(Protocol):
    model_id: str
    revision: str
    fingerprint: str
    dimension: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class FastEmbedBackend:
    def __init__(self, project_root: str | Path, model_id: str = DEFAULT_MODEL_ID) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise VectorIndexUnavailable("optional dependency 'fastembed' is not installed") from exc
        self.model_id = model_id
        self.dimension = DEFAULT_DIMENSION
        version = importlib.metadata.version("fastembed")
        source_model = "qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
        cache_dir = Path(project_root).resolve() / MODEL_CACHE_RELATIVE_PATH
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._model = TextEmbedding(model_name=model_id, cache_dir=str(cache_dir))
        revision_file = cache_dir / "models--qdrant--paraphrase-multilingual-MiniLM-L12-v2-onnx-Q/refs/main"
        self.revision = revision_file.read_text(encoding="utf-8").strip() if revision_file.is_file() else "unknown"
        self.fingerprint = hashlib.sha256(
            f"fastembed:{version}:{model_id}:{source_model}:{self.revision}:{self.dimension}".encode("utf-8")
        ).hexdigest()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[float(value) for value in vector] for vector in self._model.embed(list(texts))]


def semantic_query_eligible(query: str) -> bool:
    useful = [ch for ch in (query or "") if ch.isalnum() or "\u3400" <= ch <= "\u9fff"]
    return len(useful) >= 3


def _pack_vector(vector: Sequence[float], dimension: int) -> bytes:
    values = [float(value) for value in vector]
    if len(values) != dimension or not all(math.isfinite(value) for value in values):
        raise ValueError("invalid embedding vector")
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 0:
        raise ValueError("zero embedding vector")
    return struct.pack(f"<{dimension}f", *values)


def _unpack_vector(blob: bytes, dimension: int) -> tuple[float, ...]:
    if len(blob) != dimension * 4:
        raise ValueError("embedding dimension mismatch")
    values = struct.unpack(f"<{dimension}f", blob)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("invalid embedding vector")
    if sum(value * value for value in values) <= 0:
        raise ValueError("zero embedding vector")
    return values


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding dimension mismatch")
    dot = sum(a * b for a, b in zip(left, right))
    ln = math.sqrt(sum(value * value for value in left))
    rn = math.sqrt(sum(value * value for value in right))
    if ln <= 0 or rn <= 0:
        raise ValueError("zero embedding vector")
    return dot / (ln * rn)


class MemoryVectorIndex:
    def __init__(
        self,
        project_root: str | Path,
        *,
        backend: EmbeddingBackend | None = None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.root = Path(project_root).resolve()
        self.db_path = self.root / DB_RELATIVE_PATH
        self.dirty_path = self.root / DIRTY_RELATIVE_PATH
        self.lock_path = self.root / "state/.memory-vectors.lock"
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))
        self._backend = backend

    @property
    def backend(self) -> EmbeddingBackend:
        if self._backend is None:
            key = (os.path.normcase(str(self.root)), DEFAULT_MODEL_ID)
            with _BACKEND_CACHE_LOCK:
                cached = _BACKEND_CACHE.get(key)
                if cached is None:
                    cached = FastEmbedBackend(self.root)
                    _BACKEND_CACHE[key] = cached
                self._backend = cached
        return self._backend

    @contextmanager
    def _lock(self, timeout: float = 30.0):
        key = os.path.normcase(str(self.lock_path.resolve()))
        with _PROCESS_LOCKS_GUARD:
            local = _PROCESS_LOCKS.setdefault(key, threading.RLock())
        if not local.acquire(timeout=timeout):
            raise TimeoutError("memory vector lock timed out")
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
                        raise TimeoutError("memory vector lock timed out")
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
            row = db.execute("SELECT value FROM vector_meta WHERE key='schema_version'").fetchone()
            if row is None or str(row[0]) != SCHEMA_VERSION:
                db.close()
                raise VectorIndexUnavailable("vector index schema mismatch")
        return db

    @staticmethod
    def _create_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE vector_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE memory_vectors (
                memory_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                scope TEXT NOT NULL,
                source TEXT,
                updated_at TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                model_id TEXT NOT NULL,
                model_fingerprint TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                embedding BLOB NOT NULL,
                indexed_at TEXT NOT NULL
            );
            """
        )
        db.execute("INSERT INTO vector_meta(key,value) VALUES('schema_version',?)", (SCHEMA_VERSION,))

    def mark_dirty(self, reason: str) -> None:
        self.dirty_path.parent.mkdir(parents=True, exist_ok=True)
        self.dirty_path.write_text(str(reason)[:200], encoding="utf-8")

    def is_dirty(self) -> bool:
        return self.dirty_path.exists()

    def _require_usable(self) -> None:
        if not self.db_path.is_file():
            raise VectorIndexUnavailable("vector index is missing")
        if self.is_dirty():
            raise VectorIndexUnavailable("vector index needs rebuild")

    def rebuild(self, records: Iterable[dict[str, Any]]) -> dict[str, Any]:
        rows = list(records)
        backend = self.backend
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.db_path.with_name(f"memory-vectors.rebuild-{os.getpid()}-{time.time_ns()}.sqlite3")
        reusable: dict[str, tuple[str, bytes]] = {}
        if self.db_path.is_file() and not self.is_dirty():
            try:
                with self._lock():
                    old = self._connect()
                    meta = dict(old.execute("SELECT key,value FROM vector_meta"))
                    if meta.get("model_fingerprint") == backend.fingerprint:
                        reusable = {
                            str(row["memory_id"]): (str(row["content_hash"]), bytes(row["embedding"]))
                            for row in old.execute("SELECT memory_id,content_hash,embedding FROM memory_vectors")
                        }
                    old.close()
            except sqlite3.Error:
                reusable = {}
        try:
            db = self._connect(tmp, require_schema=False)
            self._create_schema(db)
            db.executemany(
                "INSERT INTO vector_meta(key,value) VALUES(?,?)",
                (("model_id", backend.model_id), ("model_fingerprint", backend.fingerprint),
                 ("dimension", str(backend.dimension)),
                 ("model_revision", str(getattr(backend, "revision", "unknown"))),
                 ("last_rebuild_at", datetime.now(timezone.utc).isoformat())),
            )
            db.commit()
            pending = [row for row in rows if reusable.get(str(row["id"]), (None,))[0] != record_hash(row)]
            generated = backend.embed([str(row.get("content") or "") for row in pending]) if pending else []
            generated_by_id = {str(row["id"]): vector for row, vector in zip(pending, generated)}
            now = datetime.now(timezone.utc).isoformat()
            for row in rows:
                memory_id = str(row["id"])
                digest = record_hash(row)
                blob = reusable[memory_id][1] if reusable.get(memory_id, (None,))[0] == digest else _pack_vector(generated_by_id[memory_id], backend.dimension)
                db.execute(
                    """INSERT INTO memory_vectors VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (memory_id, str(row.get("owner") or ""), str(row.get("scope") or ""),
                     str(row.get("source") or "") or None, str(row.get("updated_at") or ""),
                     digest, backend.model_id, backend.fingerprint, backend.dimension, blob, now),
                )
            db.commit(); db.close()
            with self._lock():
                for suffix in ("-wal", "-shm"):
                    Path(str(self.db_path) + suffix).unlink(missing_ok=True)
                for delay in (0.0,) + _REPLACE_RETRY_DELAYS:
                    if delay:
                        time.sleep(delay)
                    try:
                        os.replace(tmp, self.db_path)
                        break
                    except PermissionError:
                        if delay == _REPLACE_RETRY_DELAYS[-1]:
                            raise
                verify = self._connect(); verify.close()
                self.dirty_path.unlink(missing_ok=True)
            return {"ok": True, "indexed": len(rows), "embedded": len(pending),
                    "reused": len(rows) - len(pending), "model_id": backend.model_id,
                    "model_fingerprint": backend.fingerprint, "dimension": backend.dimension}
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def search(self, query: str, *, owner: str = "all", limit: int = 50,
               min_similarity: float = DEFAULT_MIN_SIMILARITY) -> list[dict[str, Any]] | None:
        if not semantic_query_eligible(query):
            return None
        self._require_usable()
        backend = self.backend
        query_vector = backend.embed([query])[0]
        if len(query_vector) != backend.dimension:
            raise VectorIndexUnavailable("query embedding dimension mismatch")
        with self._lock():
            db = self._connect()
            meta = dict(db.execute("SELECT key,value FROM vector_meta"))
            if meta.get("model_fingerprint") != backend.fingerprint or int(meta.get("dimension", 0)) != backend.dimension:
                db.close()
                raise VectorIndexUnavailable("vector model fingerprint mismatch")
            where, params = [], []
            normalized_owner = (owner or "all").strip().lower()
            if normalized_owner == "shared":
                where.append("scope='shared'")
            elif normalized_owner == "human":
                where.append("owner='human'")
            elif normalized_owner != "all":
                where.append("owner=?"); params.append(normalized_owner)
            sql = "SELECT * FROM memory_vectors" + ((" WHERE " + " AND ".join(where)) if where else "")
            rows = db.execute(sql, params).fetchall(); db.close()
        result = []
        for row in rows:
            vector = _unpack_vector(bytes(row["embedding"]), int(row["dimension"]))
            similarity = _cosine(query_vector, vector)
            if similarity >= min_similarity:
                result.append({"memory_id": str(row["memory_id"]), "similarity": similarity,
                               "source": row["source"], "updated_at": row["updated_at"]})
        result.sort(key=lambda row: str(row["updated_at"]), reverse=True)
        result.sort(key=lambda row: 1 if row["source"] == "inferred" else 0)
        result.sort(key=lambda row: float(row["similarity"]), reverse=True)
        return result[: max(1, min(int(limit), 1000))]

    def upsert(self, record: dict[str, Any]) -> None:
        self._require_usable()
        backend = self.backend
        blob = _pack_vector(backend.embed([str(record.get("content") or "")])[0], backend.dimension)
        with self._lock():
            db = self._connect()
            meta = dict(db.execute("SELECT key,value FROM vector_meta"))
            if meta.get("model_fingerprint") != backend.fingerprint:
                db.close(); raise VectorIndexUnavailable("vector model fingerprint mismatch")
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM memory_vectors WHERE memory_id=?", (str(record["id"]),))
            db.execute("INSERT INTO memory_vectors VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (str(record["id"]), str(record.get("owner") or ""), str(record.get("scope") or ""),
                        str(record.get("source") or "") or None, str(record.get("updated_at") or ""),
                        record_hash(record), backend.model_id, backend.fingerprint, backend.dimension, blob,
                        datetime.now(timezone.utc).isoformat()))
            db.commit(); db.close()

    def delete(self, memory_id: str) -> None:
        self._require_usable()
        with self._lock():
            db = self._connect(); db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM memory_vectors WHERE memory_id=?", (memory_id,)); db.commit(); db.close()

    def diagnostics(self, records: Iterable[dict[str, Any]], *, load_backend: bool = False) -> dict[str, Any]:
        source = {str(row["id"]): row for row in records}
        if not self.db_path.is_file():
            return {"status": "UNAVAILABLE", "reason": "missing", "indexed": 0,
                    "missing": sorted(source), "orphan": [], "hash_mismatch": []}
        try:
            with self._lock():
                db = self._connect(); integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
                meta = dict(db.execute("SELECT key,value FROM vector_meta"))
                rows = db.execute("SELECT memory_id,content_hash,dimension,embedding FROM memory_vectors").fetchall(); db.close()
            indexed = {str(row["memory_id"]): row for row in rows}
            missing = sorted(set(source) - set(indexed)); orphan = sorted(set(indexed) - set(source))
            mismatch = sorted(mid for mid in set(source) & set(indexed) if indexed[mid]["content_hash"] != record_hash(source[mid]))
            expected_dim = int(meta.get("dimension", 0)); invalid = []
            for row in rows:
                try:
                    if int(row["dimension"]) != expected_dim:
                        raise ValueError("dimension")
                    _unpack_vector(bytes(row["embedding"]), expected_dim)
                except (ValueError, struct.error):
                    invalid.append(str(row["memory_id"]))
            fingerprint_mismatch = False
            semantic_smoke = None
            if load_backend:
                backend = self.backend
                fingerprint_mismatch = meta.get("model_fingerprint") != backend.fingerprint
                semantic_smoke = len(backend.embed(["semantic doctor smoke"])[0]) == expected_dim
            stale = bool(missing or orphan or mismatch or invalid or fingerprint_mismatch or integrity != "ok" or self.is_dirty())
            return {"status": "STALE" if stale else "PASS", "reason": "mismatch" if stale else None,
                    "indexed": len(indexed), "missing": missing, "orphan": orphan,
                    "hash_mismatch": mismatch, "invalid_vectors": invalid, "integrity": integrity,
                    "schema_version": meta.get("schema_version"), "model_id": meta.get("model_id"),
                    "model_revision": meta.get("model_revision"),
                    "model_fingerprint": meta.get("model_fingerprint"), "dimension": expected_dim,
                    "fingerprint_mismatch": fingerprint_mismatch, "semantic_smoke": semantic_smoke}
        except (sqlite3.Error, ValueError, VectorIndexUnavailable) as exc:
            return {"status": "STALE", "reason": type(exc).__name__, "indexed": 0,
                    "missing": sorted(source), "orphan": [], "hash_mismatch": []}


def rebuild_project(project_root: str | Path, *, backend: EmbeddingBackend | None = None) -> dict[str, Any]:
    from memory_store import MemoryStore

    store = MemoryStore(project_root, "vector-rebuild")
    index = MemoryVectorIndex(project_root, backend=backend)
    with store._write_lock():
        records = [row for row in store._read_all() if not str(row.get("id") or "").startswith("human:_house/")]
        return index.rebuild(records)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Rebuild the optional local semantic vector index")
    parser.add_argument("command", choices=("rebuild",))
    parser.add_argument("--root", default=str(data_root()))
    args = parser.parse_args(argv)
    try:
        print(json.dumps(rebuild_project(args.root), sort_keys=True))
        return 0
    except Exception as exc:
        print(f"memory vector rebuild failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


