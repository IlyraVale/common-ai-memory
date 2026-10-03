from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from execution_receipts import ExecutionReceiptStore
from memory_doctor import inspect_memory
from memory_store import MemoryStore


class ReceiptEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="receipt-evidence-")
        self.root = Path(self.temp.name)
        self.gpt = MemoryStore(self.root, "gpt")

    def tearDown(self):
        self.temp.cleanup()

    def add(self, text="receipt evidence marker", **kwargs):
        return self.gpt.remember(text, "life/daily", **kwargs)

    def receipt_rows(self):
        db = sqlite3.connect(self.root / "state" / "execution-receipts.sqlite3")
        db.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in db.execute("select * from execution_receipts order by rowid")]
        finally:
            db.close()

    def test_legacy_empty_and_explicit_deduplicated_refs(self):
        saved = self.add()
        self.assertEqual(self.gpt.recall("receipt evidence")[0]["evidence_refs"], [])
        ref = "receipt:" + saved["execution_receipt_id"]
        changed = self.gpt.update(saved["id"], saved["content"], evidence_refs=[ref, ref])
        self.assertEqual(changed["evidence_refs"], [ref])
        self.assertEqual(self.gpt.recent()[0]["evidence_refs"], [ref])

    def test_validation_limits_dangling_and_cross_owner(self):
        saved = self.add()
        for refs in (["archive:abc"], ["receipt:nope"], ["receipt:" + "0" * 32]):
            with self.assertRaises(ValueError):
                self.gpt.update(saved["id"], saved["content"], evidence_refs=refs)
        with self.assertRaises(ValueError):
            self.gpt.update(saved["id"], saved["content"], evidence_refs=["receipt:" + saved["execution_receipt_id"]] * 33)
        claude = MemoryStore(self.root, "claude").remember("other owner receipt", "life/daily")
        with self.assertRaises(ValueError):
            self.gpt.update(saved["id"], saved["content"], evidence_refs=["receipt:" + claude["execution_receipt_id"]])

    def test_refs_do_not_change_ranking_or_dimensions(self):
        first = self.add("same rank evidence marker", source="observed")
        second = self.add("same rank evidence marker", source="observed")
        before = [row["id"] for row in self.gpt.recall("same rank evidence marker")]
        ref = "receipt:" + first["execution_receipt_id"]
        with patch("memory_vectors.MemoryVectorIndex.upsert") as embed, patch("memory_search.MemorySearchIndex.upsert") as fts:
            changed = self.gpt.update(first["id"], first["content"], evidence_refs=[ref])
        embed.assert_not_called()
        fts.assert_not_called()
        after = [row["id"] for row in self.gpt.recall("same rank evidence marker")]
        self.assertEqual(before, after)
        self.assertEqual(changed.get("verification", "unknown"), "unknown")
        self.assertEqual(changed.get("lifecycle", "active"), "active")
        self.assertEqual(changed["source"], "observed")
        self.assertIsNone(changed.get("status"))
        self.assertEqual(second["source"], "observed")

    def test_core_mutations_create_safe_success_receipts(self):
        saved = self.add(status="open")
        updated = self.gpt.update(saved["id"], saved["content"], verification="partial")
        forgotten = self.gpt.forget(saved["id"])
        rows = self.receipt_rows()
        self.assertEqual([r["operation"] for r in rows], ["remember", "update_memory", "forget"])
        self.assertTrue(all(r["outcome"] == "success" and r["actor"] == "gpt" for r in rows))
        self.assertEqual(len({r["receipt_id"] for r in rows}), 3)
        self.assertEqual(updated["execution_receipt_id"], rows[1]["receipt_id"])
        self.assertEqual(forgotten["execution_receipt_id"], rows[2]["receipt_id"])

    def test_import_creates_receipt_without_changing_path_contract(self):
        record = {"id": "a" * 32, "owner": "gpt", "scope": "agent", "category": "life/daily", "content": "migration marker"}
        path = self.gpt.import_record(record)
        self.assertIsInstance(path, Path)
        self.assertEqual(self.receipt_rows()[0]["operation"], "import_memory")

    def test_append_only_and_owner_scoped_cli_contract(self):
        saved = self.add()
        rid = saved["execution_receipt_id"]
        store = ExecutionReceiptStore(self.root)
        self.assertEqual(store.get(rid, owner="gpt")["target_id"], saved["id"])
        with self.assertRaises(KeyError):
            store.get(rid, owner="claude")
        db = sqlite3.connect(store.path)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("update execution_receipts set outcome='failed' where receipt_id=?", (rid,))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("delete from execution_receipts where receipt_id=?", (rid,))
        db.close()

    def test_failure_receipt_is_metadata_only_and_validation_is_not_logged(self):
        store = ExecutionReceiptStore(self.root)
        secret = "sk-THIS_MUST_NEVER_PERSIST"
        row = store.append(owner="gpt", actor="gpt", operation="update_memory", target_id="a" * 32,
                           outcome="failed", error_class=RuntimeError(secret))
        self.assertEqual(row["error_class"], "RuntimeError")
        self.assertNotIn(secret, store.path.read_bytes().decode("latin1", errors="ignore"))
        count = len(self.receipt_rows())
        with self.assertRaises(ValueError):
            self.add("x", visibility="invalid")
        self.assertEqual(len(self.receipt_rows()), count)

    def test_receipt_failure_keeps_memory_and_reports_unavailable(self):
        with patch("execution_receipts.ExecutionReceiptStore.append", side_effect=sqlite3.OperationalError("private raw message")):
            saved = self.add("durable after receipt failure")
        self.assertIsNotNone(self.gpt._find_record(saved["id"]))
        self.assertIsNone(saved["execution_receipt_id"])
        self.assertEqual(saved["receipt_status"], "unavailable")
        self.assertNotIn("private raw message", json.dumps(saved))

    def test_doctor_evidence_and_receipt_diagnostics(self):
        saved = self.add()
        report = inspect_memory(self.root)
        self.assertEqual(report["execution_receipts"]["status"], "PASS")
        self.assertEqual(report["evidence_refs"]["legacy_missing"], 1)
        self.gpt.update(saved["id"], saved["content"], verification="confirmed")
        report = inspect_memory(self.root)
        self.assertEqual(report["evidence_refs"]["confirmed_without_evidence"], 1)
        self.assertEqual(report["result"], "PASS")

    def test_doctor_detects_malformed_and_dangling_refs(self):
        saved = self.add()
        path = self.gpt._find_record(saved["id"])[0]
        text = path.read_text(encoding="utf-8")
        text = text.replace("---\n\n", 'evidence_refs: ["receipt:00000000000000000000000000000000"]\n---\n\n', 1)
        path.write_text(text, encoding="utf-8")
        report = inspect_memory(self.root)
        self.assertIn("dangling_receipt_ref", {e["error_type"] for e in report["errors"]})

    def test_forget_retains_metadata_only_receipt(self):
        saved = self.add("privacy delete body sk-PRIVATE")
        self.gpt.forget(saved["id"])
        data = (self.root / "state" / "execution-receipts.sqlite3").read_bytes().decode("latin1", errors="ignore")
        self.assertNotIn("privacy delete body", data)
        self.assertNotIn("sk-PRIVATE", data)
        self.assertEqual(len(self.receipt_rows()), 2)

    def test_concurrent_receipts_unique_and_recall_safe(self):
        ids, errors = [], []
        lock = threading.Lock()
        def worker(index):
            try:
                store = MemoryStore(self.root, "gpt")
                row = store.remember(f"concurrent receipt marker {index}", "life/daily")
                store.recall("concurrent receipt")
                with lock: ids.append(row["execution_receipt_id"])
            except Exception as exc:
                with lock: errors.append(type(exc).__name__)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ExecutionReceiptStore(self.root).diagnostics()["status"], "PASS")


if __name__ == "__main__":
    unittest.main()

