from pathlib import Path
import tempfile

from memory_audit import MemoryAuditLog, read_recent_audit_events

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    log = MemoryAuditLog(root, "gpt")
    ok = log.log_read(
        tool="recall",
        query="五子棋",
        owner="all",
        limit=3,
        items=[
            {"id": "abc123", "owner": "claude", "category": "game/minigames", "content": "THIS MUST NOT BE LOGGED"},
            {"id": "def456", "owner": "gpt", "category": "project/common-ai-memory", "path": "THIS MUST NOT BE LOGGED"},
        ],
    )
    assert ok
    events = read_recent_audit_events(root, limit=10)
    assert len(events) == 1
    e = events[0]
    assert e["actor"] == "gpt"
    assert e["count"] == 2
    raw = (root / ".activity" / "memory-audit-gpt.jsonl").read_text(encoding="utf-8")
    assert "THIS MUST NOT BE LOGGED" not in raw
    print("audit_write=OK")
    print("actor=", e["actor"])
    print("count=", e["count"])
    print("metadata_only=OK")

