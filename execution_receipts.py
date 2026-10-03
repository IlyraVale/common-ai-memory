from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from config import data_root, load_dotenv


SCHEMA_VERSION = 2
VALID_OPERATIONS = {"remember", "update_memory", "memory_set_status", "forget", "import_memory"}
VALID_OUTCOMES = {"success", "failed"}
_IDENTITY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TARGET_RE = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _aware_iso(value: str) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("receipt timestamps must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _identity(value: str, name: str) -> str:
    value = str(value or "").strip().lower()
    if not _IDENTITY_RE.fullmatch(value):
        raise ValueError(f"invalid receipt {name}")
    return value


class ExecutionReceiptStore:
    """Append-only, metadata-only execution provenance store."""

    def __init__(self, project_root: str | Path, db_path: str | Path | None = None):
        self.project_root = Path(project_root).resolve()
        self.path = Path(db_path) if db_path else self.project_root / "state" / "execution-receipts.sqlite3"

    _INIT_LOCK_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16)

    @staticmethod
    def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
        message = str(exc).lower()
        return "database is locked" in message or "database is busy" in message

    def _open_once(self, create: bool) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10.0)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA secure_delete=ON")
            if create:
                db.execute("PRAGMA journal_mode=WAL")
                self._ensure_schema(db)
            return db
        except Exception:
            db.close()
            raise

    def _open(self, create: bool) -> sqlite3.Connection:
        # A brand-new database's first switch to WAL needs an exclusive lock that
        # concurrent first openers can return "database is locked" for without
        # waiting on busy_timeout. Retry only that, briefly (same policy as
        # DreamLeaseStore); any other OperationalError is raised at once.
        for delay in (*self._INIT_LOCK_RETRY_DELAYS, None):
            try:
                return self._open_once(create)
            except sqlite3.OperationalError as exc:
                if delay is None or not self._is_locked_error(exc):
                    raise
                time.sleep(delay)
        raise AssertionError("unreachable")

    @contextmanager
    def _connect(self, *, create: bool = True) -> Iterator[sqlite3.Connection]:
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.path.is_file():
            raise FileNotFoundError("execution receipt store is missing")
        db = self._open(create)
        try:
            yield db
            if create:
                db.commit()
        except Exception:
            if create:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _ensure_schema(db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE IF NOT EXISTS receipt_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        version_row = db.execute("SELECT value FROM receipt_meta WHERE key='schema_version'").fetchone()
        if version_row and int(version_row[0]) == 1:
            count = int(db.execute("SELECT count(*) FROM execution_receipts").fetchone()[0])
            if count:
                raise RuntimeError("receipt schema v1 contains rows; explicit compatibility migration required")
            db.executescript("DROP TRIGGER IF EXISTS execution_receipts_no_update; DROP TRIGGER IF EXISTS execution_receipts_no_delete; DROP INDEX IF EXISTS idx_receipts_owner_target; DROP TABLE execution_receipts;")
            db.execute("UPDATE receipt_meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS execution_receipts (
                receipt_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                actor TEXT NOT NULL,
                operation TEXT NOT NULL,
                target_type TEXT NOT NULL,
                outcome TEXT NOT NULL,
                changed_fields_json TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                error_class TEXT,
                parent_operation_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS receipt_targets (
                receipt_id TEXT PRIMARY KEY REFERENCES execution_receipts(receipt_id),
                target_id TEXT,
                target_status TEXT NOT NULL CHECK(target_status IN ('linked','redacted')),
                CHECK((target_status='linked' AND target_id IS NOT NULL) OR
                      (target_status='redacted' AND target_id IS NULL))
            );
            CREATE INDEX IF NOT EXISTS idx_receipt_targets_id ON receipt_targets(target_id);
            CREATE TRIGGER IF NOT EXISTS execution_receipts_no_update
                BEFORE UPDATE ON execution_receipts BEGIN
                    SELECT RAISE(ABORT, 'execution receipts are immutable');
                END;
            CREATE TRIGGER IF NOT EXISTS execution_receipts_no_delete
                BEFORE DELETE ON execution_receipts BEGIN
                    SELECT RAISE(ABORT, 'execution receipts are append-only');
                END;
            """
        )
        row = db.execute("SELECT value FROM receipt_meta WHERE key='schema_version'").fetchone()
        if row and int(row[0]) != SCHEMA_VERSION:
            raise RuntimeError("unsupported execution receipt schema")
        db.execute(
            "INSERT OR IGNORE INTO receipt_meta(key,value) VALUES('schema_version',?)",
            (str(SCHEMA_VERSION),),
        )

    def append(
        self, *, owner: str, actor: str, operation: str, target_id: str | None,
        outcome: str = "success", changed_fields: list[str] | tuple[str, ...] = (),
        started_at: str | None = None, completed_at: str | None = None,
        error_class: str | None = None, parent_operation_id: str | None = None,
    ) -> dict[str, Any]:
        owner = _identity(owner, "owner")
        actor = _identity(actor, "actor")
        if operation not in VALID_OPERATIONS:
            raise ValueError("invalid receipt operation")
        if outcome not in VALID_OUTCOMES:
            raise ValueError("invalid receipt outcome")
        if target_id is not None and not _TARGET_RE.fullmatch(str(target_id)):
            raise ValueError("invalid receipt target_id")
        fields = sorted(set(str(field) for field in changed_fields))
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", field) for field in fields):
            raise ValueError("invalid changed field name")
        if error_class is not None:
            error_class = type(error_class).__name__ if not isinstance(error_class, str) else error_class
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,127}", error_class):
                error_class = "Error"
        started = _aware_iso(started_at or _utc_now())
        completed = _aware_iso(completed_at or _utc_now())
        if completed < started:
            raise ValueError("receipt completed_at precedes started_at")
        receipt_id = uuid.uuid4().hex
        created = _utc_now()
        with self._connect() as db:
            db.execute(
                "INSERT INTO execution_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (receipt_id, owner, actor, operation, "memory", outcome,
                 json.dumps(fields, separators=(",", ":")), started, completed,
                 error_class, parent_operation_id, created),
            )
            db.execute(
                "INSERT INTO receipt_targets VALUES(?,?,?)",
                (receipt_id, target_id, "linked" if target_id is not None else "redacted"),
            )
        return self.get(receipt_id, owner=owner)

    def get(self, receipt_id: str, *, owner: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", str(receipt_id)):
            raise ValueError("invalid receipt id")
        owner = _identity(owner, "owner")
        with self._connect(create=False) as db:
            row = db.execute(
                """SELECT e.*, t.target_id, t.target_status
                     FROM execution_receipts e LEFT JOIN receipt_targets t USING(receipt_id)
                    WHERE e.receipt_id=? AND e.owner=?""",
                (receipt_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError("execution receipt not found")
        result = dict(row)
        result["changed_fields"] = json.loads(result.pop("changed_fields_json"))
        return result

    def exists(self, receipt_id: str, *, owner: str) -> bool:
        try:
            self.get(receipt_id, owner=owner)
            return True
        except (FileNotFoundError, KeyError, ValueError):
            return False

    def reference_status(self, receipt_id: str, *, owner: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", str(receipt_id)) or not self.path.is_file():
            return "missing"
        owner = _identity(owner, "owner")
        with self._connect(create=False) as db:
            row = db.execute(
                "SELECT owner FROM execution_receipts WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
        if row is None:
            return "missing"
        return "valid" if str(row["owner"]) == owner else "cross_owner"

    def scrub_target(self, *, owner: str, target_id: str) -> int:
        owner = _identity(owner, "owner")
        if not _TARGET_RE.fullmatch(str(target_id)):
            raise ValueError("invalid receipt target_id")
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE receipt_targets SET target_id=NULL,target_status='redacted'
                     WHERE target_id=? AND receipt_id IN
                           (SELECT receipt_id FROM execution_receipts WHERE owner=?)""",
                (target_id, owner),
            )
        return cursor.rowcount

    def diagnostics(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"status": "UNAVAILABLE", "reason": "missing", "rows": 0}
        with self._connect(create=False) as db:
            integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
            journal = str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            columns = [row[1] for row in db.execute("PRAGMA table_info(execution_receipts)")]
            rows = [dict(row) for row in db.execute("SELECT * FROM execution_receipts")]
            targets = [dict(row) for row in db.execute("SELECT * FROM receipt_targets")]
        errors: list[dict[str, str]] = []
        for row in rows:
            rid = str(row["receipt_id"])
            try:
                _identity(row["owner"], "owner"); _identity(row["actor"], "actor")
                if row["operation"] not in VALID_OPERATIONS or row["outcome"] not in VALID_OUTCOMES:
                    raise ValueError("invalid enum")
                started = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
                completed = datetime.fromisoformat(row["completed_at"].replace("Z", "+00:00"))
                if started.tzinfo is None or completed.tzinfo is None or completed < started:
                    raise ValueError("invalid timestamps")
            except (TypeError, ValueError):
                errors.append({"receipt_id": rid, "error_type": "invalid_receipt"})
        forbidden = sorted(set(columns) & {"content", "prompt", "raw_args", "raw_result", "content_hash", "secret"})
        if "target_id" in columns:
            forbidden.append("target_id")
        receipt_ids = {row["receipt_id"] for row in rows}
        orphan_targets = [row["receipt_id"] for row in targets if row["receipt_id"] not in receipt_ids]
        invalid_targets = [row["receipt_id"] for row in targets if
                           (row["target_status"] == "linked") != (row["target_id"] is not None)]
        if forbidden:
            errors.append({"receipt_id": "schema", "error_type": "unsafe_columns"})
        if orphan_targets or invalid_targets:
            errors.append({"receipt_id": "targets", "error_type": "invalid_target_linkage"})
        status = "PASS" if integrity == "ok" and journal == "wal" and not errors else "FAIL"
        return {"status": status, "schema_version": SCHEMA_VERSION, "rows": len(rows),
                "integrity": integrity, "journal_mode": journal, "errors": errors,
                "unsafe_columns": sorted(set(forbidden)),
                "linked_targets": sum(1 for row in targets if row["target_status"] == "linked"),
                "redacted_targets": sum(1 for row in targets if row["target_status"] == "redacted"),
                "orphan_targets": orphan_targets}


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Inspect one safe execution receipt by owner and id.")
    parser.add_argument("receipt_id")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--root", default=str(data_root()))
    args = parser.parse_args()
    try:
        print(json.dumps(ExecutionReceiptStore(args.root).get(args.receipt_id, owner=args.owner), ensure_ascii=False))
        return 0
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
