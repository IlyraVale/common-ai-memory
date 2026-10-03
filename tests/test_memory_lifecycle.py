from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from memory_doctor import inspect_memory
from memory_search import MemorySearchIndex, rebuild_project
from memory_store import MemoryStore


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memory-lifecycle-")
        self.root = Path(self.temp.name)
        self.gpt = MemoryStore(self.root, "gpt")

    def tearDown(self):
        self.temp.cleanup()

    def add(self, text: str, *, owner: str = "gpt", status=None, source=None):
        return MemoryStore(self.root, owner).remember(
            text, "life/daily", status=status, source=source
        )

    def change(self, saved, lifecycle, superseded_by=None):
        return self.gpt.update(
            saved["id"], saved["content"], lifecycle=lifecycle,
            superseded_by=superseded_by,
        )

    def test_legacy_is_active_and_result_marks_it(self):
        saved = self.add("legacy lifecycle marker")
        row = self.gpt.recall("legacy lifecycle marker")[0]
        self.assertEqual(row["id"], saved["id"])
        self.assertEqual(row["lifecycle"], "active")

    def test_default_and_history_eligibility(self):
        active = self.add("shared lifecycle phrase active")
        review = self.add("shared lifecycle phrase review")
        stale = self.add("shared lifecycle phrase stale")
        old = self.add("shared lifecycle phrase superseded")
        replacement = self.add("replacement fact")
        self.change(review, "review_needed")
        self.change(stale, "stale")
        self.change(old, "superseded", replacement["id"])
        default = {x["id"] for x in self.gpt.recall("shared lifecycle phrase", limit=30)}
        history = {x["id"] for x in self.gpt.recall(
            "shared lifecycle phrase", limit=30, include_inactive=True
        )}
        self.assertEqual(default, {active["id"], review["id"]})
        self.assertTrue({stale["id"], old["id"]} <= history)
        old_row = next(x for x in self.gpt.recall(
            "superseded", limit=30, include_inactive=True
        ) if x["id"] == old["id"])
        self.assertEqual(old_row["superseded_by"], replacement["id"])

    def test_review_tie_break_but_relevance_wins(self):
        review = self.add("alpha alpha alpha exact")
        active = self.add("alpha category weaker")
        self.change(review, "review_needed")
        self.assertEqual(self.gpt.recall("alpha alpha alpha exact")[0]["id"], review["id"])
        one = self.add("same exact tie")
        two = self.add("same exact tie")
        self.change(two, "review_needed")
        ids = [x["id"] for x in self.gpt.recall("same exact tie")]
        self.assertLess(ids.index(one["id"]), ids.index(two["id"]))

    def test_source_status_and_lifecycle_are_independent(self):
        row = self.add("independent dimensions", status="open", source="inferred")
        changed = self.change(row, "review_needed")
        self.assertEqual((changed["status"], changed["source"], changed["lifecycle"]),
                         ("open", "inferred", "review_needed"))
        active = self.add("source tie exact", source="observed")
        inferred = self.add("source tie exact", source="inferred")
        ids = [x["id"] for x in self.gpt.recall("source tie exact")]
        self.assertLess(ids.index(active["id"]), ids.index(inferred["id"]))

    def test_wake_filters_open_by_lifecycle(self):
        active = self.add("open active", status="open")
        review = self.add("open review", status="open")
        stale = self.add("open stale", status="open")
        old = self.add("open replaced", status="open")
        replacement = self.add("new open fact")
        self.change(review, "review_needed")
        self.change(stale, "stale")
        self.change(old, "superseded", replacement["id"])
        items = self.gpt.recent(limit=20)
        ids = {x["id"] for x in items if x.get("status") == "open"}
        self.assertEqual(ids, {active["id"], review["id"]})
        self.assertEqual(next(x for x in items if x["id"] == review["id"])["lifecycle"], "review_needed")

    def test_restore_active_clears_superseded_by(self):
        old = self.add("old restore fact")
        new = self.add("new restore fact")
        self.change(old, "superseded", new["id"])
        restored = self.change(old, "active")
        self.assertEqual(restored["lifecycle"], "active")
        self.assertNotIn("superseded_by", restored)

    def test_supersession_validation(self):
        a = self.add("fact a")
        b = self.add("fact b")
        foreign = self.add("foreign fact", owner="claude")
        with self.assertRaises(ValueError): self.change(a, "superseded", a["id"])
        with self.assertRaises(KeyError): self.change(a, "superseded", "missing-id")
        with self.assertRaises(PermissionError): self.change(a, "superseded", foreign["id"])
        self.change(a, "superseded", b["id"])
        with self.assertRaises(ValueError): self.change(b, "superseded", a["id"])

    def test_forget_replacement_reviews_predecessor(self):
        old = self.add("predecessor")
        replacement = self.add("replacement")
        self.change(old, "superseded", replacement["id"])
        self.gpt.forget(replacement["id"])
        record = self.gpt._find_record(old["id"])[1]
        self.assertEqual(record["lifecycle"], "review_needed")
        self.assertNotIn("superseded_by", record)

    def test_fts_metadata_updates_without_vector_reembed(self):
        saved = self.add("metadata-only lifecycle marker")
        rebuild_project(self.root)
        with patch("memory_vectors.MemoryVectorIndex.upsert") as embed:
            self.change(saved, "stale")
        embed.assert_not_called()
        with sqlite3.connect(self.root / "state/memory-search.sqlite3") as db:
            row = db.execute("SELECT lifecycle FROM memories WHERE memory_id=?", (saved["id"],)).fetchone()
        self.assertEqual(row[0], "stale")

    def test_hybrid_excludes_semantic_only_stale(self):
        stale = self.add("semantic-only hidden material")
        active = self.add("lexical visible marker")
        self.change(stale, "stale")
        semantic = [{"memory_id": stale["id"], "similarity": .9,
                     "source": None, "updated_at": "x"}]
        with patch("memory_vectors.MemoryVectorIndex.search", return_value=semantic):
            ids = [x["id"] for x in self.gpt.recall("lexical visible marker")]
            historical = [x["id"] for x in self.gpt.recall(
                "lexical visible marker", include_inactive=True
            )]
        self.assertEqual(ids, [active["id"]])
        self.assertIn(stale["id"], historical)

    def test_doctor_lifecycle_validation_and_counts(self):
        legacy = self.add("doctor legacy")
        stale = self.add("doctor stale")
        self.change(stale, "stale")
        report = inspect_memory(self.root)
        self.assertEqual(report["lifecycle"]["active"], 1)
        self.assertEqual(report["lifecycle"]["stale"], 1)
        self.assertEqual(report["lifecycle"]["legacy_missing"], 1)
        path = self.gpt._find_record(legacy["id"])[0]
        text = path.read_text(encoding="utf-8").replace("---\n\ndoctor legacy", 'lifecycle: "broken"\n---\n\ndoctor legacy')
        path.write_text(text, encoding="utf-8")
        self.assertIn("invalid_lifecycle", {x["error_type"] for x in inspect_memory(self.root)["errors"]})

    def test_doctor_missing_target_and_cycle(self):
        a = self.add("doctor link a")
        b = self.add("doctor link b")
        for saved, target in ((a, b["id"]), (b, a["id"])):
            path = self.gpt._find_record(saved["id"])[0]
            text = path.read_text(encoding="utf-8").replace(
                "---\n\n", f'lifecycle: "superseded"\nsuperseded_by: "{target}"\n---\n\n'
            )
            path.write_text(text, encoding="utf-8")
        errors = {x["error_type"] for x in inspect_memory(self.root)["errors"]}
        self.assertIn("supersession_cycle", errors)
        self.gpt._find_record(b["id"])[0].unlink()
        errors = {x["error_type"] for x in inspect_memory(self.root)["errors"]}
        self.assertIn("supersession_target_missing", errors)

    def test_concurrent_updates_and_recall_are_safe(self):
        saved = self.add("concurrent lifecycle marker")
        failures = []
        def writer():
            try:
                for value in ("review_needed", "active") * 5:
                    self.change(saved, value)
            except Exception as exc:
                failures.append(type(exc).__name__)
        def reader():
            try:
                for _ in range(30): self.gpt.recall("concurrent lifecycle marker")
            except Exception as exc:
                failures.append(type(exc).__name__)
        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        [thread.start() for thread in threads]
        [thread.join() for thread in threads]
        self.assertEqual(failures, [])
        self.assertEqual(self.gpt._find_record(saved["id"])[1]["lifecycle"], "active")


if __name__ == "__main__":
    unittest.main()

