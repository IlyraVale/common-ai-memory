from __future__ import annotations

import asyncio
import importlib.util
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
import time as time_module
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dreams import (
    ApiRunner,
    CliRunner,
    DreamConfig,
    DreamLeaseStore,
    DreamPipeline,
    DreamPreparer,
    DreamStore,
    OwnerConfig,
    PreparedDreamStore,
    RunnerConfig,
    attach_dream_to_wake,
    catch_up_due,
    claim_on_wake_dream,
    build_prompt,
    dream_instruction,
    dream_commit_result,
    dream_get_result,
    effective_retrievals,
    load_owner_config,
    most_recent_complete_dream_date,
    normalize_query_fingerprint,
    parse_dream,
    run_configured_dream,
    safe_dream_paths,
    serialize_dream,
    _cross_process_file_lock,
)
from memory_feedback import MemoryFeedbackStore
from memory_store import MemoryStore
from dream_nightly import run_batch, run_owner


HERE = Path(__file__).resolve().parent.parent


class FakeRunner:
    name = "fake"
    model = "fake-v1"

    def __init__(self, output: str = "a derived dream") -> None:
        self.output = output
        self.calls = 0

    def run(self, package):
        self.calls += 1
        return self.output


class FakeApiAdapter:
    name = "fake"
    model = "fake-api-v1"

    def __init__(self, output="api dream", error=None):
        self.output = output
        self.error = error
        self.calls = []

    def generate(self, prompt, *, timeout, metadata):
        self.calls.append((prompt, timeout, metadata))
        if self.error:
            raise self.error
        return self.output


def _record(root: Path, memory_id: str, owner: str, created_at: str, *, status=None, content=None):
    record = {
        "id": memory_id,
        "owner": owner,
        "scope": "agent",
        "category": "plan/general",
        "created_at": created_at,
        "updated_at": created_at,
        "content": content or f"content {memory_id}",
    }
    if status:
        record["status"] = status
    path = root / "memory" / "plan" / "general" / f"{created_at[:10]}_{memory_id}__{owner}__agent.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(MemoryStore._serialize(record), encoding="utf-8")
    return record


def _event(feedback: MemoryFeedbackStore, retrieval_id: str, owner: str, query: str, ids, created_at: str):
    db = feedback._connect()
    try:
        db.execute(
            "INSERT INTO retrieval_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (retrieval_id, owner, query, "all", json.dumps(ids), None, created_at),
        )
        db.commit()
    finally:
        db.close()


def _feedback(feedback: MemoryFeedbackStore, retrieval_id: str, owner: str, memory_id: str, verdict: str):
    db = feedback._connect()
    try:
        db.execute(
            "INSERT INTO retrieval_feedback(retrieval_id,agent_id,memory_id,verdict,note,source,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (retrieval_id, owner, memory_id, verdict, None, "system_test", "2026-09-29T04:00:00Z"),
        )
        db.commit()
    finally:
        db.close()


def _package(owner: str, day: str):
    return {
        "owner": owner,
        "dream_date": day,
        "timezone": "Asia/Shanghai",
        "generation_id": f"generation-{owner}-{day}",
        "source_memory_ids": ["m1", "m1"],
        "source_event_range": {"start": None, "end": None},
        "truncated": False,
    }


def _process_commit(root: str, owner: str, day: str, marker: str):
    result = DreamStore(root).commit(_package(owner, day), f"dream {owner}", runner="fake", model=None)
    Path(root, f"result-{marker}.json").write_text(json.dumps({"created": result["created"]}), encoding="utf-8")


def _process_claim(root: str, marker: str) -> None:
    store = DreamLeaseStore(root, busy_timeout_ms=15000)
    claim = store.claim(
        "gpt", "2026-09-30", timedelta(minutes=10),
        datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc),
    )
    Path(root, f"claim-{marker}.json").write_text(
        json.dumps({"token": None if claim is None else claim.claim_token}),
        encoding="utf-8",
    )


def _process_pending(root: str, marker: str) -> None:
    pending = claim_on_wake_dream(
        root,
        "gpt",
        now=datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc),
    )
    Path(root, f"pending-{marker}.json").write_text(
        json.dumps({"token": None if pending is None else pending["claim_token"]}),
        encoding="utf-8",
    )


def _process_grace_pending(root: str, marker: str, now_iso: str) -> None:
    pending = claim_on_wake_dream(
        root,
        "gpt",
        now=datetime.fromisoformat(now_iso),
        timezone_name="Asia/Shanghai",
    )
    Path(root, f"grace-{marker}.json").write_text(
        json.dumps({"token": None if pending is None else pending["claim_token"],
                    "date": None if pending is None else pending["dream_date"]}),
        encoding="utf-8",
    )


def _process_prepare_packet(root: str, marker: str) -> None:
    store = PreparedDreamStore(root)
    packet, created = store.prepare(
        DreamPreparer(root), "gpt", "2026-09-29",
        timezone_name="Asia/Shanghai",
        now=datetime(2026, 9, 30, tzinfo=timezone.utc),
    )
    Path(root, f"prepare-{marker}.json").write_text(
        json.dumps({"generation_id": packet["generation_id"], "created": created}), encoding="utf-8"
    )


def _process_nightly_batch(root: str) -> None:
    results, code = run_batch(
        Path(root), ["gpt"], timezone_name="Asia/Shanghai",
        now=datetime(2026, 9, 30, 4, tzinfo=timezone.utc),
    )
    Path(root, "batch-result.json").write_text(
        json.dumps({"results": results, "code": code}), encoding="utf-8"
    )


def _process_protocol_commit(root: str, dream_date: str, token: str, marker: str) -> None:
    result = dream_commit_result(
        root,
        "gpt",
        dream_date,
        token,
        "concurrent body",
        now=datetime(2026, 9, 30, 4, 1, tzinfo=timezone.utc),
    )
    Path(root, f"protocol-commit-{marker}.json").write_text(
        json.dumps({"status": result["status"], "error": result["error"]}),
        encoding="utf-8",
    )


class DreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dream-v1-")
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.feedback_path = self.root / "state" / "memory-feedback.sqlite3"
        self.feedback = MemoryFeedbackStore(self.root, "gpt", db_path=self.feedback_path)
        self.config = DreamConfig(new_limit=4, retrieval_limit=4, historical_limit=2, total_chars=5000)
        self.preparer = DreamPreparer(self.root, feedback_db=self.feedback_path, config=self.config)

    def tearDown(self):
        self.temp.cleanup()

    def test_new_retrieval_feedback_and_corrected_selection(self):
        _record(self.root, "new", "gpt", "2026-09-29T01:00:00Z")
        _record(self.root, "used", "gpt", "2026-09-20T01:00:00Z")
        _record(self.root, "ignored", "gpt", "2026-09-20T02:00:00Z")
        _record(self.root, "stale", "gpt", "2026-09-20T03:00:00Z")
        _record(self.root, "corrected", "gpt", "2026-09-20T04:00:00Z")
        _record(self.root, "neutral", "gpt", "2026-09-20T05:00:00Z")
        ids = ["used", "ignored", "stale", "corrected", "neutral"]
        _event(self.feedback, "r1", "gpt", "same query", ids, "2026-09-29T02:01:00Z")
        for memory_id, verdict in (("used", "used"), ("ignored", "ignored"), ("stale", "stale"), ("corrected", "corrected")):
            _feedback(self.feedback, "r1", "gpt", memory_id, verdict)
        package = self.preparer.prepare("gpt", dream_date="2026-09-29")
        rows = {item["id"]: item for item in package["materials"]}
        self.assertIn("new", rows)
        self.assertNotIn("corrected", rows)
        self.assertGreater(rows["used"]["score"], rows["neutral"]["score"])
        self.assertLess(rows["ignored"]["score"], rows["neutral"]["score"])
        self.assertLess(rows["stale"]["score"], rows["ignored"]["score"])
        self.assertEqual(rows["neutral"]["feedback"], [])

    def test_episode_normalization_bucket_and_cap(self):
        _record(self.root, "m", "gpt", "2026-09-20T01:00:00Z")
        for index, stamp in enumerate(("00:01", "00:20", "00:31", "01:01", "02:01"), 1):
            _event(self.feedback, f"r{index}", "gpt", " Hello　WORLD ", ["m"], f"2026-09-29T{stamp}:00Z")
        db = self.feedback._connect()
        try:
            counts, _, _, _ = effective_retrievals(db, "gpt", "2026-09-29")
        finally:
            db.close()
        self.assertEqual(counts["m"], 3)
        self.assertEqual(normalize_query_fingerprint(" Hello　WORLD "), normalize_query_fingerprint("hello world"))

    def test_historical_reproducible_and_filtered(self):
        for memory_id, status in (("a", "done"), ("b", "done"), ("c", "open"), ("d", "done")):
            _record(self.root, memory_id, "gpt", "2026-09-20T01:00:00Z", status=status)
        _event(self.feedback, "r1", "gpt", "q", ["d"], "2026-09-29T01:00:00Z")
        _feedback(self.feedback, "r1", "gpt", "d", "stale")
        first = self.preparer.prepare("gpt", dream_date="2026-09-29")
        second = self.preparer.prepare("gpt", dream_date="2026-09-29")
        self.assertEqual(first["source_memory_ids"], second["source_memory_ids"])
        rows = {item["id"]: item for item in first["materials"]}
        self.assertEqual(
            {memory_id for memory_id, item in rows.items() if "historical" in item["reasons"]},
            {"a", "b"},
        )
        self.assertNotIn("historical", rows["d"]["reasons"])
        self.assertNotIn("c", rows)

    def test_owner_isolation_and_timezone_boundary(self):
        _record(self.root, "g1", "gpt", "2026-09-28T16:30:00Z")
        _record(self.root, "c1", "claude", "2026-09-28T16:30:00Z")
        package = self.preparer.prepare("gpt", dream_date="2026-09-29", timezone_name="Asia/Shanghai")
        self.assertEqual(package["source_memory_ids"], ["g1"])

    def test_owner_dream_mode_default_and_valid_values(self):
        self.assertEqual(
            load_owner_config(self.root, "gpt"),
            OwnerConfig(owner="gpt", dream_mode="on_wake"),
        )
        path = self.root / "owner-config.json"
        config = {
            "schema_version": 7,
            "owners": {
                "gpt": {"dream_mode": "cli", "display_name": "GPT"},
                "claude": {"dream_mode": "api", "enabled": False},
                "local": {"dream_mode": "on_wake", "other": {"keep": True}},
                "defaulted": {"display_name": "No mode"},
            },
            "unrelated": {"preserve": "exactly"},
        }
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        before = path.read_bytes()

        self.assertEqual(load_owner_config(self.root, "gpt").dream_mode, "cli")
        self.assertEqual(load_owner_config(self.root, "claude").dream_mode, "api")
        self.assertEqual(load_owner_config(self.root, "local").dream_mode, "on_wake")
        self.assertEqual(load_owner_config(self.root, "defaulted").dream_mode, "on_wake")
        self.assertEqual(load_owner_config(self.root, "missing").dream_mode, "on_wake")
        self.assertEqual(path.read_bytes(), before)

    def test_owner_dream_mode_invalid_values_are_rejected(self):
        path = self.root / "owner-config.json"
        for value in ("CLI", "onwake", "unknown", "", None, 1):
            with self.subTest(value=value):
                path.write_text(
                    json.dumps({"owners": {"gpt": {"dream_mode": value}}}),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "invalid dream_mode"):
                    load_owner_config(self.root, "gpt")

    def test_owner_dream_mode_does_not_branch_pipeline(self):
        (self.root / "owner-config.json").write_text(
            json.dumps({"owners": {"gpt": {"dream_mode": "api"}}}),
            encoding="utf-8",
        )
        runner = FakeRunner()
        with patch("dreams.load_owner_config", side_effect=AssertionError("pipeline read mode")):
            result = DreamPipeline(self.preparer, DreamStore(self.root), runner).generate(
                "gpt", dream_date="2026-09-29"
            )
        self.assertTrue(result["created"])
        self.assertEqual(runner.calls, 1)

    def test_dream_lease_claim_expiry_isolation_and_persistence(self):
        store = DreamLeaseStore(self.root)
        now = datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc)
        ttl = timedelta(minutes=10)

        first = store.claim("gpt", "2026-09-30", ttl, now)
        self.assertIsNotNone(first)
        self.assertEqual(first.claimed_at, "2026-09-30T00:00:00Z")
        self.assertEqual(first.expires_at, "2026-09-30T00:10:00Z")
        self.assertIsNone(store.claim("gpt", "2026-09-30", ttl, now + timedelta(minutes=9)))
        self.assertEqual(
            DreamLeaseStore(self.root).current("gpt", "2026-09-30", now + timedelta(minutes=5)),
            first,
        )

        owner_claim = store.claim("claude", "2026-09-30", ttl, now)
        date_claim = store.claim("gpt", "2026-10-01", ttl, now)
        self.assertIsNotNone(owner_claim)
        self.assertIsNotNone(date_claim)

        renewed = store.claim("gpt", "2026-09-30", ttl, now + ttl)
        self.assertIsNotNone(renewed)
        self.assertNotEqual(renewed.claim_token, first.claim_token)
        self.assertEqual(renewed.claimed_at, "2026-09-30T00:10:00Z")
        self.assertIsNone(store.current("gpt", "2026-09-30", now + timedelta(minutes=20)))

    def test_dream_lease_rejects_naive_time_and_invalid_ttl(self):
        store = DreamLeaseStore(self.root)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            store.claim("gpt", "2026-09-30", timedelta(minutes=1), datetime(2026, 9, 30))
        with self.assertRaisesRegex(ValueError, "positive timedelta"):
            store.claim("gpt", "2026-09-30", timedelta(0), datetime.now(timezone.utc))

    def test_dream_lease_two_process_claim_has_one_winner(self):
        DreamLeaseStore(self.root).current(
            "gpt", "2026-09-30", datetime(2026, 9, 30, tzinfo=timezone.utc)
        )
        context = multiprocessing.get_context("spawn")
        processes = [
            context.Process(target=_process_claim, args=(str(self.root), str(index)))
            for index in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(20)
            self.assertEqual(process.exitcode, 0)
        tokens = [
            json.loads((self.root / f"claim-{index}.json").read_text(encoding="utf-8"))["token"]
            for index in range(2)
        ]
        self.assertEqual(sum(token is not None for token in tokens), 1)

    def test_on_wake_claim_prepares_recent_complete_day_once(self):
        _record(self.root, "yesterday", "gpt", "2026-09-29T03:00:00Z", content="yesterday material")
        _record(self.root, "older", "gpt", "2026-09-25T03:00:00Z", content="older material")
        now = datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc)

        pending = claim_on_wake_dream(self.root, "gpt", now=now)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["dream_date"], "2026-09-29")
        self.assertIn("yesterday", {item["id"] for item in pending["materials"]})
        lease = DreamLeaseStore(self.root).current("gpt", "2026-09-29", now)
        self.assertIsNotNone(lease)
        self.assertEqual(pending["claim_token"], lease.claim_token)
        self.assertEqual(pending["expires_at"], lease.expires_at)
        self.assertIn("do not add facts", pending["instruction"])
        with sqlite3.connect(self.root / "state" / "dream-runtime.sqlite3") as db:
            package_json = db.execute(
                "SELECT package_json FROM dream_leases WHERE owner = ? AND dream_date = ?",
                ("gpt", pending["dream_date"]),
            ).fetchone()[0]
        self.assertNotIn("materials", json.loads(package_json))
        self.assertNotIn("yesterday material", package_json)
        self.assertIsNone(claim_on_wake_dream(self.root, "gpt", now=now))
        self.assertFalse((self.root / "dreams").exists())

    def test_on_wake_claim_respects_modes_and_existing_dated(self):
        path = self.root / "owner-config.json"
        for mode in ("cli", "api"):
            with self.subTest(mode=mode):
                path.write_text(
                    json.dumps({"owners": {"gpt": {"dream_mode": mode}}}),
                    encoding="utf-8",
                )
                self.assertIsNone(claim_on_wake_dream(
                    self.root, "gpt", now=datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc)
                ))
        path.write_text(
            json.dumps({"owners": {"gpt": {"dream_mode": "on_wake"}}}),
            encoding="utf-8",
        )
        DreamStore(self.root).commit(
            _package("gpt", "2026-09-29"), "existing dream", runner="fake", model=None
        )
        before = sorted(str(item.relative_to(self.root)) for item in (self.root / "dreams").rglob("*"))
        self.assertIsNone(claim_on_wake_dream(
            self.root, "gpt", now=datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc)
        ))
        after = sorted(str(item.relative_to(self.root)) for item in (self.root / "dreams").rglob("*"))
        self.assertEqual(after, before)

    def test_on_wake_prepare_failure_leaves_no_lease(self):
        class FailingPreparer:
            def prepare(self, *args, **kwargs):
                raise RuntimeError("prepare failed")

        now = datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc)
        with self.assertRaisesRegex(RuntimeError, "prepare failed"):
            claim_on_wake_dream(self.root, "gpt", now=now, preparer=FailingPreparer())
        self.assertIsNone(DreamLeaseStore(self.root).current("gpt", "2026-09-29", now))

    def test_on_wake_two_processes_have_one_pending(self):
        context = multiprocessing.get_context("spawn")
        processes = [
            context.Process(target=_process_pending, args=(str(self.root), str(index)))
            for index in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(20)
            self.assertEqual(process.exitcode, 0)
        tokens = [
            json.loads((self.root / f"pending-{index}.json").read_text(encoding="utf-8"))["token"]
            for index in range(2)
        ]
        self.assertEqual(sum(token is not None for token in tokens), 1)
        self.assertFalse((self.root / "dreams").exists())

    def test_claimed_commit_authorization_retry_conflict_and_repair(self):
        now = datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc)
        pending = claim_on_wake_dream(self.root, "gpt", now=now)
        content = "bounded on-wake dream"
        current, dated = safe_dream_paths(self.root, "gpt", pending["dream_date"])

        result = dream_commit_result(
            self.root, "gpt", pending["dream_date"], pending["claim_token"], content,
            now=now + timedelta(minutes=1),
        )
        self.assertEqual(result["status"], "committed")
        dated_bytes = dated.read_bytes()
        memory_files = list((self.root / "memory").rglob("*.md")) if (self.root / "memory").exists() else []

        repeated = dream_commit_result(
            self.root, "gpt", pending["dream_date"], pending["claim_token"], content,
            now=now + timedelta(hours=1),
        )
        self.assertEqual(repeated["status"], "already_committed")
        self.assertEqual(dated.read_bytes(), dated_bytes)

        current.unlink()
        repaired_missing = dream_commit_result(
            self.root, "gpt", pending["dream_date"], pending["claim_token"], content,
            now=now + timedelta(hours=2),
        )
        self.assertEqual(repaired_missing["status"], "already_committed")
        self.assertEqual(current.read_bytes(), dated_bytes)
        current.write_text("broken", encoding="utf-8")
        repaired_broken = dream_commit_result(
            self.root, "gpt", pending["dream_date"], pending["claim_token"], content,
            now=now + timedelta(hours=3),
        )
        self.assertEqual(repaired_broken["status"], "already_committed")
        self.assertEqual(current.read_bytes(), dated_bytes)
        self.assertEqual(dated.read_bytes(), dated_bytes)

        conflict = dream_commit_result(
            self.root, "gpt", pending["dream_date"], pending["claim_token"], "different",
            now=now + timedelta(hours=4),
        )
        self.assertEqual(conflict["error"]["code"], "content_conflict")
        self.assertEqual(dated.read_bytes(), dated_bytes)
        after_memory = list((self.root / "memory").rglob("*.md")) if (self.root / "memory").exists() else []
        self.assertEqual(after_memory, memory_files)
        with sqlite3.connect(self.root / "state" / "dream-runtime.sqlite3") as db:
            row = db.execute(
                "SELECT claim_token, content_sha256 FROM dream_commits WHERE owner = ? AND dream_date = ?",
                ("gpt", pending["dream_date"]),
            ).fetchone()
        self.assertEqual(row[0], pending["claim_token"])
        self.assertEqual(len(row[1]), 64)

    def test_claimed_commit_recovers_files_written_before_runtime_status(self):
        now = datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc)
        pending = claim_on_wake_dream(self.root, "gpt", now=now)
        body = "written before runtime status"
        DreamStore(self.root).commit(
            pending, body, runner="on_wake", model=None, generated_at=now
        )
        current, dated = safe_dream_paths(self.root, "gpt", pending["dream_date"])
        dated_bytes = dated.read_bytes()
        current.unlink()

        recovered = dream_commit_result(
            self.root, "gpt", pending["dream_date"], pending["claim_token"], body,
            now=now + timedelta(hours=1),
        )
        self.assertEqual(recovered["status"], "already_committed")
        self.assertEqual(current.read_bytes(), dated_bytes)
        self.assertEqual(dated.read_bytes(), dated_bytes)

    def test_claimed_commit_two_process_retry_is_idempotent(self):
        now = datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc)
        pending = claim_on_wake_dream(self.root, "gpt", now=now)
        context = multiprocessing.get_context("spawn")
        processes = [
            context.Process(
                target=_process_protocol_commit,
                args=(str(self.root), pending["dream_date"], pending["claim_token"], str(index)),
            )
            for index in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(20)
            self.assertEqual(process.exitcode, 0)
        results = [
            json.loads((self.root / f"protocol-commit-{index}.json").read_text(encoding="utf-8"))
            for index in range(2)
        ]
        self.assertEqual({item["status"] for item in results}, {"committed", "already_committed"})
        self.assertTrue(all(item["error"] is None for item in results))
        _, dated = safe_dream_paths(self.root, "gpt", pending["dream_date"])
        self.assertEqual(DreamStore(self.root).open_day("gpt", pending["dream_date"])["content"], "concurrent body")
        self.assertTrue(dated.is_file())

    def test_claimed_commit_rejects_wrong_owner_date_and_failed_side_effects(self):
        now = datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc)
        pending = claim_on_wake_dream(self.root, "gpt", now=now)
        attempts = [
            ("gpt", pending["dream_date"], "wrong-token", "invalid_claim"),
            ("claude", pending["dream_date"], pending["claim_token"], "invalid_claim"),
            ("gpt", "2026-09-28", pending["claim_token"], "invalid_claim"),
        ]
        for owner, day, token, code in attempts:
            with self.subTest(owner=owner, day=day):
                result = dream_commit_result(self.root, owner, day, token, "body", now=now)
                self.assertEqual(result["error"]["code"], code)
        self.assertFalse((self.root / "dreams").exists())

    def test_expired_claim_reclaim_rejects_old_and_accepts_new(self):
        now = datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc)
        old = claim_on_wake_dream(self.root, "gpt", now=now)
        expired = dream_commit_result(
            self.root, "gpt", old["dream_date"], old["claim_token"], "body",
            now=now + timedelta(minutes=10),
        )
        self.assertEqual(expired["error"]["code"], "expired_claim")
        new = claim_on_wake_dream(self.root, "gpt", now=now + timedelta(minutes=10))
        self.assertNotEqual(new["claim_token"], old["claim_token"])
        rejected_old = dream_commit_result(
            self.root, "gpt", old["dream_date"], old["claim_token"], "body",
            now=now + timedelta(minutes=11),
        )
        self.assertEqual(rejected_old["error"]["code"], "invalid_claim")
        accepted = dream_commit_result(
            self.root, "gpt", new["dream_date"], new["claim_token"], "body",
            now=now + timedelta(minutes=11),
        )
        self.assertEqual(accepted["status"], "committed")

    def test_dream_get_current_dated_not_found_and_owner_isolation(self):
        now = datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc)
        pending = claim_on_wake_dream(self.root, "gpt", now=now)
        dream_commit_result(
            self.root, "gpt", pending["dream_date"], pending["claim_token"], "gettable",
            now=now + timedelta(minutes=1),
        )
        current_path, dated_path = safe_dream_paths(self.root, "gpt", pending["dream_date"])
        before = (current_path.read_bytes(), dated_path.read_bytes())
        current = dream_get_result(self.root, "gpt")
        dated = dream_get_result(self.root, "gpt", pending["dream_date"])
        missing = dream_get_result(self.root, "gpt", "2026-09-28")
        other_owner = dream_get_result(self.root, "claude", pending["dream_date"])
        self.assertEqual(current["dream"]["content"], "gettable")
        self.assertEqual(dated["dream"]["content"], "gettable")
        self.assertEqual(missing["status"], "not_found")
        self.assertEqual(other_owner["status"], "not_found")
        self.assertEqual((current_path.read_bytes(), dated_path.read_bytes()), before)

    def test_aware_now_is_converted_once_for_date_commit_and_wake(self):
        fixed_utc = datetime(2026, 9, 29, 16, 30, tzinfo=timezone.utc)
        store = DreamStore(self.root)
        runner = FakeRunner("fixed-time dream")
        result = DreamPipeline(self.preparer, store, runner).generate(
            "gpt", timezone_name="Asia/Shanghai", now=fixed_utc
        )

        self.assertEqual(result["dream_date"], "2026-09-30")
        generated = datetime.fromisoformat(result["generated_at"])
        self.assertEqual(generated.astimezone(timezone.utc), fixed_utc)
        self.assertEqual((generated.hour, generated.minute), (0, 30))

        stored = store.open_day("gpt", "2026-09-30")
        self.assertEqual(stored["generated_at"], result["generated_at"])
        packet = attach_dream_to_wake({}, project_root=self.root, owner="gpt")
        self.assertEqual(packet["dream"]["dream_date"], "2026-09-30")
        self.assertNotIn("generated_at", packet["dream"])

    def test_prepare_dry_nothing_written_and_recall_excludes_dreams(self):
        _record(self.root, "m1", "gpt", "2026-09-29T01:00:00Z", content="ordinary memory")
        package = self.preparer.prepare("gpt", dream_date="2026-09-29")
        self.assertFalse((self.root / "dreams").exists())
        DreamStore(self.root).commit(package, "unique dream-only-word", runner="fake", model=None)
        self.assertEqual(MemoryStore(self.root, "gpt").recall("unique dream-only-word"), [])

    def test_idempotent_force_frontmatter_and_paths(self):
        store = DreamStore(self.root)
        first = store.commit(_package("gpt", "2026-09-29"), "first", runner="fake", model=None)
        second = store.commit(_package("gpt", "2026-09-29"), "second", runner="fake", model=None)
        forced = store.commit(_package("gpt", "2026-09-29"), "second", runner="fake", model=None, force=True)
        self.assertTrue(first["created"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(forced["content"], "second")
        parsed = store.open_day("gpt", "2026-09-29")
        self.assertEqual(parsed["source_memory_ids"], ["m1"])
        self.assertFalse(parsed["factual_authority"])
        with self.assertRaises(ValueError):
            safe_dream_paths(self.root, "../escape", "2026-09-29")

    def test_pipeline_missing_runner_and_fake_runner(self):
        _record(self.root, "m1", "gpt", "2026-09-29T01:00:00Z")
        store = DreamStore(self.root)
        with self.assertRaises(RuntimeError):
            DreamPipeline(self.preparer, store, None).generate("gpt", dream_date="2026-09-29")
        runner = FakeRunner()
        result = DreamPipeline(self.preparer, store, runner).generate("gpt", dream_date="2026-09-29")
        again = DreamPipeline(self.preparer, store, runner).generate("gpt", dream_date="2026-09-29")
        self.assertTrue(result["created"])
        self.assertTrue(again["idempotent"])
        self.assertEqual(runner.calls, 1)

    def test_runner_failure_timeout_empty_long_and_no_content_argv(self):
        cases = [
            (("-c", "import sys;sys.exit(3)"), RuntimeError, 2, 100),
            (("-c", "import time;time.sleep(2);print('x')"), TimeoutError, 1, 100),
            (("-c", "print('')"), ValueError, 2, 100),
            (("-c", "print('x'*101)"), ValueError, 2, 100),
        ]
        for args, error, timeout, maximum in cases:
            runner = CliRunner(RunnerConfig("custom", sys.executable, args, timeout_seconds=timeout, max_output_chars=maximum))
            with self.assertRaises(error):
                runner.run({"materials": [{"content": "SECRET-CONTENT"}]})
            self.assertTrue(all("SECRET-CONTENT" not in value for value in runner.argv))

    def test_tempfile_cleanup(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["path"] = next(value for value in argv if value.endswith("input.txt"))
            return subprocess.CompletedProcess(argv, 0, "dream", "")

        runner = CliRunner(RunnerConfig("custom", "fake", ("--input", "{input_file}"), input_mode="tempfile"))
        with patch("dreams.subprocess.run", side_effect=fake_run):
            self.assertEqual(runner.run({"materials": []}), "dream")
        self.assertFalse(Path(captured["path"]).exists())

    def test_atomic_failure_preserves_current(self):
        store = DreamStore(self.root)
        store.commit(_package("gpt", "2026-09-28"), "old", runner="fake", model=None)
        current, dated = safe_dream_paths(self.root, "gpt", "2026-09-29")
        old_current = current.read_text(encoding="utf-8")

        def failing_writer(path, text):
            if path.name == "current.md":
                raise OSError("simulated")
            MemoryStore._atomic_write(path, text)

        with self.assertRaises(OSError):
            store.commit(_package("gpt", "2026-09-29"), "new", runner="fake", model=None, atomic_writer=failing_writer)
        self.assertEqual(current.read_text(encoding="utf-8"), old_current)
        self.assertFalse(dated.exists())

    def test_half_commit_repairs_current_from_unchanged_dated(self):
        store = DreamStore(self.root)
        package = _package("gpt", "2026-09-29")
        current, dated = safe_dream_paths(self.root, "gpt", "2026-09-29")

        class SimulatedProcessInterruption(BaseException):
            pass

        def interrupted_writer(path, text):
            if path.name == "current.md":
                raise SimulatedProcessInterruption()
            MemoryStore._atomic_write(path, text)

        with self.assertRaises(SimulatedProcessInterruption):
            store.commit(
                package, "committed body", runner="fake", model=None,
                atomic_writer=interrupted_writer,
            )
        self.assertTrue(dated.is_file())
        self.assertFalse(current.exists())
        dated_before = dated.read_bytes()

        writes = []

        def recording_writer(path, text):
            writes.append(path)
            MemoryStore._atomic_write(path, text)

        recovered = store.commit(
            package, "must not replace committed body", runner="fake", model=None,
            atomic_writer=recording_writer,
        )
        self.assertTrue(recovered["idempotent"])
        self.assertEqual(recovered["generation_id"], package["generation_id"])
        self.assertEqual(current.read_bytes(), dated_before)
        self.assertEqual(dated.read_bytes(), dated_before)
        self.assertEqual(writes, [current])

        writes.clear()
        again = store.commit(
            package, "still must not replace committed body", runner="fake", model=None,
            atomic_writer=recording_writer,
        )
        self.assertTrue(again["idempotent"])
        self.assertEqual(writes, [])
        self.assertEqual(current.read_bytes(), dated_before)
        self.assertEqual(dated.read_bytes(), dated_before)

    def test_pipeline_repairs_damaged_current_without_runner(self):
        store = DreamStore(self.root)
        package = _package("gpt", "2026-09-29")
        store.commit(package, "committed body", runner="fake", model=None)
        current, dated = safe_dream_paths(self.root, "gpt", "2026-09-29")
        dated_before = dated.read_bytes()
        current.write_text("broken", encoding="utf-8")
        runner = FakeRunner()

        result = DreamPipeline(self.preparer, store, runner).generate(
            "gpt", dream_date="2026-09-29"
        )
        self.assertTrue(result["idempotent"])
        self.assertEqual(runner.calls, 0)
        self.assertEqual(current.read_bytes(), dated_before)
        self.assertEqual(dated.read_bytes(), dated_before)

    def test_two_process_concurrency(self):
        context = multiprocessing.get_context("spawn")
        different = [context.Process(target=_process_commit, args=(str(self.root), owner, "2026-09-29", owner)) for owner in ("gpt", "claude")]
        for process in different: process.start()
        for process in different:
            process.join(20)
            self.assertEqual(process.exitcode, 0)
        self.assertTrue(safe_dream_paths(self.root, "gpt", "2026-09-29")[1].exists())
        self.assertTrue(safe_dream_paths(self.root, "claude", "2026-09-29")[1].exists())

        same_root = self.root / "same"
        same_root.mkdir()
        same = [context.Process(target=_process_commit, args=(str(same_root), "gpt", "2026-09-29", str(i))) for i in range(2)]
        for process in same: process.start()
        for process in same:
            process.join(20)
            self.assertEqual(process.exitcode, 0)
        results = [json.loads(path.read_text()) for path in same_root.glob("result-*.json")]
        self.assertEqual(sum(item["created"] for item in results), 1)

    def test_wake_none_present_truncated_and_corrupt(self):
        packet = {"now": {"content": "now"}, "open": {"total": 0, "items": []}, "recent": []}
        self.assertNotIn("dream", attach_dream_to_wake(dict(packet), project_root=self.root, owner="gpt"))
        DreamStore(self.root).commit(_package("gpt", "2026-09-29"), "z" * 30, runner="fake", model=None)
        got = attach_dream_to_wake(dict(packet), project_root=self.root, owner="gpt", max_chars=10)
        self.assertTrue(got["dream"]["truncated"])
        self.assertFalse(got["dream"]["factual_authority"])
        current, _ = safe_dream_paths(self.root, "gpt", "2026-09-29")
        current.write_text("broken", encoding="utf-8")
        self.assertNotIn("dream", attach_dream_to_wake(dict(packet), project_root=self.root, owner="gpt"))

    def test_month_listing_no_current_duplicate_and_catchup(self):
        store = DreamStore(self.root)
        store.commit(_package("gpt", "2026-09-28"), "one", runner="fake", model=None)
        self.assertEqual(store.list_months("gpt"), [{"month": "2026-09", "days": ["2026-09-28"]}])
        now = datetime(2026, 9, 29, 3, 0, tzinfo=timezone.utc)
        self.assertTrue(catch_up_due("gpt", project_root=self.root, timezone_name="UTC", now=now))
        self.assertFalse(catch_up_due("gpt", project_root=self.root, timezone_name="UTC", now=now, enabled=False))

    @unittest.skip("single-server public distribution has no private 8767 OAuth variant")
    def test_schema_wire_parity_and_gitignore(self):
        def load(path, name, agent):
            env = {"AI_MEMORY_ROOT": str(self.root / name), "AI_MEMORY_AGENT": agent}
            if "claude" in path.name:
                env.update(AI_MEMORY_PUBLIC_BASE="https://example.test", AI_MEMORY_OAUTH_SECRET="test", AI_MEMORY_OAUTH_TOKEN_STATE=str(self.root / name / "token"))
            with patch.dict(os.environ, env, clear=False):
                spec = importlib.util.spec_from_file_location(name, path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module

        local = load(HERE / "server.py", "dream_schema_8765", "gpt")
        public = load(HERE / "server-claude-public.py", "dream_schema_8767", "claude")
        tools_local = {tool.name: tool for tool in asyncio.run(local.mcp.list_tools())}
        tools_public = {tool.name: tool for tool in asyncio.run(public.mcp.list_tools())}
        self.assertEqual(tools_local["wake"].input_schema, tools_public["wake"].input_schema)
        self.assertEqual(tools_local["dream_get"].input_schema, tools_public["dream_get"].input_schema)
        self.assertEqual(tools_local["dream_commit"].input_schema, tools_public["dream_commit"].input_schema)
        self.assertNotIn("owner", tools_local["dream_get"].input_schema.get("properties", {}))
        self.assertNotIn("owner", tools_local["dream_commit"].input_schema.get("properties", {}))

        def shape(value):
            if isinstance(value, dict):
                return {key: shape(item) for key, item in sorted(value.items())}
            if isinstance(value, list):
                return [shape(value[0])] if value else []
            return type(value).__name__

        wake_packets = []
        errors = []
        for module in (local, public):
            response = asyncio.run(module.mcp.call_tool("wake", {}))
            self.assertIsNone(response.structured_content)
            packet = json.loads(response.content[0].text)
            self.assertIn("open", packet)
            self.assertIn("pending_dream", packet)
            wake_packets.append(packet)
            failed = asyncio.run(module.mcp.call_tool("dream_commit", {
                "dream_date": "2026-09-29",
                "claim_token": "wrong",
                "content": "body",
            }))
            errors.append(json.loads(failed.content[0].text))
        self.assertEqual(shape(wake_packets[0]["pending_dream"]), shape(wake_packets[1]["pending_dream"]))
        self.assertEqual(shape(errors[0]), shape(errors[1]))
        self.assertEqual(errors[0]["error"]["code"], errors[1]["error"]["code"])
        self.assertIn("dreams/", (HERE / ".gitignore").read_text(encoding="utf-8").splitlines())


class PreparedDreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dream-prepared-")
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 9, 30, 4, tzinfo=timezone.utc)
        self.store = PreparedDreamStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_and_repeated_prepare_is_atomic_stable_and_secret_free(self):
        first, created = self.store.prepare(DreamPreparer(self.root), "gpt", "2026-09-29", now=self.now)
        second, created_again = self.store.prepare(DreamPreparer(self.root), "gpt", "2026-09-29", now=self.now + timedelta(hours=1))
        self.assertTrue(created); self.assertFalse(created_again)
        self.assertEqual(first, second); self.assertEqual(first["generation_id"], second["generation_id"])
        self.assertTrue(first["derived"]); self.assertFalse(first["factual_authority"])
        serialized = json.dumps(first)
        for forbidden in ("claim_token", "api_key", "oauth", "cookie", "secret"):
            self.assertNotIn(forbidden, serialized.lower())

    def test_concurrent_two_and_four_prepare_one_packet(self):
        context = multiprocessing.get_context("spawn")
        for count in (2, 4):
            root = self.root / f"p{count}"; root.mkdir()
            processes = [context.Process(target=_process_prepare_packet, args=(str(root), str(i))) for i in range(count)]
            for process in processes: process.start()
            for process in processes:
                process.join(20); self.assertEqual(process.exitcode, 0)
            results = [json.loads((root / f"prepare-{i}.json").read_text()) for i in range(count)]
            self.assertEqual(sum(item["created"] for item in results), 1)
            self.assertEqual(len({item["generation_id"] for item in results}), 1)
            packet = PreparedDreamStore(root).load("gpt", "2026-09-29")
            self.assertIsNotNone(packet)

    def test_prepared_wake_preserves_generation_and_commit_cleans_packet(self):
        packet, _ = self.store.prepare(DreamPreparer(self.root), "gpt", "2026-09-29", now=self.now)
        pending = claim_on_wake_dream(self.root, "gpt", now=self.now)
        self.assertEqual(pending["generation_id"], packet["generation_id"])
        db = sqlite3.connect(self.root / "state" / "dream-runtime.sqlite3")
        try:
            raw = db.execute("SELECT package_json FROM dream_leases").fetchone()[0]
        finally:
            db.close()
        self.assertNotIn("materials", raw)
        done = dream_commit_result(self.root, "gpt", "2026-09-29", pending["claim_token"], "body", now=self.now)
        self.assertTrue(done["ok"]); self.assertIsNone(self.store.load("gpt", "2026-09-29"))
        retry = dream_commit_result(self.root, "gpt", "2026-09-29", pending["claim_token"], "body", now=self.now)
        self.assertEqual(retry["status"], "already_committed")

    def test_dated_before_runtime_commit_retry_cleans_packet_and_recovers_current(self):
        packet, _ = self.store.prepare(DreamPreparer(self.root), "gpt", "2026-09-29", now=self.now)
        pending = claim_on_wake_dream(self.root, "gpt", now=self.now)
        DreamStore(self.root).commit(packet, "crash body", runner="on_wake", model=None, generated_at=self.now)
        current, _ = safe_dream_paths(self.root, "gpt", "2026-09-29")
        current.unlink()
        result = dream_commit_result(
            self.root, "gpt", "2026-09-29", pending["claim_token"], "crash body", now=self.now
        )
        self.assertTrue(result["ok"]); self.assertTrue(current.is_file())
        self.assertIsNone(self.store.load("gpt", "2026-09-29"))

    def test_wake_fallback_creates_packet_and_only_one_claim(self):
        pending = claim_on_wake_dream(self.root, "gpt", now=self.now)
        self.assertIsNotNone(pending); self.assertIsNotNone(self.store.load("gpt", "2026-09-29"))
        self.assertIsNone(claim_on_wake_dream(self.root, "gpt", now=self.now))

    def test_prepared_without_lease_is_grace_proof_but_old_packet_is_not_backlog(self):
        preparer = DreamPreparer(self.root)
        self.store.prepare(preparer, "gpt", "2026-09-29", now=self.now)
        self.store.prepare(preparer, "gpt", "2026-09-28", now=self.now)
        next_day = datetime(2026, 9, 30, 16, 1, tzinfo=timezone.utc)
        pending = claim_on_wake_dream(self.root, "gpt", now=next_day)
        self.assertEqual(pending["dream_date"], "2026-09-29")
        self.assertIsNone(DreamLeaseStore(self.root).current("gpt", "2026-09-28", next_day))

    def test_nightly_on_wake_prepares_and_reuses_without_lease(self):
        first = run_owner(self.root, "gpt", timezone_name="Asia/Shanghai", now=self.now)
        second = run_owner(self.root, "gpt", timezone_name="Asia/Shanghai", now=self.now)
        self.assertEqual(first["status"], "created"); self.assertEqual(second["status"], "reused")
        self.assertEqual(first["generation_id"], second["generation_id"])
        self.assertIsNone(DreamLeaseStore(self.root).current("gpt", "2026-09-29", self.now))

    def test_runtime_paths_are_gitignored(self):
        ignored = (HERE / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("state/dream-prepared/", ignored)
        self.assertIn("state/dream-scheduler/", ignored)
        self.assertIn("owner-config.json", ignored)

    def test_batch_owner_failure_does_not_block_on_wake_and_log_is_sanitized(self):
        (self.root / "owner-config.json").write_text(json.dumps({"owners": {
            "broken": {"dream_mode": "cli", "dream_cli": ["missing-dream-cli"]},
            "brokenapi": {"dream_mode": "api", "dream_api_adapter": "missing"},
            "ordinary": {"dream_mode": "on_wake"},
        }}), encoding="utf-8")
        results, code = run_batch(self.root, ["broken", "brokenapi", "ordinary"], timezone_name="Asia/Shanghai", now=self.now)
        self.assertEqual(code, 2); self.assertEqual(results[0]["status"], "error")
        self.assertEqual(results[1]["status"], "error"); self.assertEqual(results[2]["status"], "created")
        log_text = next((self.root / "state" / "dream-scheduler").glob("*.jsonl")).read_text(encoding="utf-8")
        self.assertNotIn("materials", log_text); self.assertNotIn("claim_token", log_text)

    def test_batch_mutex_is_cross_process_and_nonblocking(self):
        lock = self.root / "state" / ".dream-nightly.lock"
        context = multiprocessing.get_context("spawn")
        with _cross_process_file_lock(lock):
            process = context.Process(target=_process_nightly_batch, args=(str(self.root),))
            process.start(); process.join(20)
            self.assertEqual(process.exitcode, 0)
        result = json.loads((self.root / "batch-result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["code"], 3)
        self.assertEqual(result["results"][0]["status"], "busy")


class DreamGraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dream-grace-")
        self.root = Path(self.temp.name)
        self.store = DreamLeaseStore(self.root, busy_timeout_ms=15000)
        self.claimed_at = datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc)
        self.next_day = datetime(2026, 9, 30, 16, 1, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def _old_claim(self):
        return claim_on_wake_dream(
            self.root, "gpt", now=self.claimed_at, timezone_name="Asia/Shanghai",
            ttl=timedelta(minutes=1), lease_store=self.store,
        )

    def test_cross_day_grace_reclaims_new_token_and_commits(self):
        old = self._old_claim()
        self.assertEqual(old["dream_date"], "2026-09-29")
        new = claim_on_wake_dream(
            self.root, "gpt", now=self.next_day, timezone_name="Asia/Shanghai",
            lease_store=self.store,
        )
        self.assertEqual(new["dream_date"], "2026-09-29")
        self.assertNotEqual(new["claim_token"], old["claim_token"])
        failed = dream_commit_result(
            self.root, "gpt", "2026-09-29", old["claim_token"], "old", now=self.next_day
        )
        self.assertEqual(failed["error"]["code"], "invalid_claim")
        committed = dream_commit_result(
            self.root, "gpt", "2026-09-29", new["claim_token"], "grace body", now=self.next_day
        )
        self.assertTrue(committed["ok"])
        latest = claim_on_wake_dream(
            self.root, "gpt", now=self.next_day, timezone_name="Asia/Shanghai",
            lease_store=self.store,
        )
        self.assertEqual(latest["dream_date"], "2026-09-30")

    def test_no_history_uses_latest_and_never_scans_older(self):
        old = self.store.claim(
            "gpt", "2026-09-28", timedelta(minutes=1), self.claimed_at,
        )
        self.assertIsNotNone(old)
        pending = claim_on_wake_dream(
            self.root, "gpt", now=self.next_day, timezone_name="Asia/Shanghai",
            lease_store=self.store,
        )
        self.assertEqual(pending["dream_date"], "2026-09-30")

    def test_active_grace_blocks_latest_until_resolved(self):
        claim_on_wake_dream(
            self.root, "gpt", now=self.claimed_at, timezone_name="Asia/Shanghai",
            ttl=timedelta(days=2), lease_store=self.store,
        )
        self.assertIsNone(claim_on_wake_dream(
            self.root, "gpt", now=self.next_day, timezone_name="Asia/Shanghai",
            lease_store=self.store,
        ))

    def test_grace_dated_or_committed_is_not_reclaimed(self):
        old = self._old_claim()
        DreamStore(self.root).commit(_package("gpt", "2026-09-29"), "dated", runner="fake", model=None)
        pending = claim_on_wake_dream(
            self.root, "gpt", now=self.next_day, timezone_name="Asia/Shanghai",
            lease_store=self.store,
        )
        self.assertEqual(pending["dream_date"], "2026-09-30")
        self.assertNotEqual(pending["claim_token"], old["claim_token"])

    def test_completed_runtime_commit_blocks_grace_even_if_dated_is_missing(self):
        old = self._old_claim()
        result = dream_commit_result(
            self.root, "gpt", "2026-09-29", old["claim_token"], "completed", now=self.claimed_at
        )
        self.assertTrue(result["ok"])
        current, dated = safe_dream_paths(self.root, "gpt", "2026-09-29")
        current.unlink()
        dated.unlink()
        pending = claim_on_wake_dream(
            self.root, "gpt", now=self.next_day, timezone_name="Asia/Shanghai",
            lease_store=self.store,
        )
        self.assertEqual(pending["dream_date"], "2026-09-30")

    def test_shanghai_midnight_boundary_ignores_system_zone(self):
        before = datetime(2026, 10, 1, 15, 59, tzinfo=timezone.utc)
        after = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(most_recent_complete_dream_date(before, "Asia/Shanghai"), "2026-09-30")
        self.assertEqual(most_recent_complete_dream_date(after, "Asia/Shanghai"), "2026-10-01")

    def test_two_and_four_processes_only_one_grace_winner(self):
        context = multiprocessing.get_context("spawn")
        for count in (2, 4):
            with self.subTest(count=count):
                root = self.root / f"p{count}"
                root.mkdir()
                store = DreamLeaseStore(root, busy_timeout_ms=15000)
                claim_on_wake_dream(
                    root, "gpt", now=self.claimed_at, timezone_name="Asia/Shanghai",
                    ttl=timedelta(minutes=1), lease_store=store,
                )
                processes = [context.Process(
                    target=_process_grace_pending,
                    args=(str(root), str(index), self.next_day.isoformat()),
                ) for index in range(count)]
                for process in processes: process.start()
                for process in processes:
                    process.join(20); self.assertEqual(process.exitcode, 0)
                results = [json.loads((root / f"grace-{i}.json").read_text()) for i in range(count)]
                self.assertEqual(sum(item["token"] is not None for item in results), 1)
                self.assertEqual({item["date"] for item in results if item["date"]}, {"2026-09-29"})


class DreamRunnerModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self): self.tmp.cleanup()

    def _config(self, owners):
        (self.root / "owner-config.json").write_text(json.dumps({"owners": owners}), encoding="utf-8")

    def test_owner_runner_config_validation(self):
        self._config({"a": {"dream_mode": "cli", "dream_cli": ["agent", "-p"], "other": 7}})
        cfg = load_owner_config(self.root, "a")
        self.assertEqual(cfg.dream_cli, ("agent", "-p")); self.assertEqual(cfg.dream_timeout_seconds, 180)
        self._config({"a": {"dream_mode": "cli", "dream_cli": "agent"}})
        with self.assertRaisesRegex(ValueError, "dream_cli"): load_owner_config(self.root, "a")
        self._config({"arbitrary-owner": {"dream_mode": "api", "dream_api_adapter": "fake",
                                          "dream_api_base_url": "https://api.example.invalid/v1",
                                          "dream_api_key_env": "DREAM_TEST_KEY"}})
        cfg = load_owner_config(self.root, "arbitrary-owner")
        self.assertEqual(cfg.dream_api_key_env, "DREAM_TEST_KEY")
        self.assertEqual(load_owner_config(self.root, "missing").dream_mode, "on_wake")
        self._config({"a": {"dream_mode": "api", "dream_api_key_env": "not valid"}})
        with self.assertRaisesRegex(ValueError, "environment variable name"):
            load_owner_config(self.root, "a")

    def test_unified_plain_instruction(self):
        prompt = build_prompt(_package("gpt", "2026-09-29"))
        self.assertTrue(prompt.startswith(dream_instruction()))
        for phrase in ("derived shadow", "not a factual source", "open items", "relationship conclusions", "project persona"):
            self.assertIn(phrase, prompt)

    def test_cli_isolated_cwd_args_cleanup_and_no_marker_leak(self):
        (self.root / "CLAUDE.md").write_text("PERSONA_MARKER_CLAUDE", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("PERSONA_MARKER_AGENTS", encoding="utf-8")
        observed = {}
        def fake_run(argv, **kwargs):
            observed.update(argv=argv, **kwargs); cwd = Path(kwargs["cwd"])
            observed["cwd_exists_during"] = cwd.is_dir(); observed["cwd_files"] = list(cwd.iterdir())
            return subprocess.CompletedProcess(argv, 0, stdout="isolated dream", stderr="")
        runner = CliRunner(RunnerConfig(name="configured_cli", executable="fake-agent", args=("--plain",), explicit_command=True))
        with patch("dreams.subprocess.run", side_effect=fake_run):
            self.assertEqual(runner.run(_package("gpt", "2026-09-29")), "isolated dream")
        self.assertEqual(observed["argv"], ["fake-agent", "--plain"]); self.assertFalse(observed["shell"])
        self.assertNotEqual(Path(observed["cwd"]), self.root); self.assertTrue(observed["cwd_exists_during"])
        self.assertEqual(observed["cwd_files"], []); self.assertFalse(Path(observed["cwd"]).exists())
        self.assertNotIn("PERSONA_MARKER", observed["input"])

    def test_cli_failure_modes_never_commit_and_cleanup(self):
        cases = [subprocess.CompletedProcess(["x"], 2, stdout="", stderr="SECRET_VALUE"), subprocess.CompletedProcess(["x"], 0, stdout="   ", stderr=""), subprocess.CompletedProcess(["x"], 0, stdout="x" * 20, stderr="")]
        runner = CliRunner(RunnerConfig(name="configured_cli", executable="x", explicit_command=True, max_output_chars=10))
        for completed in cases:
            seen = {}
            def fake_run(*args, **kwargs): seen["cwd"] = kwargs["cwd"]; return completed
            with patch("dreams.subprocess.run", side_effect=fake_run):
                with self.assertRaises((RuntimeError, ValueError)) as error: runner.run(_package("gpt", "2026-09-29"))
            self.assertNotIn("SECRET_VALUE", str(error.exception)); self.assertFalse(Path(seen["cwd"]).exists())
        seen = {}
        def timeout(*args, **kwargs): seen["cwd"] = kwargs["cwd"]; raise subprocess.TimeoutExpired(args[0], 1)
        with patch("dreams.subprocess.run", side_effect=timeout):
            with self.assertRaises(TimeoutError): runner.run(_package("gpt", "2026-09-29"))
        self.assertFalse(Path(seen["cwd"]).exists())

    def test_api_adapter_success_failure_limits_and_secret_redaction(self):
        package = {**_package("api", "2026-09-29"), "schema_version": 1, "materials": []}
        adapter = FakeApiAdapter(); runner = ApiRunner(adapter, timeout_seconds=17, max_output_chars=20)
        self.assertEqual(runner.run(package), "api dream"); prompt, timeout, metadata = adapter.calls[0]
        self.assertEqual(timeout, 17); self.assertNotIn("materials", metadata); self.assertIn(dream_instruction(), prompt)
        for output in ("", "x" * 21):
            with self.assertRaises(ValueError): ApiRunner(FakeApiAdapter(output), max_output_chars=20).run(package)
        secret = "SUPER_SECRET_API_KEY"
        with self.assertRaisesRegex(RuntimeError, "adapter 'fake' failed") as error: ApiRunner(FakeApiAdapter(error=RuntimeError(secret))).run(package)
        self.assertNotIn(secret, str(error.exception))
        with self.assertRaises(TimeoutError): ApiRunner(FakeApiAdapter(error=TimeoutError("late"))).run(package)

    def test_active_cli_latest_day_idempotent_and_no_second_call(self):
        self._config({"a": {"dream_mode": "cli", "dream_cli": ["fake"]}}); calls = []
        def fake_run(argv, **kwargs): calls.append((argv, kwargs)); return subprocess.CompletedProcess(argv, 0, stdout="cli body", stderr="")
        now = datetime(2026, 9, 30, 4, tzinfo=timezone.utc)
        with patch("dreams.subprocess.run", side_effect=fake_run):
            first = run_configured_dream(self.root, "a", now=now, timezone_name="UTC"); second = run_configured_dream(self.root, "a", now=now, timezone_name="UTC")
        self.assertTrue(first["created"]); self.assertEqual(first["dream_date"], "2026-09-29"); self.assertTrue(second["idempotent"]); self.assertEqual(len(calls), 1)
        current, dated = safe_dream_paths(self.root, "a", "2026-09-29"); self.assertEqual(current.read_bytes(), dated.read_bytes())

    def test_active_api_and_on_wake_modes_are_isolated(self):
        self._config({"apiowner": {"dream_mode": "api", "dream_api_adapter": "fake"}, "wakeowner": {"dream_mode": "on_wake"}})
        adapter = FakeApiAdapter("api body"); now = datetime(2026, 9, 30, 4, tzinfo=timezone.utc)
        result = run_configured_dream(self.root, "apiowner", now=now, timezone_name="UTC", api_adapters={"fake": adapter})
        skipped = run_configured_dream(self.root, "wakeowner", now=now, timezone_name="UTC")
        self.assertTrue(result["created"]); self.assertEqual(len(adapter.calls), 1); self.assertEqual(skipped["reason"], "on_wake")
        self.assertIsNone(claim_on_wake_dream(self.root, "apiowner", now=now, timezone_name="UTC"))
        pending = claim_on_wake_dream(self.root, "wakeowner", now=now, timezone_name="UTC")
        self.assertIsNotNone(pending); self.assertEqual(pending["instruction"], dream_instruction())
        _, wake_dated = safe_dream_paths(self.root, "wakeowner", "2026-09-29"); self.assertFalse(wake_dated.exists())

    def test_mode_switch_never_overwrites_dated(self):
        self._config({"switch": {"dream_mode": "cli", "dream_cli": ["fake"]}}); store = DreamStore(self.root)
        original = store.commit(_package("switch", "2026-09-29"), "first", runner="on_wake", model=None)
        _, dated = safe_dream_paths(self.root, "switch", "2026-09-29"); before = dated.read_bytes()
        with patch("dreams.subprocess.run") as call: got = run_configured_dream(self.root, "switch", now=datetime(2026, 9, 30, tzinfo=timezone.utc), timezone_name="UTC")
        self.assertTrue(got["idempotent"]); call.assert_not_called(); self.assertEqual(dated.read_bytes(), before); self.assertEqual(got["generation_id"], original["generation_id"])

    def test_three_mode_isolated_acceptance(self):
        self._config({"a": {"dream_mode": "cli", "dream_cli": ["fake-cli"]}, "b": {"dream_mode": "api", "dream_api_adapter": "fake-api"}, "c": {"dream_mode": "on_wake"}})
        marker = "PERSONA_MUST_NOT_LEAK"; (self.root / "CLAUDE.md").write_text(marker, encoding="utf-8"); (self.root / "AGENTS.md").write_text(marker, encoding="utf-8")
        cli_inputs = []
        def fake_cli(argv, **kwargs): cli_inputs.append(kwargs["input"]); return subprocess.CompletedProcess(argv, 0, stdout="body a", stderr="")
        api = FakeApiAdapter("body b"); now = datetime(2026, 9, 30, 4, tzinfo=timezone.utc)
        with patch("dreams.subprocess.run", side_effect=fake_cli): a = run_configured_dream(self.root, "a", now=now, timezone_name="UTC")
        b = run_configured_dream(self.root, "b", now=now, timezone_name="UTC", api_adapters={"fake-api": api})
        pending = claim_on_wake_dream(self.root, "c", now=now, timezone_name="UTC")
        c = dream_commit_result(self.root, "c", pending["dream_date"], pending["claim_token"], "body c", now=now)
        self.assertTrue(a["created"] and b["created"] and c["ok"])
        for owner, body in (("a", "body a"), ("b", "body b"), ("c", "body c")):
            self.assertEqual(dream_get_result(self.root, owner, "2026-09-29")["dream"]["content"], body)
            current, dated = safe_dream_paths(self.root, owner, "2026-09-29"); self.assertEqual(current.read_bytes(), dated.read_bytes()); self.assertEqual(parse_dream(dated.read_text(encoding="utf-8"))["owner"], owner)
        self.assertNotIn(marker, cli_inputs[0]); self.assertNotIn(marker, api.calls[0][0]); self.assertEqual(list((self.root / "memory").rglob("*.md")), [])


if __name__ == "__main__":
    unittest.main()
