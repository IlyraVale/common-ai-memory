"""Handoff capsules: several per owner, TTL, close, owner rules, compact wake index, never memory."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from handoffs import FIELD_LIMITS, HandoffError, HandoffStore
from memory_store import MemoryStore

HERE = Path(__file__).resolve().parent
T0 = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def wake_in_subprocess(root: Path, agent: str) -> dict:
    """server.py binds DATA_DIR at import, so wake runs in its own interpreter."""
    import importlib.util

    origin = importlib.util.find_spec("server").origin  # source tree or installed package, not imported here
    code = "import json, server; print(json.dumps(server.wake(include_dream=False), ensure_ascii=False))"
    env = {**os.environ, "DATA_DIR": str(root), "AI_MEMORY_AGENT": agent,
           "PYTHONPATH": str(Path(origin).resolve().parent)}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8",
                         env=env, cwd=root, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


class HandoffStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="handoffs-")
        self.root = Path(self.temp.name) / "断点 测试"
        self.root.mkdir()
        self.clock = Clock()
        self.gpt = HandoffStore(self.root, "gpt", clock=self.clock)
        self.claude = HandoffStore(self.root, "claude", clock=self.clock)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_multiple_capsules_do_not_overwrite_each_other(self) -> None:
        a = self.gpt.set(topic="窗口 A：UI", next_steps="修手机溢出")["handoff"]["handoff_id"]
        b = self.gpt.set(topic="窗口 B：Dream", summary="观察第一晚")["handoff"]["handoff_id"]
        self.assertNotEqual(a, b)
        topics = {h["topic"] for h in self.gpt.list()["handoffs"]}
        self.assertEqual(topics, {"窗口 A：UI", "窗口 B：Dream"})
        self.gpt.set(handoff_id=a, topic="窗口 A：UI", next_steps="改好了，等验收")
        self.assertEqual(self.gpt.get(b)["handoff"]["summary"], "观察第一晚")

    def test_ttl_expiry_and_bounds(self) -> None:
        hid = self.gpt.set(topic="t", summary="s", ttl_hours=2)["handoff"]["handoff_id"]
        self.clock.now = T0 + timedelta(hours=3)
        self.assertEqual(self.gpt.list()["handoffs"], [])
        self.assertEqual(self.gpt.get(hid)["handoff"]["status"], "expired")
        self.assertEqual(self.gpt.wake_index(), [])
        for bad in (0, 169):
            with self.assertRaises(HandoffError):
                self.gpt.set(topic="t", summary="s", ttl_hours=bad)

    def test_close_and_owner_rules(self) -> None:
        hid = self.gpt.set(topic="t", summary="s")["handoff"]["handoff_id"]
        self.assertTrue(self.claude.get(hid)["ok"])                           # readable across owners
        self.assertFalse(self.claude.close(hid)["ok"])                         # but not closable
        with self.assertRaises(HandoffError):
            self.claude.set(handoff_id=hid, topic="hijack", summary="x")       # nor editable
        self.assertEqual(self.gpt.close(hid)["action"], "closed")
        self.assertEqual(self.gpt.close(hid)["action"], "already_closed")
        self.assertEqual(self.gpt.get(hid)["handoff"]["status"], "closed")
        self.assertEqual(self.gpt.list()["handoffs"], [])
        with self.assertRaises(HandoffError):
            self.gpt.set(handoff_id=hid, topic="reopen", summary="x")

    def test_limits_and_max_active(self) -> None:
        for name, limit in FIELD_LIMITS.items():
            fields = {"topic": "t", "summary": "s", name: "x" * (limit + 1)}
            with self.subTest(field=name), self.assertRaises(HandoffError):
                self.gpt.set(**fields)
        with self.assertRaises(HandoffError):
            self.gpt.set(topic="only a topic")
        for index in range(5):
            self.gpt.set(topic=f"t{index}", summary="s")
        with self.assertRaises(HandoffError) as ctx:
            self.gpt.set(topic="t6", summary="s")
        self.assertIn("Close one first", str(ctx.exception))
        self.assertEqual(len(self.claude.set(topic="claude can still", summary="s")["handoff"]["handoff_id"]) > 0, True)

    def test_compact_index_and_full_get(self) -> None:
        long_steps = "下一步：" + "检查" * 150
        hid = self.gpt.set(topic="t", summary="SECRET-SUMMARY", next_steps=long_steps[:400],
                           temporary_context="SECRET-CONTEXT")["handoff"]["handoff_id"]
        listed = json.dumps(self.gpt.list(), ensure_ascii=False)
        index = json.dumps(self.gpt.wake_index(), ensure_ascii=False)
        for text in (listed, index):
            self.assertNotIn("SECRET-SUMMARY", text)
            self.assertNotIn("SECRET-CONTEXT", text)
        hint = self.gpt.wake_index()[0]["next_hint"]
        self.assertLessEqual(len(hint), 120)
        full = self.gpt.get(hid)["handoff"]
        self.assertEqual((full["summary"], full["temporary_context"]), ("SECRET-SUMMARY", "SECRET-CONTEXT"))

    def test_wake_index_max_three_own_newest_first(self) -> None:
        for index in range(5):
            self.clock.now = T0 + timedelta(minutes=index)
            self.gpt.set(topic=f"t{index}", summary="s")
        self.claude.set(topic="claude's", summary="s")
        topics = [h["topic"] for h in self.gpt.wake_index()]
        self.assertEqual(topics, ["t4", "t3", "t2"])
        self.assertEqual(set(self.gpt.wake_index()[0]), {"handoff_id", "topic", "updated_at", "next_hint"})

    def test_not_a_memory(self) -> None:
        self.gpt.set(topic="zanzibar-handoff", summary="zanzibar-handoff unique words")
        self.assertFalse(any((self.root / "memory").rglob("*.md")) if (self.root / "memory").exists() else False)
        self.assertEqual(MemoryStore(self.root, "gpt").recall("zanzibar-handoff"), [])
        from dreams import DreamPreparer
        package = DreamPreparer(self.root, scraps_enabled=False).prepare("gpt", dream_date="2026-10-06")
        self.assertNotIn("zanzibar", json.dumps(package))


class WakeIntegrationTests(unittest.TestCase):
    def test_wake_without_handoffs_has_no_handoff_key_and_with_some_only_compact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="handoff-wake-") as tmp:
            root = Path(tmp) / "wake 测试"
            root.mkdir()
            baseline = wake_in_subprocess(root, "gpt")
            self.assertNotIn("active_handoffs", baseline)
            store = HandoffStore(root, "gpt")
            for index in range(5):
                store.set(topic=f"topic {index}", summary="BODY-NOT-IN-WAKE " * 10, next_steps=f"next {index}")
            packet = wake_in_subprocess(root, "gpt")
            self.assertEqual(len(packet["active_handoffs"]), 3)
            self.assertNotIn("BODY-NOT-IN-WAKE", json.dumps(packet, ensure_ascii=False))
            claude_packet = wake_in_subprocess(root, "claude")
            self.assertNotIn("active_handoffs", claude_packet)


if __name__ == "__main__":
    unittest.main()
