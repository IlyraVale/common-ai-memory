"""First-time initialization races of the execution receipt store.

All writes stay in TemporaryDirectory. Threads and processes open a not-yet-
existing execution-receipts.sqlite3 at the same moment; injection cases prove the
init retry is bounded and only covers SQLite lock contention.
"""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import tempfile
import threading
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

import execution_receipts
from execution_receipts import ExecutionReceiptStore

REPEATS = int(os.getenv("RECEIPT_INIT_REPEATS", "10"))
TARGET = "b" * 32
TRIGGERS = {"execution_receipts_no_update", "execution_receipts_no_delete"}


def _proc_append(root: str, start, count: int, out) -> None:
    try:
        store = ExecutionReceiptStore(root)
        start.wait(30)
        ids = [store.append(owner="gpt", actor="gpt", operation="update_memory", target_id=TARGET)["receipt_id"]
               for _ in range(count)]
        out.put(("ok", ids))
    except Exception:
        out.put(("error", traceback.format_exc()))


class ReceiptInitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="receipt-init-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fresh(self) -> None:
        self.tearDown()
        self.setUp()

    def assert_store_healthy(self, expected_ids: list[str]) -> None:
        db = sqlite3.connect(self.root / "state" / "execution-receipts.sqlite3")
        try:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            triggers = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            meta = db.execute("SELECT key, value FROM receipt_meta").fetchall()
            ids = [row[0] for row in db.execute("SELECT receipt_id FROM execution_receipts")]
            linked = db.execute("SELECT count(*) FROM receipt_targets WHERE target_status='linked'").fetchone()[0]
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            db.close()
        self.assertEqual(tables, {"receipt_meta", "execution_receipts", "receipt_targets"})
        self.assertEqual(triggers, TRIGGERS)
        self.assertEqual(meta, [("schema_version", str(execution_receipts.SCHEMA_VERSION))])
        self.assertEqual(len(ids), len(set(ids)), "duplicate receipt ids")
        self.assertEqual(sorted(ids), sorted(expected_ids), "lost or extra receipts")
        self.assertEqual(linked, len(expected_ids))
        self.assertEqual(ExecutionReceiptStore(self.root).diagnostics()["status"], "PASS")

    def threads_first_init(self, workers: int, per_worker: int = 1) -> None:
        store = ExecutionReceiptStore(self.root)
        barrier = threading.Barrier(workers)
        errors: list[str] = []
        ids: list[str] = []
        lock = threading.Lock()

        def worker():
            try:
                barrier.wait(10)
                mine = [store.append(owner="gpt", actor="gpt", operation="update_memory", target_id=TARGET)["receipt_id"]
                        for _ in range(per_worker)]
                with lock:
                    ids.extend(mine)
            except Exception:
                errors.append(traceback.format_exc())

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assert_store_healthy(ids)

    def processes_first_init(self, workers: int, per_worker: int = 3) -> None:
        ctx = multiprocessing.get_context("spawn")
        start, out = ctx.Event(), ctx.Queue()
        procs = [ctx.Process(target=_proc_append, args=(str(self.root), start, per_worker, out)) for _ in range(workers)]
        [p.start() for p in procs]
        start.set()
        results = [out.get(timeout=120) for _ in procs]
        [p.join(timeout=60) for p in procs]
        self.assertEqual([r[1] for r in results if r[0] != "ok"], [])
        self.assert_store_healthy([rid for _, ids in results for rid in ids])

    def test_two_and_eight_threads_first_init(self) -> None:
        for workers in (2, 8):
            for repeat in range(REPEATS):
                with self.subTest(workers=workers, repeat=repeat):
                    self.fresh()
                    self.threads_first_init(workers)

    def test_two_and_four_processes_first_init(self) -> None:
        for workers in (2, 4):
            for repeat in range(max(1, REPEATS // 3)):
                with self.subTest(workers=workers, repeat=repeat):
                    self.fresh()
                    self.processes_first_init(workers)

    def test_first_init_with_simultaneous_receipt_writes(self) -> None:
        for repeat in range(REPEATS):
            with self.subTest(repeat=repeat):
                self.fresh()
                self.threads_first_init(6, per_worker=5)

    def test_existing_database_concurrent_open(self) -> None:
        store = ExecutionReceiptStore(self.root)
        first = store.append(owner="gpt", actor="gpt", operation="remember", target_id=TARGET)["receipt_id"]
        errors: list[str] = []
        ids = [first]
        lock = threading.Lock()

        def worker():
            try:
                rid = store.append(owner="gpt", actor="gpt", operation="update_memory", target_id=TARGET)["receipt_id"]
                store.get(first, owner="gpt")
                self.assertEqual(store.reference_status(rid, owner="gpt"), "valid")
                with lock:
                    ids.append(rid)
            except Exception:
                errors.append(traceback.format_exc())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assert_store_healthy(ids)

    def test_append_only_and_privacy_scrub_unchanged(self) -> None:
        store = ExecutionReceiptStore(self.root)
        rid = store.append(owner="gpt", actor="gpt", operation="forget", target_id=TARGET)["receipt_id"]
        db = sqlite3.connect(store.path)
        try:
            with self.assertRaisesRegex(sqlite3.DatabaseError, "immutable"):
                db.execute("UPDATE execution_receipts SET outcome='failed'")
            with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                db.execute("DELETE FROM execution_receipts")
        finally:
            db.close()
        self.assertEqual(store.scrub_target(owner="gpt", target_id=TARGET), 1)
        row = store.get(rid, owner="gpt")
        self.assertEqual((row["target_status"], row["target_id"]), ("redacted", None))


class ReceiptInitRetryInjection(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="receipt-init-inject-")
        self.store = ExecutionReceiptStore(self.temp.name)
        self.sleeps: list[float] = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def flaky_open(self, failures: int, message: str):
        real = ExecutionReceiptStore._open_once
        calls = {"n": 0}

        def opener(store, create):
            calls["n"] += 1
            if calls["n"] <= failures:
                raise sqlite3.OperationalError(message)
            return real(store, create)
        return opener, calls

    def test_transient_lock_recovers(self) -> None:
        opener, calls = self.flaky_open(2, "database is locked")
        with patch.object(ExecutionReceiptStore, "_open_once", opener), \
                patch.object(execution_receipts.time, "sleep", side_effect=self.sleeps.append):
            receipt = self.store.append(owner="gpt", actor="gpt", operation="remember", target_id=TARGET)
        # Two locked attempts, then success: exactly the first two back-off steps.
        self.assertEqual(self.sleeps, list(ExecutionReceiptStore._INIT_LOCK_RETRY_DELAYS[:2]))
        self.assertGreaterEqual(calls["n"], 3)
        self.assertEqual(self.store.get(receipt["receipt_id"], owner="gpt")["target_status"], "linked")

    def test_persistent_lock_fails_bounded(self) -> None:
        opener, calls = self.flaky_open(10_000, "database is busy")
        with patch.object(ExecutionReceiptStore, "_open_once", opener), \
                patch.object(execution_receipts.time, "sleep", side_effect=self.sleeps.append):
            with self.assertRaisesRegex(sqlite3.OperationalError, "busy"):
                self.store.append(owner="gpt", actor="gpt", operation="remember", target_id=TARGET)
        self.assertEqual(calls["n"], len(ExecutionReceiptStore._INIT_LOCK_RETRY_DELAYS) + 1)
        self.assertLess(sum(self.sleeps), 0.5)

    def test_unrelated_operational_error_is_not_retried(self) -> None:
        for message in ("disk I/O error", "no such table: receipt_meta", "unable to open database file"):
            with self.subTest(message=message):
                self.sleeps.clear()
                opener, calls = self.flaky_open(10_000, message)
                with patch.object(ExecutionReceiptStore, "_open_once", opener), \
                        patch.object(execution_receipts.time, "sleep", side_effect=self.sleeps.append):
                    with self.assertRaisesRegex(sqlite3.OperationalError, message.split(":")[0]):
                        self.store.append(owner="gpt", actor="gpt", operation="remember", target_id=TARGET)
                self.assertEqual((calls["n"], self.sleeps), (1, []))

    def test_failed_attempt_closes_its_connection(self) -> None:
        closed: list[bool] = []
        real_connect = sqlite3.connect

        class Tracking:
            def __init__(self, inner):
                object.__setattr__(self, "inner", inner)

            def __setattr__(self, name, value):
                setattr(self.inner, name, value)

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def execute(self, sql, *args):
                if sql == "PRAGMA journal_mode=WAL" and not closed:
                    raise sqlite3.OperationalError("database is locked")
                return self.inner.execute(sql, *args)

            def close(self):
                closed.append(True)
                self.inner.close()

        with patch.object(execution_receipts.sqlite3, "connect", lambda *a, **k: Tracking(real_connect(*a, **k))), \
                patch.object(execution_receipts.time, "sleep", side_effect=self.sleeps.append):
            self.store.append(owner="gpt", actor="gpt", operation="remember", target_id=TARGET)
        self.assertGreaterEqual(len(closed), 2, "the locked attempt's connection must be closed")


if __name__ == "__main__":
    unittest.main()
