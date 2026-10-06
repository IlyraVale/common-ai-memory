"""Snapshots: consistent capture, verification, read-only planning and guarded two-step restore."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import snapshots
from memory_store import MemoryStore
from snapshots import SnapshotError, SnapshotManager

T0 = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)  # distinct snapshot ids
        return self.now


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="snapshots-")
        self.root = Path(self.temp.name) / "快照 数据"
        self.root.mkdir()
        patcher = patch.object(MemoryStore, "_git_commit", lambda *_: "disabled-in-test")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = MemoryStore(self.root, "gpt")
        self.first = self.store.remember("第一条记忆", "life/daily")["id"]
        (self.root / "owner-config.json").write_text('{"owners": {}}', encoding="utf-8")
        (self.root / "dreams" / "gpt" / "2026-10").mkdir(parents=True)
        (self.root / "dreams" / "gpt" / "2026-10" / "2026-10-05.md").write_text("梦", encoding="utf-8")
        (self.root / ".lounge").mkdir()
        (self.root / ".lounge" / "messages.jsonl").write_text('{"seq": 1}\n', encoding="utf-8")
        for excluded in ("logs/run.log", "state/models/model.bin", "state/memory-search.sqlite3",
                         "state/x.sqlite3-wal", "memory/.tmp-123"):
            path = self.root / excluded
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("excluded", encoding="utf-8")
        self.clock = Clock()
        self.manager = SnapshotManager(self.root, clock=self.clock)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def memory_text(self) -> dict[str, bytes]:
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in (self.root / "memory").rglob("*.md")}

    def test_create_manifest_scope_and_list(self) -> None:
        result = self.manager.create(label="before experiment")
        manifest = json.loads((self.root / "snapshots" / result["snapshot_id"] / "manifest.json").read_text(encoding="utf-8"))
        paths = {f["path"] for f in manifest["files"]}
        self.assertIn("owner-config.json", paths)
        self.assertIn("dreams/gpt/2026-10/2026-10-05.md", paths)
        self.assertIn(".lounge/messages.jsonl", paths)
        self.assertIn("state/execution-receipts.sqlite3", paths)
        self.assertTrue(any(p.startswith("memory/life/daily/") for p in paths))
        for excluded in ("logs/run.log", "state/models/model.bin", "state/memory-search.sqlite3", "state/x.sqlite3-wal",
                         "memory/.tmp-123"):
            self.assertNotIn(excluded, paths)
        self.assertFalse(any(p.startswith("snapshots/") for p in paths))
        for entry in manifest["files"]:
            self.assertEqual(len(entry["sha256"]), 64)
        self.assertEqual(manifest["file_count"], len(manifest["files"]))
        self.assertEqual({d["path"] for d in manifest["sqlite"]}, {"state/execution-receipts.sqlite3"})
        self.assertEqual(manifest["sqlite"][0]["integrity"], "ok")
        listed = self.manager.list()["snapshots"]
        self.assertEqual((listed[0]["snapshot_id"], listed[0]["label"]), (result["snapshot_id"], "before experiment"))
        self.assertFalse(any(p.name.startswith(".partial-") for p in (self.root / "snapshots").iterdir()))

    def test_sqlite_online_backup_includes_uncheckpointed_wal(self) -> None:
        db_path = self.root / "state" / "live.sqlite3"
        writer = sqlite3.connect(db_path)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("CREATE TABLE t (v TEXT)")
            writer.executemany("INSERT INTO t VALUES (?)", [("行",)] * 50)
            writer.commit()  # committed into the WAL, not yet in the main file; writer still open
            snap = self.manager.create()["snapshot_id"]
        finally:
            writer.close()
        copy = self.root / "snapshots" / snap / "data" / "state" / "live.sqlite3"
        with closing(sqlite3.connect(copy)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM t").fetchone()[0], 50)
        self.assertTrue(self.manager.verify(snap)["ok"])

    def test_verify_detects_corruption(self) -> None:
        snap = self.manager.create()["snapshot_id"]
        self.assertTrue(self.manager.verify(snap)["ok"])
        data = self.root / "snapshots" / snap / "data"
        target = next(data.rglob("*.md"))
        target.write_text("tampered", encoding="utf-8")
        (data / "intruder.txt").write_text("x", encoding="utf-8")
        result = self.manager.verify(snap)
        self.assertFalse(result["ok"])
        self.assertEqual({p["problem"] for p in result["problems"]}, {"content changed", "unexpected file"})
        with self.assertRaises(SnapshotError):
            self.manager.verify("../escape")

    def test_restore_plan_is_read_only_and_accurate(self) -> None:
        snap = self.manager.create()["snapshot_id"]
        second = self.store.remember("后来的记忆", "life/daily")["id"]
        self.store.update(memory_id=self.first, content="第一条记忆（改过）")
        (self.root / ".lounge" / "messages.jsonl").unlink()
        before = self.memory_text()
        plan = self.manager.restore_plan(snap)
        self.assertEqual(self.memory_text(), before)
        self.assertIn(".lounge/messages.jsonl", plan["add"])
        self.assertTrue(any(second in p for p in plan["remove"]))
        self.assertTrue(any(self.first in p for p in plan["overwrite"]))
        self.assertTrue(any(d["path"] == "state/execution-receipts.sqlite3" for d in plan["sqlite"]))
        self.assertEqual(plan, self.manager.restore_plan(snap))

    def test_full_restore_round_trip_with_safety_snapshot(self) -> None:
        snap = self.manager.create()["snapshot_id"]
        original = self.memory_text()
        self.store.remember("要被撤销的记忆", "life/daily")
        self.store.update(memory_id=self.first, content="改坏了")
        (self.root / "state" / "memory-search.sqlite3").write_text("derived", encoding="utf-8")
        token = self.manager.issue_restore_token(snap)["restore_token"]
        result = self.manager.restore(snap, confirm=token)
        self.assertTrue(result["ok"])
        self.assertEqual(self.memory_text(), original)
        self.assertFalse((self.root / "state" / "memory-search.sqlite3").exists())  # rebuilt later
        safety = result["safety_snapshot"]
        self.assertEqual(self.manager.verify(safety)["ok"], True)
        safety_paths = {f["path"] for f in json.loads(
            (self.root / "snapshots" / safety / "manifest.json").read_text(encoding="utf-8"))["files"]}
        self.assertEqual(len([p for p in safety_paths if p.startswith("memory/") and p.endswith(".md")]), 2)
        self.assertFalse(any(p.name.startswith(".restore-") for p in self.root.iterdir()))
        with self.assertRaises(SnapshotError):
            self.manager.restore(snap, confirm=token)  # one-time

    def test_token_rules(self) -> None:
        snap = self.manager.create()["snapshot_id"]
        other = self.manager.create()["snapshot_id"]
        with self.assertRaises(SnapshotError):
            self.manager.restore(snap, confirm="rst-made-up")
        token = self.manager.issue_restore_token(snap)["restore_token"]
        with self.assertRaises(SnapshotError):
            self.manager.restore(other, confirm=token)          # bound to its snapshot (and now used)
        token = self.manager.issue_restore_token(snap)["restore_token"]
        self.clock.now += timedelta(minutes=11)
        with self.assertRaises(SnapshotError) as ctx:
            self.manager.restore(snap, confirm=token)           # stale
        self.assertIn("expired", str(ctx.exception))

    def test_digest_mismatch_refuses_and_changes_nothing(self) -> None:
        snap = self.manager.create()["snapshot_id"]
        token = self.manager.issue_restore_token(snap)["restore_token"]
        self.store.remember("计划之后写入", "life/daily")
        before = self.memory_text()
        with self.assertRaises(SnapshotError) as ctx:
            self.manager.restore(snap, confirm=token)
        self.assertIn("changed", str(ctx.exception))
        self.assertEqual(self.memory_text(), before)

    def test_safety_snapshot_failure_stops_restore(self) -> None:
        snap = self.manager.create()["snapshot_id"]
        self.store.update(memory_id=self.first, content="不应被覆盖")
        token = self.manager.issue_restore_token(snap)["restore_token"]
        before = self.memory_text()
        with patch.object(SnapshotManager, "create", side_effect=OSError("disk full")):
            with self.assertRaises(SnapshotError) as ctx:
                self.manager.restore(snap, confirm=token)
        self.assertIn("safety snapshot", str(ctx.exception))
        self.assertEqual(self.memory_text(), before)

    def test_failure_mid_swap_rolls_back(self) -> None:
        snap = self.manager.create()["snapshot_id"]
        self.store.remember("第二条", "life/daily")
        self.store.update(memory_id=self.first, content="当前版本")
        before = self.memory_text()
        token = self.manager.issue_restore_token(snap)["restore_token"]
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            if ".restore-staging-" in str(src):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise PermissionError("file in use")
            return real_replace(src, dst)

        with patch.object(snapshots.os, "replace", side_effect=flaky):
            with self.assertRaises(SnapshotError) as ctx:
                self.manager.restore(snap, confirm=token)
        self.assertIn("rolled back", str(ctx.exception))
        self.assertEqual(self.memory_text(), before)
        self.assertFalse(any(p.name.startswith(".restore-staging") for p in self.root.iterdir()))

    def test_busy_lock_is_refused(self) -> None:
        lock = self.store._acquire_lock()
        try:
            with patch.object(snapshots, "LOCK_TIMEOUT", 0.3):
                with self.assertRaises(SnapshotError):
                    self.manager.create()
        finally:
            self.store._release_lock(lock)
        self.assertFalse((self.root / "snapshots").exists() and any(
            p.name.startswith(".partial-") for p in (self.root / "snapshots").iterdir()))

    def test_cli_round_trip(self) -> None:
        import contextlib
        import io

        def run(*argv) -> dict:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                snapshots.cli(["--root", str(self.root), *argv])
            return json.loads(out.getvalue())

        snap = run("create", "--label", "cli")["snapshot_id"]
        self.assertTrue(run("verify", snap)["ok"])
        plan = run("restore-plan", snap)
        self.assertIn("restore_token", plan)
        self.assertTrue(run("restore", snap, "--confirm", plan["restore_token"])["ok"])
        self.assertFalse(run("restore", snap, "--confirm", plan["restore_token"])["ok"])


if __name__ == "__main__":
    unittest.main()
