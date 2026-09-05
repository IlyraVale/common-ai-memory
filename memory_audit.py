from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_MAX_QUERY_CHARS = 160
_MAX_ITEMS_PER_EVENT = 24
_MAX_LOG_BYTES = 8 * 1024 * 1024


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_actor(actor: str) -> str:
    actor = (actor or "unknown").strip().lower()
    safe = "".join(ch for ch in actor if ch.isalnum() or ch in "-_")
    return safe or "unknown"


class MemoryAuditLog:
    """Persistent metadata-only audit trail for AI memory reads."""

    def __init__(self, project_root: str | Path, actor: str) -> None:
        self.project_root = Path(project_root).resolve()
        self.actor = _safe_actor(actor)
        self.dir = self.project_root / ".activity"
        self.path = self.dir / f"memory-audit-{self.actor}.jsonl"

    def log_read(
        self,
        *,
        tool: str,
        items: list[dict[str, Any]],
        query: str = "",
        owner: str = "all",
        limit: int | None = None,
    ) -> bool:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed()

            refs = []
            for item in (items or [])[:_MAX_ITEMS_PER_EVENT]:
                refs.append(
                    {
                        "id": str(item.get("id") or "")[:96],
                        "owner": str(item.get("owner") or item.get("scope") or "")[:48],
                        "category": str(item.get("category") or "")[:120],
                        "location": str(item.get("location") or "")[:180],
                        "room_path": str(item.get("room_path") or "")[:120],
                    }
                )

            event = {
                "ts": _utc_now_iso(),
                "actor": self.actor,
                "action": "read",
                "tool": str(tool or "read")[:32],
                "query": str(query or "")[:_MAX_QUERY_CHARS],
                "owner_filter": str(owner or "all")[:48],
                "requested_limit": int(limit) if limit is not None else None,
                "count": len(items or []),
                "items": refs,
            }

            line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
            return True
        except Exception:
            return False

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size >= _MAX_LOG_BYTES:
                rotated = self.path.with_suffix(".jsonl.1")
                rotated.unlink(missing_ok=True)
                self.path.replace(rotated)
        except OSError:
            pass


def _tail_lines(path: Path, max_lines: int) -> list[str]:
    if max_lines <= 0 or not path.exists():
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            data = b""
            while pos > 0 and data.count(b"\n") <= max_lines:
                size = min(8192, pos)
                pos -= size
                f.seek(pos)
                data = f.read(size) + data
            lines = data.splitlines()[-max_lines:]
        return [line.decode("utf-8", errors="replace") for line in lines]
    except OSError:
        return []


def read_recent_audit_events(project_root: str | Path, *, limit: int = 100) -> list[dict[str, Any]]:
    root = Path(project_root).resolve()
    activity_dir = root / ".activity"
    if not activity_dir.exists():
        return []

    per_file = max(40, min(250, limit * 2))
    events: list[dict[str, Any]] = []
    for path in activity_dir.glob("memory-audit-*.jsonl"):
        for line in _tail_lines(path, per_file):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("action") == "read":
                events.append(event)

    events.sort(key=lambda e: str(e.get("ts") or ""), reverse=True)
    return events[:limit]

