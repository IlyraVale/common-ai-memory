"""What changed: deterministic events from receipts, legacy records, Dream commits and handoffs."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import timeline
from dreams import DreamStore
from handoffs import HandoffStore
from memory_store import MemoryStore


class TimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="timeline-")
        self.root = Path(self.temp.name) / "时间线 根"
        self.root.mkdir()
        patcher = patch.object(MemoryStore, "_git_commit", lambda *_: "disabled-in-test")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gpt = MemoryStore(self.root, "gpt")
        self.claude = MemoryStore(self.root, "claude")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def kinds(self, **kwargs) -> list[str]:
        return [e["kind"] for e in timeline.changes(self.root, **kwargs)["events"]]

    def test_memory_lifecycle_events_from_receipts(self) -> None:
        a = self.gpt.remember("SECRET BODY one", "life/daily")["id"]
        b = self.gpt.remember("SECRET BODY two", "project/general")["id"]
        self.gpt.update(memory_id=a, content="SECRET BODY one, edited")
        self.gpt.update(memory_id=a, content="SECRET BODY one, edited", status="done")
        self.gpt.update(memory_id=b, content="SECRET BODY two", lifecycle="superseded", superseded_by=a)
        c = self.claude.remember("SECRET BODY three", "life/daily")["id"]
        self.claude.forget(c)
        result = timeline.changes(self.root)
        kinds = sorted(e["kind"] for e in result["events"])
        self.assertEqual(kinds.count("memory.created"), 3)
        for kind in ("memory.updated", "memory.status_changed", "memory.superseded", "memory.forgotten"):
            self.assertIn(kind, kinds)
        self.assertNotIn("SECRET BODY", json.dumps(result, ensure_ascii=False))
        forgotten = [e for e in result["events"] if e["kind"] == "memory.forgotten"][0]
        self.assertIsNone(forgotten["target_id"])  # forget redacts the target
        created = [e for e in result["events"] if e["kind"] == "memory.created" and e["target_id"] == a][0]
        self.assertEqual(created["category"], "life/daily")
        self.assertTrue(created["source_ref"].startswith("receipt:"))

    def test_dream_and_handoff_events(self) -> None:
        DreamStore(self.root).commit({"owner": "gpt", "dream_date": "2026-10-05", "timezone": "Asia/Shanghai",
                                      "generation_id": "g1", "source_memory_ids": [],
                                      "source_event_range": {"start": None, "end": None}, "truncated": False},
                                     "a dream", runner="fake", model=None)
        store = HandoffStore(self.root, "claude")
        hid = store.set(topic="继续 UI", summary="s")["handoff"]["handoff_id"]
        store.set(handoff_id=hid, topic="继续 UI", summary="s2")
        store.close(hid)
        kinds = self.kinds()
        for kind in ("handoff.created", "handoff.updated", "handoff.closed"):
            self.assertIn(kind, kinds)
        # dream commit lives in the runtime DB only when committed through the claim protocol;
        # a direct DreamStore commit writes files only, so it is not a commit event
        self.assertNotIn("dream.committed", kinds)

    def test_dream_commit_event_through_claim(self) -> None:
        from datetime import datetime, timezone
        from dreams import claim_on_wake_dream, dream_commit_result

        self.gpt.remember("material", "life/daily")
        pending = claim_on_wake_dream(self.root, "gpt", now=datetime(2030, 1, 2, 9, 0, tzinfo=timezone.utc))
        self.assertIsNotNone(pending)
        dream_commit_result(self.root, "gpt", pending["dream_date"], pending["claim_token"], "a dream")
        events = timeline.changes(self.root, kinds=["dream"])["events"]
        self.assertEqual([e["kind"] for e in events], ["dream.committed"])
        self.assertEqual(events[0]["target_id"], pending["dream_date"])

    def test_filters_limits_and_determinism(self) -> None:
        for index in range(5):
            self.gpt.remember(f"m{index}", "life/daily")
        self.claude.remember("c", "life/daily")
        self.assertEqual(len(timeline.changes(self.root, owner="claude")["events"]), 1)
        self.assertEqual(set(self.kinds(kinds=["memory"])), {"memory.created"})
        self.assertEqual(self.kinds(kinds=["handoff"]), [])
        self.assertFalse(timeline.changes(self.root, kinds=["nonsense"])["ok"])
        limited = timeline.changes(self.root, limit=2)
        self.assertEqual((len(limited["events"]), limited["total"], limited["truncated"]), (2, 6, True))
        self.assertEqual(len(timeline.changes(self.root, limit=10_000)["events"]), 6)  # capped at 100
        self.assertEqual(timeline.changes(self.root), timeline.changes(self.root))
        stamps = [e["timestamp"] for e in timeline.changes(self.root)["events"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_time_window(self) -> None:
        self.gpt.remember("now", "life/daily")
        self.assertEqual(timeline.changes(self.root, since="2099-01-01")["events"], [])
        self.assertEqual(timeline.changes(self.root, until="2000-01-01")["events"], [])
        self.assertEqual(len(timeline.changes(self.root, since="2000-01-01")["events"]), 1)

    def test_legacy_records_without_receipts(self) -> None:
        record = {"id": "legacy1", "owner": "gpt", "scope": "agent", "category": "life/daily",
                  "created_at": "2026-09-01T01:00:00Z", "updated_at": "2026-09-01T01:00:00Z", "content": "old"}
        path = self.root / "memory" / "life" / "daily" / "2026-09-01_legacy1__gpt__agent.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(MemoryStore._serialize(record), encoding="utf-8")
        events = timeline.changes(self.root)["events"]
        self.assertEqual([(e["kind"], e["source_ref"]) for e in events], [("memory.created", "memory:legacy1")])

    def test_no_model_and_no_writes(self) -> None:
        self.gpt.remember("x", "life/daily")
        def data_files():  # SQLite may create -wal/-shm sidecars when a WAL database is read; data stays as is
            return sorted((p.relative_to(self.root), p.stat().st_mtime_ns, p.stat().st_size)
                          for p in self.root.rglob("*") if p.is_file() and not p.name.endswith(("-wal", "-shm")))

        before = data_files()
        with patch("dream_cli_runner.PreferredCliRunner.generate", side_effect=AssertionError("model called")):
            timeline.changes(self.root)
        after = data_files()
        self.assertEqual(before, after)

    def test_empty_root(self) -> None:
        self.assertEqual(timeline.changes(self.root)["events"], [])


if __name__ == "__main__":
    unittest.main()
