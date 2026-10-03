"""Read-only duplicate / overlap scanner for ordinary memories.

It only analyses and suggests: it never writes memory, FTS, vectors or receipts.
Embeddings are reused from the existing vector index when it is current for a
memory; nothing is re-embedded. Without usable vectors the scanner falls back to
exact and lexical signals and reports semantic analysis as unavailable.

Thresholds were calibrated on synthetic Chinese and English pairs with the local
paraphrase-multilingual-MiniLM-L12-v2 model. Conflicting statements ("meeting on
Wednesday" vs "on Thursday") embed as close as paraphrases, so a high cosine is
never enough on its own: differing values or polarity are checked first.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import struct
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from memory_store import MemoryStore, _match_units
from config import data_root, load_dotenv

RELATIONS = ("exact_duplicate", "likely_duplicate", "update_of", "overlap", "conflict", "uncertain")
ACTIVE_LIFECYCLES = frozenset({"active", "review_needed"})
MAX_PAIRWISE_MEMORIES = 3000
PREVIEW_CHARS = 120


@dataclass(frozen=True)
class Thresholds:
    likely_duplicate_cosine: float = 0.90
    likely_duplicate_length_ratio: float = 0.60
    topic_cosine: float = 0.75
    topic_lexical_jaccard: float = 0.50
    topic_lexical_min_cosine: float = 0.60
    topic_containment: float = 0.75
    topic_containment_min_cosine: float = 0.45
    topic_change_min_cosine: float = 0.65
    topic_change_min_containment: float = 0.40
    overlap_containment: float = 0.80
    overlap_max_length_ratio: float = 0.60
    overlap_min_cosine: float = 0.60
    uncertain_cosine: float = 0.85
    # Lexical-only mode (no usable vectors): stricter, since there is no semantic check.
    lexical_duplicate_jaccard: float = 0.80
    lexical_duplicate_length_ratio: float = 0.80
    lexical_overlap_containment: float = 0.90
    min_units_for_overlap: int = 6
    # A negation only flips meaning when the rest of the wording matches closely.
    polarity_min_jaccard: float = 0.50


DEFAULT_THRESHOLDS = Thresholds()

# Dates and times say *when* something was noted, not *what* the value is.
_DATE_RE = re.compile(
    r"\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?|\d{1,2}月\d{1,2}[日号]|\d{1,2}:\d{2}(?::\d{2})?",
    re.IGNORECASE,
)
# Identifiers mixing letters and digits (game ids, hashes, builds) name distinct things.
_IDENT_RE = re.compile(
    r"\b(?=[a-z0-9-]*\d)(?=[a-z0-9-]*[a-z])[a-z0-9]+(?:-[a-z0-9]+)+\b|\b[0-9a-f]{8,}\b",
    re.IGNORECASE,
)
_CJK_NUM = "零〇一二两三四五六七八九十百千万半"
_VALUE_RE = re.compile(
    r"\d+(?:[.,]\d+)?|"
    r"(?:周|星期|礼拜)[一二三四五六日天]|"
    r"[" + _CJK_NUM + r"]+(?=[点元块天周月年次个岁号小时分钟遍份倍])|"
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|one|two|three|four|five|six|seven|"
    r"eight|nine|ten|eleven|twelve|half)\b",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(r"[不没未别无非]|\b(?:not|no|never|none|cannot)\b|n't", re.IGNORECASE)
_CHANGE_RE = re.compile(
    r"改为|改成|改到|换成|换到|调整为|调整到|后来|现在|目前|如今|不再|已经不|搬到|升级为|取消了|"
    r"\b(?:changed to|change to|now|no longer|instead|switched|moved to|updated to|from now on)\b",
    re.IGNORECASE,
)


def normalize_text(text: str) -> str:
    """Deterministic normalization for exact matching only; never rewrites meaning."""
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    kept = "".join(" " if unicodedata.category(ch).startswith("P") else ch for ch in folded)
    return " ".join(kept.split())


def normalized_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def _dates(text: str) -> frozenset[str]:
    return frozenset(match.group(0).lower() for match in _DATE_RE.finditer(text))


def _identifiers(text: str) -> frozenset[str]:
    return frozenset(match.group(0).lower() for match in _IDENT_RE.finditer(text))


def _values(text: str) -> frozenset[str]:
    stripped = _IDENT_RE.sub(" ", _DATE_RE.sub(" ", text))
    return frozenset(match.group(0).lower() for match in _VALUE_RE.finditer(stripped))


def _negations(text: str) -> int:
    return len(_NEGATION_RE.findall(text))


def lexical_signals(a: str, b: str) -> dict[str, float]:
    ua, ub = _match_units(normalize_text(a)), _match_units(normalize_text(b))
    inter = len(ua & ub)
    return {
        "jaccard": round(inter / max(1, len(ua | ub)), 3),
        "containment": round(inter / max(1, min(len(ua), len(ub))), 3),
        "length_ratio": round(min(len(a), len(b)) / max(1, max(len(a), len(b))), 3),
        "min_units": float(min(len(ua), len(ub))),
    }


def _timestamp(record: dict[str, Any]) -> str:
    return str(record.get("created_at") or record.get("updated_at") or "")


def classify_pair(
    a: dict[str, Any], b: dict[str, Any], cosine: float | None,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> dict[str, Any] | None:
    """Return a suggested relation with its signals, or None when unrelated."""
    t = thresholds
    text_a, text_b = str(a.get("content") or ""), str(b.get("content") or "")
    if normalize_text(text_a) and normalize_text(text_a) == normalize_text(text_b):
        return {"relation": "exact_duplicate", "confidence": "high", "reason": "identical after normalization",
                "signals": {"cosine": cosine, **lexical_signals(text_a, text_b)}}
    semantic = cosine is not None
    ids_a, ids_b = _identifiers(text_a), _identifiers(text_b)
    distinct_entities = bool(ids_a) and bool(ids_b) and not (ids_a & ids_b)
    dates_a, dates_b = _dates(text_a), _dates(text_b)
    dates_differ = bool(dates_a) and bool(dates_b) and not (dates_a & dates_b)
    values_a, values_b = _values(text_a), _values(text_b)
    values_differ = bool(values_a) and bool(values_b) and values_a != values_b
    lex = lexical_signals(text_a, text_b)
    polarity_differs = ((_negations(text_a) > 0) != (_negations(text_b) > 0)
                        and lex["jaccard"] >= t.polarity_min_jaccard)
    newer, older = (a, b) if _timestamp(a) >= _timestamp(b) else (b, a)
    newer_text, older_text = str(newer.get("content") or ""), str(older.get("content") or "")
    change_marker = bool(_CHANGE_RE.search(newer_text)) and not _CHANGE_RE.search(older_text)
    if semantic:
        topic = (
            cosine >= t.topic_cosine
            or (lex["jaccard"] >= t.topic_lexical_jaccard and cosine >= t.topic_lexical_min_cosine)
            or (lex["containment"] >= t.topic_containment and cosine >= t.topic_containment_min_cosine)
            or (change_marker and values_differ and cosine >= t.topic_change_min_cosine
                and lex["containment"] >= t.topic_change_min_containment)
        )
    else:
        topic = lex["jaccard"] >= t.topic_lexical_jaccard
    signals = {"cosine": None if cosine is None else round(cosine, 3), **lex,
               "values_differ": values_differ, "polarity_differs": polarity_differs,
               "change_marker_in_newer": change_marker, "dates_differ": dates_differ,
               "distinct_entities": distinct_entities}

    def result(relation: str, confidence: str, reason: str, **extra: Any) -> dict[str, Any]:
        return {"relation": relation, "confidence": confidence, "reason": reason, "signals": signals, **extra}

    if distinct_entities:
        # Same template about differently identified things (two game records, two
        # builds): neither a duplicate nor a conflict.
        return None
    if topic and (values_differ or polarity_differs or change_marker):
        if change_marker:
            confidence = "medium" if (values_differ or polarity_differs) else "low"
            return result("update_of", confidence, "same topic; the newer memory reads like a change",
                          newer_id=newer.get("id"), older_id=older.get("id"))
        if dates_differ:
            return result("uncertain", "low", "same topic noted on different dates; may be a change over time")
        what = "different values" if values_differ else "opposite polarity"
        return result("conflict", "medium", f"same topic with {what}")
    contained = (lex["containment"] >= (t.overlap_containment if semantic else t.lexical_overlap_containment)
                 and lex["length_ratio"] <= t.overlap_max_length_ratio
                 and lex["min_units"] >= t.min_units_for_overlap)
    if contained and (not semantic or cosine >= t.overlap_min_cosine):
        return result("overlap", "medium", "one memory largely contains the other")
    if semantic:
        if cosine >= t.likely_duplicate_cosine and lex["length_ratio"] >= t.likely_duplicate_length_ratio:
            confidence = "high" if cosine >= 0.95 and lex["jaccard"] >= 0.25 else "medium"
            return result("likely_duplicate", confidence, "very close meaning with matching values")
        if cosine >= t.uncertain_cosine:
            return result("uncertain", "low", "semantically close but no reliable relation")
    elif lex["jaccard"] >= t.lexical_duplicate_jaccard and lex["length_ratio"] >= t.lexical_duplicate_length_ratio:
        return result("likely_duplicate", "medium", "near-identical wording (lexical only)")
    return None


def _ordinary_records(root: Path) -> list[dict[str, Any]]:
    store = MemoryStore(root, "duplicate-scanner")
    return [record for record in store._read_all() if not str(record.get("id") or "").startswith("human:")]


def load_vectors(root: Path, records: Iterable[dict[str, Any]]) -> tuple[dict[str, tuple[float, ...]], dict[str, Any]]:
    """Read existing embeddings strictly read-only; only vectors whose hash matches the record."""
    from memory_search import record_hash

    path = root / "state" / "memory-vectors.sqlite3"
    if not path.is_file():
        return {}, {"status": "unavailable", "reason": "vector_index_missing"}
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = db.execute("SELECT memory_id, content_hash, dimension, embedding FROM memory_vectors").fetchall()
        finally:
            db.close()
    except sqlite3.Error as exc:
        return {}, {"status": "unavailable", "reason": f"vector_index_unreadable:{type(exc).__name__}"}
    indexed = {str(row[0]): row for row in rows}
    vectors: dict[str, tuple[float, ...]] = {}
    stale = missing = 0
    for record in records:
        row = indexed.get(str(record["id"]))
        if row is None:
            missing += 1
            continue
        if str(row[1]) != record_hash(record):
            stale += 1
            continue
        dimension = int(row[2])
        blob = bytes(row[3])
        if len(blob) != dimension * 4:
            stale += 1
            continue
        vectors[str(record["id"])] = struct.unpack(f"<{dimension}f", blob)
    status = "ok" if vectors else "unavailable"
    return vectors, {"status": status, "reason": None if vectors else "no_current_vectors",
                     "vectors_used": len(vectors), "stale_vectors": stale, "missing_vectors": missing}


def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _member(record: dict[str, Any], include_content: bool) -> dict[str, Any]:
    member = {key: record.get(key) for key in ("id", "owner", "scope", "category", "created_at", "updated_at",
                                                "source", "status", "superseded_by")}
    member["lifecycle"] = record.get("lifecycle") or "active"
    member["verification"] = record.get("verification") or "unknown"
    member["evidence_refs"] = len(record.get("evidence_refs") or [])
    if include_content:
        member["content"] = record.get("content")
    return member


def scan(
    root: str | Path, *, owner: str | None = None, include_content: bool = False,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> dict[str, Any]:
    root = Path(root).resolve()
    started = time.monotonic()
    records = _ordinary_records(root)
    if owner:
        records = [r for r in records if str(r.get("owner")) == owner]
    if len(records) > MAX_PAIRWISE_MEMORIES:
        raise ValueError(f"pairwise scan is bounded to {MAX_PAIRWISE_MEMORIES} memories")
    by_id = {str(r["id"]): r for r in records}

    exact_started = time.monotonic()
    buckets: dict[str, list[str]] = {}
    for record in records:
        if normalize_text(record.get("content") or ""):
            buckets.setdefault(normalized_hash(record.get("content") or ""), []).append(str(record["id"]))
    exact_groups = [sorted(ids) for ids in buckets.values() if len(ids) > 1]
    exact_pairs = {frozenset((x, y)) for ids in exact_groups for x in ids for y in ids if x != y}
    exact_seconds = time.monotonic() - exact_started

    semantic_started = time.monotonic()
    vectors, semantic = load_vectors(root, records)
    linked = {frozenset((str(r["id"]), str(r.get("superseded_by")))) for r in records if r.get("superseded_by")}
    pairs: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    ids = sorted(by_id)
    for i, left in enumerate(ids):
        for right in ids[i + 1:]:
            key = frozenset((left, right))
            if key in exact_pairs or key in linked:
                continue
            a, b = by_id[left], by_id[right]
            if (a.get("lifecycle") or "active") not in ACTIVE_LIFECYCLES and \
                    (b.get("lifecycle") or "active") not in ACTIVE_LIFECYCLES:
                continue
            cosine = _cosine(vectors[left], vectors[right]) if left in vectors and right in vectors else None
            verdict = classify_pair(a, b, cosine, thresholds)
            if verdict is not None:
                pairs.append((a, b, verdict))
    semantic_seconds = time.monotonic() - semantic_started

    groups: list[dict[str, Any]] = []
    for members in exact_groups:
        records_ = [by_id[m] for m in members]
        groups.append({
            "relation": "exact_duplicate", "confidence": "high", "reason": "identical after normalization",
            "memory_ids": members, "owners": sorted({str(r.get("owner")) for r in records_}),
            "signals": {"normalized_hash": normalized_hash(records_[0].get("content") or "")[:16]},
            "members": [_member(r, include_content) for r in records_],
        })
    for a, b, verdict in pairs:
        group = {
            "relation": verdict["relation"], "confidence": verdict["confidence"], "reason": verdict["reason"],
            "memory_ids": [str(a["id"]), str(b["id"])],
            "owners": sorted({str(a.get("owner")), str(b.get("owner"))}),
            "signals": verdict["signals"], "members": [_member(a, include_content), _member(b, include_content)],
        }
        for key in ("newer_id", "older_id"):
            if key in verdict:
                group[key] = verdict[key]
        groups.append(group)
    order = {name: index for index, name in enumerate(RELATIONS)}
    groups.sort(key=lambda g: (order[g["relation"]], -(g["signals"].get("cosine") or 0)))
    for index, group in enumerate(groups, start=1):
        group["group_id"] = f"g{index:04d}"
        group["cross_owner"] = len(group["owners"]) > 1
    distribution = {name: sum(1 for g in groups if g["relation"] == name) for name in RELATIONS}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "read_only": True,
        "memory_count": len(records),
        "owner_filter": owner,
        "semantic": semantic,
        "thresholds": asdict(thresholds),
        "timings_seconds": {"exact": round(exact_seconds, 3), "semantic_and_pairwise": round(semantic_seconds, 3),
                            "total": round(time.monotonic() - started, 3)},
        "distribution": distribution,
        "groups": groups,
    }


def _summary_lines(report: dict[str, Any]) -> list[str]:
    lines = [f"memories={report['memory_count']} semantic={report['semantic']['status']} "
             f"total_seconds={report['timings_seconds']['total']}",
             "distribution: " + ", ".join(f"{k}={v}" for k, v in report["distribution"].items())]
    for group in report["groups"]:
        cosine = group["signals"].get("cosine")
        lines.append(f"{group['group_id']} {group['relation']:<16} {group['confidence']:<6} "
                     f"cos={'-' if cosine is None else cosine} owners={','.join(group['owners'])} "
                     f"ids={','.join(i[:12] for i in group['memory_ids'])}")
    return lines


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Read-only duplicate/overlap scan of ordinary memories.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("scan", help="analyse and suggest; never modifies memory")
    run.add_argument("--root", default=str(data_root()))
    run.add_argument("--owner")
    run.add_argument("--json", action="store_true", help="machine-readable output (metadata only)")
    args = parser.parse_args(argv)
    report = scan(args.root, owner=args.owner, include_content=False)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("\n".join(_summary_lines(report)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
