"""Limited provenance query tests. All writes stay in TemporaryDirectory."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from archive_store import ArchiveStore
from dream_scraps import DreamScrapStore
from dreams import DreamLeaseStore, serialize_dream
from execution_receipts import ExecutionReceiptStore
from memory_audit import MemoryAuditLog
from memory_feedback import MemoryFeedbackStore
from memory_provenance import MAX_LIMIT, REDACTION_NOTE, query_provenance
from memory_store import MemoryStore
from memory_witness import MemoryWitnessStore


SECRET_MEMORY = "记忆正文-PROVENANCE-BODY-不应出现"
SECRET_QUERY = "原始查询-PROVENANCE-QUERY-不应出现"
SECRET_DREAM = "梦的正文-PROVENANCE-DREAM-不应出现"
SECRET_SCRAP = "碎片正文-PROVENANCE-SCRAP-不应出现"
SECRET_ARCHIVE = "archive needle 归档正文-PROVENANCE-ARCHIVE"
SECRET_AUDIT_QUERY = "审计查询-PROVENANCE-AUDIT-不应出现"
SECRETS = (SECRET_MEMORY, SECRET_QUERY, SECRET_DREAM, SECRET_SCRAP, SECRET_ARCHIVE, SECRET_AUDIT_QUERY)


def snapshot(root: Path) -> dict[str, str]:
    """Hash every file except SQLite's WAL sidecars.

    A mode=ro reader of a checkpointed WAL database lets SQLite create the
    -shm index and an empty -wal; those are SQLite's own read machinery, not
    data. Any -wal that exists must be empty unless it existed before
    (checked separately by assert_no_new_wal_data).
    """
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.name.endswith(("-shm", "-wal")):
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def wal_sizes(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): p.stat().st_size for p in root.rglob("*-wal") if p.is_file()}


class ProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="memory-provenance-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assert_readonly(self, root: Path, before: dict[str, str], wal_before: dict[str, int]) -> None:
        self.assertEqual(snapshot(root), before, "provenance query must not write")
        for name, size in wal_sizes(root).items():
            self.assertEqual(size, wal_before.get(name, 0), name)

    def assert_no_secret(self, result: dict) -> None:
        text = json.dumps(result, ensure_ascii=False)
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        forbidden_keys = {"query", "content", "body", "text", "snippet", "note_text"}

        def walk(value) -> None:
            if isinstance(value, dict):
                self.assertFalse(forbidden_keys & set(value), sorted(forbidden_keys & set(value)))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(result)

    # ---------- bounds and store availability ----------

    def test_limit_bounds(self) -> None:
        for bad in (0, MAX_LIMIT + 1, -1, True, "5", 2.0, None):
            with self.assertRaises(ValueError):
                query_provenance(self.root, "claude", "abc", limit=bad)
        self.assertEqual(query_provenance(self.root, "claude", "abc", limit=1)["limit"], 1)
        self.assertEqual(query_provenance(self.root, "claude", "abc", limit=MAX_LIMIT)["limit"], MAX_LIMIT)
        self.assertEqual(query_provenance(self.root, "claude", "abc")["limit"], 20)

    def test_invalid_owner_and_memory_id(self) -> None:
        for owner in ("", "Bad Owner", "../x"):
            with self.assertRaises(ValueError):
                query_provenance(self.root, owner, "abc")
        for memory_id in ("", "../etc", "a b", "human:_house/x.md"):
            with self.assertRaises(ValueError):
                query_provenance(self.root, "claude", memory_id)

    def test_empty_root_is_unavailable_and_creates_nothing(self) -> None:
        before = snapshot(self.root)
        result = query_provenance(self.root, "claude", "abc")
        self.assertEqual(snapshot(self.root), before)
        self.assertEqual(list(self.root.iterdir()), [])
        for section in ("memory", "evidence_refs", "receipts", "exposures", "retrievals",
                        "witnesses", "dreams", "scraps", "audit_reads"):
            self.assertEqual(result[section]["status"], "unavailable", section)
        self.assertEqual(result["receipts"]["note"], REDACTION_NOTE)

    def test_present_but_empty_stores_are_ok_not_unavailable(self) -> None:
        MemoryWitnessStore(self.root)._connect().close()
        MemoryFeedbackStore(self.root, "claude")._connect().close()
        DreamScrapStore(self.root)._connect().close()
        result = query_provenance(self.root, "claude", "abc")
        for section in ("exposures", "retrievals", "witnesses"):
            self.assertEqual(result[section]["status"], "ok", section)
            self.assertEqual(result[section]["total"], 0)
        self.assertEqual(result["scraps"], {"status": "ok", "total": 0, "unexpired": 0, "latest_expires_at": None})
        self.assertEqual(result["receipts"]["status"], "unavailable")

    def test_feedback_ledger_missing_is_unavailable_not_empty(self) -> None:
        memory = MemoryStore(self.root, "claude").remember(SECRET_MEMORY, "plan/general")
        MemoryWitnessStore(self.root).expose("claude", [memory["id"]], "rid-1", source="recall",
                                            context_kind="recall", retrieval_id="rid-1")
        result = query_provenance(self.root, "claude", memory["id"])
        self.assertEqual(result["exposures"]["status"], "ok")
        self.assertEqual(result["exposures"]["total"], 1)
        self.assertEqual(result["retrievals"]["status"], "unavailable")
        self.assertNotIn("items", result["retrievals"])
        self.assertNotIn("total", result["retrievals"])

    def test_corrupt_store_is_unavailable(self) -> None:
        (self.root / "state").mkdir()
        (self.root / "state" / "memory-feedback.sqlite3").write_bytes(b"not a sqlite database" * 20)
        result = query_provenance(self.root, "claude", "abc")
        self.assertEqual(result["retrievals"], {"status": "unavailable", "reason": "unreadable_store"})

    # ---------- full linkage ----------

    def _populate(self) -> dict:
        store = MemoryStore(self.root, "claude")
        memory = store.remember(SECRET_MEMORY, "plan/general", status="open", source="observed")
        mid = memory["id"]
        remember_receipt = memory["execution_receipt_id"]
        self.assertTrue(remember_receipt)

        source_file = self.root / "archive-source.json"
        source_file.write_text("source", encoding="utf-8")
        archives = ArchiveStore(self.root / "archive")
        archives.import_records(source_file, owner="claude", source="chatgpt", sessions=[{
            "session_id": "s1",
            "messages": [{"timestamp": "2026-09-29T00:00:00Z", "role": "user", "text": SECRET_ARCHIVE}],
        }])
        archive_id = archives.search("archive needle", owner="claude")[0]["archive_id"]
        updated = store.update(
            mid, SECRET_MEMORY, verification="partial",
            evidence_refs=[f"receipt:{remember_receipt}", f"archive:{archive_id}"],
        )
        archives.delete_message(archive_id, owner="claude")  # now dangling

        feedback = MemoryFeedbackStore(self.root, "claude")
        passive_rid = feedback.record_retrieval(SECRET_QUERY, "claude", ["other", mid])
        normal_rid = feedback.record_retrieval(SECRET_QUERY, "all", [mid])
        legacy_rid = feedback.record_retrieval(SECRET_QUERY, "all", [mid, "x"])
        feedback.record_retrieval(SECRET_QUERY, "all", ["unrelated"])
        MemoryFeedbackStore(self.root, "gpt").record_retrieval(SECRET_QUERY, "all", [mid])
        feedback.add_feedback(normal_rid, "used", memory_ids=[mid])
        feedback.add_feedback(normal_rid, "helpful")

        witness = MemoryWitnessStore(self.root)
        witness.expose("claude", [mid, "other"], passive_rid, source="passive_recall",
                       context_kind="passive_recall", retrieval_id=passive_rid)
        witness.expose("claude", [mid], normal_rid, source="recall", context_kind="recall",
                       retrieval_id=normal_rid)
        witness.expose("claude", [mid], "wake-x", source="wake", context_kind="wake_context")
        witness.expose("gpt", [mid], "gpt-wake", source="wake", context_kind="wake_context")
        witness.record_witness("claude", mid, "receipt:" + "f" * 32, "fresh-episode")
        witness.record_witness("claude", mid, "source-msg-1", "wake-x")
        witness.record_witness("claude", mid, "bad ref with spaces!", "other-episode")

        dream_path = self.root / "dreams" / "claude" / "2026-09" / "2026-09-30.md"
        dream_path.parent.mkdir(parents=True)
        meta = {
            "schema_version": 1, "owner": "claude", "dream_date": "2026-09-30",
            "generated_at": "2026-10-01T08:00:00+08:00", "timezone": "Asia/Shanghai",
            "runner": "on_wake", "model": None, "generation_id": "g" * 32,
            "source_memory_ids": [mid, "other"], "source_event_range": {"start": None, "end": None},
            "truncated": False,
        }
        dream_path.write_text(serialize_dream(meta, SECRET_DREAM), encoding="utf-8")
        (self.root / "dreams" / "claude" / "current.md").write_text(
            serialize_dream(meta, SECRET_DREAM), encoding="utf-8")
        db = DreamLeaseStore(self.root)._connect()
        try:
            for day, ids in (("2026-09-30", [mid]), ("2026-09-28", [mid]), ("2026-09-27", ["other"])):
                db.execute("INSERT INTO dream_commits VALUES(?,?,?,?,?,?,?)", (
                    "claude", day, "tok", "sha", "h" * 32,
                    json.dumps({"source_memory_ids": ids}), "2026-10-01T00:00:00Z"))
            db.commit()
        finally:
            db.close()

        DreamScrapStore(self.root).add("claude", SECRET_SCRAP, source_type="memory", source_ref=mid)
        audit = MemoryAuditLog(self.root, "claude")
        audit.log_read(tool="recall", query=SECRET_AUDIT_QUERY, items=[{"id": mid}])
        audit.log_read(tool="wake", items=[{"id": "other"}])
        return {"mid": mid, "remember_receipt": remember_receipt,
                "update_receipt": updated["execution_receipt_id"], "archive_id": archive_id,
                "passive_rid": passive_rid, "normal_rid": normal_rid, "legacy_rid": legacy_rid}

    def test_full_linkage_metadata_only(self) -> None:
        ids = self._populate()
        mid = ids["mid"]
        before, wal_before = snapshot(self.root), wal_sizes(self.root)
        result = query_provenance(self.root, "claude", mid, archive_root=self.root / "archive")
        self.assert_readonly(self.root, before, wal_before)
        self.assert_no_secret(result)

        memory = result["memory"]
        self.assertEqual(memory["status"], "present")
        self.assertEqual((memory["owner"], memory["task_status"], memory["source"]), ("claude", "open", "observed"))
        self.assertEqual((memory["lifecycle"], memory["verification"]), ("active", "partial"))

        evidence = {item["kind"]: item for item in result["evidence_refs"]["items"]}
        self.assertEqual(evidence["receipt"]["status"], "valid")
        self.assertEqual(evidence["archive"]["status"], "missing")

        receipts = result["receipts"]
        self.assertEqual(receipts["status"], "ok")
        self.assertEqual(receipts["note"], REDACTION_NOTE)
        self.assertEqual({r["receipt_id"] for r in receipts["items"]},
                         {ids["remember_receipt"], ids["update_receipt"]})
        self.assertEqual({r["operation"] for r in receipts["items"]}, {"remember", "update_memory"})

        exposures = result["exposures"]
        self.assertEqual(exposures["total"], 3)
        self.assertEqual(set(exposures["by_context_kind"]), {"passive_recall", "recall", "wake_context"})

        retrievals = {item["retrieval_id"]: item for item in result["retrievals"]["items"]}
        self.assertEqual(set(retrievals), {ids["passive_rid"], ids["normal_rid"], ids["legacy_rid"]})
        self.assertEqual(retrievals[ids["passive_rid"]]["mode"], "passive_recall")
        self.assertEqual(retrievals[ids["passive_rid"]]["rank"], 2)
        self.assertEqual(retrievals[ids["normal_rid"]]["mode"], "recall")
        self.assertEqual(retrievals[ids["legacy_rid"]]["mode"], "unknown")
        self.assertEqual(retrievals[ids["normal_rid"]]["feedback"], [{"verdict": "used", "source": "agent_report"}])

        witnesses = result["witnesses"]
        self.assertEqual(witnesses["total"], 3)
        self.assertTrue(witnesses["has_independent_witness"])
        by_kind = {item["evidence"]["kind"]: item for item in witnesses["items"]}
        self.assertEqual(by_kind["receipt"]["evidence"]["status"], "missing")
        self.assertTrue(by_kind["receipt"]["independent"])
        self.assertEqual(by_kind["opaque"]["evidence"], {"ref": "source-msg-1", "kind": "opaque", "status": "unresolvable"})
        self.assertFalse(by_kind["opaque"]["independent"])
        self.assertEqual(by_kind["unrecognized"]["evidence"]["ref"], None)

        dreams = {item["dream_date"]: item for item in result["dreams"]["items"]}
        self.assertEqual(set(dreams), {"2026-09-30", "2026-09-28"})
        self.assertEqual((dreams["2026-09-30"]["file"], dreams["2026-09-30"]["commit"]), ("present", "present"))
        self.assertEqual((dreams["2026-09-28"]["file"], dreams["2026-09-28"]["commit"]), ("absent", "present"))
        self.assertFalse(dreams["2026-09-30"]["factual_authority"])

        self.assertEqual((result["scraps"]["total"], result["scraps"]["unexpired"]), (1, 1))
        self.assertEqual(result["audit_reads"]["reads"], 1)
        self.assertEqual(result["audit_reads"]["by_tool"], {"recall": 1})

    def test_limit_truncates_lists(self) -> None:
        ids = self._populate()
        result = query_provenance(self.root, "claude", ids["mid"], limit=1)
        for section in ("receipts", "exposures", "retrievals", "witnesses", "dreams"):
            self.assertEqual(len(result[section]["items"]), 1, section)
            self.assertTrue(result[section]["truncated"], section)
            self.assertGreater(result[section]["total"], 1, section)

    def test_cross_owner_sees_only_safe_status(self) -> None:
        ids = self._populate()
        result = query_provenance(self.root, "gpt", ids["mid"])
        self.assert_no_secret(result)
        self.assertEqual(result["memory"]["owner"], "claude")
        evidence = {item["kind"]: item for item in result["evidence_refs"]["items"]}
        self.assertEqual(set(evidence["receipt"]), {"ref", "kind", "status"})
        self.assertEqual(evidence["receipt"]["status"], "cross_owner")
        self.assertEqual(result["receipts"]["total"], 0)
        text = json.dumps(result)
        self.assertNotIn(ids["update_receipt"], text)
        self.assertEqual(result["exposures"]["total"], 1)
        self.assertEqual(result["exposures"]["items"][0]["episode_id"], "gpt-wake")
        self.assertEqual(result["retrievals"]["total"], 1)
        self.assertEqual(result["retrievals"]["items"][0]["mode"], "unknown")
        self.assertEqual(result["witnesses"]["total"], 0)
        self.assertEqual(result["scraps"]["total"], 0)
        self.assertEqual(result["audit_reads"]["status"], "unavailable")
        self.assertEqual(result["dreams"]["total"], 0)

    def test_forget_redaction_is_not_reattributed(self) -> None:
        store = MemoryStore(self.root, "claude")
        memory = store.remember(SECRET_MEMORY, "plan/general")
        mid = memory["id"]
        updated = store.update(mid, SECRET_MEMORY + " v2")
        linked_before = query_provenance(self.root, "claude", mid)["receipts"]
        self.assertEqual(linked_before["total"], 2)
        forgotten = store.forget(mid)
        receipt_ids = {memory["execution_receipt_id"], updated["execution_receipt_id"],
                       forgotten["execution_receipt_id"]}

        result = query_provenance(self.root, "claude", mid)
        self.assertEqual(result["memory"], {"status": "missing"})
        self.assertEqual(result["evidence_refs"]["status"], "unavailable")
        receipts = result["receipts"]
        self.assertEqual(receipts["status"], "ok")
        self.assertEqual(receipts["note"], REDACTION_NOTE)
        self.assertEqual(receipts["total"], 0)
        self.assertEqual(receipts["items"], [])
        self.assertEqual(set(receipts), {"status", "note", "total", "truncated", "items"})
        text = json.dumps(result)
        for receipt_id in receipt_ids:
            self.assertNotIn(receipt_id, text)
            # The receipt itself still exists, with its target redacted.
            row = ExecutionReceiptStore(self.root).get(receipt_id, owner="claude")
            self.assertEqual(row["target_status"], "redacted")
            self.assertIsNone(row["target_id"])

    def test_superseded_by_dangling_is_marked(self) -> None:
        store = MemoryStore(self.root, "claude")
        old = store.remember("旧的", "plan/general")
        new = store.remember("新的", "plan/general")
        store.update(old["id"], "旧的", lifecycle="superseded", superseded_by=new["id"])
        present = query_provenance(self.root, "claude", old["id"])["memory"]
        self.assertEqual(present["superseded_by"], {"id": new["id"], "status": "present"})
        self.assertEqual(present["lifecycle"], "superseded")

    # ---------- MCP tool ----------

    def test_mcp_tool_is_owner_bound_and_records_no_exposure(self) -> None:
        settings = {"DATA_DIR": str(self.root), "AI_MEMORY_ROOT": str(self.root), "AI_MEMORY_AGENT": "claude"}
        with patch.dict(os.environ, settings, clear=False):
            # Resolve the importable server module (source tree or clean install).
            origin = importlib.util.find_spec("server").origin
            spec = importlib.util.spec_from_file_location("provenance_server_under_test", origin)
            server = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(server)
            self.assertEqual(Path(server.PROJECT_ROOT), self.root.resolve())
            params = list(inspect.signature(server.memory_provenance).parameters)
            self.assertEqual(params, ["memory_id", "limit"])
            memory = server.store.remember(SECRET_MEMORY, "plan/general")
            server.recall(query="PROVENANCE-BODY")
            state = self.root / "state"
            before, wal_before = snapshot(state), wal_sizes(state)
            result = server.memory_provenance(memory["id"])
            self.assert_readonly(state, before, wal_before)
            self.assertEqual(result["queried_as"], "claude")
            self.assertEqual(result["exposures"]["status"], "ok")
            self.assertEqual(result["exposures"]["by_context_kind"]["recall"]["count"], 1)
            # This build keeps no retrieval ledger: unavailable, never a fake empty history.
            self.assertEqual(result["retrievals"], {"status": "unavailable", "reason": "missing_store"})
            self.assertFalse((state / "memory-feedback.sqlite3").exists())
            self.assert_no_secret(result)
            for bad in (0, 51):
                with self.assertRaises(ValueError):
                    server.memory_provenance(memory["id"], limit=bad)


if __name__ == "__main__":
    unittest.main()
