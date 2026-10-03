"""Local Memory Manager: admin layer and localhost HTTP. All writes stay in TemporaryDirectory."""

from __future__ import annotations

import hashlib
import http.client
import inspect
import json
import os
import re
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import memory_manager
from execution_receipts import ExecutionReceiptStore
from memory_manager import AdminError, MemoryAdmin, create_server
from memory_search import MemorySearchIndex, rebuild_project as rebuild_fts
from memory_store import MemoryStore
from memory_vectors import DEFAULT_MODEL_ID, MemoryVectorIndex, _BACKEND_CACHE, rebuild_project as rebuild_vec


class TinyBackend:
    model_id = "test/tiny"
    revision = "1"
    fingerprint = "tiny-fingerprint"
    dimension = 3

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += len(texts)
        return [[1.0, float(len(text) % 7 + 1), 0.5] for text in texts]


def memory_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((root / "memory").rglob("*.md")):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="memory-manager-")
        self.root = Path(self.temp.name)
        self.backend = TinyBackend()
        _BACKEND_CACHE[(os.path.normcase(str(self.root.resolve())), DEFAULT_MODEL_ID)] = self.backend
        self.gpt = MemoryStore(self.root, "gpt")
        self.claude = MemoryStore(self.root, "claude")
        self.a = self.gpt.remember("用户喜欢无糖冰美式。", "preference/food", source="user_statement")
        self.b = self.gpt.remember("项目前端用 Vue 3 和 Vite。", "project/general", status="open")
        self.c = self.claude.remember("Claude 记下的一条笔记。", "life/daily")
        rebuild_fts(self.root)
        rebuild_vec(self.root, backend=self.backend)
        self.backend.calls = 0
        self.admin = MemoryAdmin(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()


class AdminTests(Base):
    def test_list_search_and_filters(self) -> None:
        self.assertEqual(len(self.admin.list()), 3)
        self.assertEqual([r["id"] for r in self.admin.list({"q": "vue"})], [self.b["id"]])
        self.assertEqual({r["owner"] for r in self.admin.list({"owner": "claude"})}, {"claude"})
        self.assertEqual([r["id"] for r in self.admin.list({"status": "open"})], [self.b["id"]])
        self.assertEqual([r["id"] for r in self.admin.list({"source": "user_statement"})], [self.a["id"]])
        row = self.admin.list({"q": "冰美式"})[0]
        for key in ("id", "owner", "category", "preview", "created_at", "updated_at", "source", "status",
                    "lifecycle", "verification", "evidence_ref_count"):
            self.assertIn(key, row)

    def test_stale_and_superseded_remain_visible(self) -> None:
        self.gpt.update(self.a["id"], "用户喜欢无糖冰美式。", lifecycle="stale")
        self.gpt.update(self.b["id"], "项目前端用 Vue 3 和 Vite。", lifecycle="superseded", superseded_by=self.a["id"])
        self.assertEqual({r["lifecycle"] for r in self.admin.list()}, {"stale", "superseded", "active"})
        self.assertEqual([r["id"] for r in self.admin.list({"lifecycle": "superseded"})], [self.b["id"]])

    def test_detail_read(self) -> None:
        detail = self.admin.get(self.a["id"])
        self.assertEqual(detail["content"], "用户喜欢无糖冰美式。")
        self.assertEqual(detail["evidence_refs"], [])
        with self.assertRaises(AdminError) as raised:
            self.admin.get("no-such-id")
        self.assertEqual(raised.exception.status, 404)

    def test_content_edit_updates_fts_vector_and_receipt(self) -> None:
        before_hash = MemoryVectorIndex(self.root, backend=self.backend).diagnostics(
            self.gpt._filtered("all", include_inactive=True))["status"]
        updated = self.admin.update(self.a["id"], "gpt", {"content": "用户改喝燕麦拿铁。"})
        self.assertEqual(updated["content"], "用户改喝燕麦拿铁。")
        self.assertEqual(before_hash, "PASS")
        hits = MemorySearchIndex(self.root).search("燕麦拿铁", owner="all")
        self.assertIn(self.a["id"], [h["memory_id"] if isinstance(h, dict) and "memory_id" in h else h.get("id") for h in hits])
        records = self.gpt._filtered("all", include_inactive=True)
        self.assertEqual(MemorySearchIndex(self.root).diagnostics(records)["status"], "PASS")
        self.assertEqual(MemoryVectorIndex(self.root, backend=self.backend).diagnostics(records)["status"], "PASS")
        self.assertGreater(self.backend.calls, 0, "content change re-embeds")
        db = sqlite3.connect(ExecutionReceiptStore(self.root).path)
        try:
            ops = [r[0] for r in db.execute(
                "SELECT e.operation FROM execution_receipts e JOIN receipt_targets t USING(receipt_id) "
                "WHERE t.target_id=?", (self.a["id"],))]
        finally:
            db.close()
        self.assertIn("update_memory", ops)

    def test_metadata_only_edit_does_not_reembed(self) -> None:
        with patch("memory_vectors.MemoryVectorIndex.upsert") as upsert:
            updated = self.admin.update(self.a["id"], "gpt", {"verification": "confirmed", "lifecycle": "review_needed"})
        self.assertEqual((updated["verification"], updated["lifecycle"]), ("confirmed", "review_needed"))
        upsert.assert_not_called()
        self.assertEqual(self.backend.calls, 0)
        records = self.gpt._filtered("all", include_inactive=True)
        self.assertEqual(MemorySearchIndex(self.root).diagnostics(records)["status"], "PASS")

    def test_owner_admin_semantics(self) -> None:
        for owner in ("claude", "", "human", "root"):
            with self.subTest(owner=owner), self.assertRaises(AdminError) as raised:
                self.admin.update(self.a["id"], owner, {"content": "x"})
            self.assertEqual(raised.exception.status, 403)
        restricted = MemoryAdmin(self.root, admin_owners=("claude",))
        with self.assertRaises(AdminError):
            restricted.update(self.a["id"], "gpt", {"content": "x"})
        self.assertEqual(restricted.update(self.c["id"], "claude", {"content": "改过的笔记。"})["content"], "改过的笔记。")
        # MCP-level owner rules are unchanged: a gpt store still cannot touch claude's memory.
        with self.assertRaises(PermissionError):
            self.gpt.update(self.c["id"], "x")

    def test_validation_preserved(self) -> None:
        cases = [({"lifecycle": "deleted"}, 400), ({"verification": "maybe"}, 400),
                 ({"evidence_refs": ["receipt:" + "0" * 32]}, 400), ({"evidence_refs": ["http://x"]}, 400),
                 ({"lifecycle": "superseded", "superseded_by": ""}, 400),
                 ({"lifecycle": "superseded", "superseded_by": self.c["id"]}, 403),
                 ({"lifecycle": "superseded", "superseded_by": self.a["id"]}, 400),
                 ({"category": "no-such/category"}, 400), ({"content": "   "}, 400)]
        before = memory_digest(self.root)
        for changes, status in cases:
            with self.subTest(changes=changes), self.assertRaises(AdminError) as raised:
                self.admin.update(self.a["id"], "gpt", changes)
            self.assertEqual(raised.exception.status, status)
        self.assertEqual(memory_digest(self.root), before)

    def test_valid_evidence_ref_and_supersede(self) -> None:
        receipt = self.a["execution_receipt_id"]
        updated = self.admin.update(self.a["id"], "gpt", {"evidence_refs": [f"receipt:{receipt}"]})
        self.assertEqual(updated["evidence_refs"], [f"receipt:{receipt}"])
        superseded = self.admin.supersede(self.b["id"], self.a["id"], "gpt")
        self.assertEqual((superseded["lifecycle"], superseded["superseded_by"]), ("superseded", self.a["id"]))

    def test_delete_uses_forget_and_requires_confirmation(self) -> None:
        with self.assertRaises(AdminError):
            self.admin.forget(self.a["id"], "gpt", confirm_id="wrong")
        self.admin.supersede(self.b["id"], self.a["id"], "gpt")
        with patch.object(MemoryStore, "forget", autospec=True, side_effect=MemoryStore.forget) as forget:
            result = self.admin.forget(self.a["id"], "gpt", confirm_id=self.a["id"])
        forget.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertIsNone(self.gpt._find_record(self.a["id"]))
        # Existing forget semantics: the predecessor becomes review_needed, the receipt is redacted.
        self.assertEqual(self.admin.get(self.b["id"])["lifecycle"], "review_needed")
        row = ExecutionReceiptStore(self.root).get(self.a["execution_receipt_id"], owner="gpt")
        self.assertEqual(row["target_status"], "redacted")

    def test_stale_edit_is_rejected_and_fresh_edit_saves(self) -> None:
        loaded = self.admin.get(self.a["id"])
        # Another window (or an MCP caller) changes the memory after this page loaded it.
        self.gpt.update(self.a["id"], "别处改过的正文。", verification="partial")
        before = memory_digest(self.root)
        with self.assertRaises(AdminError) as raised:
            self.admin.update(self.a["id"], "gpt", {"content": "拿着旧版本的覆盖。"}, loaded["version"])
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(memory_digest(self.root), before, "stale save must not write")
        fresh = self.admin.get(self.a["id"])
        saved = self.admin.update(self.a["id"], "gpt", {"content": "刷新后保存。"}, fresh["version"])
        self.assertEqual((saved["content"], saved["saved"], saved["receipt"]), ("刷新后保存。", True, "recorded"))
        self.assertNotEqual(saved["version"], fresh["version"])
        with self.assertRaises(AdminError) as raised:
            self.admin.forget(self.a["id"], "gpt", self.a["id"], fresh["version"])
        self.assertEqual(raised.exception.status, 409, "delete also refuses a stale view")

    def test_metadata_change_alone_changes_version(self) -> None:
        loaded = self.admin.get(self.a["id"])
        self.gpt.update(self.a["id"], loaded["content"], lifecycle="review_needed")
        self.assertNotEqual(self.admin.get(self.a["id"])["version"], loaded["version"])

    def test_scan_cache_is_derived_and_invalidated(self) -> None:
        self.assertEqual(self.admin.duplicates(run=False)["status"], "not_scanned")
        first = self.admin.duplicates(run=True)
        self.assertFalse(first["cached"])
        self.assertTrue(self.admin.duplicates(run=False)["cached"])
        self.admin.update(self.a["id"], "gpt", {"verification": "partial"})
        self.assertEqual(self.admin.duplicates(run=False)["status"], "not_scanned", "mutation voids the cache")
        self.admin.duplicates(run=True)
        MemoryStore(self.root, "claude").remember("外部写入的新记忆。", "life/daily")
        self.assertEqual(self.admin.duplicates(run=False)["status"], "not_scanned", "external change voids the cache")

    def test_review_needed_button(self) -> None:
        self.assertEqual(self.admin.mark_review_needed(self.a["id"], "gpt")["lifecycle"], "review_needed")

    def test_concurrent_edits_are_safe(self) -> None:
        errors = []

        def edit(i):
            try:
                for n in range(5):
                    self.admin.update(self.a["id"], "gpt", {"content": f"并发编辑 {i}-{n}", "verification": "partial"})
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=edit, args=(i,)) for i in range(3)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertRegex(self.admin.get(self.a["id"])["content"], r"^并发编辑 \d-4$")
        self.assertEqual(len(list((self.root / "memory").rglob("*.tmp-*"))), 0)

    def test_manager_never_writes_files_directly(self) -> None:
        source = inspect.getsource(memory_manager)
        for pattern in (r"write_text", r"write_bytes", r"\.unlink\(", r"os\.replace", r"open\([^)]*['\"][wa]"):
            self.assertIsNone(re.search(pattern, source), pattern)
        before = memory_digest(self.root)
        real_write = Path.write_text

        def guarded(path, *args, **kwargs):
            frames = [f.filename for f in inspect.stack()[1:6]]
            if path.suffix == ".md" and not any(f.endswith("memory_store.py") for f in frames):
                raise AssertionError(f"direct markdown write outside MemoryStore: {path.name}")
            return real_write(path, *args, **kwargs)

        with patch.object(Path, "write_text", guarded):
            self.admin.update(self.a["id"], "gpt", {"content": "经由 MemoryStore 写入。"})
        self.assertNotEqual(memory_digest(self.root), before)


class MergeTests(Base):
    def test_preview_shows_plan_and_requires_confirm(self) -> None:
        before = memory_digest(self.root)
        preview = self.admin.merge_preview([self.a["id"], self.b["id"]])
        self.assertTrue(preview["executable"])
        self.assertIn("用户喜欢无糖冰美式", preview["draft"]["content"])
        self.assertIn("Vue 3", preview["draft"]["content"])
        self.assertEqual({r["id"] for r in preview["will_supersede"]}, {self.a["id"], self.b["id"]})
        self.assertEqual(memory_digest(self.root), before, "preview must not write")
        with self.assertRaises(AdminError):
            self.admin.merge_execute([self.a["id"], self.b["id"]], "gpt", preview["draft"], confirm=False)
        self.assertEqual(memory_digest(self.root), before)

    def test_cross_owner_merge_is_plan_only(self) -> None:
        preview = self.admin.merge_preview([self.a["id"], self.c["id"]])
        self.assertFalse(preview["executable"])
        with self.assertRaises(AdminError):
            self.admin.merge_execute([self.a["id"], self.c["id"]], "gpt", preview["draft"], confirm=True)

    def test_merge_creates_new_and_supersedes_both(self) -> None:
        draft = {"content": "合并后的记忆。", "category": "preference/food", "source": "user_statement",
                 "status": "", "verification": "confirmed"}
        result = self.admin.merge_execute([self.a["id"], self.b["id"]], "gpt", draft, confirm=True)
        merged = self.admin.get(result["merged_id"])
        self.assertEqual((merged["content"], merged["verification"]), ("合并后的记忆。", "confirmed"))
        for old in (self.a["id"], self.b["id"]):
            detail = self.admin.get(old)
            self.assertEqual((detail["lifecycle"], detail["superseded_by"]), ("superseded", result["merged_id"]))

    def test_failed_merge_leaves_no_half_state(self) -> None:
        before = {r["id"]: (r["lifecycle"], r["superseded_by"]) for r in self.admin.list()}
        real_update = MemoryStore.update
        calls = {"n": 0}

        def flaky(store, memory_id, content, **kwargs):
            if kwargs.get("lifecycle") == "superseded":
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("disk went away")
            return real_update(store, memory_id, content, **kwargs)

        with patch.object(MemoryStore, "update", flaky):
            with self.assertRaises(AdminError) as raised:
                self.admin.merge_execute([self.a["id"], self.b["id"]], "gpt",
                                         {"content": "不会留下的合并。", "category": "preference/food"}, confirm=True)
        self.assertIn("rolled back", str(raised.exception))
        after = {r["id"]: (r["lifecycle"], r["superseded_by"]) for r in self.admin.list()}
        self.assertEqual(after, before, "no new memory, no leftover supersession")


class HttpTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.server, self.token = create_server(self.root, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, method, path, body=None, *, token=True, host=None, origin=None, ui=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Host": host or f"127.0.0.1:{self.port}", "Content-Type": "application/json"}
        if ui:
            headers["X-Memory-UI"] = "1"
        if token:
            headers["X-Manager-Token"] = self.token
        if origin:
            headers["Origin"] = origin
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data, dict(response.getheaders())

    def test_binds_localhost_only(self) -> None:
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        for host in ("0.0.0.0", "192.168.1.10", "::"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                create_server(self.root, host=host, port=0)

    def test_refused_posts_always_get_their_reply(self):
        big = {"acting_owner": "gpt", "changes": {"content": "x" * 200_000}}
        for _ in range(30):
            self.assertEqual(self.request("POST", f"/api/memory/{self.a['id']}/update", big,
                                          origin="http://evil.example")[0], 403)
            self.assertEqual(self.request("POST", f"/api/memory/{self.a['id']}/update", big, token=False)[0], 403)
            self.assertEqual(self.request("POST", "/nowhere", big)[0], 404)

    def test_page_and_api_guards(self) -> None:
        status, page, headers = self.request("GET", "/", token=False, ui=False)
        self.assertEqual(status, 200)
        self.assertIn("Content-Security-Policy", headers)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn(self.token.encode(), page, "the admin token is never written into HTML")
        status, data, _ = self.request("GET", "/api/session", token=False)
        self.assertEqual((status, json.loads(data)["token"]), (200, self.token))
        self.assertEqual(self.request("GET", "/api/session", token=False, ui=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/memories", token=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/memories", ui=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/memories", host="evil.example:80")[0], 421)
        self.assertEqual(self.request("POST", f"/api/memory/{self.a['id']}/update",
                                      {"acting_owner": "gpt", "changes": {"content": "x"}},
                                      origin="http://evil.example")[0], 403)

    def test_http_list_detail_update_and_forget(self) -> None:
        status, data, _ = self.request("GET", "/api/memories?owner=gpt")
        self.assertEqual((status, len(json.loads(data))), (200, 2))
        status, data, _ = self.request("POST", f"/api/memory/{self.a['id']}/update",
                                       {"acting_owner": "gpt", "changes": {"content": "经由 HTTP 修改。"}})
        self.assertEqual((status, json.loads(data)["content"]), (200, "经由 HTTP 修改。"))
        status, data, _ = self.request("POST", f"/api/memory/{self.a['id']}/update",
                                       {"acting_owner": "gpt", "changes": {"lifecycle": "bogus"}})
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", f"/api/memory/{self.a['id']}/forget",
                                    {"acting_owner": "gpt", "confirm_id": self.a["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.request("GET", f"/api/memory/{self.a['id']}")[0], 404)

    def test_http_duplicates_and_merge_preview(self) -> None:
        status, data, _ = self.request("GET", "/api/duplicates")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["read_only"])
        status, data, _ = self.request("POST", "/api/merge/preview", {"memory_ids": [self.a["id"], self.b["id"]]})
        self.assertEqual((status, json.loads(data)["executable"]), (200, True))
        status, _, _ = self.request("POST", "/api/merge/execute",
                                    {"memory_ids": [self.a["id"], self.b["id"]], "acting_owner": "gpt",
                                     "draft": {"content": "x", "category": "preference/food"}})
        self.assertEqual(status, 400, "execute without confirm is refused")


if __name__ == "__main__":
    unittest.main()
