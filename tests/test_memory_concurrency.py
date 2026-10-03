"""Windows file-sharing races between memory writers and lock-free readers.

All writes stay in TemporaryDirectory. The stress cases run real threads and
processes; the injection cases patch os.replace / Path.read_text to prove the
retry is bounded, narrow, and leaves no partial or temporary files behind.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import tempfile
import threading
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

import memory_store
from memory_search import MemorySearchIndex, rebuild_project as rebuild_fts
from memory_store import MemoryStore
from memory_vectors import DEFAULT_MODEL_ID, MemoryVectorIndex, _BACKEND_CACHE, rebuild_project as rebuild_vec


STATES = ("unverified", "confirmed", "partial", "unknown")
STRESS_REPEATS = int(os.getenv("MEMORY_CONCURRENCY_REPEATS", "5"))


class TinyBackend:
    model_id = "test/tiny"
    revision = "1"
    fingerprint = "tiny-fingerprint"
    dimension = 3

    def embed(self, texts):
        return [[1.0, float(len(text) % 7 + 1), 0.5] for text in texts]


def install_backend(root: Path) -> None:
    _BACKEND_CACHE[(os.path.normcase(str(Path(root).resolve())), DEFAULT_MODEL_ID)] = TinyBackend()


def _proc_writer(root: str, memory_id: str, out) -> None:
    logging.disable(logging.WARNING)
    install_backend(Path(root))
    try:
        store = MemoryStore(root, "gpt")
        for i in range(16):
            store.update(memory_id, f"process marker v{i + 1}", verification=STATES[i % 4])
        out.put(None)
    except Exception:
        out.put(traceback.format_exc())


def _proc_reader(root: str, memory_id: str, out) -> None:
    logging.disable(logging.WARNING)
    install_backend(Path(root))
    try:
        store = MemoryStore(root, "claude")
        misses = sum(
            memory_id not in {x["id"] for x in store.recall("process marker", limit=30)} for _ in range(40)
        )
        out.put(f"reader missed the memory {misses} times" if misses else None)
    except Exception:
        out.put(traceback.format_exc())


class ConcurrencyTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="memory-concurrency-")
        self.root = Path(self.temp.name)
        install_backend(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def seed(self, texts: list[str]) -> list[dict]:
        store = MemoryStore(self.root, "gpt")
        saved = [store.remember(text, "life/daily") for text in texts]
        rebuild_fts(self.root)
        rebuild_vec(self.root, backend=TinyBackend())
        return saved

    def assert_store_consistent(self, expected: dict[str, tuple[str, ...]]) -> None:
        self.assertEqual([p.name for p in (self.root / "memory").rglob("*.tmp-*")], [], "temp-file leak")
        store = MemoryStore(self.root, "gpt")
        records = {}
        for path in store._iter_memory_paths():
            record = store._parse(path.read_text(encoding="utf-8"))  # raises on partial frontmatter
            self.assertIsNotNone(record, f"partial markdown: {path.name}")
            records[record["id"]] = record
        for memory_id, allowed in expected.items():
            self.assertIn(memory_id, records)
            self.assertIn(records[memory_id]["content"], allowed)
        source = store._filtered("all", include_inactive=True)
        fts = MemorySearchIndex(self.root).diagnostics(source)
        self.assertEqual(fts["status"], "PASS", fts)
        vectors = MemoryVectorIndex(self.root, backend=TinyBackend()).diagnostics(source)
        self.assertEqual(vectors["status"], "PASS", vectors)

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
        [thread.start() for thread in threads]
        [thread.join() for thread in threads]
        return errors

    def reader(self, query: str, ids: list[str], misses: list, rounds: int):
        store = MemoryStore(self.root, "gpt")

        def fn():
            for _ in range(rounds):
                found = {x["id"] for x in store.recall(query, limit=30)}
                misses.extend(memory_id for memory_id in ids if memory_id not in found)
        return fn


class ConcurrentStressTests(ConcurrencyTestBase):
    def test_same_memory_two_writers_two_readers(self) -> None:
        for _ in range(STRESS_REPEATS):
            with self.subTest(repeat=_):
                self.tearDown(); self.setUp()
                memory_id = self.seed(["same marker v0"])[0]["id"]
                variants = {w: [f"same marker w{w}-{i}" for i in range(12)] for w in (1, 2)}
                misses: list[str] = []

                def writer(w):
                    def fn():
                        store = MemoryStore(self.root, "gpt")
                        for i, text in enumerate(variants[w]):
                            store.update(memory_id, text, verification=STATES[i % 4])
                    return fn

                errors = self.run_threads([writer(1), writer(2), *[self.reader("same marker", [memory_id], misses, 30)] * 2])
                self.assertEqual(errors, [])
                self.assertEqual(misses, [])
                self.assert_store_consistent({memory_id: (variants[1][-1], variants[2][-1])})

    def test_different_memories_three_writers_two_readers(self) -> None:
        for _ in range(STRESS_REPEATS):
            with self.subTest(repeat=_):
                self.tearDown(); self.setUp()
                ids = [item["id"] for item in self.seed([f"diff marker m{i} v0" for i in range(3)])]
                finals = {memory_id: f"diff marker m{i} final" for i, memory_id in enumerate(ids)}
                misses: list[str] = []

                def writer(i):
                    def fn():
                        store = MemoryStore(self.root, "gpt")
                        for n in range(10):
                            store.update(ids[i], f"diff marker m{i} v{n + 1}", verification=STATES[n % 4])
                        store.update(ids[i], finals[ids[i]])
                    return fn

                errors = self.run_threads([*(writer(i) for i in range(3)), *[self.reader("diff marker", ids, misses, 25)] * 2])
                self.assertEqual(errors, [])
                self.assertEqual(misses, [])
                self.assert_store_consistent({memory_id: (text,) for memory_id, text in finals.items()})

    def test_cross_process_writer_and_reader(self) -> None:
        # Separate server processes sharing one memory directory.
        memory_id = self.seed(["process marker v0"])[0]["id"]
        ctx = multiprocessing.get_context("spawn")
        out = ctx.Queue()
        procs = [ctx.Process(target=_proc_writer, args=(str(self.root), memory_id, out)),
                 ctx.Process(target=_proc_reader, args=(str(self.root), memory_id, out))]
        [proc.start() for proc in procs]
        results = [out.get(timeout=120) for _ in procs]
        [proc.join(timeout=60) for proc in procs]
        self.assertEqual([r for r in results if r], [])
        self.assert_store_consistent({memory_id: ("process marker v16",)})


class SharingRetryInjectionTests(ConcurrencyTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.store = MemoryStore(self.root, "gpt")
        self.saved = self.store.remember("injection marker v0", "life/daily")
        self.path = self.store._find_record(self.saved["id"])[0]
        self.sleeps: list[float] = []
        self.sleep_patch = patch.object(memory_store.time, "sleep", side_effect=self.sleeps.append)

    def flaky_replace(self, failures: int, exc: BaseException):
        real = os.replace
        calls = {"n": 0}

        def replace(src, dst):
            calls["n"] += 1
            if calls["n"] <= failures:
                raise exc
            return real(src, dst)
        return replace, calls

    def test_transient_permission_error_recovers_atomically(self) -> None:
        replace, calls = self.flaky_replace(3, PermissionError(13, "sharing violation"))
        with patch.object(memory_store, "_RETRY_SHARING_VIOLATIONS", True), patch.object(memory_store.os, "replace", replace), self.sleep_patch:
            self.store.update(self.saved["id"], "injection marker v1", verification="confirmed")
        self.assertEqual(calls["n"], 4)
        self.assertEqual(self.sleeps, list(memory_store._SHARING_RETRY_DELAYS[:3]))
        record = self.store._find_record(self.saved["id"])[1]
        self.assertEqual((record["content"], record["verification"]), ("injection marker v1", "confirmed"))
        self.assertEqual(list(self.path.parent.glob("*.tmp-*")), [])

    def test_persistent_permission_error_is_bounded_and_raised(self) -> None:
        before = self.path.read_bytes()
        replace, calls = self.flaky_replace(10_000, PermissionError(13, "still locked"))
        with patch.object(memory_store, "_RETRY_SHARING_VIOLATIONS", True), patch.object(memory_store.os, "replace", replace), self.sleep_patch:
            with self.assertRaises(PermissionError):
                self.store.update(self.saved["id"], "injection marker never", verification="confirmed")
        self.assertEqual(calls["n"], len(memory_store._SHARING_RETRY_DELAYS) + 1)
        self.assertLess(sum(self.sleeps), 0.5)
        self.assertEqual(self.path.read_bytes(), before, "original memory must stay intact")
        self.assertEqual(list(self.path.parent.glob("*.tmp-*")), [])

    def test_other_errors_are_not_retried(self) -> None:
        before = self.path.read_bytes()
        replace, calls = self.flaky_replace(10_000, OSError(28, "no space left"))
        with patch.object(memory_store, "_RETRY_SHARING_VIOLATIONS", True), patch.object(memory_store.os, "replace", replace), self.sleep_patch:
            with self.assertRaises(OSError) as raised:
                self.store.update(self.saved["id"], "injection marker never")
        self.assertNotIsInstance(raised.exception, PermissionError)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp-*")), [])

    def test_no_retry_off_windows(self) -> None:
        replace, calls = self.flaky_replace(10_000, PermissionError(13, "denied"))
        with patch.object(memory_store, "_RETRY_SHARING_VIOLATIONS", False), patch.object(memory_store.os, "replace", replace), self.sleep_patch:
            with self.assertRaises(PermissionError):
                self.store.update(self.saved["id"], "injection marker never")
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(list(self.path.parent.glob("*.tmp-*")), [])

    def test_tmp_write_failure_leaves_no_temp_file(self) -> None:
        real_write = Path.write_text

        def failing_write(path, *args, **kwargs):
            if ".tmp-" in path.name:
                real_write(path, "partial", encoding="utf-8")
                raise OSError(28, "disk full mid-write")
            return real_write(path, *args, **kwargs)

        before = self.path.read_bytes()
        with patch.object(Path, "write_text", failing_write):
            with self.assertRaises(OSError):
                self.store.update(self.saved["id"], "injection marker never")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp-*")), [])

    def test_reader_retries_transient_sharing_violation(self) -> None:
        real_read = Path.read_text
        state = {"failures": 2}

        def flaky_read(path, *args, **kwargs):
            if path == self.path and state["failures"]:
                state["failures"] -= 1
                raise PermissionError(13, "being replaced")
            return real_read(path, *args, **kwargs)

        with patch.object(memory_store, "_RETRY_SHARING_VIOLATIONS", True), patch.object(Path, "read_text", flaky_read), self.sleep_patch:
            found = [item["id"] for item in self.store.recall("injection marker")]
        self.assertIn(self.saved["id"], found)
        self.assertEqual(self.sleeps, list(memory_store._SHARING_RETRY_DELAYS[:2]))


if __name__ == "__main__":
    logging.disable(logging.WARNING)
    unittest.main()
