"""What changed: a deterministic, read-only view of system events. No model, no new fact store.

Events are assembled on demand from the stores that already own the facts: execution receipts (memory
create/update/status/supersede/forget), memory records that predate receipts, Dream commits, handoff
events, snapshots and relay runs. Nothing here is a source of truth and nothing is cached, so there is
no index to rebuild. Memory and Dream text is never included.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
KINDS = (
    "memory.created", "memory.updated", "memory.status_changed", "memory.superseded", "memory.forgotten",
    "dream.committed", "handoff.created", "handoff.updated", "handoff.closed", "snapshot.created",
    "relay.replied", "relay.blocked", "relay.failed",
)


@dataclass(frozen=True)
class ChangeEvent:
    event_id: str
    timestamp: str
    kind: str
    actor: str | None
    owner: str | None
    target_id: str | None
    category: str | None
    summary: str
    source_ref: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def _norm_time(value: str | None) -> str | None:
    """Accept a date or ISO timestamp; return a UTC ISO string comparable with stored timestamps."""
    if not value:
        return None
    text = str(value).strip()
    if len(text) == 10:
        text += "T00:00:00+00:00"
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _in_window(stamp: str, since: str | None, until: str | None) -> bool:
    value = _norm_time(stamp)
    if value is None:
        return False
    return (since is None or value >= since) and (until is None or value < until)


def _memory_index(root: Path) -> dict[str, dict[str, Any]]:
    from memory_store import MemoryStore

    try:
        return {str(r.get("id")): r for r in MemoryStore(root, "timeline")._read_all()
                if r.get("id") and not str(r.get("id")).startswith("human:")}
    except (OSError, ValueError):
        return {}


# --- adapters: each reads one existing source -------------------------------------------------------
def receipt_events(root: Path, since: str | None, until: str | None, memories: dict) -> list[ChangeEvent]:
    db = _ro(root / "state" / "execution-receipts.sqlite3")
    if db is None:
        return []
    events = []
    with closing(db):
        try:
            rows = db.execute(
                "SELECT r.receipt_id, r.owner, r.actor, r.operation, r.outcome, r.changed_fields_json, r.completed_at, "
                "t.target_id, t.target_status FROM execution_receipts r "
                "LEFT JOIN receipt_targets t ON t.receipt_id = r.receipt_id WHERE r.outcome='success'").fetchall()
        except sqlite3.Error:
            return []
    for row in rows:
        if not _in_window(row["completed_at"], since, until):
            continue
        try:
            fields = set(json.loads(row["changed_fields_json"] or "[]"))
        except (TypeError, ValueError):
            fields = set()
        op, target = row["operation"], row["target_id"]
        if op in ("remember", "import_memory"):
            kind = "memory.created"
        elif op == "forget":
            kind = "memory.forgotten"
        elif "superseded_by" in fields:
            kind = "memory.superseded"
        elif op == "memory_set_status" or fields == {"status"}:
            kind = "memory.status_changed"
        else:
            kind = "memory.updated"
        category = (memories.get(str(target)) or {}).get("category") if target else None
        verb = kind.split(".", 1)[1].replace("_", " ")
        where = f" in {category}" if category else ""
        events.append(ChangeEvent(
            event_id=f"receipt:{row['receipt_id']}", timestamp=_norm_time(row["completed_at"]), kind=kind,
            actor=row["actor"], owner=row["owner"], target_id=target, category=category,
            summary=f"{row['actor']} {verb} a memory{where}", source_ref=f"receipt:{row['receipt_id']}",
            metadata={"changed_fields": sorted(fields), "operation": op,
                      "target": row["target_status"] or "unknown"}))
    return events


def legacy_memory_events(root: Path, since: str | None, until: str | None, memories: dict,
                         covered: set[str]) -> list[ChangeEvent]:
    """memory.created for records that predate receipts (no create receipt points at them)."""
    events = []
    for memory_id, rec in memories.items():
        if memory_id in covered or not _in_window(str(rec.get("created_at") or ""), since, until):
            continue
        owner = str(rec.get("owner") or "") or None
        events.append(ChangeEvent(
            event_id=f"memory:{memory_id}", timestamp=_norm_time(str(rec["created_at"])), kind="memory.created",
            actor=owner, owner=owner, target_id=memory_id, category=rec.get("category"),
            summary=f"{owner} created a memory in {rec.get('category')}", source_ref=f"memory:{memory_id}",
            metadata={"source": "memory_record"}))
    return events


def dream_events(root: Path, since: str | None, until: str | None) -> list[ChangeEvent]:
    db = _ro(root / "state" / "dream-runtime.sqlite3")
    if db is None:
        return []
    with closing(db):
        try:
            rows = db.execute("SELECT owner, dream_date, generation_id, committed_at FROM dream_commits").fetchall()
        except sqlite3.Error:
            return []
    return [ChangeEvent(
        event_id=f"dream:{r['owner']}:{r['dream_date']}", timestamp=_norm_time(r["committed_at"]),
        kind="dream.committed", actor=r["owner"], owner=r["owner"], target_id=r["dream_date"], category=None,
        summary=f"{r['owner']} committed the Dream for {r['dream_date']}",
        source_ref=f"dream:{r['owner']}:{r['dream_date']}", metadata={"generation_id": r["generation_id"]})
        for r in rows if _in_window(r["committed_at"], since, until)]


def handoff_change_events(root: Path, since: str | None, until: str | None) -> list[ChangeEvent]:
    from handoffs import handoff_events

    return [ChangeEvent(
        event_id=f"handoff:{r['event_id']}", timestamp=_norm_time(r["at"]), kind=f"handoff.{r['kind']}",
        actor=r["owner"], owner=r["owner"], target_id=r["handoff_id"], category=None,
        summary=f"{r['owner']} {r['kind']} handoff “{r['topic']}”", source_ref=f"handoff:{r['handoff_id']}")
        for r in handoff_events(root) if _in_window(r["at"], since, until)]


def snapshot_change_events(root: Path, since: str | None, until: str | None) -> list[ChangeEvent]:
    from snapshots import snapshot_events

    return [ChangeEvent(
        event_id=f"snapshot:{s['snapshot_id']}", timestamp=_norm_time(s["created_at"]), kind="snapshot.created",
        actor=None, owner=None, target_id=s["snapshot_id"], category=None,
        summary=f"snapshot {s['snapshot_id']} created ({s['file_count']} files)" + (f": {s['label']}" if s.get("label") else ""),
        source_ref=f"snapshot:{s['snapshot_id']}", metadata={"reason": s.get("reason"), "total_bytes": s.get("total_bytes")})
        for s in snapshot_events(root) if _in_window(s["created_at"], since, until)]


EXTRA_ADAPTERS: list[Callable[[Path, str | None, str | None], Iterable[ChangeEvent]]] = []


def changes(project_root: str | Path, *, since: str | None = None, until: str | None = None,
            kinds: Iterable[str] | None = None, owner: str | None = None,
            limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Newest-first events in [since, until), optionally filtered by kind and owner. Deterministic."""
    root = Path(project_root).resolve()
    since_n, until_n = _norm_time(since), _norm_time(until)
    wanted = {k.strip() for k in (kinds or []) if k and k.strip()}
    unknown = sorted(k for k in wanted if k not in KINDS and not any(x.startswith(k.rstrip(".*") + ".") for x in KINDS))
    if unknown:
        return {"ok": False, "error": f"unknown kinds: {', '.join(unknown)}", "kinds": list(KINDS)}
    memories = _memory_index(root)
    events = receipt_events(root, since_n, until_n, memories)
    covered = {e.target_id for e in receipt_events(root, None, None, memories)
               if e.kind == "memory.created" and e.target_id}
    events += legacy_memory_events(root, since_n, until_n, memories, covered)
    events += dream_events(root, since_n, until_n)
    events += handoff_change_events(root, since_n, until_n)
    events += snapshot_change_events(root, since_n, until_n)
    for adapter in EXTRA_ADAPTERS:
        events += list(adapter(root, since_n, until_n))

    def keep(event: ChangeEvent) -> bool:
        if wanted and not any(event.kind == k or event.kind.startswith(k.rstrip(".*") + ".") for k in wanted):
            return False
        return owner in (None, "", "all") or event.owner == owner

    selected = sorted((e for e in events if keep(e)), key=lambda e: (e.timestamp, e.event_id), reverse=True)
    limit = max(1, min(int(limit), MAX_LIMIT))
    counts: dict[str, int] = {}
    for event in selected:
        counts[event.kind] = counts.get(event.kind, 0) + 1
    return {"ok": True, "since": since_n, "until": until_n, "total": len(selected), "counts": counts,
            "events": [asdict(e) for e in selected[:limit]], "truncated": len(selected) > limit}


def main(argv: list[str] | None = None) -> int:
    from config import data_root, load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description="What changed: deterministic system event timeline (read-only)")
    parser.add_argument("--root", default=str(data_root()))
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--kind", action="append", default=[])
    parser.add_argument("--owner")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args(argv)
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    print(json.dumps(changes(args.root, since=since, kinds=args.kind, owner=args.owner, limit=args.limit),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
