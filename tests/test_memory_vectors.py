from __future__ import annotations

import math
import multiprocessing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory_doctor import inspect_memory
from memory_search import MemorySearchIndex
from memory_store import MemoryStore
from memory_vectors import MemoryVectorIndex, VectorIndexUnavailable, semantic_query_eligible


class FakeBackend:
    model_id = "test/multilingual"
    revision = "test-revision"
    fingerprint = "test-fingerprint-v1"
    dimension = 6

    def __init__(self, *, fail: bool = False, dimension: int = 6, fingerprint: str | None = None):
        self.fail = fail
        self.dimension = dimension
        if fingerprint:
            self.fingerprint = fingerprint

    def embed(self, texts):
        if self.fail:
            raise RuntimeError("embedding failed")
        return [self._one(text) for text in texts]

    def _one(self, text):
        lowered = text.lower()
        vector = [0.02, 0.02, 0.02, 0.02, 0.02, 0.02]
        if any(word in lowered for word in ("英语", "语法", "时态", "句式", "grammar")):
            vector[0] = 1.0
        if any(word in lowered for word in ("moonharbor", "月港", "vue", "vite", "pinia", "frontend", "前端", "技术栈")):
            vector[1] = 1.0
        if any(word in lowered for word in ("深红", "颜色", "color", "red")):
            vector[2] = 1.0
        if any(word in lowered for word in ("atomic", "rebuild", "half-written", "原子", "重建")):
            vector[3] = 1.0
        if any(word in lowered for word in ("西瓜", "吃", "food")):
            vector[4] = 1.0
        if any(word in lowered for word in ("天气", "下雨", "weather")):
            vector[5] = 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector[: self.dimension]]


def _record(mid, content, owner="gpt", source="observed", updated="2026-01-01T00:00:00Z"):
    return {"id": mid, "owner": owner, "scope": "agent", "category": "life/daily",
            "created_at": updated, "updated_at": updated, "content": content,
            "source": source, "status": "done"}


def _concurrent_search(root, queue):
    try:
        rows = MemoryVectorIndex(root, backend=FakeBackend()).search("复杂语法让我不想学", limit=10)
        queue.put(("ok", len(rows or [])))
    except Exception as exc:
        queue.put(("error", type(exc).__name__))


def _concurrent_rebuild(root, queue):
    try:
        store = MemoryStore(root, "gpt")
        rows = [row for row in store._read_all() if not str(row.get("id", "")).startswith("human:_house/")]
        queue.put(("ok", MemoryVectorIndex(root, backend=FakeBackend()).rebuild(rows)["indexed"]))
    except Exception as exc:
        queue.put(("error", type(exc).__name__))


class VectorIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memory-vectors-")
        self.root = Path(self.temp.name)
        self.store = MemoryStore(self.root, "gpt")
        self.backend = FakeBackend()
        self.index = MemoryVectorIndex(self.root, backend=self.backend)
        self.records = [
            _record("grammar", "不喜欢硬啃英语时态，更适合从基础句式开始"),
            _record("stack", "Moonharbor 使用 Vue3 + Vite + Pinia"),
            _record("color", "用户偏好深红色", owner="claude"),
            _record("atomic", "The release uses atomic replacement after a complete rebuild."),
            _record("food", "今天想吃西瓜"),
        ]

    def tearDown(self):
        self.temp.cleanup()

    def rebuild(self):
        return self.index.rebuild(self.records)

    def test_semantic_capability_chinese_english_mixed_and_negative(self):
        self.rebuild()
        self.assertEqual(self.index.search("看到复杂语法就不想学")[0]["memory_id"], "grammar")
        self.assertEqual(self.index.search("How does rebuild avoid a half-written index?")[0]["memory_id"], "atomic")
        self.assertEqual(self.index.search("月港 frontend 技术栈")[0]["memory_id"], "stack")
        self.assertEqual(self.index.search("她喜欢什么颜色", owner="claude")[0]["memory_id"], "color")
        self.assertEqual(self.index.search("天气预报说要下雨", min_similarity=0.9), [])

    def test_rebuild_and_idempotent_reuse(self):
        first = self.rebuild(); second = self.rebuild()
        self.assertEqual(first["embedded"], 5)
        self.assertEqual(second["reused"], 5)

    def test_model_fingerprint_mismatch_is_stale(self):
        self.rebuild()
        result = MemoryVectorIndex(self.root, backend=FakeBackend(fingerprint="new-model")).diagnostics(self.records, load_backend=True)
        self.assertEqual(result["status"], "STALE")

    def test_dimension_missing_orphan_hash_and_invalid_detected(self):
        self.rebuild()
        db = sqlite3.connect(self.index.db_path)
        db.execute("DELETE FROM memory_vectors WHERE memory_id='grammar'")
        db.execute("INSERT INTO memory_vectors SELECT 'orphan',owner,scope,source,updated_at,content_hash,model_id,model_fingerprint,dimension,embedding,indexed_at FROM memory_vectors LIMIT 1")
        db.execute("UPDATE memory_vectors SET content_hash='bad' WHERE memory_id='stack'")
        db.execute("UPDATE memory_vectors SET dimension=3 WHERE memory_id='food'")
        db.commit(); db.close()
        result = self.index.diagnostics(self.records)
        self.assertEqual(result["status"], "STALE")
        self.assertIn("grammar", result["missing"]); self.assertIn("orphan", result["orphan"])
        self.assertIn("stack", result["hash_mismatch"]); self.assertIn("food", result["invalid_vectors"])

    def test_incremental_upsert_update_delete(self):
        self.rebuild()
        added = _record("new", "复杂语法规则", updated="2026-02-01T00:00:00Z")
        self.index.upsert(added)
        self.assertEqual(self.index.search("语法学习")[0]["memory_id"], "new")
        added["content"] = "深红色偏好"; added["updated_at"] = "2026-02-01T00:00:00Z"
        self.index.upsert(added)
        self.assertEqual(self.index.search("喜欢什么颜色")[0]["memory_id"], "new")
        self.index.delete("new")
        self.assertNotIn("new", [row["memory_id"] for row in self.index.search("喜欢什么颜色")])

    def test_embedding_failure_does_not_lose_markdown(self):
        self.rebuild()
        failing = MemoryVectorIndex(self.root, backend=FakeBackend(fail=True))
        with patch("memory_vectors.MemoryVectorIndex", return_value=failing):
            saved = self.store.remember("durable memory despite vector failure", "life/daily", source="observed")
        self.assertTrue(saved["ok"])
        self.assertIsNotNone(self.store._find_record(saved["id"]))
        self.assertTrue(failing.is_dirty())

    def test_missing_corrupt_dirty_and_missing_backend_fallback(self):
        saved = self.store.remember("exact lexical fallback marker", "life/daily")
        with patch("memory_vectors.MemoryVectorIndex", return_value=self.index):
            self.assertEqual(self.store.recall("exact lexical fallback marker")[0]["id"], saved["id"])
            self.index.rebuild([self.store._find_record(saved["id"])[1]])
            self.index.mark_dirty("test")
            self.assertEqual(self.store.recall("exact lexical fallback marker")[0]["id"], saved["id"])
            self.index.dirty_path.unlink(); self.index.db_path.write_bytes(b"corrupt")
            self.assertEqual(self.store.recall("exact lexical fallback marker")[0]["id"], saved["id"])

    def test_semantic_zero_result_and_short_query_bypass(self):
        self.rebuild()
        self.assertEqual(self.index.search("天气预报", min_similarity=0.99), [])
        self.assertIsNone(self.index.search("红")); self.assertIsNone(self.index.search("😀"))
        self.assertFalse(semantic_query_eligible("ab")); self.assertTrue(semantic_query_eligible("abc"))

    def test_rrf_semantic_entry_exact_guard_and_limit(self):
        for row in self.records:
            self.store.import_record(row)
        MemorySearchIndex(self.root).rebuild(self.records)
        self.index.rebuild(self.records)
        with patch("memory_vectors.MemoryVectorIndex", return_value=self.index):
            semantic = self.store.recall("看到复杂语法就不想学", limit=2)
            self.assertIn("grammar", [row["id"] for row in semantic])
            exact = self.store.recall("今天想吃西瓜", limit=1)
            self.assertEqual(exact[0]["id"], "food")
            self.assertLessEqual(len(semantic), 2)

    def test_inferred_tie_owner_filter_and_empty_query(self):
        rows = [_record("neutral", "复杂语法", owner="gpt", source="observed"),
                _record("infer", "复杂语法", owner="gpt", source="inferred"),
                _record("other", "复杂语法", owner="claude", source="observed")]
        for row in rows: self.store.import_record(row)
        MemorySearchIndex(self.root).rebuild(rows); self.index.rebuild(rows)
        with patch("memory_vectors.MemoryVectorIndex", return_value=self.index):
            result = self.store.recall("复杂语法", owner="gpt", limit=3)
            self.assertEqual([row["id"] for row in result], ["neutral", "infer"])
            with patch.object(self.index, "search", side_effect=AssertionError("empty query embedded")):
                self.assertTrue(self.store.recall("", owner="gpt"))

    def test_doctor_vector_pass_and_stale(self):
        for row in self.records: self.store.import_record(row)
        current = [row for row in self.store._read_all() if not str(row.get("id", "")).startswith("human:_house/")]
        MemorySearchIndex(self.root).rebuild(current); self.index.rebuild(current)
        with patch("memory_doctor.importlib.util.find_spec", return_value=None):
            report = inspect_memory(self.root)
        self.assertEqual(report["vector_index"]["status"], "UNAVAILABLE")
        self.assertEqual(self.index.diagnostics(current)["status"], "PASS")
        self.index.mark_dirty("test")
        self.assertEqual(self.index.diagnostics(self.records)["status"], "STALE")

    def test_concurrent_search_and_rebuild(self):
        for row in self.records: self.store.import_record(row)
        self.index.rebuild(self.records)
        ctx = multiprocessing.get_context("spawn"); queue = ctx.Queue()
        workers = [ctx.Process(target=_concurrent_search, args=(str(self.root), queue)) for _ in range(2)]
        workers += [ctx.Process(target=_concurrent_rebuild, args=(str(self.root), queue)) for _ in range(2)]
        for process in workers: process.start()
        for process in workers: process.join(20); self.assertEqual(process.exitcode, 0)
        self.assertEqual([queue.get(timeout=2)[0] for _ in workers], ["ok"] * len(workers))
        current = [row for row in self.store._read_all() if not str(row.get("id", "")).startswith("human:_house/")]
        self.assertEqual(self.index.diagnostics(current)["status"], "PASS")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    unittest.main()

