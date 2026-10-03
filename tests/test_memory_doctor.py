from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from memory_doctor import inspect_memory, main
from memory_store import MemoryStore


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memory-doctor-")
        self.root = Path(self.temp.name)
        self.store = MemoryStore(self.root, "gpt")

    def tearDown(self):
        self.temp.cleanup()

    def remember(self, content="独特星砂坐标", **kwargs):
        return self.store.remember(content, "life/daily", **kwargs)

    def path_for(self, saved):
        found = self.store._find_record(saved["id"])
        self.assertIsNotNone(found)
        return found[0]

    def test_healthy_store_passes_and_legacy_only_warns(self):
        self.remember()
        report = inspect_memory(self.root)
        self.assertEqual(report["result"], "PASS")
        self.assertEqual(report["files"], 1)
        self.assertEqual(report["parsed"], 1)
        self.assertEqual(report["warnings"], 2)

    def test_malformed_frontmatter_detected(self):
        path = self.path_for(self.remember())
        path.write_text("---\nid: nope\nbroken", encoding="utf-8")
        report = inspect_memory(self.root)
        self.assertEqual(report["result"], "FAIL")
        self.assertEqual(report["parse_errors"], 1)

    def test_duplicate_id_detected(self):
        path = self.path_for(self.remember())
        path.with_name("duplicate.md").write_bytes(path.read_bytes())
        self.assertEqual(inspect_memory(self.root)["duplicate_ids"], 1)

    def test_invalid_source_detected(self):
        path = self.path_for(self.remember(source="observed"))
        path.write_text(path.read_text(encoding="utf-8").replace('source: "observed"', 'source: "guess"'), encoding="utf-8")
        self.assertIn("invalid_source", {x["error_type"] for x in inspect_memory(self.root)["errors"]})

    def test_invalid_status_detected(self):
        path = self.path_for(self.remember(status="open"))
        path.write_text(path.read_text(encoding="utf-8").replace('status: "open"', 'status: "maybe"'), encoding="utf-8")
        self.assertIn("invalid_status", {x["error_type"] for x in inspect_memory(self.root)["errors"]})

    def test_invalid_category_detected(self):
        path = self.path_for(self.remember())
        path.write_text(path.read_text(encoding="utf-8").replace('category: "life/daily"', 'category: "bad/category"'), encoding="utf-8")
        self.assertIn("invalid_category", {x["error_type"] for x in inspect_memory(self.root)["errors"]})

    def test_normal_memory_probe_searchable(self):
        self.remember("火星玻璃鲸落坐标")
        report = inspect_memory(self.root)
        self.assertEqual(report["searchable"], 1)
        self.assertEqual(report["search_failures"], 0)

    def test_long_body_tail_is_searched(self):
        saved = self.remember(("普通前文 " * 1200) + "尾部独有markerquartz")
        self.assertEqual(self.store.recall("markerquartz")[0]["id"], saved["id"])
        self.assertEqual(inspect_memory(self.root)["search_failures"], 0)

    def test_owner_filter(self):
        saved = self.remember("私有银河针脚")
        self.assertIn(saved["id"], {x["id"] for x in self.store.recall("银河针脚", owner="gpt")})
        self.assertNotIn(saved["id"], {x["id"] for x in self.store.recall("银河针脚", owner="claude")})
        self.assertEqual(inspect_memory(self.root)["result"], "PASS")

    def test_house_manual_not_counted_as_memory_error(self):
        self.remember()
        house = self.root / "memory" / "_house"
        house.mkdir(parents=True)
        (house / "HOUSE.md").write_text("# manual without frontmatter", encoding="utf-8")
        report = inspect_memory(self.root)
        self.assertEqual(report["files"], 1)
        self.assertEqual(report["result"], "PASS")

    def test_untestable_probe_is_warning_free_and_safe(self):
        self.remember("!!! ???")
        report = inspect_memory(self.root)
        self.assertEqual(report["result"], "PASS")
        self.assertEqual(report["untestable"], 1)

    def test_doctor_does_not_modify_memory(self):
        path = self.path_for(self.remember())
        before = path.read_bytes()
        inspect_memory(self.root)
        self.assertEqual(path.read_bytes(), before)

    def test_json_and_exit_codes(self):
        self.remember()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--root", str(self.root), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["result"], "PASS")
        bad = self.root / "memory" / "life" / "daily" / "bad.md"
        bad.write_text("broken", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--root", str(self.root), "--json"]), 1)

    def test_missing_memory_root_stays_read_only(self):
        other = self.root / "empty-project"
        other.mkdir()
        self.assertEqual(inspect_memory(other)["result"], "PASS")
        self.assertFalse((other / "memory").exists())


class SourceRankingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="source-rank-")
        self.store = MemoryStore(self.temp.name, "gpt")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def item(memory_id, content, source=None, updated="2026-01-01T00:00:00Z", category="life/daily", owner="gpt"):
        result = {"id": memory_id, "content": content, "category": category, "owner": owner, "updated_at": updated}
        if source is not None:
            result["source"] = source
        return result

    def test_equal_observed_beats_inferred(self):
        rows = [self.item("i", "蓝色月亮", "inferred", "2026-02-01Z"), self.item("o", "蓝色月亮", "observed")]
        self.assertEqual(self.store._rank("蓝色月亮", rows)[0]["id"], "o")

    def test_equal_user_statement_beats_inferred(self):
        rows = [self.item("i", "蓝色月亮", "inferred", "2026-02-01Z"), self.item("u", "蓝色月亮", "user_statement")]
        self.assertEqual(self.store._rank("蓝色月亮", rows)[0]["id"], "u")

    def test_missing_source_is_neutral(self):
        rows = [self.item("i", "蓝色月亮", "inferred", "2026-02-01Z"), self.item("legacy", "蓝色月亮")]
        self.assertEqual(self.store._rank("蓝色月亮", rows)[0]["id"], "legacy")

    def test_observed_and_user_statement_are_not_artificially_ordered(self):
        rows = [self.item("o", "蓝色月亮", "observed"), self.item("u", "蓝色月亮", "user_statement", "2026-02-01Z")]
        self.assertEqual(self.store._rank("蓝色月亮", rows)[0]["id"], "u")

    def test_more_relevant_inferred_still_wins(self):
        rows = [self.item("i", "蓝色月亮 蓝色月亮", "inferred"), self.item("o", "蓝色月亮", "observed")]
        self.assertEqual(self.store._rank("蓝色月亮", rows)[0]["id"], "i")

    def test_exact_inferred_beats_category_only_observed(self):
        rows = [self.item("i", "life marker", "inferred", category="project/common-ai-memory"), self.item("o", "别的正文", "observed", category="life/daily")]
        self.assertEqual(self.store._rank("life", rows)[0]["id"], "i")

    def test_owner_filter_and_limit_unchanged(self):
        for index in range(3):
            self.store.remember(f"共同查询词 marker {index}", "life/daily", source="inferred" if index == 0 else "observed")
        self.assertEqual(len(self.store.recall("共同查询词", owner="gpt", limit=2)), 2)
        self.assertEqual(self.store.recall("共同查询词", owner="claude"), [])

    def test_legacy_ranking_is_deterministic(self):
        rows = [self.item("a", "蓝色月亮", updated="2026-01-01Z"), self.item("b", "蓝色月亮", updated="2026-02-01Z")]
        self.assertEqual([x["id"] for x in self.store._rank("蓝色月亮", rows)], ["b", "a"])


if __name__ == "__main__":
    unittest.main()
