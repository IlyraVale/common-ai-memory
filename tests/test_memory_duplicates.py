"""Read-only duplicate/overlap scanner. All writes stay in TemporaryDirectory."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import memory_duplicates
from memory_duplicates import classify_pair, normalize_text, normalized_hash, scan
from memory_manager import MemoryAdmin
from memory_search import rebuild_project as rebuild_fts
from memory_store import MemoryStore
from memory_vectors import DEFAULT_MODEL_ID, _BACKEND_CACHE, rebuild_project as rebuild_vec

SECRET = "PRIVATE-MEMORY-BODY"


def _unit(angle_deg: float, axis: int, dim: int = 8) -> list[float]:
    vector = [0.0] * dim
    vector[axis] = math.cos(math.radians(angle_deg))
    vector[(axis + 1) % dim] = math.sin(math.radians(angle_deg))
    return vector


class ControlledBackend:
    """Embeds chosen texts at chosen angles so cosine values are exact."""
    model_id = "test/controlled"
    revision = "1"
    fingerprint = "controlled"
    dimension = 8

    def __init__(self, table: dict[str, list[float]]):
        self.table = table
        self.calls = 0

    def embed(self, texts):
        self.calls += len(texts)
        out = []
        for text in texts:
            if text in self.table:
                out.append(self.table[text])
            else:
                axis = int(hashlib.sha256(text.encode()).hexdigest(), 16) % self.dimension
                out.append(_unit(0, axis))
        return out


def rec(memory_id: str, content: str, created: str = "2026-01-01T00:00:00Z", **extra) -> dict:
    return {"id": memory_id, "owner": "gpt", "content": content, "created_at": created, **extra}


class ClassificationTests(unittest.TestCase):
    def test_normalization_is_deterministic_and_meaning_preserving(self) -> None:
        self.assertEqual(normalize_text("  用户喜欢，无糖冰美式！ "), normalize_text("用户喜欢 无糖冰美式"))
        self.assertEqual(normalize_text("ＡＢＣ  Coffee."), "abc coffee")
        self.assertNotEqual(normalize_text("每天 10 元"), normalize_text("每天 5 元"))
        self.assertEqual(len(normalized_hash("x")), 64)

    def test_exact_and_punctuation_whitespace_duplicate(self) -> None:
        verdict = classify_pair(rec("a", "用户喜欢无糖冰美式。"), rec("b", "用户喜欢无糖冰美式"), None)
        self.assertEqual(verdict["relation"], "exact_duplicate")
        verdict = classify_pair(rec("a", "The user, prefers dark mode!"), rec("b", "the user prefers   dark mode"), 0.99)
        self.assertEqual(verdict["relation"], "exact_duplicate")

    def test_semantic_paraphrase(self) -> None:
        verdict = classify_pair(rec("a", "用户喜欢喝无糖的冰美式咖啡。"), rec("b", "用户偏爱不加糖的冰美式。"), 0.96)
        self.assertEqual(verdict["relation"], "likely_duplicate")

    def test_overlap(self) -> None:
        verdict = classify_pair(rec("a", "项目前端用 Vue 3 和 Vite 搭建。"),
                                rec("b", "项目前端用 Vue 3 和 Vite 搭建，后端用 FastAPI，数据库是 SQLite，部署在本机。"), 0.73)
        self.assertEqual(verdict["relation"], "overlap")

    def test_update_of_marks_newer_and_older(self) -> None:
        old, new = rec("old", "每天定投 10 元。", "2026-01-01T00:00:00Z"), rec("new", "后来改为每天定投 5 元。", "2026-03-01T00:00:00Z")
        verdict = classify_pair(old, new, 0.48)
        self.assertEqual((verdict["relation"], verdict["newer_id"], verdict["older_id"]), ("update_of", "new", "old"))
        verdict = classify_pair(rec("o", "The weekly sync is on Monday at 10am."),
                                rec("n", "The weekly sync has now moved to Tuesday at 2pm.", "2026-02-01T00:00:00Z"), 0.674)
        self.assertEqual(verdict["relation"], "update_of")

    def test_conflict_values_and_polarity(self) -> None:
        self.assertEqual(classify_pair(rec("a", "每周三下午开组会。"), rec("b", "每周四下午开组会。"), 0.92)["relation"], "conflict")
        self.assertEqual(classify_pair(rec("a", "用户对花生过敏。"), rec("b", "用户不对花生过敏。"), 0.85)["relation"], "conflict")
        self.assertEqual(classify_pair(rec("a", "The server runs on port 8080."),
                                       rec("b", "The server runs on port 9090."), 0.66)["relation"], "conflict")

    def test_unrelated_and_same_topic_different_facts_excluded(self) -> None:
        self.assertIsNone(classify_pair(rec("a", "她每天早上七点起床跑步。"), rec("b", "数据库备份放在外接硬盘。"), 0.03))
        self.assertIsNone(classify_pair(rec("a", "项目前端用 Vue 3 和 Vite 搭建。"), rec("b", "项目后端使用 FastAPI 提供接口。"), 0.60))
        self.assertIsNone(classify_pair(rec("a", "The cat sleeps on the windowsill."), rec("b", "Deploys happen on Fridays."), None))

    def test_false_positive_controls(self) -> None:
        # Two records of different identified events: same template, not a duplicate nor a conflict.
        a = rec("a", "Game gmk-e40184 finished: GPT black, Claude white, 35 moves.")
        b = rec("b", "Game gmk-dfa6a0 finished: GPT black, Claude white, 25 moves.")
        self.assertIsNone(classify_pair(a, b, 0.944))
        # Dates are not values: two diary notes on different days with a common word are not an update.
        a = rec("a", "2026-09-01 记录了一次部署，目前一切正常。")
        b = rec("b", "2026-09-27 写下了另一件事，和部署无关。", "2026-09-27T00:00:00Z")
        self.assertIsNone(classify_pair(a, b, 0.66))
        # Different dates and differing values without a change marker: uncertain, not conflict.
        a = rec("a", "2026-09-04 岛上收入大约 3 万。")
        b = rec("b", "2026-09-20 岛上收入大约 5 万。", "2026-09-20T00:00:00Z")
        self.assertEqual(classify_pair(a, b, 0.93)["relation"], "uncertain")

    def test_uncertain_is_allowed(self) -> None:
        verdict = classify_pair(rec("a", "她偏好前端和全栈方向的工作。"), rec("b", "长期职业方向偏前端，暂不考虑硬件。"), 0.86)
        self.assertEqual((verdict["relation"], verdict["confidence"]), ("uncertain", "low"))


class ScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="memory-duplicates-")
        self.root = Path(self.temp.name)
        gpt, claude = MemoryStore(self.root, "gpt"), MemoryStore(self.root, "claude")
        self.texts = {
            "dup_a": "用户喜欢喝无糖的冰美式咖啡。" + SECRET,
            "dup_b": "用户喜欢喝无糖的冰美式咖啡！" + SECRET,
            "para_a": "她每天早上七点起床去跑步。",
            "para_b": "她习惯每天早晨七点起来跑步。",
            "cross": "她习惯每天早晨七点起来跑步呀。",
            "other": "数据库备份放在外接硬盘里。",
        }
        self.ids = {}
        for key, text in self.texts.items():
            store = claude if key == "cross" else gpt
            self.ids[key] = store.remember(text, "life/daily")["id"]
        old = gpt.remember("旧的已被取代的笔记。", "life/daily")["id"]
        gpt.update(old, "旧的已被取代的笔记。", lifecycle="superseded", superseded_by=self.ids["other"])
        self.ids["superseded"] = old
        self.backend = ControlledBackend({
            self.texts["para_a"]: _unit(0, 2), self.texts["para_b"]: _unit(14, 2),   # cos ~0.970
            self.texts["cross"]: _unit(20, 2),
        })
        _BACKEND_CACHE[(os.path.normcase(str(self.root.resolve())), DEFAULT_MODEL_ID)] = self.backend
        rebuild_fts(self.root)
        rebuild_vec(self.root, backend=self.backend)
        self.backend.calls = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def snapshot(self) -> dict[str, str]:
        files = list((self.root / "memory").rglob("*.md")) + list((self.root / "state").glob("*.sqlite3"))
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}

    def test_scan_finds_exact_and_semantic_without_mutation_or_reembedding(self) -> None:
        before = self.snapshot()
        with patch("memory_vectors.FastEmbedBackend", side_effect=AssertionError("scanner must not embed")):
            report = scan(self.root)
        self.assertEqual(self.snapshot(), before, "scanner must not write anything")
        self.assertEqual(self.backend.calls, 0, "existing vectors reused; nothing re-embedded")
        self.assertTrue(report["read_only"])
        self.assertEqual(report["semantic"]["status"], "ok")
        self.assertEqual(report["semantic"]["vectors_used"], 7)
        relations = {(g["relation"], frozenset(g["memory_ids"])) for g in report["groups"]}
        self.assertIn(("exact_duplicate", frozenset({self.ids["dup_a"], self.ids["dup_b"]})), relations)
        self.assertIn(("likely_duplicate", frozenset({self.ids["para_a"], self.ids["para_b"]})), relations)
        unrelated = frozenset({self.ids["other"], self.ids["dup_a"]})
        self.assertFalse(any(ids == unrelated for _, ids in relations))

    def test_owner_labels_and_cross_owner(self) -> None:
        report = scan(self.root)
        cross = [g for g in report["groups"] if self.ids["cross"] in g["memory_ids"]]
        self.assertTrue(cross)
        for group in cross:
            self.assertTrue(group["cross_owner"])
            self.assertEqual(set(group["owners"]), {"gpt", "claude"})
            self.assertEqual({m["owner"] for m in group["members"]}, {"gpt", "claude"})
        self.assertTrue(all(g["owners"] == ["gpt"] for g in scan(self.root, owner="gpt")["groups"]))

    def test_superseded_pairs_skipped_but_visible_in_manager(self) -> None:
        report = scan(self.root)
        self.assertFalse(any(self.ids["superseded"] in g["memory_ids"] and self.ids["other"] in g["memory_ids"]
                             for g in report["groups"]))
        rows = MemoryAdmin(self.root).list({"lifecycle": "superseded"})
        self.assertEqual([r["id"] for r in rows], [self.ids["superseded"]])

    def test_stale_vectors_are_not_used_and_lexical_fallback_works(self) -> None:
        MemoryStore(self.root, "gpt").update(self.ids["para_a"], "她每天早上七点起床去跑步，风雨无阻。")
        report = scan(self.root)
        self.assertGreaterEqual(report["semantic"]["stale_vectors"] + report["semantic"]["vectors_used"], 6)
        (self.root / "state" / "memory-vectors.sqlite3").unlink()
        report = scan(self.root)
        self.assertEqual(report["semantic"]["status"], "unavailable")
        self.assertEqual(report["semantic"]["reason"], "vector_index_missing")
        self.assertIn("exact_duplicate", {g["relation"] for g in report["groups"]})
        self.assertTrue(all(g["signals"].get("cosine") is None for g in report["groups"] if g["relation"] != "exact_duplicate"))

    def test_cli_output_has_no_content_or_paths(self) -> None:
        for argv in (["scan", "--root", str(self.root)], ["scan", "--root", str(self.root), "--json"]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                memory_duplicates.main(argv)
            text = out.getvalue()
            self.assertNotIn(SECRET, text)
            self.assertNotIn(self.texts["para_a"], text)
            self.assertNotIn(str(self.root), text)
            self.assertNotIn(self.temp.name, text)
        report = json.loads(text)
        self.assertTrue(all("content" not in m for g in report["groups"] for m in g["members"]))

    def test_manager_view_includes_content_only_when_requested(self) -> None:
        self.assertTrue(any("content" in m for g in scan(self.root, include_content=True)["groups"] for m in g["members"]))


if __name__ == "__main__":
    unittest.main()
