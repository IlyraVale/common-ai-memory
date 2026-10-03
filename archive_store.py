from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


SCHEMA_VERSION = 2
MAX_SEARCH_LIMIT = 50
MAX_SNIPPET_CHARS = 360
MAX_OPEN_CHARS = 12_000
_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]+")
_OWNER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _owner(value: str) -> str:
    value = str(value or "").strip().lower()
    if not _OWNER_RE.fullmatch(value):
        raise ValueError("invalid archive owner")
    return value


class ArchiveIntegrityError(RuntimeError):
    """The immutable raw archive copy is missing or no longer matches its hash."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _terms(text: str) -> set[str]:
    result: set[str] = set()
    for token in _WORD_RE.findall(text.casefold()):
        if re.fullmatch(r"[\u3400-\u4dbf\u4e00-\u9fff]+", token):
            if len(token) == 1:
                result.add(token)
            else:
                result.update(token[index : index + 2] for index in range(len(token) - 1))
        else:
            result.add(token)
    return result


def _bounded_int(value: int, *, minimum: int, maximum: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _timestamp(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("message timestamp must be an ISO-8601 string or null")
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _date_bound(value: str | None, *, end: bool) -> str | None:
    if not value:
        return None
    raw = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raw += "T23:59:59.999999+00:00" if end else "T00:00:00+00:00"
    return _timestamp(raw)


class ArchiveStore:
    """Separate, bounded old-chat archive. It never reads or writes MemoryStore."""

    def __init__(self, archive_root: str | os.PathLike[str]):
        self.root = Path(archive_root).resolve()
        self.raw_root = self.root / "raw"
        self.db_path = self.root / "archive.sqlite3"

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write:
            self.root.mkdir(parents=True, exist_ok=True)
        elif not self.db_path.exists():
            raise FileNotFoundError(f"archive database does not exist: {self.db_path}")
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if write:
            self._ensure_schema(connection)
        try:
            yield connection
            if write:
                connection.commit()
        except Exception:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS archive_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_files (
                file_hash TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                original_name TEXT NOT NULL,
                raw_path TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                session_count INTEGER NOT NULL,
                message_count INTEGER NOT NULL,
                owner TEXT
            );
            CREATE TABLE IF NOT EXISTS messages (
                archive_id TEXT PRIMARY KEY,
                file_hash TEXT NOT NULL REFERENCES source_files(file_hash),
                source TEXT NOT NULL,
                session_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                timestamp TEXT,
                role TEXT NOT NULL,
                body TEXT NOT NULL,
                owner TEXT,
                UNIQUE(file_hash, session_id, ordinal)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_source_time ON messages(source, timestamp);
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, ordinal);
            CREATE TABLE IF NOT EXISTS message_terms (
                term TEXT NOT NULL,
                archive_id TEXT NOT NULL REFERENCES messages(archive_id) ON DELETE CASCADE,
                PRIMARY KEY(term, archive_id)
            );
            CREATE INDEX IF NOT EXISTS idx_message_terms_archive ON message_terms(archive_id);
            """
        )
        row = db.execute("SELECT value FROM archive_meta WHERE key='schema_version'").fetchone()
        version = int(row[0]) if row else SCHEMA_VERSION
        if version == 1:
            source_columns = {item[1] for item in db.execute("PRAGMA table_info(source_files)")}
            message_columns = {item[1] for item in db.execute("PRAGMA table_info(messages)")}
            if "owner" not in source_columns:
                db.execute("ALTER TABLE source_files ADD COLUMN owner TEXT")
            if "owner" not in message_columns:
                db.execute("ALTER TABLE messages ADD COLUMN owner TEXT")
            db.execute("UPDATE archive_meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
        elif version != SCHEMA_VERSION:
            raise RuntimeError(f"unsupported archive schema version: {version}")
        db.execute(
            "INSERT OR IGNORE INTO archive_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def import_records(
        self,
        source_file: str | os.PathLike[str],
        *,
        owner: str,
        source: str,
        sessions: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        """Import normalized records while preserving an immutable copy of source_file.

        A format-specific adapter must provide sessions shaped as
        {"session_id": str, "messages": [{"timestamp": ISO str|None,
        "role": str, "text": str}]}. No vendor export format is guessed here.
        """
        path = Path(source_file).resolve(strict=True)
        if not path.is_file():
            raise ValueError("source_file must be a regular file")
        source = source.strip()
        owner = _owner(owner)
        if not source or len(source) > 120:
            raise ValueError("source must contain 1..120 characters")
        file_hash = _sha256(path)
        normalized = self._normalize_sessions(sessions)

        with self._connect(write=True) as db:
            existing = db.execute(
                "SELECT session_count, message_count, raw_path, owner FROM source_files WHERE file_hash=?",
                (file_hash,),
            ).fetchone()
            if existing:
                if existing["owner"] != owner:
                    raise PermissionError("archive source already belongs to another owner or is legacy unowned")
                self._verify_raw_copy(existing["raw_path"], file_hash)
                return {
                    "file_hash": file_hash,
                    "imported": False,
                    "idempotent": True,
                    "sessions": existing["session_count"],
                    "messages": existing["message_count"],
                    "raw_path": existing["raw_path"],
                }

            self.raw_root.mkdir(parents=True, exist_ok=True)
            suffix = path.suffix.lower() if re.fullmatch(r"\.[a-z0-9]{1,10}", path.suffix.lower()) else ".bin"
            raw_path = self.raw_root / f"{file_hash}{suffix}"
            if not raw_path.exists():
                with path.open("rb") as source_stream, raw_path.open("xb") as target_stream:
                    shutil.copyfileobj(source_stream, target_stream, 1024 * 1024)
            raw_path.chmod(0o444)
            self._verify_raw_copy(raw_path, file_hash)
            imported_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            message_count = sum(len(item["messages"]) for item in normalized)
            db.execute(
                "INSERT INTO source_files VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (file_hash, source, path.name, str(raw_path), imported_at, len(normalized), message_count, owner),
            )
            for session in normalized:
                for ordinal, message in enumerate(session["messages"]):
                    archive_id = hashlib.sha256(
                        f"{file_hash}\0{session['session_id']}\0{ordinal}".encode("utf-8")
                    ).hexdigest()[:32]
                    db.execute(
                        "INSERT INTO messages VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            archive_id, file_hash, source, session["session_id"], ordinal,
                            message["timestamp"], message["role"], message["text"],
                            owner,
                        ),
                    )
                    db.executemany(
                        "INSERT INTO message_terms(term, archive_id) VALUES(?, ?)",
                        ((term, archive_id) for term in sorted(_terms(message["text"]))),
                    )
        return {
            "file_hash": file_hash,
            "imported": True,
            "idempotent": False,
            "sessions": len(normalized),
            "messages": message_count,
            "raw_path": str(raw_path),
            "owner": owner,
        }

    @staticmethod
    def _verify_raw_copy(raw_path: str | os.PathLike[str], expected_hash: str) -> None:
        path = Path(raw_path)
        if not path.is_file():
            raise ArchiveIntegrityError(
                f"raw archive copy is missing or not a regular file: {path}"
            )
        actual_hash = _sha256(path)
        if actual_hash != expected_hash:
            raise ArchiveIntegrityError(
                "raw archive copy failed SHA-256 verification: "
                f"expected {expected_hash}, got {actual_hash}; path={path}"
            )

    @staticmethod
    def _normalize_sessions(sessions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for session in sessions:
            if not isinstance(session, dict):
                raise ValueError("each session must be an object")
            session_id = session.get("session_id")
            messages = session.get("messages")
            if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 500:
                raise ValueError("session_id must contain 1..500 characters")
            session_id = session_id.strip()
            if session_id in seen:
                raise ValueError(f"duplicate session_id in import: {session_id}")
            seen.add(session_id)
            if not isinstance(messages, list):
                raise ValueError("messages must be a list")
            clean_messages = []
            for message in messages:
                if not isinstance(message, dict):
                    raise ValueError("each message must be an object")
                role, text = message.get("role"), message.get("text")
                if not isinstance(role, str) or not role.strip() or len(role) > 120:
                    raise ValueError("message role must contain 1..120 characters")
                if not isinstance(text, str):
                    raise ValueError("message text must be a string")
                clean_messages.append({
                    "timestamp": _timestamp(message.get("timestamp")),
                    "role": role.strip(),
                    "text": text,
                })
            result.append({"session_id": session_id, "messages": clean_messages})
        return result

    def search(
        self,
        query: str,
        *,
        source: str | None = None,
        session_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 10,
        snippet_chars: int = 240,
        owner: str | None = None,
        include_legacy_unowned: bool = True,
    ) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            raise ValueError("query is required")
        limit = _bounded_int(limit, minimum=1, maximum=MAX_SEARCH_LIMIT, name="limit")
        snippet_chars = _bounded_int(
            snippet_chars, minimum=40, maximum=MAX_SNIPPET_CHARS, name="snippet_chars"
        )
        terms = sorted(_terms(query))
        if not terms:
            return []
        if not self.db_path.exists():
            return []
        clauses, params = [], []
        if owner is not None:
            owner = _owner(owner)
            if include_legacy_unowned:
                clauses.append("(m.owner = ? OR m.owner IS NULL)")
            else:
                clauses.append("m.owner = ?")
            params.append(owner)
        if source:
            clauses.append("m.source = ?")
            params.append(source)
        if session_id:
            clauses.append("m.session_id = ?")
            params.append(session_id)
        lower, upper = _date_bound(date_from, end=False), _date_bound(date_to, end=True)
        if lower:
            clauses.append("m.timestamp >= ?")
            params.append(lower)
        if upper:
            clauses.append("m.timestamp <= ?")
            params.append(upper)
        placeholders = ",".join("?" for _ in terms)
        where = (" AND " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT m.*, COUNT(DISTINCT t.term) AS matched_terms
            FROM messages m JOIN message_terms t ON t.archive_id=m.archive_id
            WHERE t.term IN ({placeholders}) {where}
            GROUP BY m.archive_id
            HAVING matched_terms = ?
            ORDER BY CASE WHEN instr(lower(m.body), lower(?)) > 0 THEN 0 ELSE 1 END,
                     m.timestamp DESC, m.archive_id
            LIMIT ?
        """
        with self._connect() as db:
            rows = db.execute(sql, [*terms, *params, len(terms), query, limit]).fetchall()
        return [self._search_result(row, query, snippet_chars) for row in rows]

    @staticmethod
    def _search_result(row: sqlite3.Row, query: str, size: int) -> dict[str, Any]:
        body = row["body"]
        folded, needle = body.casefold(), query.casefold()
        position = folded.find(needle)
        if position < 0:
            positions = [folded.find(term) for term in _terms(query) if folded.find(term) >= 0]
            position = min(positions) if positions else 0
        has_prefix = position - size // 3 > 0
        start = max(0, position - size // 3)
        has_suffix = len(body) - start > size
        payload_size = size - int(has_prefix) - int(has_suffix)
        end = min(len(body), start + payload_size)
        snippet = ("…" if has_prefix else "") + body[start:end] + ("…" if has_suffix else "")
        return {
            "archive_id": row["archive_id"],
            "source": row["source"],
            "owner": row["owner"],
            "session_id": row["session_id"],
            "timestamp": row["timestamp"],
            "role": row["role"],
            "snippet": snippet,
        }

    def open(
        self,
        archive_id: str,
        *,
        before: int = 2,
        after: int = 2,
        max_chars: int = 6_000,
        owner: str | None = None,
        include_legacy_unowned: bool = True,
    ) -> dict[str, Any]:
        before = _bounded_int(before, minimum=0, maximum=20, name="before")
        after = _bounded_int(after, minimum=0, maximum=20, name="after")
        max_chars = _bounded_int(max_chars, minimum=200, maximum=MAX_OPEN_CHARS, name="max_chars")
        with self._connect() as db:
            target = db.execute("SELECT * FROM messages WHERE archive_id=?", (archive_id,)).fetchone()
            if not target:
                raise KeyError(f"archive message not found: {archive_id}")
            if owner is not None:
                checked_owner = _owner(owner)
                if target["owner"] is not None and target["owner"] != checked_owner:
                    raise PermissionError("archive message belongs to another owner")
                if target["owner"] is None and not include_legacy_unowned:
                    raise PermissionError("legacy unowned archive is not accessible here")
            source_file = db.execute(
                "SELECT raw_path FROM source_files WHERE file_hash=?", (target["file_hash"],)
            ).fetchone()
            if not source_file:
                raise ArchiveIntegrityError(
                    f"source file metadata missing for archive hash: {target['file_hash']}"
                )
            self._verify_raw_copy(source_file["raw_path"], target["file_hash"])
            rows = db.execute(
                """SELECT archive_id, timestamp, role, body, ordinal FROM messages
                   WHERE file_hash=? AND session_id=? AND ordinal BETWEEN ? AND ?
                   ORDER BY ordinal""",
                (
                    target["file_hash"], target["session_id"],
                    max(0, target["ordinal"] - before), target["ordinal"] + after,
                ),
            ).fetchall()
        messages, used, truncated = [], 0, False
        for row in rows:
            allowance = max_chars - used
            if allowance <= 0:
                truncated = True
                break
            body = row["body"]
            if len(body) > allowance:
                body, truncated = body[: max(0, allowance - 1)] + "…", True
            messages.append({
                "archive_id": row["archive_id"], "timestamp": row["timestamp"],
                "role": row["role"], "text": body, "is_target": row["archive_id"] == archive_id,
            })
            used += len(body)
            if truncated:
                break
        return {
            "archive_id": archive_id,
            "source": target["source"],
            "owner": target["owner"],
            "session_id": target["session_id"],
            "file_hash": target["file_hash"],
            "messages": messages,
            "truncated": truncated,
            "max_chars": max_chars,
        }

    def evidence_status(self, archive_id: str, *, owner: str) -> str:
        owner = _owner(owner)
        if not self.db_path.is_file() or not re.fullmatch(r"[0-9a-f]{32}", str(archive_id)):
            return "missing"
        with self._connect() as db:
            row = db.execute("SELECT owner FROM messages WHERE archive_id=?", (archive_id,)).fetchone()
        if row is None:
            return "missing"
        if row["owner"] is None:
            return "unowned"
        return "valid" if row["owner"] == owner else "cross_owner"

    def delete_message(self, archive_id: str, *, owner: str) -> None:
        owner = _owner(owner)
        with self._connect(write=True) as db:
            row = db.execute("SELECT owner FROM messages WHERE archive_id=?", (archive_id,)).fetchone()
            if row is None:
                raise KeyError("archive message not found")
            if row["owner"] != owner:
                raise PermissionError("cannot delete another owner's or legacy archive")
            db.execute("DELETE FROM messages WHERE archive_id=?", (archive_id,))

    def diagnostics(self) -> dict[str, Any]:
        if not self.db_path.is_file():
            return {"status": "UNAVAILABLE", "owned": 0, "legacy_unowned": 0, "invalid_owner": 0}
        with self._connect() as db:
            integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
            rows = db.execute("SELECT archive_id,owner FROM messages").fetchall()
        owned = sum(1 for row in rows if row["owner"] is not None and _OWNER_RE.fullmatch(str(row["owner"])))
        unowned = sum(1 for row in rows if row["owner"] is None)
        invalid = len(rows) - owned - unowned
        duplicate = len(rows) - len({row["archive_id"] for row in rows})
        status = "PASS" if integrity == "ok" and invalid == 0 and duplicate == 0 else "FAIL"
        return {"status": status, "schema_version": SCHEMA_VERSION, "owned": owned,
                "legacy_unowned": unowned, "invalid_owner": invalid,
                "duplicate_ids": duplicate, "integrity": integrity}

