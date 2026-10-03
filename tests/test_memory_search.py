from __future__ import annotations

import multiprocessing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory_doctor import inspect_memory
from memory_search import MemorySearchIndex, fts5_available, normalize_match_query, rebuild_project
from memory_store import MemoryStore


def _recall_loop(root: str, queue) -> None:
    try:
        store = MemoryStore(root, "gpt")
        for _ in range(20):
            store.recall("英语时态", limit=5)
        queue.put("ok")
    except Exception as exc:
        queue.put(type(exc).__name__)


def _rebuild_once(root: str, queue) -> None:
    try:
        queue.put(rebuild_project(root)["indexed"])
    except Exception as exc:
        queue.put(type(exc).__name__)


class SearchIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memory-search-")
        self.root = Path(self.temp.name)
        self.store = MemoryStore(self.root, "gpt")

    def tearDown(self):
        self.temp.cleanup()

    def add(self, content: str, *, owner="gpt", source=None, category="life/daily"):
        return MemoryStore(self.root, owner).remember(content, category, source=source)

    def rebuild(self):
        return rebuild_project(self.root)

    def test_fts5_capability_and_tokenizer(self):
        self.assertTrue(fts5_available())
        db = sqlite3.connect(":memory:")
        db.execute("CREATE VIRTUAL TABLE x USING fts5(content, tokenize='trigram')")
        db.execute("INSERT INTO x VALUES('英语时态 English grammar')")
        self.assertEqual(db.execute("SELECT count(*) FROM x WHERE x MATCH ?", ('"英语时态"',)).fetchone()[0], 1)
        db.close()

    def test_full_rebuild_and_idempotency(self):
        self.add("English grammar baseline")
        self.add("中文基础句式")
        self.assertEqual(self.rebuild()["indexed"], 2)
        self.assertEqual(self.rebuild()["indexed"], 2)
        self.assertEqual(MemorySearchIndex(self.root).diagnostics(self.store._read_all())["status"], "PASS")

    def test_english_chinese_and_mixed_search(self):
        saved = self.add("小林不喜欢硬啃英语时态，更喜欢从基础句式开始。 English grammar baseline")
        self.rebuild()
        for query in ("English", "英语时态", "基础句式", "English 基础"):
            self.assertEqual(self.store.recall(query)[0]["id"], saved["id"])
        self.assertEqual(self.store.recall("硬啃语法"), [])

    def test_punctuation_quote_and_malicious_syntax_safe(self):
        saved = self.add('quoted phrase alpha-beta "gamma"')
        self.rebuild()
        self.assertEqual(self.store.recall('"alpha-beta"')[0]["id"], saved["id"])
        self.assertIsNone(normalize_match_query('" OR * NOT (' ))
        self.assertEqual(self.store.recall('" OR * NOT ('), [])

    def test_bm25_multi_term_and_rare_term_ranking(self):
        both = self.add("orchid quartz common words")
        self.add("orchid common common common")
        self.add("quartz alone")
        self.rebuild()
        self.assertEqual(self.store.recall("orchid quartz")[0]["id"], both["id"])
        self.assertEqual(self.store.recall("quartz")[0]["content"], "quartz alone")

    def test_strong_content_beats_weak_category(self):
        strong = self.add("project common memory exact body", category="life/daily")
        self.add("unrelated body text", category="project/common-ai-memory")
        self.rebuild()
        self.assertEqual(self.store.recall("common memory")[0]["id"], strong["id"])

    def test_source_tie_break_and_neutral_peers(self):
        inferred = self.add("same lexical comet", source="inferred")
        observed = self.add("same lexical comet", source="observed")
        self.rebuild()
        self.assertEqual(self.store.recall("same lexical comet")[0]["id"], observed["id"])
        user = self.add("neutral lexical planet", source="user_statement")
        obs = self.add("neutral lexical planet", source="observed")
        self.assertIn(self.store.recall("neutral lexical planet")[0]["id"], {user["id"], obs["id"]})
        self.assertNotEqual(inferred["id"], observed["id"])

    def test_owner_filter_limit_and_determinism(self):
        gpt = self.add("shared lexical galaxy one", owner="gpt")
        self.add("shared lexical galaxy two", owner="claude")
        self.rebuild()
        self.assertEqual(self.store.recall("lexical galaxy", owner="gpt")[0]["id"], gpt["id"])
        self.assertEqual(len(self.store.recall("lexical galaxy", limit=1)), 1)
        first = [x["id"] for x in self.store.recall("lexical galaxy")]
        self.assertEqual(first, [x["id"] for x in self.store.recall("lexical galaxy")])

    def test_incremental_remember_update_forget(self):
        self.rebuild()
        saved = self.add("incremental cedar marker")
        self.assertEqual(self.store.recall("cedar marker")[0]["id"], saved["id"])
        self.store.update(saved["id"], "incremental birch marker")
        self.assertEqual(self.store.recall("birch marker")[0]["id"], saved["id"])
        self.assertEqual(self.store.recall("cedar marker"), [])
        self.store.forget(saved["id"])
        self.assertEqual(self.store.recall("birch marker"), [])

    def test_index_failure_never_loses_memory_and_falls_back(self):
        self.rebuild()
        with patch("memory_search.MemorySearchIndex.upsert", side_effect=sqlite3.OperationalError("locked")):
            saved = self.add("fallback durable amber")
        self.assertTrue(saved["ok"])
        self.assertIsNotNone(self.store._find_record(saved["id"]))
        self.assertTrue(MemorySearchIndex(self.root).is_dirty())
        self.assertEqual(self.store.recall("durable amber")[0]["id"], saved["id"])

    def test_missing_dirty_and_corrupt_db_fallback(self):
        saved = self.add("fallback cobalt marker")
        self.assertEqual(self.store.recall("cobalt marker")[0]["id"], saved["id"])
        self.rebuild()
        MemorySearchIndex(self.root).mark_dirty("test")
        self.assertEqual(self.store.recall("cobalt marker")[0]["id"], saved["id"])
        MemorySearchIndex(self.root).dirty_path.unlink()
        MemorySearchIndex(self.root).db_path.write_bytes(b"corrupt")
        self.assertEqual(self.store.recall("cobalt marker")[0]["id"], saved["id"])

    def test_rebuild_repairs_stale_missing_and_orphan(self):
        first = self.add("doctor missing marker")
        self.rebuild()
        db = sqlite3.connect(MemorySearchIndex(self.root).db_path)
        db.execute("DELETE FROM memory_fts WHERE memory_id=?", (first["id"],))
        db.execute("DELETE FROM memories WHERE memory_id=?", (first["id"],))
        db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("orphan","gpt","agent","life/daily","x","x",None,None,"active",None,"unknown","bad"))
        db.execute("INSERT INTO memory_fts VALUES(?,?,?)", ("orphan","orphan text","life/daily"))
        db.commit(); db.close()
        diagnosis = inspect_memory(self.root)["search_index"]
        self.assertEqual(diagnosis["status"], "STALE")
        self.assertIn(first["id"], diagnosis["missing"])
        self.assertIn("orphan", diagnosis["orphan"])
        self.rebuild()
        self.assertEqual(inspect_memory(self.root)["search_index"]["status"], "PASS")

    def test_count_parity_and_long_tail(self):
        tail = self.add(("front material " * 900) + "uniquetailmarker")
        self.rebuild()
        diagnosis = inspect_memory(self.root)["search_index"]
        self.assertEqual(diagnosis["indexed"], 1)
        self.assertEqual(diagnosis["fts_rows"], 1)
        self.assertEqual(self.store.recall("uniquetailmarker")[0]["id"], tail["id"])

    def test_empty_short_and_emoji_bypass_fts(self):
        saved = self.add("短字 啊 emoji🌙 marker")
        self.rebuild()
        with patch("memory_search.MemorySearchIndex.search", side_effect=AssertionError("FTS should not run")):
            self.assertTrue(self.store.recall(""))
        self.assertIn(saved["id"], {x["id"] for x in self.store.recall("啊")})
        self.assertIn(saved["id"], {x["id"] for x in self.store.recall("🌙")})

    def test_concurrent_recall_during_rebuild(self):
        self.add("英语时态 concurrent marker")
        self.rebuild()
        ctx = multiprocessing.get_context("spawn"); queue = ctx.Queue()
        reader = ctx.Process(target=_recall_loop, args=(str(self.root), queue))
        builder = ctx.Process(target=_rebuild_once, args=(str(self.root), queue))
        reader.start(); builder.start(); reader.join(30); builder.join(30)
        self.assertEqual(reader.exitcode, 0); self.assertEqual(builder.exitcode, 0)
        self.assertEqual({queue.get(timeout=2), queue.get(timeout=2)}, {"ok", 1})

    def test_concurrent_remember_and_rebuild_preserves_source(self):
        self.add("initial concurrent source")
        self.rebuild()
        ctx = multiprocessing.get_context("spawn"); queue = ctx.Queue()
        builder = ctx.Process(target=_rebuild_once, args=(str(self.root), queue))
        builder.start()
        saved = self.add("second concurrent source")
        builder.join(30)
        self.assertEqual(builder.exitcode, 0)
        self.assertIsNotNone(self.store._find_record(saved["id"]))
        self.rebuild()
        self.assertEqual(inspect_memory(self.root)["search_index"]["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
