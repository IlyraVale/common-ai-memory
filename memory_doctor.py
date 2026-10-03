from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from category_policy import CategoryPolicy
from archive_store import ArchiveStore
from execution_receipts import ExecutionReceiptStore
from memory_store import (
    MemoryStore,
    PASSIVE_CONTEXT_BUDGET,
    PASSIVE_MAX_RESULTS,
    VALID_SOURCE,
    VALID_STATUS,
    VALID_LIFECYCLE,
    VALID_VERIFICATION,
    _CJK_RUN_RE,
    _FRONTMATTER_KEYS,
    _WORD_RE,
    _read_memory_text,
    passive_candidate_allowed,
    passive_query_signal,
)
from memory_search import MemorySearchIndex, rebuild_project
from memory_vectors import MemoryVectorIndex, rebuild_project as rebuild_vectors
from config import data_root, load_dotenv


_OWNER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_GENERIC = {
    "this", "that", "with", "from", "have", "memory", "project", "today",
    "一个", "这个", "那个", "可以", "已经", "我们", "他们", "事情", "时候",
}


def _raw_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise ValueError("malformed_frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("malformed_frontmatter")
    meta: dict[str, Any] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            raise ValueError("malformed_frontmatter")
        key, raw = line.split(":", 1)
        key = key.strip()
        if key in _FRONTMATTER_KEYS or key in {
            "status", "source", "lifecycle", "superseded_by", "lifecycle_updated_at",
            "verification", "verification_updated_at",
            "evidence_refs",
        }:
            try:
                loaded = json.loads(raw.strip())
                meta[key] = loaded if key == "evidence_refs" else str(loaded)
            except (ValueError, json.JSONDecodeError) as exc:
                raise ValueError("malformed_frontmatter") from exc
    return meta, text[end + 5 :].lstrip("\n").rstrip()


def _probe_candidates(content: str) -> list[str]:
    candidates: set[str] = set()
    for word in _WORD_RE.findall(content.lower()):
        if len(word) >= 4 and word not in _GENERIC:
            candidates.add(word[:24])
    for run in _CJK_RUN_RE.findall(content):
        for size in (4, 3, 2):
            if len(run) >= size:
                candidates.update(run[index : index + size] for index in range(len(run) - size + 1))
    return sorted((value for value in candidates if value not in _GENERIC), key=lambda x: (-len(x), x))


def inspect_memory(project_root: str | Path, *, result_limit: int = 50) -> dict[str, Any]:
    root = Path(project_root).resolve()
    memory_root = root / "memory"
    if not memory_root.is_dir():
        return {
            "files": 0, "parsed": 0, "parse_errors": 0, "duplicate_ids": 0,
            "searchable": 0, "untestable": 0, "search_failures": 0,
            "warnings": 0, "errors": [], "warning_details": [], "result": "PASS",
            "memory": "PASS",
            "lifecycle": {"active": 0, "review_needed": 0, "stale": 0,
                          "superseded": 0, "legacy_missing": 0},
            "verification": {"unknown": 0, "unverified": 0, "confirmed": 0,
                             "partial": 0, "not_applicable": 0, "legacy_missing": 0},
            "evidence_refs": {"total": 0, "legacy_missing": 0, "confirmed_without_evidence": 0},
            "execution_receipts": ExecutionReceiptStore(root).diagnostics(),
            "search_index": {"status": "STALE", "reason": "missing", "indexed": 0,
                             "missing": [], "orphan": [], "hash_mismatch": []},
            "vector_index": {"status": "UNAVAILABLE", "reason": "missing", "indexed": 0,
                             "missing": [], "orphan": [], "hash_mismatch": []},
        }
    store = MemoryStore(root, "doctor")
    policy = CategoryPolicy(root)
    allowed = set(policy.allowed_categories())
    paths = sorted(store._iter_memory_paths())
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    parsed: list[dict[str, Any]] = []
    ids: Counter[str] = Counter()

    for path in paths:
        rel = str(path.relative_to(memory_root))
        try:
            text = _read_memory_text(path)
            raw, _ = _raw_frontmatter(text)
            missing = [key for key in _FRONTMATTER_KEYS if not raw.get(key)]
            if missing:
                raise ValueError("missing_required_fields:" + ",".join(missing))
            record = store._parse(text)
            if record is None:
                raise ValueError("silent_read_skip")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append({"memory_id": rel, "error_type": str(exc) or type(exc).__name__})
            continue

        memory_id = str(raw["id"])
        ids[memory_id] += 1
        category = str(raw["category"])
        if category not in allowed:
            errors.append({"memory_id": memory_id, "error_type": "invalid_category"})
        owner = str(raw["owner"]).lower()
        if not _OWNER_RE.fullmatch(owner):
            errors.append({"memory_id": memory_id, "error_type": "invalid_owner"})
        if raw.get("scope") not in {"agent", "shared"}:
            errors.append({"memory_id": memory_id, "error_type": "invalid_scope"})
        if raw.get("status") and raw["status"] not in VALID_STATUS:
            errors.append({"memory_id": memory_id, "error_type": "invalid_status"})
        if raw.get("source") and raw["source"] not in VALID_SOURCE:
            errors.append({"memory_id": memory_id, "error_type": "invalid_source"})
        if raw.get("lifecycle") and raw["lifecycle"] not in VALID_LIFECYCLE:
            errors.append({"memory_id": memory_id, "error_type": "invalid_lifecycle"})
        if raw.get("verification") and raw["verification"] not in VALID_VERIFICATION:
            errors.append({"memory_id": memory_id, "error_type": "invalid_verification"})
        if raw.get("verification_updated_at"):
            try:
                stamp = datetime.fromisoformat(raw["verification_updated_at"].replace("Z", "+00:00"))
                if stamp.utcoffset() is None:
                    raise ValueError("timezone required")
            except ValueError:
                errors.append({"memory_id": memory_id, "error_type": "invalid_verification_updated_at"})
        for key in ("status", "source"):
            if not raw.get(key):
                warnings.append({"memory_id": memory_id, "warning_type": f"legacy_missing_{key}"})
        parsed.append({**record, "path": str(path), "raw": raw})

    records_by_id = {str(item["id"]): item for item in parsed}
    lifecycle_counts = Counter(
        str(item.get("lifecycle") or "active") for item in parsed
    )
    legacy_missing_lifecycle = sum(1 for item in parsed if not item["raw"].get("lifecycle"))
    verification_counts = Counter(
        str(item.get("verification") or "unknown") for item in parsed
    )
    legacy_missing_verification = sum(
        1 for item in parsed if not item["raw"].get("verification")
    )
    receipt_store = ExecutionReceiptStore(root)
    archive_store = ArchiveStore(root / "archive")
    evidence_total = 0
    legacy_missing_evidence = 0
    confirmed_without_evidence = 0
    for item in parsed:
        memory_id = str(item["id"])
        raw_refs = item["raw"].get("evidence_refs")
        if raw_refs is None:
            legacy_missing_evidence += 1
            refs: list[Any] = []
        elif not isinstance(raw_refs, list):
            errors.append({"memory_id": memory_id, "error_type": "malformed_evidence_refs"})
            refs = []
        else:
            refs = raw_refs
        if len(refs) > 32:
            errors.append({"memory_id": memory_id, "error_type": "excessive_evidence_refs"})
        normalized = [ref for ref in refs if isinstance(ref, str)]
        if len(normalized) != len(refs):
            errors.append({"memory_id": memory_id, "error_type": "malformed_evidence_ref"})
        if len(normalized) != len(set(normalized)):
            errors.append({"memory_id": memory_id, "error_type": "duplicate_evidence_ref"})
        for ref in normalized:
            receipt_match = re.fullmatch(r"receipt:([0-9a-f]{32})", ref)
            archive_match = re.fullmatch(r"archive:([0-9a-f]{32})", ref)
            if receipt_match:
                state = receipt_store.reference_status(receipt_match.group(1), owner=str(item["owner"]))
                if state == "missing":
                    errors.append({"memory_id": memory_id, "error_type": "dangling_receipt_ref"})
                elif state == "cross_owner":
                    errors.append({"memory_id": memory_id, "error_type": "cross_owner_receipt_ref"})
            elif archive_match:
                state = archive_store.evidence_status(archive_match.group(1), owner=str(item["owner"]))
                if state == "missing":
                    errors.append({"memory_id": memory_id, "error_type": "dangling_archive_ref"})
                elif state == "unowned":
                    errors.append({"memory_id": memory_id, "error_type": "legacy_unowned_archive_ref"})
                elif state == "cross_owner":
                    errors.append({"memory_id": memory_id, "error_type": "cross_owner_archive_ref"})
            else:
                errors.append({"memory_id": memory_id, "error_type": "unsupported_evidence_ref"})
        evidence_total += len(normalized)
        if str(item.get("verification") or "unknown") == "confirmed" and not normalized:
            confirmed_without_evidence += 1
    for item in parsed:
        memory_id = str(item["id"])
        lifecycle = str(item.get("lifecycle") or "active")
        target_id = str(item.get("superseded_by") or "")
        if lifecycle == "superseded" and not target_id:
            errors.append({"memory_id": memory_id, "error_type": "superseded_without_target"})
        if lifecycle != "superseded" and target_id:
            errors.append({"memory_id": memory_id, "error_type": "unexpected_superseded_by"})
        if not target_id:
            continue
        if target_id == memory_id:
            errors.append({"memory_id": memory_id, "error_type": "supersession_self_reference"})
            continue
        target = records_by_id.get(target_id)
        if target is None:
            errors.append({"memory_id": memory_id, "error_type": "supersession_target_missing"})
        elif target.get("owner") != item.get("owner"):
            errors.append({"memory_id": memory_id, "error_type": "supersession_cross_owner"})

    cycle_members: set[str] = set()
    for item in parsed:
        origin = str(item["id"])
        cursor = origin
        path_seen: list[str] = []
        while cursor in records_by_id:
            if cursor in path_seen:
                cycle_members.update(path_seen[path_seen.index(cursor):])
                break
            path_seen.append(cursor)
            cursor = str(records_by_id[cursor].get("superseded_by") or "")
            if not cursor:
                break
    for memory_id in sorted(cycle_members):
        errors.append({"memory_id": memory_id, "error_type": "supersession_cycle"})

    duplicate_ids = sorted(memory_id for memory_id, count in ids.items() if count > 1)
    for memory_id in duplicate_ids:
        errors.append({"memory_id": memory_id, "error_type": "duplicate_id"})

    doc_frequency: Counter[str] = Counter()
    candidate_map: dict[str, list[str]] = {}
    for item in parsed:
        candidates = _probe_candidates(str(item.get("content") or ""))
        candidate_map[str(item["id"])] = candidates
        doc_frequency.update(set(candidates))

    searchable = 0
    untestable = 0
    search_failures = 0
    recall_duplicate_ids: set[str] = set()
    owner_filter_failures: set[str] = set()

    def fallback_results(probe: str, owner: str) -> list[dict[str, Any]]:
        ranked = store._rank(probe, store._filtered(owner=owner, include_inactive=True))
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for row in ranked:
            key = str(row.get("id") or row.get("path") or "")
            if key in seen:
                continue
            seen.add(key)
            result.append(row)
            if len(result) >= result_limit:
                break
        return result

    for item in parsed:
        memory_id = str(item["id"])
        candidates = candidate_map[memory_id]
        if not candidates:
            untestable += 1
            continue
        probe = min(candidates, key=lambda value: (doc_frequency[value], -len(value), value))
        results = fallback_results(probe, "all")
        result_ids = [str(row.get("id")) for row in results]
        if len(result_ids) != len(set(result_ids)):
            recall_duplicate_ids.add(memory_id)
        if memory_id not in result_ids:
            search_failures += 1
            errors.append({"memory_id": memory_id, "error_type": "lexical_probe_not_found"})
            continue
        owner_results = fallback_results(probe, str(item["owner"]))
        if memory_id not in {str(row.get("id")) for row in owner_results}:
            owner_filter_failures.add(memory_id)
            errors.append({"memory_id": memory_id, "error_type": "owner_filter_mismatch"})
            continue
        searchable += 1

    for memory_id in sorted(recall_duplicate_ids):
        errors.append({"memory_id": memory_id, "error_type": "duplicate_recall_id"})

    parse_errors = sum(
        1 for error in errors
        if error["error_type"].startswith(("malformed_", "missing_", "silent_"))
    )
    report = {
        "files": len(paths),
        "parsed": len(parsed),
        "parse_errors": parse_errors,
        "duplicate_ids": len(duplicate_ids),
        "searchable": searchable,
        "untestable": untestable,
        "search_failures": search_failures,
        "warnings": len(warnings),
        "errors": errors,
        "warning_details": warnings,
        "lifecycle": {
            "active": lifecycle_counts.get("active", 0),
            "review_needed": lifecycle_counts.get("review_needed", 0),
            "stale": lifecycle_counts.get("stale", 0),
            "superseded": lifecycle_counts.get("superseded", 0),
            "legacy_missing": legacy_missing_lifecycle,
        },
        "verification": {
            "unknown": verification_counts.get("unknown", 0),
            "unverified": verification_counts.get("unverified", 0),
            "confirmed": verification_counts.get("confirmed", 0),
            "partial": verification_counts.get("partial", 0),
            "not_applicable": verification_counts.get("not_applicable", 0),
            "legacy_missing": legacy_missing_verification,
        },
        "evidence_refs": {
            "total": evidence_total,
            "legacy_missing": legacy_missing_evidence,
            "confirmed_without_evidence": confirmed_without_evidence,
        },
        "result": "PASS" if not errors else "FAIL",
    }
    report["memory"] = report["result"]
    report["search_index"] = MemorySearchIndex(root).diagnostics(parsed)
    vector = MemoryVectorIndex(root).diagnostics(
        parsed, load_backend=importlib.util.find_spec("fastembed") is not None
    )
    if importlib.util.find_spec("fastembed") is None:
        vector["status"] = "UNAVAILABLE"
        vector["reason"] = "optional_dependency_missing"
    report["vector_index"] = vector
    positive_signal = passive_query_signal("看到复杂语法规则我就不想学了")
    negative_signal = passive_query_signal("哈哈哈哈")
    report["passive_recall"] = {
        "mode": "model_initiated",
        "hybrid": "available" if vector.get("status") == "PASS" else "fallback",
        "positive_probe": "PASS" if positive_signal["eligible"] and passive_candidate_allowed(
            positive_signal, exact=False, lexical_hits=0, lexical_coverage=0.0,
            similarity=0.62, is_semantic_top=True, semantic_margin=0.20,
        ) else "FAIL",
        "negative_probe": "PASS" if not negative_signal["eligible"] else "FAIL",
        "max_results": PASSIVE_MAX_RESULTS,
        "context_budget": PASSIVE_CONTEXT_BUDGET,
    }
    report["execution_receipts"] = receipt_store.diagnostics()
    report["archive"] = archive_store.diagnostics()
    return report


def _print_report(report: dict[str, Any]) -> None:
    print("Memory Doctor")
    print("-------------")
    for key in (
        "files", "parsed", "parse_errors", "duplicate_ids", "searchable",
        "untestable", "search_failures", "warnings",
    ):
        print(f"{key}: {report[key]}")
    for error in report["errors"]:
        print(f"{error['memory_id']}: {error['error_type']}")
    search = report.get("search_index", {})
    vector = report.get("vector_index", {})
    print(f"memory: {report.get('memory', report['result'])}")
    lifecycle = report.get("lifecycle", {})
    print(
        "lifecycle: "
        + " ".join(f"{key}={lifecycle.get(key, 0)}" for key in (
            "active", "review_needed", "stale", "superseded", "legacy_missing"
        ))
    )
    verification = report.get("verification", {})
    print(
        "verification: "
        + " ".join(f"{key}={verification.get(key, 0)}" for key in (
            "unknown", "unverified", "confirmed", "partial", "not_applicable",
            "legacy_missing",
        ))
    )
    evidence = report.get("evidence_refs", {})
    print("evidence_refs: " + " ".join(
        f"{key}={evidence.get(key, 0)}"
        for key in ("total", "legacy_missing", "confirmed_without_evidence")
    ))
    receipts = report.get("execution_receipts", {})
    print(f"execution_receipts: {receipts.get('status', 'UNKNOWN')} rows={receipts.get('rows', 0)}")
    archive = report.get("archive", {})
    print("archive: " + " ".join(
        f"{key}={archive.get(key, 0)}" for key in ("owned", "legacy_unowned", "invalid_owner")
    ))
    print(f"search_index: {search.get('status', 'UNKNOWN')}")
    if search.get("status") == "STALE":
        print("rebuild: python memory_search.py rebuild")
    print(f"vector_index: {vector.get('status', 'UNKNOWN')}")
    if vector.get("status") == "STALE":
        print("rebuild vectors: python memory_vectors.py rebuild")
    elif vector.get("status") == "UNAVAILABLE":
        print("semantic retrieval unavailable; BM25/lexical fallback is usable")
    passive = report.get("passive_recall", {})
    print(
        "passive_recall: "
        f"mode={passive.get('mode', 'unavailable')} hybrid={passive.get('hybrid', 'fallback')} "
        f"positive={passive.get('positive_probe', 'UNKNOWN')} "
        f"negative={passive.get('negative_probe', 'UNKNOWN')} "
        f"max_results={passive.get('max_results', PASSIVE_MAX_RESULTS)}"
    )
    print()
    print(f"result: {report['result']}")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Read-only lexical retrieval integrity doctor")
    parser.add_argument("--root", default=str(data_root()))
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--rebuild-search-index", action="store_true")
    parser.add_argument("--rebuild-vector-index", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.rebuild_search_index:
            rebuild_project(args.root)
        if args.rebuild_vector_index:
            rebuild_vectors(args.root)
        report = inspect_memory(args.root)
    except Exception as exc:
        if args.as_json:
            print(json.dumps({"result": "ERROR", "error_type": type(exc).__name__}))
        else:
            print(f"Memory Doctor error: {type(exc).__name__}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        _print_report(report)
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

