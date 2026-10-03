"""Windows open/replace sharing races on dream files and Doctor reads.

All writes stay in TemporaryDirectory. Stress cases use real threads and
processes; injection cases prove the retry is bounded, narrow (PermissionError
only, never parse errors), and keeps the original error/degrade semantics.
"""

from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing
import os
import sqlite3
import subprocess
import tempfile
import threading
import traceback
import unittest
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import memory_store
from dream_nightly import run_owner
from dreams import (
    DreamPreparer, DreamStore, PreparedDreamStore, claim_on_wake_dream,
    dream_commit_result, dream_get_result, run_configured_dream,
)
from memory_doctor import inspect_memory
from memory_provenance import query_provenance
from memory_store import MemoryStore


NOW = datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc)
DAY = "2026-09-29"
REPEATS = int(os.getenv("DREAM_CONCURRENCY_REPEATS", "3"))


def _record(root: Path, memory_id: str, owner: str = "gpt") -> None:
    created = "2026-09-29T03:00:00Z"
    record = {"id": memory_id, "owner": owner, "scope": "agent", "category": "plan/general",
              "created_at": created, "updated_at": created, "content": f"material {memory_id}"}
    path = root / "memory" / "plan" / "general" / f"2026-09-29_{memory_id}__{owner}__agent.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(MemoryStore._serialize(record), encoding="utf-8")


def _package(owner: str = "gpt", day: str = DAY) -> dict:
    return {"owner": owner, "dream_date": day, "timezone": "Asia/Shanghai",
            "generation_id": f"generation-{owner}-{day}", "source_memory_ids": ["m1"],
            "source_event_range": {"start": None, "end": None}, "truncated": False}


def _proc_claim(root: str, marker: str) -> None:
    logging.disable(logging.WARNING)
    out = {"token": None, "error": None, "loads": 0}
    try:
        pending = claim_on_wake_dream(root, "gpt", now=NOW)
        out["token"] = None if pending is None else pending["claim_token"]
        for _ in range(20):
            if PreparedDreamStore(root).load("gpt", DAY) is not None:
                out["loads"] += 1
    except Exception:
        out["error"] = traceback.format_exc()
    Path(root, f"claim-{marker}.json").write_text(json.dumps(out), encoding="utf-8")


def _proc_wake_service(root: str, marker: str) -> None:
    """One realistic service wake: claim once; the winner reads its packet a few
    times while composing, then commits; the other wakes, gets no claim and reads
    the dream a few times. Packet cleanup runs while the peer may be reading."""
    warnings: list[str] = []
    handler = logging.Handler(logging.WARNING)
    handler.emit = lambda record: warnings.append(record.getMessage())
    logging.getLogger("dreams").addHandler(handler)
    out = {"token": None, "status": None, "error": None, "warnings": warnings}
    try:
        pending = claim_on_wake_dream(root, "gpt", now=NOW)
        if pending is not None:
            out["token"] = pending["claim_token"]
            for _ in range(3):
                PreparedDreamStore(root).load("gpt", DAY)
            out["status"] = dream_commit_result(root, "gpt", DAY, pending["claim_token"], "the one dream",
                                                now=NOW)["status"]
        else:
            for _ in range(3):
                PreparedDreamStore(root).load("gpt", DAY)
                dream_get_result(root, "gpt", DAY)
    except Exception:
        out["error"] = traceback.format_exc()
    Path(root, f"wake-{marker}.json").write_text(json.dumps(out), encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="dream-concurrency-")
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        _record(self.root, "m1")
        _record(self.root, "m2")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_threads(self, functions) -> list[str]:
        errors: list[str] = []

        def guard(fn):
            def inner():
                try:
                    fn()
                except Exception:
                    errors.append(traceback.format_exc())
            return inner

        threads = [threading.Thread(target=guard(fn)) for fn in functions]
        [t.start() for t in threads]
        [t.join() for t in threads]
        return errors

    def assert_no_residue(self) -> None:
        leftovers = [p.name for p in self.root.rglob("*") if p.is_file()
                     and (".tmp-" in p.name or p.name.endswith(".tmp"))]
        self.assertEqual(leftovers, [], "temporary file residue")

    def commit_rows(self) -> int:
        db = sqlite3.connect(self.root / "state" / "dream-runtime.sqlite3")
        try:
            return db.execute("SELECT count(*) FROM dream_commits WHERE owner='gpt' AND dream_date=?",
                              (DAY,)).fetchone()[0]
        finally:
            db.close()

    def lease_rows(self) -> list[tuple]:
        db = sqlite3.connect(self.root / "state" / "dream-runtime.sqlite3")
        try:
            return db.execute("SELECT owner,dream_date,claim_token,expires_at FROM dream_leases ORDER BY 1,2").fetchall()
        finally:
            db.close()

    def dated_path(self) -> Path:
        return self.root / "dreams" / "gpt" / "2026-09" / f"{DAY}.md"

    def packet_path(self) -> Path:
        return PreparedDreamStore(self.root).path_for("gpt", DAY)

    def assert_committed_state(self, body: str) -> None:
        self.assertEqual(self.commit_rows(), 1)
        self.assertEqual(DreamStore(self.root).existing("gpt", DAY)["content"], body)
        self.assertEqual(dream_get_result(self.root, "gpt", DAY)["dream"]["content"], body)
        self.assertEqual(dream_get_result(self.root, "gpt")["dream"]["content"], body)
        self.assertEqual(DreamStore(self.root).read_current("gpt")["content"], body)
        self.assertEqual(len(list(self.dated_path().parent.glob("*.md"))), 1)

    def assert_sqlite_ok(self) -> None:
        for path in (self.root / "state").glob("*.sqlite3"):
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok", path.name)
            finally:
                db.close()


class DreamConcurrencyStress(Base):
    def test_concurrent_prepare_and_load_create_exactly_one_packet(self) -> None:
        results: list[bool] = []
        seen: list[str] = []
        done = threading.Event()

        def prepare():
            _, made = PreparedDreamStore(self.root).prepare(
                DreamPreparer(self.root), "gpt", DAY, timezone_name="Asia/Shanghai", now=NOW)
            results.append(made)

        def preparers():
            try:
                self.assertEqual(self.run_threads([prepare] * 4), [])
            finally:
                done.set()

        def loader():
            reader = PreparedDreamStore(self.root)
            while not done.is_set():
                packet = reader.load("gpt", DAY)  # None before creation, a whole packet after
                if packet is not None:
                    seen.append(packet["generation_id"])

        self.assertEqual(self.run_threads([preparers, loader, loader]), [])
        self.assertEqual(sorted(results), [False, False, False, True])
        final = PreparedDreamStore(self.root).load("gpt", DAY)["generation_id"]
        self.assertTrue(set(seen) <= {final}, "partial or foreign packet observed")
        self.assert_no_residue()

    def test_realistic_two_service_wake_cleans_packet(self) -> None:
        for repeat in range(REPEATS * 2):
            with self.subTest(repeat=repeat):
                self.tearDown(); self.setUp()
                ctx = multiprocessing.get_context("spawn")
                procs = [ctx.Process(target=_proc_wake_service, args=(str(self.root), str(i))) for i in range(2)]
                [p.start() for p in procs]
                [p.join(timeout=120) for p in procs]
                outs = [json.loads((self.root / f"wake-{i}.json").read_text(encoding="utf-8")) for i in range(2)]
                self.assertEqual([o["error"] for o in outs], [None, None])
                self.assertEqual(len([o for o in outs if o["token"]]), 1)
                self.assertEqual([o["status"] for o in outs if o["token"]], ["committed"])
                # The committing service's cleanup must succeed under realistic reads.
                self.assertEqual([w for o in outs for w in o["warnings"] if "cleanup deferred" in w], [])
                self.assert_committed_state("the one dream")
                if self.packet_path().exists():
                    # Pre-existing claim behaviour: the peer can re-prepare the packet after
                    # the winner's cleanup, then see the dated dream and return no claim.
                    # That stale packet must be inert and recoverable.
                    self.assertIsNone(claim_on_wake_dream(self.root, "gpt", now=NOW))
                    run_owner(self.root, "gpt", timezone_name="Asia/Shanghai", now=NOW)
                    self.assertFalse(self.packet_path().exists())
                self.assert_committed_state("the one dream")
                self.assert_no_residue()
                self.assert_sqlite_ok()

    def test_sustained_readers_cleanup_degrades_to_warning(self) -> None:
        # Two readers loading back-to-back can starve Windows unlink past the
        # bounded retry. The invariant is not "cleanup always wins" but "the
        # commit stays a success and every later step stays correct".
        for repeat in range(REPEATS):
            with self.subTest(repeat=repeat):
                self.tearDown(); self.setUp()
                token = claim_on_wake_dream(self.root, "gpt", now=NOW)["claim_token"]
                done = threading.Event()
                partial: list[str] = []

                def loader():
                    reader = PreparedDreamStore(self.root)
                    while not done.is_set():
                        try:
                            reader.load("gpt", DAY)
                        except ValueError as exc:
                            partial.append(type(exc).__name__)

                readers = [threading.Thread(target=loader) for _ in range(2)]
                [r.start() for r in readers]
                try:
                    started = time.monotonic()
                    result = dream_commit_result(self.root, "gpt", DAY, token, "the one dream", now=NOW)
                    elapsed = time.monotonic() - started
                finally:
                    done.set()
                    [r.join() for r in readers]
                self.assertEqual((result["ok"], result["status"]), (True, "committed"))
                self.assertLess(elapsed, 10.0, "cleanup retry must stay bounded")
                self.assertEqual(partial, [])
                self.assert_committed_state("the one dream")
                self.assertIsNone(claim_on_wake_dream(self.root, "gpt", now=NOW))
                run_owner(self.root, "gpt", timezone_name="Asia/Shanghai", now=NOW)
                self.assertFalse(self.packet_path().exists())
                self.assert_no_residue()

    def test_two_services_claim_and_read(self) -> None:
        for repeat in range(REPEATS):
            with self.subTest(repeat=repeat):
                self.tearDown(); self.setUp()
                ctx = multiprocessing.get_context("spawn")
                procs = [ctx.Process(target=_proc_claim, args=(str(self.root), str(i))) for i in range(2)]
                [p.start() for p in procs]
                [p.join(timeout=120) for p in procs]
                outs = [json.loads((self.root / f"claim-{i}.json").read_text(encoding="utf-8")) for i in range(2)]
                self.assertEqual([o["error"] for o in outs], [None, None])
                tokens = [o["token"] for o in outs if o["token"]]
                self.assertEqual(len(tokens), 1, "exactly one service may hold the claim")
                self.assertEqual(sum(o["loads"] for o in outs), 40)
                db = sqlite3.connect(self.root / "state" / "dream-runtime.sqlite3")
                try:
                    self.assertEqual(db.execute("SELECT count(*) FROM dream_leases").fetchone()[0], 1)
                finally:
                    db.close()
                # Racing commits with the single token: exactly one commits.
                statuses: list[str] = []

                def commit():
                    statuses.append(dream_commit_result(self.root, "gpt", DAY, tokens[0], "the one dream",
                                                        now=NOW)["status"])

                self.assertEqual(self.run_threads([commit] * 3), [])
                self.assertEqual(sorted(statuses), ["already_committed", "already_committed", "committed"])
                db = sqlite3.connect(self.root / "state" / "dream-runtime.sqlite3")
                try:
                    self.assertEqual(db.execute("SELECT count(*) FROM dream_commits").fetchone()[0], 1)
                finally:
                    db.close()
                self.assert_no_residue()
                self.assert_sqlite_ok()

    def _replace_loop(self, bodies: list[str]):
        def fn():
            store = DreamStore(self.root)
            for body in bodies:
                store.commit(_package(), body, runner="fake", model=None, force=True)
        return fn

    def test_dream_get_wake_and_provenance_during_replace(self) -> None:
        bodies = [f"dream body {i}" for i in range(30)]
        DreamStore(self.root).commit(_package(), "dream body initial", runner="fake", model=None)
        allowed = set(bodies) | {"dream body initial"}
        bad: list[str] = []

        def get_current():
            for _ in range(60):
                result = dream_get_result(self.root, "gpt")
                if result["status"] != "found" or result["dream"]["content"] not in allowed:
                    bad.append(f"current:{result['status']}")

        def get_dated():
            for _ in range(60):
                result = dream_get_result(self.root, "gpt", DAY)
                if result["status"] != "found" or result["dream"]["content"] not in allowed:
                    bad.append(f"dated:{result['status']}")

        def wake_read():
            for _ in range(60):
                current = DreamStore(self.root).read_current("gpt")
                if current is None or current["content"] not in allowed:
                    bad.append("wake:missing")

        def provenance():
            for _ in range(40):
                dreams = query_provenance(self.root, "gpt", "m1")["dreams"]
                dates = {item["dream_date"]: item["file"] for item in dreams.get("items", [])}
                if dreams.get("invalid_files_skipped") or dates.get(DAY) != "present":
                    bad.append("provenance:missed")

        errors = self.run_threads([self._replace_loop(bodies), get_current, get_dated, wake_read, provenance])
        self.assertEqual(errors, [])
        self.assertEqual(bad, [])
        current, dated = (self.root / "dreams" / "gpt" / "current.md"), (self.root / "dreams" / "gpt" / "2026-09" / f"{DAY}.md")
        self.assertEqual(hashlib.sha256(current.read_bytes()).digest(), hashlib.sha256(dated.read_bytes()).digest())
        self.assertEqual(DreamStore(self.root).existing("gpt", DAY)["content"], bodies[-1])
        self.assert_no_residue()

    def test_doctor_during_memory_updates(self) -> None:
        store = MemoryStore(self.root, "gpt")
        saved = store.remember("doctor concurrency marker", "plan/general")
        baseline = inspect_memory(self.root)
        reports: list[dict] = []

        def writer():
            for i in range(30):
                store.update(saved["id"], f"doctor concurrency marker v{i}")

        def doctor():
            for _ in range(6):
                reports.append(inspect_memory(self.root))

        self.assertEqual(self.run_threads([writer, doctor, doctor]), [])
        for report in reports:
            self.assertEqual(report["result"], baseline["result"])
            self.assertEqual(report["parse_errors"], baseline["parse_errors"])
            self.assertEqual(report["files"], baseline["files"])
            self.assertFalse([e for e in report["errors"] if "Permission" in str(e.get("error_type"))])
        self.assert_no_residue()


class DreamRetryInjection(Base):
    def setUp(self) -> None:
        super().setUp()
        self.sleeps: list[float] = []
        self.sleep_patch = patch.object(memory_store.time, "sleep", side_effect=self.sleeps.append)
        self.windows = patch.object(memory_store, "_RETRY_SHARING_VIOLATIONS", True)

    def _flaky_read(self, target: Path, failures: int):
        real = Path.read_text
        state = {"left": failures, "calls": 0}

        def read(path, *args, **kwargs):
            if path == target:
                state["calls"] += 1
                if state["left"]:
                    state["left"] -= 1
                    raise PermissionError(13, "being replaced")
            return real(path, *args, **kwargs)
        return read, state

    def test_packet_load_transient_recovers_and_persistent_keeps_error(self) -> None:
        store = PreparedDreamStore(self.root)
        store.prepare(DreamPreparer(self.root), "gpt", DAY, timezone_name="Asia/Shanghai", now=NOW)
        path = store.path_for("gpt", DAY)
        read, state = self._flaky_read(path, 2)
        with self.windows, self.sleep_patch, patch.object(Path, "read_text", read):
            self.assertIsNotNone(store.load("gpt", DAY))
        self.assertEqual(state["calls"], 3)
        read, state = self._flaky_read(path, 10_000)
        with self.windows, self.sleep_patch, patch.object(Path, "read_text", read):
            with self.assertRaisesRegex(ValueError, "invalid prepared dream packet"):
                store.load("gpt", DAY)
        self.assertEqual(state["calls"], len(memory_store._SHARING_RETRY_DELAYS) + 1)

    def _commit_with_cleanup_failure(self) -> tuple[dict, str, list]:
        token = claim_on_wake_dream(self.root, "gpt", now=NOW)["claim_token"]
        packet = self.packet_path()
        self.assertTrue(packet.exists())
        real_unlink = Path.unlink
        attempts = {"n": 0}

        def unlink(path, *args, **kwargs):
            if path == packet:
                attempts["n"] += 1
                raise PermissionError(32, "packet is being read")
            return real_unlink(path, *args, **kwargs)

        with self.windows, self.sleep_patch, patch.object(Path, "unlink", unlink), \
                self.assertLogs("dreams", level="WARNING") as logs:
            result = dream_commit_result(self.root, "gpt", DAY, token, "the one dream", now=NOW)
        self.assertEqual(attempts["n"], len(memory_store._SHARING_RETRY_DELAYS) + 1)
        return result, token, logs.output

    def test_cleanup_failure_keeps_commit_success_and_warns_safely(self) -> None:
        result, token, logs = self._commit_with_cleanup_failure()
        self.assertEqual((result["ok"], result["status"], result["error"]), (True, "committed", None))
        self.assertEqual(result["dream"]["content"], "the one dream")
        self.assertLess(sum(self.sleeps), 0.5)
        self.assertEqual(len(logs), 1)
        message = logs[0]
        self.assertIn("cleanup deferred", message)
        for secret in (token, "the one dream", str(self.root), "material m1", "generation-", ".json"):
            self.assertNotIn(secret, message)
        self.assertTrue(self.packet_path().exists(), "stale packet is left for later cleanup")
        self.assert_committed_state("the one dream")
        self.assert_no_residue()

    def test_stale_packet_is_idempotently_safe_and_recoverable(self) -> None:
        _, token, _ = self._commit_with_cleanup_failure()
        dated_before = self.dated_path().read_bytes()
        leases_before = self.lease_rows()
        # While the stale packet lingers, wake/dream_get read the committed dream.
        self.assertTrue(self.packet_path().exists())
        self.assert_committed_state("the one dream")
        # No second valid claim: now, after the lease expires, and on a later wake the same day.
        for when in (NOW, NOW + timedelta(minutes=30), NOW + timedelta(hours=6)):
            self.assertIsNone(claim_on_wake_dream(self.root, "gpt", now=when), when)
        self.assertTrue(self.packet_path().exists())
        self.assertEqual(self.lease_rows(), leases_before)
        # A different body is rejected; the same token and body are idempotent.
        conflict = dream_commit_result(self.root, "gpt", DAY, token, "a different dream", now=NOW)
        self.assertEqual((conflict["status"], conflict["error"]["code"]), ("error", "content_conflict"))
        again = dream_commit_result(self.root, "gpt", DAY, token, "the one dream", now=NOW)
        self.assertEqual(again["status"], "already_committed")
        self.assertEqual(self.dated_path().read_bytes(), dated_before, "dated dream must not be rewritten")
        self.assert_committed_state("the one dream")
        # The idempotent re-commit performs the deferred cleanup.
        self.assertFalse(self.packet_path().exists())

    def test_stale_packet_removed_by_nightly_and_catch_up(self) -> None:
        for cleanup in ("nightly", "catch_up"):
            with self.subTest(cleanup=cleanup):
                self.tearDown(); self.setUp()
                self.sleeps.clear()
                self._commit_with_cleanup_failure()
                self.assertTrue(self.packet_path().exists())
                self.assert_committed_state("the one dream")
                if cleanup == "nightly":
                    outcome = run_owner(self.root, "gpt", timezone_name="Asia/Shanghai", now=NOW)
                    self.assertEqual(outcome["status"], "already_exists")
                else:
                    outcome = run_configured_dream(self.root, "gpt", now=NOW, timezone_name="Asia/Shanghai")
                    self.assertTrue(outcome["idempotent"])
                self.assertFalse(self.packet_path().exists())
                self.assert_committed_state("the one dream")
                self.assertEqual(self.commit_rows(), 1)
                self.assert_no_residue()

    def test_packet_removed_between_check_and_read_loads_as_absent(self) -> None:
        store = PreparedDreamStore(self.root)
        store.prepare(DreamPreparer(self.root), "gpt", DAY, timezone_name="Asia/Shanghai", now=NOW)
        path = store.path_for("gpt", DAY)
        real = Path.read_text

        def vanished(target, *args, **kwargs):
            if target == path:
                raise FileNotFoundError(2, "removed by concurrent cleanup")
            return real(target, *args, **kwargs)

        with self.windows, self.sleep_patch, patch.object(Path, "read_text", vanished):
            self.assertIsNone(store.load("gpt", DAY))
        self.assertEqual(self.sleeps, [])

    def test_corrupt_packet_is_not_retried(self) -> None:
        store = PreparedDreamStore(self.root)
        path = store.path_for("gpt", DAY)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        with self.windows, self.sleep_patch:
            with self.assertRaisesRegex(ValueError, "invalid prepared dream packet"):
                store.load("gpt", DAY)
        self.assertEqual(self.sleeps, [])

    def test_packet_replace_persistent_failure_leaves_nothing(self) -> None:
        store = PreparedDreamStore(self.root)
        calls = {"n": 0}

        def replace(src, dst):
            calls["n"] += 1
            raise PermissionError(13, "locked")

        with self.windows, self.sleep_patch, patch("dreams.os.replace", replace):
            with self.assertRaises(PermissionError):
                store.prepare(DreamPreparer(self.root), "gpt", DAY, timezone_name="Asia/Shanghai", now=NOW)
        self.assertEqual(calls["n"], len(memory_store._SHARING_RETRY_DELAYS) + 1)
        self.assertLess(sum(self.sleeps), 0.5)
        self.assertIsNone(store.load("gpt", DAY))
        self.assert_no_residue()

    def test_dream_reads_degrade_as_before_after_retries(self) -> None:
        DreamStore(self.root).commit(_package(), "persisted dream", runner="fake", model=None)
        current = self.root / "dreams" / "gpt" / "current.md"
        dated = self.root / "dreams" / "gpt" / "2026-09" / f"{DAY}.md"
        for target, check in (
            (current, lambda: self.assertIsNone(DreamStore(self.root).read_current("gpt"))),
            (current, lambda: self.assertEqual(dream_get_result(self.root, "gpt")["status"], "error")),
            (dated, lambda: self.assertEqual(dream_get_result(self.root, "gpt", DAY)["status"], "error")),
        ):
            read, state = self._flaky_read(target, 10_000)
            with self.windows, self.sleep_patch, patch.object(Path, "read_text", read):
                check()
            self.assertEqual(state["calls"], len(memory_store._SHARING_RETRY_DELAYS) + 1)
        read, state = self._flaky_read(current, 3)
        with self.windows, self.sleep_patch, patch.object(Path, "read_text", read):
            self.assertEqual(dream_get_result(self.root, "gpt")["status"], "found")

    def test_corrupt_dream_is_not_retried(self) -> None:
        DreamStore(self.root).commit(_package(), "persisted dream", runner="fake", model=None)
        (self.root / "dreams" / "gpt" / "current.md").write_text("not a dream", encoding="utf-8")
        with self.windows, self.sleep_patch:
            self.assertEqual(dream_get_result(self.root, "gpt")["status"], "error")
            self.assertIsNone(DreamStore(self.root).read_current("gpt"))
        self.assertEqual(self.sleeps, [])

    def test_doctor_retries_then_reports_as_before(self) -> None:
        saved = MemoryStore(self.root, "gpt").remember("doctor injection marker", "plan/general")
        path = MemoryStore(self.root, "gpt")._find_record(saved["id"])[0]
        read, _ = self._flaky_read(path, 2)
        baseline = inspect_memory(self.root)
        with self.windows, self.sleep_patch, patch.object(Path, "read_text", read):
            recovered = inspect_memory(self.root)
        self.assertEqual(recovered["result"], baseline["result"])
        self.assertEqual(recovered["errors"], baseline["errors"])
        read, _ = self._flaky_read(path, 10_000)
        with self.windows, self.sleep_patch, patch.object(Path, "read_text", read):
            report = inspect_memory(self.root)
        # Exhausted retries keep the original semantics: the file is reported, Doctor FAILs.
        self.assertEqual(report["result"], "FAIL")
        self.assertEqual(len([e for e in report["errors"] if "being replaced" in e["error_type"]]), 1)


if __name__ == "__main__":
    unittest.main()
