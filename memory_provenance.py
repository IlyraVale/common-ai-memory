"""Limited, read-only provenance query for one memory id.

This module only joins relationships that already exist in the memory files and
their side stores. It never creates a database, never writes, never introduces a
new provenance id, and never returns query text or any memory/dream/scrap body.

Every backing store is reported as either ``unavailable`` (missing/unreadable)
or ``ok`` (possibly with zero rows); an absent store is never presented as an
empty history.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from memory_store import MemoryStore, _read_memory_text, _safe_memory_id
from config import data_root, load_dotenv


DEFAULT_LIMIT = 20
MIN_LIMIT = 1
MAX_LIMIT = 50
REDACTION_NOTE = "redacted receipts are intentionally unattributable after privacy scrub"
AUTHORITY_NOTE = (
    "metadata only: provenance shows recorded relationships, not that the memory content is true"
)
_OWNER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_RECEIPT_REF_RE = re.compile(r"^receipt:([0-9a-f]{32})$")
_ARCHIVE_REF_RE = re.compile(r"^archive:([0-9a-f]{32})$")
_SAFE_OPAQUE_REF_RE = re.compile(r"^[A-Za-z0-9:_-]{1,96}$")
_DREAM_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_DREAM_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


class StoreUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _unavailable(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason, **extra}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _check_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_LIMIT <= value <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer between {MIN_LIMIT} and {MAX_LIMIT}")
    return value


def _check_owner(value: Any) -> str:
    owner = str(value or "").strip().lower()
    if not _OWNER_RE.fullmatch(owner):
        raise ValueError("invalid provenance owner")
    return owner


@contextmanager
def _readonly(path: Path) -> Iterator[sqlite3.Connection]:
    """Open an existing SQLite store strictly read-only; never create it."""
    if not path.is_file():
        raise StoreUnavailable("missing_store")
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise StoreUnavailable("unreadable_store") from exc
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only = ON")
        yield db
    except sqlite3.Error as exc:
        raise StoreUnavailable("unreadable_store") from exc
    finally:
        db.close()


def _bounded(rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    return {"total": len(rows), "truncated": len(rows) > limit, "items": rows[:limit]}


class _Paths:
    def __init__(self, root: Path, archive_root: Path) -> None:
        self.root = root
        self.memory = root / "memory"
        self.receipts = root / "state" / "execution-receipts.sqlite3"
        self.witness = root / "state" / "memory-witness.sqlite3"
        self.feedback = root / "state" / "memory-feedback.sqlite3"
        self.scraps = root / "state" / "dream-scraps.sqlite3"
        self.dream_runtime = root / "state" / "dream-runtime.sqlite3"
        self.dreams = root / "dreams"
        self.archive = archive_root / "archive.sqlite3"
        self.activity = root / ".activity"


# ---------- memory ----------

def _iter_memory_paths(memory_root: Path) -> Iterator[Path]:
    for path in memory_root.rglob("*.md"):
        rel = path.relative_to(memory_root)
        if not rel.parts or rel.parts[0].startswith("_") or path.name.startswith("_"):
            continue
        yield path


def _find_memory(memory_root: Path, memory_id: str) -> dict[str, Any] | None:
    for path in _iter_memory_paths(memory_root):
        try:
            record = MemoryStore._parse(_read_memory_text(path))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if record and str(record.get("id")) == memory_id:
            return record
    return None


def _memory_section(paths: _Paths, memory_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not paths.memory.is_dir():
        return _unavailable("missing_store"), None
    record = _find_memory(paths.memory, memory_id)
    if record is None:
        return {"status": "missing"}, None
    superseded_by = record.get("superseded_by")
    superseded = None
    if superseded_by:
        superseded = {
            "id": superseded_by,
            "status": "present" if _find_memory(paths.memory, str(superseded_by)) else "dangling",
        }
    section = {
        "status": "present",
        "id": record.get("id"),
        "owner": record.get("owner"),
        "scope": record.get("scope"),
        "category": record.get("category"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "task_status": record.get("status"),
        "source": record.get("source"),
        "lifecycle": record.get("lifecycle") or "active",
        "lifecycle_updated_at": record.get("lifecycle_updated_at"),
        "superseded_by": superseded,
        "verification": record.get("verification") or "unknown",
        "verification_updated_at": record.get("verification_updated_at"),
    }
    return section, record


# ---------- evidence references ----------

def _owner_status(row: sqlite3.Row | None, owner: str) -> str:
    if row is None:
        return "missing"
    if row["owner"] is None:
        return "unowned"
    return "valid" if str(row["owner"]) == owner else "cross_owner"


def _ref_status(paths: _Paths, owner: str, ref: str) -> tuple[str, str]:
    """Return (kind, status) from the caller's point of view; never target metadata."""
    receipt = _RECEIPT_REF_RE.fullmatch(ref)
    archive = _ARCHIVE_REF_RE.fullmatch(ref)
    if receipt:
        kind, path, sql, ident = "receipt", paths.receipts, "SELECT owner FROM execution_receipts WHERE receipt_id=?", receipt.group(1)
    elif archive:
        kind, path, sql, ident = "archive", paths.archive, "SELECT owner FROM messages WHERE archive_id=?", archive.group(1)
    else:
        return "unrecognized", "malformed"
    try:
        with _readonly(path) as db:
            return kind, _owner_status(db.execute(sql, (ident,)).fetchone(), owner)
    except StoreUnavailable:
        return kind, "store_unavailable"


def _evidence_section(paths: _Paths, owner: str, record: dict[str, Any] | None, limit: int) -> dict[str, Any]:
    if record is None:
        return _unavailable("memory_not_present")
    raw = record.get("evidence_refs") or []
    items = []
    for value in raw if isinstance(raw, list) else []:
        ref = str(value).strip().lower()
        kind, status = _ref_status(paths, owner, ref)
        items.append({"ref": ref if kind != "unrecognized" else None, "kind": kind, "status": status})
    return {"status": "ok", **_bounded(items, limit)}


# ---------- receipts ----------

def _receipts_section(paths: _Paths, owner: str, memory_id: str, limit: int) -> dict[str, Any]:
    # Only receipts whose target linkage still exists. Redacted receipts carry no
    # target_id after privacy scrub and are deliberately not re-attributed here.
    try:
        with _readonly(paths.receipts) as db:
            rows = db.execute(
                """SELECT e.receipt_id, e.actor, e.operation, e.outcome, e.changed_fields_json,
                          e.started_at, e.completed_at, e.error_class, e.parent_operation_id
                     FROM execution_receipts e JOIN receipt_targets t USING(receipt_id)
                    WHERE e.owner=? AND t.target_status='linked' AND t.target_id=?
                    ORDER BY e.completed_at DESC, e.receipt_id""",
                (owner, memory_id),
            ).fetchall()
    except StoreUnavailable as exc:
        return _unavailable(exc.reason, note=REDACTION_NOTE)
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["changed_fields"] = json.loads(item.pop("changed_fields_json"))
        except (TypeError, json.JSONDecodeError):
            item["changed_fields"] = None
        items.append(item)
    return {"status": "ok", "note": REDACTION_NOTE, **_bounded(items, limit)}


# ---------- exposures / retrievals / witnesses ----------

def _exposure_rows(paths: _Paths, owner: str, memory_id: str) -> list[dict[str, Any]]:
    with _readonly(paths.witness) as db:
        return [dict(row) for row in db.execute(
            """SELECT id, episode_id, retrieval_id, source, context_kind, exposed_at
                 FROM memory_exposures WHERE owner=? AND memory_id=? ORDER BY id DESC""",
            (owner, memory_id),
        )]


def _exposures_section(rows: list[dict[str, Any]] | None, reason: str | None, limit: int) -> dict[str, Any]:
    if rows is None:
        return _unavailable(reason or "missing_store")
    by_kind: dict[str, dict[str, Any]] = {}
    for row in rows:
        kind = str(row["context_kind"])
        entry = by_kind.setdefault(kind, {"count": 0, "first": row["exposed_at"], "last": row["exposed_at"]})
        entry["count"] += 1
        entry["first"] = min(entry["first"], row["exposed_at"])
        entry["last"] = max(entry["last"], row["exposed_at"])
    items = [{key: row[key] for key in ("episode_id", "retrieval_id", "source", "context_kind", "exposed_at")}
             for row in rows]
    return {"status": "ok", "by_context_kind": by_kind, **_bounded(items, limit)}


def _retrievals_section(
    paths: _Paths, owner: str, memory_id: str, exposures: list[dict[str, Any]] | None, limit: int,
) -> dict[str, Any]:
    try:
        with _readonly(paths.feedback) as db:
            # LIKE only narrows candidates; membership is checked exactly below.
            events = db.execute(
                """SELECT retrieval_id, result_ids_json, created_at FROM retrieval_events
                    WHERE agent_id=? AND instr(result_ids_json, ?) > 0
                    ORDER BY created_at DESC, retrieval_id""",
                (owner, json.dumps(memory_id, ensure_ascii=False)),
            ).fetchall()
            matched: list[dict[str, Any]] = []
            for row in events:
                try:
                    ids = [str(value) for value in json.loads(row["result_ids_json"])]
                except (TypeError, ValueError):
                    continue
                if memory_id in ids:
                    matched.append({"retrieval_id": str(row["retrieval_id"]),
                                    "created_at": row["created_at"], "rank": ids.index(memory_id) + 1})
            verdicts: dict[str, list[str]] = {}
            if matched:
                placeholders = ",".join("?" for _ in matched)
                for row in db.execute(
                    f"""SELECT retrieval_id, verdict, source FROM retrieval_feedback
                         WHERE agent_id=? AND memory_id=? AND retrieval_id IN ({placeholders})
                         ORDER BY id""",
                    (owner, memory_id, *(item["retrieval_id"] for item in matched)),
                ):
                    verdicts.setdefault(str(row["retrieval_id"]), []).append(
                        {"verdict": row["verdict"], "source": row["source"]}
                    )
    except StoreUnavailable as exc:
        return _unavailable(exc.reason)
    modes: dict[str, str] = {}
    for row in exposures or []:
        if row["retrieval_id"] and row["context_kind"] in {"recall", "passive_recall"}:
            modes[str(row["retrieval_id"])] = str(row["context_kind"])
    for item in matched:
        item["mode"] = modes.get(item["retrieval_id"], "unknown")
        item["feedback"] = verdicts.get(item["retrieval_id"], [])
    return {
        "status": "ok",
        "mode_source": "exposure_ledger" if exposures is not None else "unavailable",
        **_bounded(matched, limit),
    }


def _witnesses_section(paths: _Paths, owner: str, memory_id: str, limit: int) -> dict[str, Any]:
    try:
        with _readonly(paths.witness) as db:
            rows = [dict(row) for row in db.execute(
                """SELECT witness_id, evidence_ref, evidence_at, episode_id, independent, reason, created_at
                     FROM memory_witnesses WHERE owner=? AND memory_id=?
                    ORDER BY created_at DESC, witness_id""",
                (owner, memory_id),
            )]
    except StoreUnavailable as exc:
        return _unavailable(exc.reason)
    items = []
    for row in rows:
        ref = str(row.pop("evidence_ref") or "")
        if _RECEIPT_REF_RE.fullmatch(ref) or _ARCHIVE_REF_RE.fullmatch(ref):
            kind, status = _ref_status(paths, owner, ref)
            evidence = {"ref": ref, "kind": kind, "status": status}
        elif _SAFE_OPAQUE_REF_RE.fullmatch(ref):
            evidence = {"ref": ref, "kind": "opaque", "status": "unresolvable"}
        else:
            evidence = {"ref": None, "kind": "unrecognized", "status": "malformed"}
        row["independent"] = bool(row["independent"])
        row["evidence"] = evidence
        items.append(row)
    return {
        "status": "ok",
        "has_independent_witness": any(item["independent"] for item in items),
        **_bounded(items, limit),
    }


# ---------- dreams / scraps ----------

def _dream_file_meta(path: Path) -> dict[str, Any] | None:
    """Parse only the frontmatter; the dream body is never read into the result."""
    try:
        from dreams import parse_dream

        parsed = parse_dream(_read_memory_text(path))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    parsed.pop("content", None)
    return parsed


def _dreams_section(paths: _Paths, owner: str, memory_id: str, limit: int) -> dict[str, Any]:
    by_date: dict[str, dict[str, Any]] = {}

    def entry(day: str) -> dict[str, Any]:
        return by_date.setdefault(day, {
            "dream_date": day, "generation_id": None, "runner": None,
            "file": "absent", "commit": "absent", "derived": True, "factual_authority": False,
        })

    files_status = "ok"
    invalid_files = 0
    if not paths.dreams.is_dir():
        files_status = "unavailable"
    else:
        owner_root = paths.dreams / owner
        months = sorted(owner_root.iterdir()) if owner_root.is_dir() else []
        for month in months:
            if not month.is_dir() or not _DREAM_MONTH_RE.fullmatch(month.name):
                continue
            for path in sorted(month.iterdir()):
                match = _DREAM_FILE_RE.fullmatch(path.name)
                if not match:
                    continue
                meta = _dream_file_meta(path)
                if meta is None or meta.get("owner") != owner:
                    invalid_files += 1
                    continue
                if memory_id in meta.get("source_memory_ids", []):
                    item = entry(match.group(1))
                    item.update(file="present", generation_id=meta.get("generation_id"), runner=meta.get("runner"))

    commits_status = "ok"
    try:
        with _readonly(paths.dream_runtime) as db:
            rows = db.execute(
                "SELECT dream_date, generation_id, package_json FROM dream_commits WHERE owner=?", (owner,)
            ).fetchall()
        for row in rows:
            try:
                package = json.loads(row["package_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if memory_id in [str(value) for value in package.get("source_memory_ids") or []]:
                item = entry(str(row["dream_date"]))
                item["commit"] = "present"
                item["generation_id"] = item["generation_id"] or row["generation_id"]
    except StoreUnavailable:
        commits_status = "unavailable"

    if files_status == "unavailable" and commits_status == "unavailable":
        return _unavailable("missing_store")
    items = sorted(by_date.values(), key=lambda value: value["dream_date"], reverse=True)
    return {
        "status": "ok", "files_status": files_status, "commits_status": commits_status,
        "invalid_files_skipped": invalid_files, **_bounded(items, limit),
    }


def _scraps_section(paths: _Paths, owner: str, memory_id: str) -> dict[str, Any]:
    try:
        with _readonly(paths.scraps) as db:
            row = db.execute(
                """SELECT count(*) AS total, sum(expires_at > ?) AS unexpired, max(expires_at) AS latest_expires_at
                     FROM dream_scraps WHERE owner=? AND source_ref=?""",
                (_now_iso(), owner, memory_id),
            ).fetchone()
    except StoreUnavailable as exc:
        return _unavailable(exc.reason)
    return {"status": "ok", "total": int(row["total"] or 0), "unexpired": int(row["unexpired"] or 0),
            "latest_expires_at": row["latest_expires_at"]}


# ---------- audit metadata ----------

def _audit_section(paths: _Paths, owner: str, memory_id: str) -> dict[str, Any]:
    files = [paths.activity / f"memory-audit-{owner}.jsonl.1", paths.activity / f"memory-audit-{owner}.jsonl"]
    present = [path for path in files if path.is_file()]
    if not present:
        return _unavailable("missing_store")
    needle = json.dumps(memory_id, ensure_ascii=False)
    count = 0
    by_tool: dict[str, int] = {}
    first = last = None
    try:
        for path in present:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if needle not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict) or event.get("action") != "read":
                        continue
                    ids = {str(item.get("id")) for item in event.get("items") or [] if isinstance(item, dict)}
                    if memory_id not in ids:
                        continue
                    count += 1
                    tool = str(event.get("tool") or "read")[:32]
                    by_tool[tool] = by_tool.get(tool, 0) + 1
                    stamp = str(event.get("ts") or "") or None
                    if stamp:
                        first = stamp if first is None else min(first, stamp)
                        last = stamp if last is None else max(last, stamp)
    except OSError:
        return _unavailable("unreadable_store")
    return {"status": "ok", "reads": count, "by_tool": by_tool, "first": first, "last": last}


# ---------- public API ----------

def query_provenance(
    project_root: str | Path,
    owner: str,
    memory_id: str,
    *,
    limit: int = DEFAULT_LIMIT,
    archive_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return bounded, metadata-only provenance for one memory, scoped to ``owner``.

    ``owner`` is the trusted caller identity (the MCP service AGENT_ID or a local
    administrator via CLI). Ledgers are only read for that owner.
    """
    owner = _check_owner(owner)
    limit = _check_limit(limit)
    memory_id = _safe_memory_id(str(memory_id))
    root = Path(project_root).resolve()
    paths = _Paths(root, Path(archive_root).resolve() if archive_root else root / "archive")

    memory, record = _memory_section(paths, memory_id)
    exposure_rows: list[dict[str, Any]] | None
    exposure_reason = None
    try:
        exposure_rows = _exposure_rows(paths, owner, memory_id)
    except StoreUnavailable as exc:
        exposure_rows, exposure_reason = None, exc.reason
    return {
        "memory_id": memory_id,
        "queried_as": owner,
        "limit": limit,
        "generated_at": _now_iso(),
        "authority": AUTHORITY_NOTE,
        "memory": memory,
        "evidence_refs": _evidence_section(paths, owner, record, limit),
        "receipts": _receipts_section(paths, owner, memory_id, limit),
        "exposures": _exposures_section(exposure_rows, exposure_reason, limit),
        "retrievals": _retrievals_section(paths, owner, memory_id, exposure_rows, limit),
        "witnesses": _witnesses_section(paths, owner, memory_id, limit),
        "dreams": _dreams_section(paths, owner, memory_id, limit),
        "scraps": _scraps_section(paths, owner, memory_id),
        "audit_reads": _audit_section(paths, owner, memory_id),
    }


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Trusted local admin: read-only, metadata-only provenance for one memory id."
    )
    parser.add_argument("memory_id")
    parser.add_argument("--owner", required=True, help="identity whose ledgers are read (local admin only)")
    parser.add_argument("--root", default=str(data_root()))
    parser.add_argument("--archive-root", default=None)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()
    try:
        result = query_provenance(args.root, args.owner, args.memory_id, limit=args.limit,
                                  archive_root=args.archive_root)
    except ValueError as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
