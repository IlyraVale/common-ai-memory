from __future__ import annotations

import json
import logging
import os
import re
import secrets
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory_house import decorate_record, room_dir, validate_category_shape

log = logging.getLogger(__name__)

_FRONTMATTER_KEYS = (
    "id",
    "owner",
    "scope",
    "category",
    "created_at",
    "updated_at",
)
_OPTIONAL_KEYS = ("status", "source", "lifecycle", "superseded_by", "lifecycle_updated_at", "verification", "verification_updated_at", "evidence_refs")
VALID_STATUS = ("open", "done")
VALID_SOURCE = ("user_statement", "observed", "inferred")
VALID_LIFECYCLE = ("active", "stale", "superseded", "review_needed")
VALID_VERIFICATION = ("unknown", "unverified", "confirmed", "partial", "not_applicable")
MAX_EVIDENCE_REFS = 32
MAX_EVIDENCE_REF_LENGTH = 96
_RECEIPT_REF_RE = re.compile(r"^receipt:([0-9a-f]{32})$")
_ARCHIVE_REF_RE = re.compile(r"^archive:([0-9a-f]{32})$")
_CLEAR_VALUES = {"none", "clear", "null"}
_CJK_RUN_RE = re.compile(r"[㐀-鿿豈-﫿]+")
_WORD_RE = re.compile(r"[a-z0-9]+")
PASSIVE_MAX_RESULTS = 3
PASSIVE_CONTEXT_BUDGET = 1000
PASSIVE_ITEM_BUDGET = 360
PASSIVE_SEMANTIC_STRONG = 0.42
PASSIVE_SEMANTIC_MODERATE = 0.25
PASSIVE_SEMANTIC_MARGIN = 0.08
_PASSIVE_FILLERS = {
    "嗯", "好", "好的", "好吧", "哈哈", "哈哈哈", "哈哈哈哈", "知道了", "继续", "行",
    "可以", "没事", "ok", "okay", "yes", "no", "thanks", "thankyou", "随便聊聊",
    "今天随便聊聊",
}


def _match_units(text: str) -> set[str]:
    text = (text or "").lower()
    units = set(_WORD_RE.findall(text))
    for run in _CJK_RUN_RE.findall(text):
        units.update(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return units


def passive_query_signal(query: str) -> dict[str, Any]:
    raw = str(query or "").strip().lower()
    compact = "".join(ch for ch in raw if ch.isalnum() or "\u3400" <= ch <= "\u9fff")
    useful_count = len(compact)
    return {"eligible": bool(compact) and compact not in _PASSIVE_FILLERS and useful_count >= 2,
            "compact": compact, "useful_count": useful_count,
            "semantic_eligible": useful_count >= 3 and compact not in _PASSIVE_FILLERS}


def passive_candidate_allowed(signal, *, exact, lexical_hits, lexical_coverage,
                              similarity, is_semantic_top, semantic_margin):
    return bool(
        exact or (lexical_hits >= 2 and lexical_coverage >= 0.60)
        or similarity >= PASSIVE_SEMANTIC_STRONG
        or (is_semantic_top and similarity >= PASSIVE_SEMANTIC_MODERATE
            and semantic_margin >= PASSIVE_SEMANTIC_MARGIN and signal["useful_count"] >= 8)
    )


# Windows refuses os.replace() onto a file another handle has open, and refuses
# opening a file in the middle of being replaced; both surface as PermissionError.
# Recall reads lock-free (also across server processes), so these windows are
# expected and brief. Retry a bounded number of times (~0.32s total), then
# re-raise. Each os.replace attempt is itself atomic.
_SHARING_RETRY_DELAYS = (0.001, 0.002, 0.005, 0.01, 0.02, 0.04, 0.08, 0.16)
_RETRY_SHARING_VIOLATIONS = os.name == "nt"


def _with_sharing_retry(operation: Any) -> Any:
    for delay in (*_SHARING_RETRY_DELAYS, None):
        try:
            return operation()
        except PermissionError:
            if delay is None or not _RETRY_SHARING_VIOLATIONS:
                raise
            time.sleep(delay)
    raise AssertionError("unreachable")


def _read_memory_text(path: Path) -> str:
    return _with_sharing_retry(lambda: path.read_text(encoding="utf-8"))


def _check_optional(key: str, value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value).strip().lower()
    if value in _CLEAR_VALUES or value == "":
        return ""
    allowed = VALID_STATUS if key == "status" else VALID_SOURCE
    if value not in allowed:
        raise ValueError(f"invalid {key}: {value!r}; allowed: {', '.join(allowed)}")
    return value


def _check_lifecycle(value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value).strip().lower()
    if value not in VALID_LIFECYCLE:
        raise ValueError(f"invalid lifecycle: {value!r}; allowed: {', '.join(VALID_LIFECYCLE)}")
    return value


def _lifecycle(record: dict[str, Any]) -> str:
    value = str(record.get("lifecycle") or "active").strip().lower()
    return value if value in VALID_LIFECYCLE else "active"


def _check_verification(value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value).strip().lower()
    if value not in VALID_VERIFICATION:
        raise ValueError(
            f"invalid verification: {value!r}; allowed: {', '.join(VALID_VERIFICATION)}"
        )
    return value


def _verification(record: dict[str, Any]) -> str:
    value = str(record.get("verification") or "unknown").strip().lower()
    return value if value in VALID_VERIFICATION else "unknown"


def _evidence_refs(record: dict[str, Any]) -> list[str]:
    value = record.get("evidence_refs")
    return list(value) if isinstance(value, list) else []


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _date_from_iso(value: str) -> str:
    value = str(value or "")
    if len(value) >= 10 and value[4:5] == "-" and value[7:8] == "-":
        return value[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _safe_agent(value: str) -> str:
    value = (value or "").strip().lower()
    safe = "".join(ch for ch in value if ch.isalnum() or ch in "-_")
    if not safe or safe != value:
        raise ValueError(f"invalid agent id: {value!r}")
    return safe


def _safe_memory_id(value: str) -> str:
    value = (value or "").strip()
    safe = "".join(ch for ch in value if ch.isalnum() or ch in "-_:")
    if not safe or safe != value:
        raise ValueError(f"invalid memory id: {value!r}")
    return safe


class MemoryStore:
    """Room-first Markdown memory store.

    Physical layout:
      memory/<section>/<subject>/<date>_<id>__<owner>__<scope>.md

    Theme comes first in the filesystem. Ownership remains metadata-enforced:
    every AI can read readable records, but update/forget require owner==agent_id.
    """

    def __init__(self, project_root: str | Path, agent_id: str) -> None:
        self.project_root = Path(project_root).resolve()
        self.memory_root = self.project_root / "memory"
        self.memory_root.mkdir(parents=True, exist_ok=True)
        self.agent_id = _safe_agent(agent_id)
        self.lock_path = self.project_root / ".memory-store.lock"

    def _validate_evidence_refs(self, refs: list[str] | tuple[str, ...]) -> list[str]:
        if not isinstance(refs, (list, tuple)):
            raise ValueError("evidence_refs must be a list")
        if len(refs) > MAX_EVIDENCE_REFS:
            raise ValueError(f"evidence_refs may contain at most {MAX_EVIDENCE_REFS} items")
        from execution_receipts import ExecutionReceiptStore
        receipts = ExecutionReceiptStore(self.project_root)
        from archive_store import ArchiveStore
        archives = ArchiveStore(self.project_root / "archive")
        result, seen = [], set()
        for raw in refs:
            if not isinstance(raw, str):
                raise ValueError("evidence_refs entries must be strings")
            ref = raw.strip().lower()
            if not ref or len(ref) > MAX_EVIDENCE_REF_LENGTH:
                raise ValueError("invalid evidence reference length")
            receipt_match = _RECEIPT_REF_RE.fullmatch(ref)
            archive_match = _ARCHIVE_REF_RE.fullmatch(ref)
            if receipt_match:
                if not receipts.exists(receipt_match.group(1), owner=self.agent_id):
                    raise ValueError("receipt evidence reference is missing or belongs to another owner")
            elif archive_match:
                state = archives.evidence_status(archive_match.group(1), owner=self.agent_id)
                if state == "unowned":
                    raise ValueError("legacy unowned archive cannot be evidence")
                if state != "valid":
                    raise ValueError("archive evidence reference is missing or belongs to another owner")
            else:
                raise ValueError("unsupported or malformed evidence reference")
            if ref not in seen:
                seen.add(ref); result.append(ref)
        return result

    def _attach_receipt(self, result: dict[str, Any], *, operation: str, target_id: str,
                        changed_fields: list[str], started_at: str) -> None:
        try:
            from execution_receipts import ExecutionReceiptStore
            receipt = ExecutionReceiptStore(self.project_root).append(
                owner=self.agent_id, actor=self.agent_id, operation=operation,
                target_id=target_id, outcome="success", changed_fields=changed_fields,
                started_at=started_at,
            )
            result["execution_receipt_id"] = receipt["receipt_id"]
            if operation == "forget":
                ExecutionReceiptStore(self.project_root).scrub_target(
                    owner=self.agent_id, target_id=target_id
                )
        except Exception as exc:
            result["execution_receipt_id"] = None
            result["receipt_status"] = "unavailable"
            result["receipt_error_class"] = type(exc).__name__
            log.warning("memory mutation succeeded but receipt write failed: %s", type(exc).__name__)

    # ---------- public API ----------

    def remember(self, content: str, category: str, visibility: str = "agent",
                 status: str | None = None, source: str | None = None) -> dict[str, Any]:
        receipt_started_at = _now_iso()
        content = self._clean_content(content)
        category = validate_category_shape(category)
        visibility = (visibility or "agent").strip().lower()
        if visibility not in {"agent", "shared"}:
            raise ValueError("visibility must be 'agent' or 'shared'")
        status = _check_optional("status", status)
        source = _check_optional("source", source)

        record = {
            "id": secrets.token_hex(16),
            "owner": self.agent_id,
            "scope": visibility,
            "category": category,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "content": content,
        }
        if status:
            record["status"] = status
        if source:
            record["source"] = source

        lock = self._acquire_lock()
        try:
            path = self._write_record(record)
        finally:
            self._release_lock(lock)

        git = self._git_commit(
            [path],
            f"memory: {self.agent_id} remember {category}",
        )
        result = decorate_record(record)
        result["ok"] = True
        result["git"] = git
        self._index_upsert(record)
        self._attach_receipt(result, operation="remember", target_id=str(record["id"]),
                             changed_fields=["content", "category", "scope", "status", "source"],
                             started_at=receipt_started_at)
        return result

    def recall(self, query: str = "", owner: str = "all", limit: int = 10,
               include_inactive: bool = False, passive: bool = False) -> list[dict[str, Any]]:
        if passive:
            return self.passive_recall(query, owner=owner, limit=limit)
        query = (query or "").strip().lower()
        items = self._filtered(owner=owner, include_inactive=include_inactive)
        all_items = items

        if query:
            fts_items = self._fts_rank(query, owner, items)
            items = self._rank(query, items) if fts_items is None else fts_items
            items = self._hybrid_rank(query, owner, all_items, items)
        else:
            items.sort(key=self._sort_key, reverse=True)

        return items[: self._safe_limit(limit)]

    def passive_recall(self, query: str, owner: str = "all", limit: int = PASSIVE_MAX_RESULTS) -> list[dict[str, Any]]:
        query = (query or "").strip().lower()
        signal = passive_query_signal(query)
        if not signal["eligible"]:
            return []
        items = self._filtered(owner=owner, include_inactive=False)
        fts_items = self._fts_rank(query, owner, items)
        lexical_items = self._rank(query, items) if fts_items is None else fts_items
        ranked = self._hybrid_rank(query, owner, items, lexical_items)
        semantic_rows = []
        if signal["semantic_eligible"]:
            try:
                from memory_vectors import MemoryVectorIndex
                semantic_rows = MemoryVectorIndex(self.project_root).search(
                    query, owner=owner, limit=50, min_similarity=PASSIVE_SEMANTIC_MODERATE
                ) or []
            except Exception:
                semantic_rows = []
        similarities = {str(row["memory_id"]): float(row["similarity"]) for row in semantic_rows}
        top = float(semantic_rows[0]["similarity"]) if semantic_rows else -1.0
        second = float(semantic_rows[1]["similarity"]) if len(semantic_rows) > 1 else 0.0
        q_units = _match_units(query)
        compact = signal["compact"]
        accepted = []
        for item in ranked:
            memory_id = str(item.get("id") or "")
            content = str(item.get("content") or "").lower()
            content_compact = "".join(ch for ch in content if ch.isalnum() or "\u3400" <= ch <= "\u9fff")
            d_units = _match_units(content)
            hits = len(q_units & d_units)
            similarity = similarities.get(memory_id, -1.0)
            if passive_candidate_allowed(
                signal, exact=len(compact) >= 2 and compact in content_compact,
                lexical_hits=hits, lexical_coverage=hits / len(q_units) if q_units else 0.0,
                similarity=similarity,
                is_semantic_top=bool(semantic_rows) and memory_id == str(semantic_rows[0]["memory_id"]),
                semantic_margin=top - second,
            ):
                accepted.append(item)
            if len(accepted) >= min(PASSIVE_MAX_RESULTS, self._safe_limit(limit)):
                break
        return accepted

    def _fts_rank(self, query: str, owner: str, items: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        try:
            from memory_search import MemorySearchIndex

            ranked = MemorySearchIndex(self.project_root).search(query, owner=owner, limit=1000)
            if ranked is None:
                return None
            by_id = {str(item.get("id") or ""): item for item in items}
            return [by_id[str(row["memory_id"])] for row in ranked if str(row["memory_id"]) in by_id]
        except Exception as exc:
            log.warning("FTS search unavailable; using lexical fallback: %s", type(exc).__name__)
            return None

    def _hybrid_rank(
        self,
        query: str,
        owner: str,
        all_items: list[dict[str, Any]],
        lexical_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Fuse lexical and optional semantic ranks without mixing score scales."""
        try:
            from memory_vectors import MemoryVectorIndex

            semantic = MemoryVectorIndex(self.project_root).search(query, owner=owner, limit=50)
            if semantic is None:
                return lexical_items
        except Exception as exc:
            if (self.project_root / "state/memory-vectors.sqlite3").is_file():
                log.warning("semantic search unavailable; using BM25/lexical: %s", type(exc).__name__)
            return lexical_items

        by_id = {str(item.get("id") or ""): item for item in all_items}
        scores: dict[str, float] = {}
        for rank, item in enumerate(lexical_items[:50], 1):
            memory_id = str(item.get("id") or "")
            scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (60 + rank)
        for rank, row in enumerate(semantic, 1):
            memory_id = str(row["memory_id"])
            if memory_id in by_id:
                scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (60 + rank)
        lowered = query.lower()
        rows = []
        for memory_id, score in scores.items():
            item = by_id[memory_id]
            exact = lowered in str(item.get("content") or "").lower()
            lifecycle_tie = 0 if _lifecycle(item) == "review_needed" else 1
            source_tie = 0 if item.get("source") == "inferred" else 1
            rows.append((exact, score, lifecycle_tie, source_tie, str(item.get("updated_at") or ""), item))
        rows.sort(key=lambda row: (row[0], row[1], row[2], row[3], row[4]), reverse=True)
        return [row[5] for row in rows]

    @staticmethod
    def _rank(query: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        scored: list[tuple[int, int, str, dict[str, Any]]] = []
        terms = [term for term in query.split() if term]
        for item in items:
            content = str(item.get("content") or "").lower()
            tags = " ".join(
                str(item.get(key) or "") for key in ("category", "owner", "location")
            ).lower()
            hay = content + " " + tags
            if query in content:
                score = 100 + content.count(query)
            elif query in tags:
                score = 75 + tags.count(query)
            elif terms and all(term in hay for term in terms):
                score = 50 + sum(hay.count(term) for term in terms)
            else:
                continue
            lifecycle_tie = 0 if _lifecycle(item) == "review_needed" else 1
            source_tie = 0 if item.get("source") == "inferred" else 1
            scored.append((score, lifecycle_tie, source_tie, str(item.get("updated_at") or ""), item))
        scored.sort(key=lambda row: (row[0], row[1], row[2], row[3]), reverse=True)
        return [row[4] for row in scored]

    def recent(self, limit: int = 10, owner: str = "all", include_inactive: bool = False) -> list[dict[str, Any]]:
        items = self._filtered(owner=owner, include_inactive=include_inactive)
        items.sort(key=self._sort_key, reverse=True)
        return items[: self._safe_limit(limit)]

    def update(self, memory_id: str, content: str, category: str | None = None,
               status: str | None = None, source: str | None = None,
               lifecycle: str | None = None, superseded_by: str | None = None,
               verification: str | None = None, evidence_refs: list[str] | None = None,
               _operation: str = "update_memory") -> dict[str, Any]:
        receipt_started_at = _now_iso()
        memory_id = _safe_memory_id(memory_id)
        content = self._clean_content(content)
        status = _check_optional("status", status)
        source = _check_optional("source", source)
        lifecycle = _check_lifecycle(lifecycle)
        verification = _check_verification(verification)
        checked_evidence_refs = self._validate_evidence_refs(evidence_refs) if evidence_refs is not None else None
        if superseded_by is not None:
            superseded_by = _safe_memory_id(superseded_by)
        found = self._find_record(memory_id)
        if found is None:
            raise KeyError(f"memory not found: {memory_id}")

        old_path, record = found
        if record.get("owner") != self.agent_id:
            raise PermissionError("cannot update a memory owned by another identity")
        if lifecycle == "superseded":
            if not superseded_by:
                raise ValueError("superseded lifecycle requires superseded_by")
            if superseded_by == memory_id:
                raise ValueError("a memory cannot supersede itself")
            target_found = self._find_record(superseded_by)
            if target_found is None:
                raise KeyError(f"superseding memory not found: {superseded_by}")
            _, target = target_found
            if target.get("owner") != record.get("owner"):
                raise PermissionError("superseding memory must have the same owner")
            if _lifecycle(target) == "superseded":
                raise ValueError("superseding target cannot itself be superseded")
            cursor = target
            seen = {memory_id}
            while cursor.get("superseded_by"):
                next_id = str(cursor["superseded_by"])
                if next_id in seen:
                    raise ValueError("supersession cycle detected")
                seen.add(next_id)
                found_next = self._find_record(next_id)
                if found_next is None:
                    break
                cursor = found_next[1]
        elif superseded_by is not None:
            raise ValueError("superseded_by is only valid with lifecycle='superseded'")

        new_category = (
            validate_category_shape(category)
            if category is not None
            else str(record.get("category") or "")
        )
        old_content = str(record.get("content") or "")
        old_source = str(record.get("source") or "")
        old_category = str(record.get("category") or "")
        record["content"] = content
        record["category"] = new_category
        if (content != old_content or new_category != old_category
                or (source is not None and source != old_source) or status is not None):
            record["updated_at"] = _now_iso()
        for key, value in (("status", status), ("source", source)):
            if value == "":
                record.pop(key, None)
            elif value:
                record[key] = value
        if lifecycle is not None:
            record["lifecycle"] = lifecycle
            record["lifecycle_updated_at"] = _now_iso()
            if lifecycle == "superseded":
                record["superseded_by"] = superseded_by
            else:
                record.pop("superseded_by", None)
        if verification is not None:
            record["verification"] = verification
            record["verification_updated_at"] = _now_iso()
        if checked_evidence_refs is not None:
            if checked_evidence_refs:
                record["evidence_refs"] = checked_evidence_refs
            else:
                record.pop("evidence_refs", None)

        lock = self._acquire_lock()
        try:
            new_path = self._path_for_record(record)
            new_path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(new_path, self._serialize(record))
            if old_path.resolve() != new_path.resolve():
                old_path.unlink(missing_ok=True)
        finally:
            self._release_lock(lock)

        git = self._git_commit(
            [old_path, new_path],
            f"memory: {self.agent_id} update {memory_id[:12]}",
        )
        result = decorate_record(record)
        result["ok"] = True
        result["git"] = git
        index_metadata_changed = any(value is not None for value in (status, source, lifecycle, superseded_by, verification))
        if content != old_content or new_category != old_category or index_metadata_changed:
            self._index_upsert(record, update_vector=(content != old_content or (source is not None and source != old_source)))
        changed_fields = [name for name, supplied in (
            ("content", content != old_content), ("category", new_category != old_category),
            ("status", status is not None), ("source", source is not None),
            ("lifecycle", lifecycle is not None), ("superseded_by", superseded_by is not None),
            ("verification", verification is not None), ("evidence_refs", evidence_refs is not None),
        ) if supplied]
        self._attach_receipt(result, operation=_operation, target_id=memory_id,
                             changed_fields=changed_fields, started_at=receipt_started_at)
        return result

    def forget(self, memory_id: str) -> dict[str, Any]:
        receipt_started_at = _now_iso()
        memory_id = _safe_memory_id(memory_id)
        found = self._find_record(memory_id)
        if found is None:
            raise KeyError(f"memory not found: {memory_id}")

        path, record = found
        if record.get("owner") != self.agent_id:
            raise PermissionError("cannot delete a memory owned by another identity")

        lock = self._acquire_lock()
        try:
            changed_paths = []
            for candidate_path in self._iter_memory_paths():
                if candidate_path.resolve() == path.resolve():
                    continue
                try:
                    candidate = self._parse(_read_memory_text(candidate_path))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if not candidate or candidate.get("owner") != self.agent_id:
                    continue
                if candidate.get("superseded_by") != memory_id:
                    continue
                candidate["lifecycle"] = "review_needed"
                candidate["lifecycle_updated_at"] = _now_iso()
                candidate.pop("superseded_by", None)
                self._atomic_write(candidate_path, self._serialize(candidate))
                changed_paths.append(candidate_path)
            path.unlink(missing_ok=False)
        finally:
            self._release_lock(lock)

        git = self._git_commit(
            [path, *changed_paths],
            f"memory: {self.agent_id} forget {memory_id[:12]}",
        )
        result = {
            "ok": True,
            "id": memory_id,
            "owner": self.agent_id,
            "category": record.get("category"),
            "location": decorate_record(record).get("location"),
            "git": git,
        }
        self._index_delete(memory_id)
        for changed_path in changed_paths:
            changed = self._parse(_read_memory_text(changed_path))
            if changed:
                self._index_upsert(changed, update_vector=False)
        self._attach_receipt(result, operation="forget", target_id=memory_id,
                             changed_fields=["memory"], started_at=receipt_started_at)
        return result

    def import_record(self, record: dict[str, Any]) -> Path:
        """Migration-only helper. Preserves id/owner/scope/timestamps."""
        clean = {
            "id": _safe_memory_id(str(record["id"])),
            "owner": _safe_agent(str(record["owner"])),
            "scope": str(record.get("scope") or "agent").strip().lower(),
            "category": validate_category_shape(str(record["category"])),
            "created_at": str(record.get("created_at") or _now_iso()),
            "updated_at": str(record.get("updated_at") or record.get("created_at") or _now_iso()),
            "content": self._clean_content(str(record.get("content") or "")),
        }
        if clean["scope"] not in {"agent", "shared"}:
            raise ValueError(f"invalid imported scope: {clean['scope']!r}")
        for key in ("status", "source"):
            if record.get(key):
                clean[key] = str(record[key])
        if record.get("lifecycle"):
            clean["lifecycle"] = _check_lifecycle(str(record["lifecycle"]))
        if record.get("superseded_by"):
            clean["superseded_by"] = _safe_memory_id(str(record["superseded_by"]))
        if record.get("lifecycle_updated_at"):
            clean["lifecycle_updated_at"] = str(record["lifecycle_updated_at"])
        if record.get("verification"):
            clean["verification"] = _check_verification(str(record["verification"]))
        if record.get("verification_updated_at"):
            clean["verification_updated_at"] = str(record["verification_updated_at"])
        if record.get("evidence_refs") is not None:
            clean["evidence_refs"] = self._validate_evidence_refs(record["evidence_refs"])
        receipt_started_at = _now_iso()
        path = self._write_record(clean)
        self._index_upsert(clean)
        receipt_result: dict[str, Any] = {}
        self._attach_receipt(receipt_result, operation="import_memory", target_id=str(clean["id"]),
                             changed_fields=["memory"], started_at=receipt_started_at)
        return path

    def _index_upsert(self, record: dict[str, Any], *, update_vector: bool = True) -> None:
        index = None
        try:
            from memory_search import MemorySearchIndex

            index = MemorySearchIndex(self.project_root)
            index.upsert(record)
        except Exception as exc:
            try:
                if index is not None and index.db_path.is_file():
                    index.mark_dirty(type(exc).__name__)
            except Exception:
                pass
            log.warning("memory saved but search index update failed: %s", type(exc).__name__)
        if not update_vector:
            return
        vector = None
        try:
            from memory_vectors import MemoryVectorIndex

            vector = MemoryVectorIndex(self.project_root)
            if vector.db_path.is_file():
                vector.upsert(record)
        except Exception as exc:
            try:
                if vector is not None and vector.db_path.is_file():
                    vector.mark_dirty(type(exc).__name__)
            except Exception:
                pass
            log.warning("memory saved but vector index update failed: %s", type(exc).__name__)

    def _index_delete(self, memory_id: str) -> None:
        index = None
        try:
            from memory_search import MemorySearchIndex

            index = MemorySearchIndex(self.project_root)
            index.delete(memory_id)
        except Exception as exc:
            try:
                if index is not None and index.db_path.is_file():
                    index.mark_dirty(type(exc).__name__)
            except Exception:
                pass
            log.warning("memory deleted but search index update failed: %s", type(exc).__name__)
        vector = None
        try:
            from memory_vectors import MemoryVectorIndex

            vector = MemoryVectorIndex(self.project_root)
            if vector.db_path.is_file():
                vector.delete(memory_id)
        except Exception as exc:
            try:
                if vector is not None and vector.db_path.is_file():
                    vector.mark_dirty(type(exc).__name__)
            except Exception:
                pass
            log.warning("memory deleted but vector index update failed: %s", type(exc).__name__)

    # ---------- record I/O ----------

    def _iter_memory_paths(self):
        for path in self.memory_root.rglob("*.md"):
            rel = path.relative_to(self.memory_root)
            if not rel.parts:
                continue
            if rel.parts[0].startswith("_"):
                continue
            if path.name.startswith("_"):
                continue
            yield path

    def _read_all(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in self._iter_memory_paths():
            try:
                record = self._parse(_read_memory_text(path))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not record:
                continue
            items.append(decorate_record(record))

        # Human-maintained house documents remain readable as manuals.
        house = self.memory_root / "_house"
        if house.exists():
            for path in sorted(house.glob("*.md")):
                try:
                    stat = path.stat()
                    content = _read_memory_text(path)
                except OSError:
                    continue
                stamp = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
                items.append(
                    decorate_record(
                        {
                            "id": f"human:_house/{path.name}",
                            "owner": "human",
                            "scope": "human",
                            "category": "manual",
                            "created_at": stamp,
                            "updated_at": stamp,
                            "content": content,
                        }
                    )
                )
        return items

    def _filtered(self, owner: str, include_inactive: bool = False) -> list[dict[str, Any]]:
        owner = (owner or "all").strip().lower()
        items = self._read_all()
        for item in items:
            item["lifecycle"] = _lifecycle(item)
            item["verification"] = _verification(item)
            item["evidence_refs"] = _evidence_refs(item)
        if not include_inactive:
            items = [item for item in items if _lifecycle(item) in {"active", "review_needed"}]
        if owner == "all":
            return items
        if owner == "shared":
            return [x for x in items if str(x.get("scope") or "").lower() == "shared"]
        if owner == "human":
            return [x for x in items if str(x.get("owner") or "").lower() == "human"]
        return [x for x in items if str(x.get("owner") or "").lower() == owner]

    def _find_record(self, memory_id: str) -> tuple[Path, dict[str, Any]] | None:
        for path in self._iter_memory_paths():
            try:
                record = self._parse(_read_memory_text(path))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if record and str(record.get("id")) == memory_id:
                return path, record
        return None

    def _path_for_record(self, record: dict[str, Any]) -> Path:
        category = validate_category_shape(str(record["category"]))
        owner = _safe_agent(str(record["owner"]))
        scope = str(record["scope"]).strip().lower()
        memory_id = _safe_memory_id(str(record["id"]))
        date = _date_from_iso(str(record.get("created_at") or ""))
        filename = f"{date}_{memory_id}__{owner}__{scope}.md"
        return room_dir(self.memory_root, category) / filename

    def _write_record(self, record: dict[str, Any]) -> Path:
        path = self._path_for_record(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(f"memory file already exists: {path}")
        self._atomic_write(path, self._serialize(record))
        return path

    @staticmethod
    def _serialize(record: dict[str, Any]) -> str:
        lines = ["---"]
        for key in _FRONTMATTER_KEYS:
            lines.append(f"{key}: {json.dumps(str(record.get(key) or ''), ensure_ascii=False)}")
        for key in _OPTIONAL_KEYS:
            if record.get(key):
                value = record[key] if key == "evidence_refs" else str(record[key])
                lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        lines.extend(["---", "", str(record.get("content") or "").strip(), ""])
        return "\n".join(lines)

    @staticmethod
    def _parse(text: str) -> dict[str, Any] | None:
        if not text.startswith("---\n"):
            return None
        end = text.find("\n---\n", 4)
        if end < 0:
            return None
        header = text[4:end]
        content = text[end + 5 :].lstrip("\n").rstrip()
        meta: dict[str, Any] = {}
        for line in header.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            if key not in _FRONTMATTER_KEYS and key not in _OPTIONAL_KEYS:
                continue
            loaded = json.loads(value.strip())
            if key == "evidence_refs":
                if not isinstance(loaded, list):
                    raise ValueError("evidence_refs must be a list")
                meta[key] = loaded
            else:
                meta[key] = str(loaded)
        if any(not meta.get(k) for k in _FRONTMATTER_KEYS):
            raise ValueError("incomplete memory frontmatter")
        for key, allowed in (("status", VALID_STATUS), ("source", VALID_SOURCE), ("lifecycle", VALID_LIFECYCLE), ("verification", VALID_VERIFICATION)):
            if meta.get(key) and meta[key] not in allowed:
                meta.pop(key)
        meta["content"] = content
        return meta

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{secrets.token_hex(3)}")
        try:
            tmp.write_text(text, encoding="utf-8")
            _with_sharing_retry(lambda: os.replace(tmp, path))
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                log.warning("could not remove temporary memory file %s", tmp.name)
            raise

    @staticmethod
    def _clean_content(content: str) -> str:
        content = (content or "").strip()
        if not content:
            raise ValueError("memory content is empty")
        if len(content) > 20000:
            raise ValueError("memory content is too long")
        return content

    @staticmethod
    def _safe_limit(limit: int) -> int:
        try:
            value = int(limit)
        except Exception:
            value = 10
        return max(1, min(value, 1000))

    @staticmethod
    def _sort_key(item: dict[str, Any]) -> str:
        return str(item.get("updated_at") or item.get("created_at") or "")

    # ---------- cross-process write lock ----------

    def _acquire_lock(self, timeout: float = 8.0) -> Path:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    if time.time() - self.lock_path.stat().st_mtime > 30:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                except PermissionError:
                    # Windows: the lock file is mid-delete by its previous holder.
                    if not _RETRY_SHARING_VIOLATIONS:
                        raise
            except PermissionError:
                # Windows reports creating a lock file whose previous holder's
                # unlink is still pending as access denied, not FileExistsError.
                # That is "busy": keep waiting within the same bounded deadline.
                if not _RETRY_SHARING_VIOLATIONS:
                    raise
            else:
                os.write(fd, f"{os.getpid()} {time.time()}".encode("ascii"))
                os.close(fd)
                return self.lock_path
            if time.monotonic() >= deadline:
                raise TimeoutError("memory store is busy; retry shortly")
            time.sleep(0.05)

    @staticmethod
    def _release_lock(lock: Path) -> None:
        lock.unlink(missing_ok=True)

    @contextmanager
    def _write_lock(self):
        """Compatibility context for derived stores sharing this project lock."""
        lock = self._acquire_lock()
        try:
            yield
        finally:
            self._release_lock(lock)

    # ---------- git ----------

    def _git_commit(self, paths: list[Path], message: str) -> str:
        git_dir = self.project_root / ".git"
        if not git_dir.exists():
            return "skipped-no-git"

        rels: list[str] = []
        for path in paths:
            try:
                rels.append(str(path.resolve().relative_to(self.project_root)))
            except Exception:
                continue
        rels = sorted(set(rels))
        if not rels:
            return "skipped"

        try:
            add = subprocess.run(
                ["git", "add", "-A", "--", *rels],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if add.returncode != 0:
                return "error-add"

            diff = subprocess.run(
                ["git", "diff", "--cached", "--quiet", "--", *rels],
                cwd=self.project_root,
                check=False,
            )
            if diff.returncode == 0:
                return "clean"

            commit = subprocess.run(
                ["git", "commit", "-m", message, "--", *rels],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                check=False,
            )
            return "committed" if commit.returncode == 0 else "error-commit"
        except OSError:
            return "unavailable"
