from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from memory_doctor import inspect_memory
from memory_search import rebuild_project
from memory_store import MemoryStore
from memory_vectors import MemoryVectorIndex


class TinyBackend:
    model_id = "test/tiny"
    revision = "1"
    fingerprint = "tiny-fingerprint"
    dimension = 3

    def embed(self, texts):
        return [[1.0, float(len(text) % 7 + 1), 0.5] for text in texts]


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memory-verification-")
        self.root = Path(self.temp.name)
        self.gpt = MemoryStore(self.root, "gpt")

    def tearDown(self):
        self.temp.cleanup()

    def add(self, text, *, owner="gpt", status=None, source=None):
        return MemoryStore(self.root, owner).remember(
            text, "life/daily", status=status, source=source
        )

    def verify(self, saved, state):
        return self.gpt.update(saved["id"], saved["content"], verification=state)

    def test_legacy_and_all_explicit_states(self):
        legacy = self.add("legacy verification marker")
        self.assertEqual(self.gpt.recall("legacy verification marker")[0]["verification"], "unknown")
        for state in ("unknown", "unverified", "confirmed", "partial", "not_applicable"):
            saved = self.add(f"verification state {state}")
            changed = self.verify(saved, state)
            self.assertEqual(changed["verification"], state)
            self.assertIn("verification_updated_at", changed)

    def test_invalid_rejected_and_owner_isolated(self):
        own = self.add("own verification")
        with self.assertRaises(ValueError): self.verify(own, "probably")
        foreign = self.add("foreign verification", owner="claude")
        with self.assertRaises(PermissionError): self.verify(foreign, "confirmed")

    def test_verification_never_changes_recall_eligibility_or_order(self):
        states = ("unknown", "unverified", "confirmed", "partial", "not_applicable")
        rows = [self.add("same verification relevance") for _ in states]
        before = [x["id"] for x in self.gpt.recall("same verification relevance", limit=30)]
        for saved, state in zip(rows, states): self.verify(saved, state)
        after = [x["id"] for x in self.gpt.recall("same verification relevance", limit=30)]
        self.assertEqual(before, after)
        self.assertEqual({x["verification"] for x in self.gpt.recall("same verification relevance", limit=30)}, set(states))

    def test_source_status_and_verification_are_independent(self):
        inferred = self.add("independent inferred confirmed", source="inferred")
        opened = self.add("independent open unverified", status="open", source="user_statement")
        a = self.verify(inferred, "confirmed")
        b = self.verify(opened, "unverified")
        self.assertEqual((a["source"], a["verification"]), ("inferred", "confirmed"))
        self.assertEqual((b["status"], b["source"], b["verification"]),
                         ("open", "user_statement", "unverified"))

    def test_lifecycle_remains_authoritative_and_independent(self):
        stale = self.add("confirmed stale marker")
        old = self.add("confirmed superseded marker")
        replacement = self.add("replacement verification marker")
        review = self.add("confirmed review marker")
        for saved in (stale, old, review): self.verify(saved, "confirmed")
        self.gpt.update(stale["id"], stale["content"], lifecycle="stale")
        self.gpt.update(old["id"], old["content"], lifecycle="superseded", superseded_by=replacement["id"])
        self.gpt.update(review["id"], review["content"], lifecycle="review_needed")
        self.assertNotIn(stale["id"], {x["id"] for x in self.gpt.recall("confirmed stale marker")})
        self.assertNotIn(old["id"], {x["id"] for x in self.gpt.recall("confirmed superseded marker")})
        self.assertEqual(self.gpt.recall("confirmed review marker")[0]["lifecycle"], "review_needed")
        self.assertEqual(self.gpt._find_record(stale["id"])[1]["verification"], "confirmed")

    def test_recent_exposes_without_filtering(self):
        opened = self.add("wake unverified marker", status="open")
        self.verify(opened, "unverified")
        row = next(x for x in self.gpt.recent(limit=10) if x["id"] == opened["id"])
        self.assertEqual((row["status"], row["verification"]), ("open", "unverified"))

    def test_verification_only_preserves_content_and_does_not_reembed(self):
        saved = self.add("content must remain byte stable")
        with patch("memory_vectors.MemoryVectorIndex.upsert") as embed:
            changed = self.verify(saved, "partial")
        embed.assert_not_called()
        self.assertEqual(changed["content"], saved["content"])

    def test_vector_rows_unchanged_and_fts_metadata_updates(self):
        saved = self.add("index verification marker")
        rebuild_project(self.root)
        vectors = MemoryVectorIndex(self.root, backend=TinyBackend())
        vectors.rebuild(self.gpt._read_all())
        with sqlite3.connect(vectors.db_path) as db:
            before = db.execute("select count(*) from memory_vectors").fetchone()[0]
        self.verify(saved, "confirmed")
        with sqlite3.connect(vectors.db_path) as db:
            after = db.execute("select count(*) from memory_vectors").fetchone()[0]
        with sqlite3.connect(self.root / "state/memory-search.sqlite3") as db:
            state = db.execute("select verification from memories where memory_id=?", (saved["id"],)).fetchone()[0]
        self.assertEqual((before, after, state), (1, 1, "confirmed"))

    def test_doctor_counts_legacy_and_explicit_states(self):
        self.add("doctor unknown legacy")
        for state in ("unverified", "confirmed", "partial", "not_applicable"):
            self.verify(self.add(f"doctor {state}"), state)
        report = inspect_memory(self.root)
        self.assertEqual(report["verification"]["unknown"], 1)
        self.assertEqual(report["verification"]["legacy_missing"], 1)
        for state in ("unverified", "confirmed", "partial", "not_applicable"):
            self.assertEqual(report["verification"][state], 1)

    def test_doctor_rejects_invalid_enum_and_timestamp(self):
        saved = self.add("doctor invalid verification")
        path = self.gpt._find_record(saved["id"])[0]
        text = path.read_text(encoding="utf-8").replace(
            "---\n\ndoctor invalid verification",
            'verification: "broken"\nverification_updated_at: "yesterday"\n---\n\ndoctor invalid verification',
        )
        path.write_text(text, encoding="utf-8")
        errors = {x["error_type"] for x in inspect_memory(self.root)["errors"]}
        self.assertEqual({"invalid_verification", "invalid_verification_updated_at"} <= errors, True)

    def test_concurrent_updates_and_recall_are_safe(self):
        saved = self.add("concurrent verification marker")
        failures = []
        def writer():
            try:
                for state in ("unverified", "confirmed", "partial", "unknown") * 4:
                    self.verify(saved, state)
            except Exception as exc: failures.append(type(exc).__name__)
        def reader():
            try:
                for _ in range(40): self.gpt.recall("concurrent verification marker")
            except Exception as exc: failures.append(type(exc).__name__)
        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(failures, [])
        self.assertEqual(self.gpt._find_record(saved["id"])[1]["verification"], "unknown")


if __name__ == "__main__":
    unittest.main()
