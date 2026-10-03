from __future__ import annotations

import json
import multiprocessing
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dream_scraps import DreamScrapStore
from dreams import DreamPreparer, PreparedDreamStore, claim_on_wake_dream
from memory_store import MemoryStore
from memory_witness import MemoryWitnessStore


NOW = datetime(2026, 9, 30, 4, tzinfo=timezone.utc)


def _add_scrap(root: str, marker: int) -> None:
    DreamScrapStore(root, busy_timeout_ms=15000).add(
        "gpt", f"fragment {marker}", source_type="filtered_candidate", now=NOW
    )


def _expose(root: str, marker: int) -> None:
    MemoryWitnessStore(root, busy_timeout_ms=15000).expose(
        "gpt", ["m1"], f"episode-{marker}", source="recall", context_kind="recall", now=NOW
    )


class ScrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scraps-"); self.root = Path(self.temp.name)
        self.store = DreamScrapStore(self.root)

    def tearDown(self): self.temp.cleanup()

    def test_add_defaults_owner_isolation_and_schema(self):
        row = self.store.add("gpt", "small atmosphere", source_type="filtered_candidate", now=NOW)
        self.assertTrue(row["derived"] and row["ephemeral"]); self.assertFalse(row["factual_authority"])
        self.assertEqual(row["expires_at"], "2026-10-03T04:00:00Z")
        self.assertEqual(len(self.store.eligible("gpt", cutoff=NOW, now=NOW)), 1)
        self.assertEqual(self.store.eligible("claude", cutoff=NOW, now=NOW), [])

    def test_expiry_cleanup_is_idempotent(self):
        self.store.add("gpt", "old", source_type="filtered_candidate", now=NOW - timedelta(hours=80))
        self.assertEqual(self.store.eligible("gpt", cutoff=NOW, now=NOW), [])
        self.assertEqual(self.store.cleanup(NOW), 0)

    def test_secret_deleted_corrected_stale_rejected(self):
        for content, source in (("api_key=secretvalue123456", "filtered_candidate"), ("gone", "forget"), ("wrong", "corrected"), ("old", "stale")):
            with self.subTest(source=source):
                with self.assertRaises(ValueError): self.store.add("gpt", content, source_type=source, now=NOW)

    def test_source_ref_delete_removes_scrap(self):
        self.store.add("gpt", "temporary", source_type="filtered_candidate", source_ref="m1", now=NOW)
        self.assertEqual(self.store.delete_by_source_ref("gpt", "m1"), 1)
        self.assertEqual(self.store.eligible("gpt", cutoff=NOW, now=NOW), [])

    def test_recall_never_reads_scraps(self):
        self.store.add("gpt", "unique scrap phrase", source_type="filtered_candidate", now=NOW)
        self.assertEqual(MemoryStore(self.root, "gpt").recall("unique scrap phrase"), [])

    def test_prepare_selects_at_most_one_deterministically_and_marks_nonfact(self):
        for i in range(4): self.store.add("gpt", f"fragment {i}", source_type="filtered_candidate", now=NOW - timedelta(hours=16))
        first = DreamPreparer(self.root).prepare("gpt", dream_date="2026-09-29", now=NOW)
        second = DreamPreparer(self.root).prepare("gpt", dream_date="2026-09-29", now=NOW)
        scraps1 = [x for x in first["materials"] if x.get("material_source") == "ephemeral_scrap"]
        scraps2 = [x for x in second["materials"] if x.get("material_source") == "ephemeral_scrap"]
        self.assertEqual(len(scraps1), 1); self.assertEqual(scraps1, scraps2)
        self.assertFalse(scraps1[0]["factual_authority"])

    def test_prepared_reuse_keeps_scrap(self):
        self.store.add("gpt", "one fragment", source_type="filtered_candidate", now=NOW - timedelta(hours=16))
        packets = PreparedDreamStore(self.root)
        first, _ = packets.prepare(DreamPreparer(self.root), "gpt", "2026-09-29", now=NOW)
        self.store.add("gpt", "later fragment", source_type="filtered_candidate", now=NOW - timedelta(hours=15))
        second, created = packets.prepare(DreamPreparer(self.root), "gpt", "2026-09-29", now=NOW)
        self.assertFalse(created); self.assertEqual(first, second)

    def test_concurrent_writes_are_safe(self):
        context=multiprocessing.get_context("spawn"); ps=[context.Process(target=_add_scrap,args=(str(self.root),i)) for i in range(4)]
        for p in ps:p.start()
        for p in ps:p.join(20);self.assertEqual(p.exitcode,0)
        self.assertEqual(len(self.store.eligible("gpt",cutoff=NOW,now=NOW)),4)


class WitnessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="witness-");self.root=Path(self.temp.name);self.store=MemoryWitnessStore(self.root)
    def tearDown(self):self.temp.cleanup()

    def test_unexposed_is_independent_and_helper_changes(self):
        self.assertFalse(self.store.has_independent_witness("gpt","m1"))
        result=self.store.record_witness("gpt","m1","e1","episode-a",evidence_at=NOW)
        self.assertTrue(result["independent"]);self.assertTrue(self.store.has_independent_witness("gpt","m1"))

    def test_recall_wake_memory_get_search_and_dream_exposures_block(self):
        for kind in ("recall","wake_context","memory_get","active_search","dream_material"):
            episode=f"ep-{kind}";self.store.expose("gpt",["m1"],episode,source=kind,context_kind=kind,now=NOW)
            result=self.store.record_witness("gpt","m1",f"e-{kind}",episode,evidence_at=NOW)
            self.assertFalse(result["independent"]);self.assertIn(kind,result["reason"])

    def test_day_and_source_do_not_override_same_episode_exposure(self):
        self.store.expose("gpt",["m1"],"episode",source="recall",context_kind="recall",now=NOW)
        result=self.store.record_witness("gpt","m1","different-source","episode",evidence_at=NOW+timedelta(days=2))
        self.assertFalse(result["independent"])

    def test_new_unexposed_episode_can_be_independent(self):
        self.store.expose("gpt",["m1"],"old",source="recall",context_kind="recall",now=NOW)
        self.assertTrue(self.store.record_witness("gpt","m1","e2","new",evidence_at=NOW)["independent"])

    def test_owner_and_memory_isolation(self):
        self.store.expose("gpt",["m1"],"ep",source="recall",context_kind="recall",now=NOW)
        self.assertTrue(self.store.record_witness("gpt","m2","e2","ep",evidence_at=NOW)["independent"])
        self.assertTrue(self.store.record_witness("claude","m1","e3","ep",evidence_at=NOW)["independent"])

    def test_delete_evidence_and_no_body_columns(self):
        self.store.record_witness("gpt","m1","source-message","ep",evidence_at=NOW)
        self.assertEqual(self.store.delete_evidence_ref("gpt","source-message"),1)
        db=sqlite3.connect(self.root/"state"/"memory-witness.sqlite3")
        try:
            columns={r[1] for table in ("memory_exposures","memory_witnesses") for r in db.execute(f"pragma table_info({table})")}
        finally:db.close()
        self.assertFalse({"content","body","text"}&columns)

    def test_concurrent_exposure_writes(self):
        context=multiprocessing.get_context("spawn");ps=[context.Process(target=_expose,args=(str(self.root),i)) for i in range(4)]
        for p in ps:p.start()
        for p in ps:p.join(20);self.assertEqual(p.exitcode,0)
        db=sqlite3.connect(self.root/"state"/"memory-witness.sqlite3")
        try:self.assertEqual(db.execute("select count(*) from memory_exposures").fetchone()[0],4)
        finally:db.close()

    def test_disabled_config_skips_dream_material_exposure(self):
        (self.root / "owner-config.json").write_text(
            json.dumps({"owners": {"gpt": {"independent_witness_enabled": False}}}),
            encoding="utf-8",
        )
        pending = claim_on_wake_dream(self.root, "gpt", now=NOW)
        self.assertIsNotNone(pending)
        self.assertFalse((self.root / "state" / "memory-witness.sqlite3").exists())


if __name__=="__main__":unittest.main()
