from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from archive_store import ArchiveStore
from execution_receipts import ExecutionReceiptStore
from memory_doctor import inspect_memory
from memory_store import MemoryStore


class ArchiveOwnerReceiptV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="archive-owner-v2-")
        self.root = Path(self.temp.name)
        self.archive = ArchiveStore(self.root / "archive")

    def tearDown(self):
        self.temp.cleanup()

    def import_one(self, owner="gpt", marker="owned archive marker"):
        source = self.root / f"{owner}-{marker[:4]}.json"
        source.write_text(json.dumps({"marker": marker}), encoding="utf-8")
        result = self.archive.import_records(
            source, owner=owner, source="fixture",
            sessions=[{"session_id": "s1", "messages": [{"timestamp": "2026-01-01T00:00:00Z", "role": "user", "text": marker}]}],
        )
        row = self.archive.search(marker, owner=owner, include_legacy_unowned=False)[0]
        return result, row

    def test_owned_archive_assignment_and_owner_access(self):
        _, gpt = self.import_one("gpt", "gpt owned marker")
        _, claude = self.import_one("claude", "claude owned marker")
        self.assertEqual((gpt["owner"], claude["owner"]), ("gpt", "claude"))
        self.assertEqual(self.archive.search("gpt owned", owner="claude", include_legacy_unowned=False), [])
        with self.assertRaises(PermissionError):
            self.archive.open(gpt["archive_id"], owner="claude")
        with self.assertRaises(ValueError):
            self.import_one("../bad", "invalid owner")

    def test_legacy_unowned_remains_readable_but_not_evidence(self):
        _, row = self.import_one("gpt", "legacy readable marker")
        with sqlite3.connect(self.archive.db_path) as db:
            db.execute("update messages set owner=NULL where archive_id=?", (row["archive_id"],))
        found = self.archive.search("legacy readable", owner="claude", include_legacy_unowned=True)
        self.assertEqual(found[0]["owner"], None)
        self.assertEqual(self.archive.evidence_status(row["archive_id"], owner="gpt"), "unowned")
        memory = MemoryStore(self.root, "gpt").remember("legacy evidence target", "life/daily")
        with self.assertRaisesRegex(ValueError, "legacy unowned"):
            MemoryStore(self.root, "gpt").update(memory["id"], memory["content"], evidence_refs=["archive:" + row["archive_id"]])

    def test_archive_evidence_owner_validation_and_no_path(self):
        _, row = self.import_one("gpt", "evidence archive marker")
        memory = MemoryStore(self.root, "gpt").remember("archive evidence memory", "life/daily")
        ref = "archive:" + row["archive_id"]
        changed = MemoryStore(self.root, "gpt").update(memory["id"], memory["content"], evidence_refs=[ref])
        self.assertEqual(changed["evidence_refs"], [ref])
        self.assertNotIn(str(self.root), ref)
        other = MemoryStore(self.root, "claude").remember("cross owner memory", "life/daily")
        with self.assertRaises(ValueError):
            MemoryStore(self.root, "claude").update(other["id"], other["content"], evidence_refs=[ref])

    def test_archive_delete_leaves_dangling_evidence_for_doctor(self):
        _, row = self.import_one("gpt", "delete archive marker")
        store = MemoryStore(self.root, "gpt")
        memory = store.remember("dangling archive memory", "life/daily")
        store.update(memory["id"], memory["content"], evidence_refs=["archive:" + row["archive_id"]])
        self.archive.delete_message(row["archive_id"], owner="gpt")
        report = inspect_memory(self.root)
        self.assertIn("dangling_archive_ref", {e["error_type"] for e in report["errors"]})

    def test_receipt_v1_empty_migrates_to_v2_core_without_target(self):
        path = self.root / "state" / "execution-receipts.sqlite3"
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as db:
            db.execute("create table receipt_meta(key text primary key,value text not null)")
            db.execute("insert into receipt_meta values('schema_version','1')")
            db.execute("create table execution_receipts(receipt_id text primary key,owner text,actor text,operation text,target_type text,target_id text,outcome text,changed_fields_json text,started_at text,completed_at text,error_class text,parent_operation_id text,created_at text)")
        store = ExecutionReceiptStore(self.root)
        with store._connect():
            pass
        with sqlite3.connect(path) as db:
            self.assertNotIn("target_id", {r[1] for r in db.execute("pragma table_info(execution_receipts)")})
            self.assertEqual(db.execute("select value from receipt_meta where key='schema_version'").fetchone()[0], "2")

    def test_receipt_core_target_split_immutable_and_scrubbable(self):
        memory = MemoryStore(self.root, "gpt").remember("privacy scrub unique target", "life/daily")
        receipt_id = memory["execution_receipt_id"]
        receipts = ExecutionReceiptStore(self.root)
        before = receipts.get(receipt_id, owner="gpt")
        self.assertEqual(before["target_id"], memory["id"])
        receipts.scrub_target(owner="gpt", target_id=memory["id"])
        after = receipts.get(receipt_id, owner="gpt")
        self.assertIsNone(after["target_id"])
        self.assertEqual(after["target_status"], "redacted")
        self.assertTrue(receipts.exists(receipt_id, owner="gpt"))
        with sqlite3.connect(receipts.path) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("update execution_receipts set outcome='failed' where receipt_id=?", (receipt_id,))

    def test_forget_scrubs_all_target_links_but_keeps_events(self):
        store = MemoryStore(self.root, "gpt")
        memory = store.remember("forgotten target no shadow", "life/daily")
        first_receipt = memory["execution_receipt_id"]
        forgotten = store.forget(memory["id"])
        receipts = ExecutionReceiptStore(self.root)
        self.assertEqual(receipts.get(first_receipt, owner="gpt")["target_status"], "redacted")
        self.assertEqual(receipts.get(forgotten["execution_receipt_id"], owner="gpt")["target_status"], "redacted")
        with sqlite3.connect(receipts.path) as db:
            self.assertEqual(db.execute("select count(*) from execution_receipts").fetchone()[0], 2)
            self.assertEqual(db.execute("select count(*) from receipt_targets where target_id is not null").fetchone()[0], 0)
        self.assertNotIn(memory["id"], receipts.path.read_bytes().decode("latin1", errors="ignore"))

    def test_redacted_receipt_remains_valid_evidence_identity(self):
        receipts = ExecutionReceiptStore(self.root)
        receipt = receipts.append(owner="gpt", actor="gpt", operation="remember", target_id="a" * 32)
        receipts.scrub_target(owner="gpt", target_id="a" * 32)
        memory = MemoryStore(self.root, "gpt").remember("redacted receipt evidence", "life/daily")
        changed = MemoryStore(self.root, "gpt").update(memory["id"], memory["content"], evidence_refs=["receipt:" + receipt["receipt_id"]])
        self.assertEqual(MemoryStore(self.root, "gpt").recall("redacted receipt")[0]["verification"], "unknown")

    def test_doctor_archive_and_receipt_counts(self):
        self.import_one("gpt", "doctor owned archive")
        MemoryStore(self.root, "gpt").remember("doctor receipt memory", "life/daily")
        report = inspect_memory(self.root)
        self.assertEqual(report["archive"]["owned"], 1)
        self.assertEqual(report["execution_receipts"]["schema_version"], 2)
        self.assertEqual(report["execution_receipts"]["linked_targets"], 1)

    def test_concurrent_receipts_scrub_and_archive_import(self):
        errors = []
        store = ExecutionReceiptStore(self.root)
        target = "b" * 32
        def receipt_worker(i):
            try: store.append(owner="gpt", actor="gpt", operation="update_memory", target_id=target)
            except Exception as exc: errors.append(type(exc).__name__)
        threads = [threading.Thread(target=receipt_worker, args=(i,)) for i in range(6)]
        for t in threads: t.start()
        for t in threads: t.join()
        scrubbers = [threading.Thread(target=lambda: store.scrub_target(owner="gpt", target_id=target)) for _ in range(3)]
        for t in scrubbers: t.start()
        for t in scrubbers: t.join()
        self.assertEqual(errors, [])
        self.assertEqual(store.diagnostics()["linked_targets"], 0)


if __name__ == "__main__":
    unittest.main()

