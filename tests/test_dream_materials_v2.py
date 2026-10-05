"""Dream Materials v2: readable corpus, historical pool, cooldown, recency tiers, category diversity."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from dreams import (
    DreamConfig,
    DreamPreparer,
    DreamStore,
    dream_readable_corpus,
    recent_dream_sources,
    safe_dream_paths,
)
from dream_scraps import DreamScrapStore
from memory_feedback import MemoryFeedbackStore
from memory_store import MemoryStore

DAY = "2026-10-10"  # Asia/Shanghai: 2026-10-09T16:00Z .. 2026-10-10T16:00Z


def _days_before(days: int, hour: int = 4) -> str:
    return (date.fromisoformat(DAY) - timedelta(days=days)).isoformat() + f"T{hour:02d}:00:00Z"


def _mem(root: Path, memory_id: str, owner: str, created_at: str, *, scope: str = "agent",
         category: str = "life/daily", status: str | None = None, lifecycle: str | None = None,
         content: str | None = None) -> None:
    record = {"id": memory_id, "owner": owner, "scope": scope, "category": category,
              "created_at": created_at, "updated_at": created_at, "content": content or f"content {memory_id}"}
    if status:
        record["status"] = status
    if lifecycle:
        record["lifecycle"] = lifecycle
        if lifecycle == "superseded":
            record["superseded_by"] = "replacement"
    section, subject = category.split("/", 1)
    path = root / "memory" / section / subject / f"{created_at[:10]}_{memory_id}__{owner}__{scope}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(MemoryStore._serialize(record), encoding="utf-8")


def _past_dream(root: Path, owner: str, day: str, source_ids: list[str]) -> None:
    DreamStore(root).commit({
        "owner": owner, "dream_date": day, "timezone": "Asia/Shanghai",
        "generation_id": f"g-{owner}-{day}", "source_memory_ids": source_ids,
        "source_event_range": {"start": None, "end": None}, "truncated": False,
    }, f"dream {day}", runner="fake", model=None)


class MaterialsBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="dream-materials-v2-")
        self.root = Path(self.temp.name) / "梦 素材"
        self.root.mkdir()
        self.feedback_path = self.root / "state" / "memory-feedback.sqlite3"
        self.feedback = MemoryFeedbackStore(self.root, "gpt", db_path=self.feedback_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def prepare(self, owner: str = "gpt", **config) -> dict:
        preparer = DreamPreparer(self.root, feedback_db=self.feedback_path, config=DreamConfig(**config),
                                 scraps_enabled=False)
        return preparer.prepare(owner, dream_date=DAY)

    def rows(self, package: dict) -> dict:
        return {item["id"]: item for item in package["materials"]}

    def event(self, retrieval_id: str, owner: str, ids: list[str], created_at: str) -> None:
        db = self.feedback._connect()
        try:
            db.execute("INSERT INTO retrieval_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (retrieval_id, owner, "q", "all", json.dumps(ids), None, created_at))
            db.commit()
        finally:
            db.close()

    def verdict(self, retrieval_id: str, owner: str, memory_id: str, verdict: str, created_at: str) -> None:
        db = self.feedback._connect()
        try:
            db.execute("INSERT INTO retrieval_feedback(retrieval_id,agent_id,memory_id,verdict,note,source,created_at) "
                       "VALUES(?,?,?,?,?,?,?)", (retrieval_id, owner, memory_id, verdict, None, "system_test", created_at))
            db.commit()
        finally:
            db.close()


class CorpusTests(MaterialsBase):
    def setUp(self) -> None:
        super().setUp()
        _mem(self.root, "own-private", "gpt", _days_before(3))
        _mem(self.root, "own-shared", "gpt", _days_before(4), scope="shared")
        _mem(self.root, "other-shared", "claude", _days_before(5), scope="shared")
        _mem(self.root, "other-private", "claude", _days_before(6))
        house = self.root / "memory" / "_house"
        house.mkdir(parents=True)
        (house / "README.md").write_text("# house manual", encoding="utf-8")

    def test_corpus_owner_private_and_all_shared_only(self) -> None:
        ids = {row["id"] for row in dream_readable_corpus(self.root, "gpt")}
        self.assertEqual(ids, {"own-private", "own-shared", "other-shared"})  # 1-4
        self.assertFalse(any(str(i).startswith("human:") for i in ids))  # 5
        claude_ids = {row["id"] for row in dream_readable_corpus(self.root, "claude")}
        self.assertEqual(claude_ids, {"other-private", "other-shared", "own-shared"})

    def test_prepare_uses_the_corpus_and_never_other_private(self) -> None:
        for owner, forbidden in (("gpt", "other-private"), ("claude", "own-private")):
            with self.subTest(owner=owner):
                ids = set(self.prepare(owner)["source_memory_ids"])
                self.assertNotIn(forbidden, ids)
                self.assertNotIn("human:_house/README.md", ids)
        gpt_ids = set(self.prepare("gpt")["source_memory_ids"])
        self.assertTrue({"own-private", "own-shared", "other-shared"} <= gpt_ids)


class HistoricalPoolTests(MaterialsBase):
    def test_status_and_lifecycle_and_feedback_filters(self) -> None:
        _mem(self.root, "none", "gpt", _days_before(3))
        _mem(self.root, "done", "gpt", _days_before(3), status="done")
        _mem(self.root, "open", "gpt", _days_before(3), status="open")
        _mem(self.root, "stale-life", "gpt", _days_before(3), lifecycle="stale")
        _mem(self.root, "superseded", "gpt", _days_before(3), lifecycle="superseded")
        _mem(self.root, "review", "gpt", _days_before(3), lifecycle="review_needed")
        _mem(self.root, "corrected", "gpt", _days_before(3))
        _mem(self.root, "stale-fb", "gpt", _days_before(3))
        # feedback given on an earlier day still blocks the historical pool
        self.event("old", "gpt", ["corrected", "stale-fb"], _days_before(2))
        self.verdict("old", "gpt", "corrected", "corrected", _days_before(2, 5))
        self.verdict("old", "gpt", "stale-fb", "stale", _days_before(2, 5))
        rows = self.rows(self.prepare(historical_limit=10, historical_category_cap=10))
        self.assertIn("historical", rows["none"]["reasons"])  # 6
        self.assertIn("historical", rows["done"]["reasons"])  # 7
        for blocked in ("open", "stale-life", "superseded", "review", "corrected", "stale-fb"):  # 8-13
            self.assertNotIn(blocked, rows, blocked)

    def test_reason_name_is_historical(self) -> None:
        _mem(self.root, "a", "gpt", _days_before(3))
        package = self.prepare()
        self.assertEqual(package["materials"][0]["reasons"], ["historical"])
        self.assertNotIn("random_done", json.dumps(package))


class CooldownTests(MaterialsBase):
    def test_recently_dreamed_history_waits_when_enough_fresh_material(self) -> None:  # 14, 15
        for index in range(8):
            _mem(self.root, f"h{index}", "gpt", _days_before(3 + index), category=f"life/c{index}")
        _past_dream(self.root, "gpt", _days_before(2)[:10], ["h0", "h1"])
        ids = set(self.prepare(historical_limit=6)["source_memory_ids"])
        self.assertEqual(len(ids), 6)
        self.assertFalse({"h0", "h1"} & ids)

    def test_cooldown_only_covers_the_configured_window(self) -> None:
        _past_dream(self.root, "gpt", _days_before(3)[:10], ["inside"])
        _past_dream(self.root, "gpt", _days_before(9)[:10], ["outside"])
        sources = recent_dream_sources(self.root, "gpt", DAY, 7)
        self.assertIn("inside", sources)
        self.assertNotIn("outside", sources)

    def test_fallback_reuses_cooled_material_rather_than_fail(self) -> None:  # 16, 17
        _mem(self.root, "only-a", "gpt", _days_before(3))
        _mem(self.root, "only-b", "gpt", _days_before(4))
        _past_dream(self.root, "gpt", _days_before(1)[:10], ["only-a"])
        _past_dream(self.root, "gpt", _days_before(5)[:10], ["only-b"])
        package = self.prepare()
        self.assertEqual(package["source_memory_ids"][0], "only-b", "fresh-first, then longest since dreamed")
        self.assertEqual(set(package["source_memory_ids"]), {"only-a", "only-b"})
        self.assertTrue(package["materials"])

    def test_fallback_prefers_least_recent_then_least_used(self) -> None:
        for memory_id in ("x", "y", "z"):
            _mem(self.root, memory_id, "gpt", _days_before(10))
        _past_dream(self.root, "gpt", _days_before(1)[:10], ["x"])
        _past_dream(self.root, "gpt", _days_before(4)[:10], ["y", "z"])
        _past_dream(self.root, "gpt", _days_before(5)[:10], ["z"])
        self.assertEqual(self.prepare(historical_limit=3)["source_memory_ids"], ["y", "z", "x"])

    def test_new_is_not_excluded_by_cooldown(self) -> None:  # 22
        _mem(self.root, "today", "gpt", f"{DAY}T03:00:00Z")
        _past_dream(self.root, "gpt", _days_before(1)[:10], ["today"])
        rows = self.rows(self.prepare())
        self.assertEqual(rows["today"]["reasons"], ["new"])

    def test_retrieved_is_not_excluded_by_cooldown_but_prefers_fresh(self) -> None:  # 23
        for memory_id in ("r-cool", "r-fresh"):
            _mem(self.root, memory_id, "gpt", _days_before(20))
        _past_dream(self.root, "gpt", _days_before(1)[:10], ["r-cool"])
        self.event("t", "gpt", ["r-cool", "r-fresh"], f"{DAY}T02:00:00Z")
        package = self.prepare(retrieval_limit=1, historical_limit=0)
        self.assertEqual(package["source_memory_ids"], ["r-fresh"])
        package = self.prepare(retrieval_limit=2, historical_limit=0)
        self.assertEqual(set(package["source_memory_ids"]), {"r-cool", "r-fresh"})

    def test_missing_or_corrupt_dream_files_are_ignored(self) -> None:  # 29
        _mem(self.root, "m", "gpt", _days_before(3))
        _, broken = safe_dream_paths(self.root, "gpt", _days_before(1)[:10])
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("not a dream at all", encoding="utf-8")
        _, legacy = safe_dream_paths(self.root, "gpt", _days_before(2)[:10])
        legacy.write_text("---\nschema_version: 1\nowner: \"gpt\"\n---\nold dream without sources\n", encoding="utf-8")
        _, binary = safe_dream_paths(self.root, "gpt", _days_before(3)[:10])
        binary.write_bytes(b"\xff\xfe\x00broken")
        self.assertEqual(recent_dream_sources(self.root, "gpt", DAY, 7), {})
        self.assertEqual(self.prepare()["source_memory_ids"], ["m"])


class TierAndDiversityTests(MaterialsBase):
    def test_recent_history_preferred_and_older_still_gets_a_slot(self) -> None:  # 18, 19
        for index in range(6):
            _mem(self.root, f"recent{index}", "gpt", _days_before(2 + index), category=f"life/r{index}")
        for index in range(6):
            _mem(self.root, f"old{index}", "gpt", _days_before(60 + index), category=f"plan/o{index}")
        ids = self.prepare(historical_limit=6, historical_older_slots=1)["source_memory_ids"]
        self.assertEqual(sum(i.startswith("recent") for i in ids), 5)
        self.assertEqual(sum(i.startswith("old") for i in ids), 1)

    def test_older_fills_when_recent_is_short(self) -> None:
        _mem(self.root, "recent0", "gpt", _days_before(2))
        for index in range(6):
            _mem(self.root, f"old{index}", "gpt", _days_before(90 + index), category=f"plan/o{index}")
        ids = self.prepare(historical_limit=4)["source_memory_ids"]
        self.assertIn("recent0", ids)
        self.assertEqual(len(ids), 4)

    def test_category_cap_and_safe_relaxation(self) -> None:  # 20, 21
        for index in range(5):
            _mem(self.root, f"p{index}", "gpt", _days_before(2 + index), category="project/general")
        for index in range(3):
            _mem(self.root, f"l{index}", "gpt", _days_before(2 + index), category=f"life/x{index}")
        rows = self.rows(self.prepare(historical_limit=5, historical_category_cap=2))
        self.assertEqual(sum(1 for item in rows.values() if item["category"] == "project/general"), 2)
        self.assertEqual(len(rows), 5)
        # only one category in the whole library: the cap gives way instead of starving the Dream
        tmp = self.root
        for path in (tmp / "memory" / "life").rglob("*.md"):
            path.unlink()
        self.assertEqual(len(self.prepare(historical_limit=5, historical_category_cap=2)["materials"]), 5)

    def test_new_and_retrieved_are_not_capped(self) -> None:
        for index in range(4):
            _mem(self.root, f"n{index}", "gpt", f"{DAY}T0{index}:00:00Z", category="project/general")
        rows = self.rows(self.prepare(historical_category_cap=1))
        self.assertEqual(sum(1 for i in rows.values() if "new" in i["reasons"]), 4)

    def test_same_id_merges_reasons_without_taking_two_slots(self) -> None:  # 24
        _mem(self.root, "both", "gpt", f"{DAY}T01:00:00Z")
        _mem(self.root, "hist-retrieved", "gpt", _days_before(3))
        self.event("t", "gpt", ["both", "hist-retrieved"], f"{DAY}T02:00:00Z")
        package = self.prepare()
        self.assertEqual(package["source_memory_ids"].count("both"), 1)
        rows = self.rows(package)
        self.assertEqual(rows["both"]["reasons"], ["new", "retrieved"])
        self.assertEqual(rows["hist-retrieved"]["reasons"], ["retrieved", "historical"])
        self.assertEqual(len(package["materials"]), len(set(package["source_memory_ids"])))


class StabilityTests(MaterialsBase):
    def setUp(self) -> None:
        super().setUp()
        for index in range(20):
            _mem(self.root, f"m{index:02d}", "gpt", _days_before(2 + index * 3),
                 category=f"life/c{index % 4}", content=f"中文记忆 {index} · Windows 路径 C:\\临时\\{index}")
        _past_dream(self.root, "gpt", _days_before(1)[:10], ["m00", "m01"])

    def test_deterministic_selection_and_generation_id(self) -> None:  # 25, 26, 30
        first, second = self.prepare(), self.prepare()
        self.assertEqual(first["source_memory_ids"], second["source_memory_ids"])
        self.assertEqual(first["generation_id"], second["generation_id"])
        self.assertTrue(any("中文记忆" in item["content"] for item in first["materials"]))
        other_day = DreamPreparer(self.root, feedback_db=self.feedback_path, scraps_enabled=False).prepare(
            "gpt", dream_date="2026-10-11")
        self.assertNotEqual(first["generation_id"], other_day["generation_id"])

    def test_source_ids_match_materials(self) -> None:  # 27
        package = self.prepare()
        self.assertEqual(package["source_memory_ids"], [item["id"] for item in package["materials"]])

    def test_scrap_behavior_unchanged(self) -> None:  # 28
        DreamScrapStore(self.root).add("gpt", "a passing image", source_type="memory", source_ref="m03",
                                       now=__import__("datetime").datetime(2026, 10, 9, 10, 0,
                                                                           tzinfo=__import__("datetime").timezone.utc))
        package = DreamPreparer(self.root, feedback_db=self.feedback_path).prepare("gpt", dream_date=DAY)
        scraps = [item for item in package["materials"] if item.get("material_source") == "ephemeral_scrap"]
        self.assertLessEqual(len(scraps), 1)
        for scrap in scraps:
            self.assertIs(scrap["factual_authority"], False)
            self.assertTrue(scrap["derived"])
            self.assertNotIn(scrap["id"], package["source_memory_ids"])


if __name__ == "__main__":
    unittest.main()
