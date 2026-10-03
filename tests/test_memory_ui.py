"""Unified memory UI: Atrium + manager + duplicate check on one localhost server.

All writes stay in TemporaryDirectory (under a Chinese-named folder on purpose).
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import http.client
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import memory_atrium
from memory_manager import create_server as create_manager_server
from memory_search import rebuild_project as rebuild_fts
from memory_store import MemoryStore
from memory_ui import create_ui_server
from memory_vectors import DEFAULT_MODEL_ID, _BACKEND_CACHE, rebuild_project as rebuild_vec

HERE = Path(__file__).resolve().parent


class TinyBackend:
    model_id = "test/tiny"
    revision = "1"
    fingerprint = "tiny-fingerprint"
    dimension = 3

    def embed(self, texts):
        return [[1.0, float(len(text) % 7 + 1), 0.5] for text in texts]


def snapshot(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(list((root / "memory").rglob("*.md")) + list((root / "state").glob("*.sqlite3"))):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class UiBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="memory-ui-")
        self.root = Path(self.temp.name) / "记忆根目录"
        self.root.mkdir()
        _BACKEND_CACHE[(os.path.normcase(str(self.root.resolve())), DEFAULT_MODEL_ID)] = TinyBackend()
        gpt = MemoryStore(self.root, "gpt")
        self.a = gpt.remember("用户喜欢无糖冰美式。", "preference/food")
        self.b = gpt.remember("用户喜欢无糖冰美式！", "preference/food")
        self.c = MemoryStore(self.root, "claude").remember("Claude 的一条笔记。", "life/daily")
        rebuild_fts(self.root)
        rebuild_vec(self.root, backend=TinyBackend())
        self.server, self.api = create_ui_server(self.root, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.token = None

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, method, path, body=None, *, admin=False, host=None, origin=None, ui=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if admin or ui:
            headers["X-Memory-UI"] = "1"
        if admin:
            if self.token is None:
                self.token = self.request("GET", "/api/admin/session", ui=True)[1]["token"]
            headers["X-Manager-Token"] = token or self.token
        if origin:
            headers["Origin"] = origin
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw
        return response.status, data, dict(response.getheaders())


class RoutingTests(UiBase):
    def test_localhost_only(self) -> None:
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        for host in ("0.0.0.0", "192.168.1.5", "::"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                create_ui_server(self.root, host=host, port=0)

    def test_shell_navigation(self) -> None:
        status, page, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        text = page.decode("utf-8")
        for label, href in (("中庭", "#atrium"), ("管理", "#manage"), ("重复检查", "#duplicates")):
            self.assertIn(f'href="{href}"', text)
            self.assertIn(label, text)
        self.assertIn('aria-current', text)
        self.assertIn("<iframe", text)
        for path in ('"/atrium"', '"/manage"', '"/duplicates"'):
            self.assertIn(path, text)
        self.assertIn('name="viewport"', text)
        self.assertIn("@media (max-width:600px)", text)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_atrium_route_and_data_unchanged(self) -> None:
        status, page, headers = self.request("GET", "/atrium")
        self.assertEqual(status, 200)
        # Byte for byte, except the game hall / lounge links now open the integrated shell pages.
        expected = memory_atrium.HTML
        for placeholder, target in (("__GAME_HALL_URL__", "games"), ("__LOUNGE_URL__", "lounge")):
            expected = expected.replace(f'href="{placeholder}" target="_blank"', f'href="/#{target}" target="_top"')
        self.assertEqual(page, expected.encode("utf-8"))
        self.assertNotIn(b"__GAME_HALL_URL__", page)
        self.assertNotIn(b"__LOUNGE_URL__", page)
        self.assertEqual(headers["X-Frame-Options"], "SAMEORIGIN")
        # Compare against the legacy Atrium-only server over HTTP (version independent).
        legacy_handler = type("LegacyHandler", (memory_atrium.Handler,), {"app": memory_atrium.MemoryAtrium(self.root)})
        legacy = ThreadingHTTPServer(("127.0.0.1", 0), legacy_handler)
        threading.Thread(target=legacy.serve_forever, daemon=True).start()
        try:
            for path in ("/api/home", "/api/memories", "/api/memories?owner=gpt"):
                conn = http.client.HTTPConnection("127.0.0.1", legacy.server_address[1], timeout=30)
                conn.request("GET", path)
                expected = json.loads(conn.getresponse().read())
                conn.close()
                status, actual, _ = self.request("GET", path)
                self.assertEqual(status, 200)
                if path == "/api/home":
                    self.assertEqual(set(actual), set(expected))
                else:
                    self.assertEqual(json.dumps(actual, sort_keys=True), json.dumps(expected, sort_keys=True))
        finally:
            legacy.shutdown()
            legacy.server_close()
        self.assertEqual(self.request("GET", "/api/activity")[0], 200)

    def test_manager_and_duplicate_routes(self) -> None:
        for path, tab in (("/manage", "list"), ("/duplicates", "dups")):
            status, page, headers = self.request("GET", path)
            text = page.decode("utf-8")
            self.assertEqual(status, 200)
            self.assertIn(f'const API = "/api/admin", INITIAL = "{tab}"', text)
            self.assertIn('class="embedded"', text)
            self.assertIn("此页面的操作会修改长期记忆。", text)
            self.assertIn("frame-ancestors 'self'", headers["Content-Security-Policy"])
            self.assertIn("script-src 'nonce-", headers["Content-Security-Policy"])
            self.assertEqual(headers["X-Frame-Options"], "SAMEORIGIN")
            self.assertEqual(headers["Cache-Control"], "no-store")

    def test_token_never_in_pages_or_urls(self) -> None:
        token = self.request("GET", "/api/admin/session", ui=True)[1]["token"]
        for path in ("/", "/atrium", "/manage", "/duplicates"):
            self.assertNotIn(token.encode(), self.request("GET", path)[1])
        self.assertNotIn("localStorage", re.sub(r"atrium-theme", "", self.request("GET", "/manage")[1].decode()))

    def test_host_origin_and_header_guards(self) -> None:
        self.assertEqual(self.request("GET", "/", host="evil.example")[0], 421)
        self.assertEqual(self.request("GET", "/api/home", host="rebind.example:80")[0], 421)
        self.assertEqual(self.request("GET", "/api/admin/session")[0], 403, "session needs the UI header")
        self.assertEqual(self.request("GET", "/api/admin/memories", ui=True)[0], 403, "API needs the token")
        self.assertEqual(self.request("GET", "/api/admin/memories", admin=True, token="wrong")[0], 403)
        status, _, _ = self.request("POST", f"/api/admin/memory/{self.a['id']}/update",
                                    {"acting_owner": "gpt", "changes": {"content": "x"}}, admin=True,
                                    origin="http://evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(self.request("POST", "/api/home", {})[0], 404)

    def test_no_access_log_output(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.request("GET", "/manage")
            self.request("GET", f"/api/admin/memory/{self.a['id']}", admin=True)
        self.assertEqual(stderr.getvalue(), "")


class AdminFlowTests(UiBase):
    def test_viewing_and_scanning_do_not_mutate(self) -> None:
        before = snapshot(self.root)
        for path in ("/", "/atrium", "/manage", "/duplicates", "/api/home", "/api/memories"):
            self.request("GET", path)
        rows = self.request("GET", "/api/admin/memories?q=" + quote("冰美式"), admin=True)[1]
        self.assertEqual(len(rows), 2)
        self.request("GET", f"/api/admin/memory/{self.a['id']}", admin=True)
        self.assertEqual(self.request("GET", "/api/admin/duplicates?run=0", admin=True)[1]["status"], "not_scanned")
        scan = self.request("GET", "/api/admin/duplicates?run=1", admin=True)[1]
        self.assertTrue(scan["read_only"])
        self.assertIn("exact_duplicate", {g["relation"] for g in scan["groups"]})
        group = next(g for g in scan["groups"] if g["relation"] == "exact_duplicate")
        self.assertEqual({m["id"] for m in group["members"]}, {self.a["id"], self.b["id"]})
        self.assertTrue(all("content" in m for m in group["members"]), "side-by-side comparison has both bodies")
        self.assertTrue(self.request("GET", "/api/admin/duplicates?run=0", admin=True)[1]["cached"])
        self.assertEqual(snapshot(self.root), before)

    def test_list_detail_edit_and_stale_edit(self) -> None:
        rows = self.request("GET", "/api/admin/memories?owner=gpt", admin=True)[1]
        self.assertEqual(len(rows), 2)
        detail = self.request("GET", f"/api/admin/memory/{self.a['id']}", admin=True)[1]
        status, saved, _ = self.request("POST", f"/api/admin/memory/{self.a['id']}/update",
                                        {"acting_owner": "gpt", "expected_version": detail["version"],
                                         "changes": {"content": "统一前端里编辑。"}}, admin=True)
        self.assertEqual((status, saved["saved"], saved["receipt"]), (200, True, "recorded"))
        self.assertEqual(MemoryStore(self.root, "gpt")._find_record(self.a["id"])[1]["content"], "统一前端里编辑。")
        status, _, _ = self.request("POST", f"/api/admin/memory/{self.a['id']}/update",
                                    {"acting_owner": "gpt", "expected_version": detail["version"],
                                     "changes": {"content": "旧页面覆盖。"}}, admin=True)
        self.assertEqual(status, 409)
        self.assertEqual(MemoryStore(self.root, "gpt")._find_record(self.a["id"])[1]["content"], "统一前端里编辑。")

    def test_supersede_merge_and_delete(self) -> None:
        status, superseded, _ = self.request("POST", f"/api/admin/memory/{self.b['id']}/supersede",
                                             {"acting_owner": "gpt", "superseded_by": self.a["id"]}, admin=True)
        self.assertEqual((status, superseded["lifecycle"]), (200, "superseded"))
        status, review, _ = self.request("POST", f"/api/admin/memory/{self.b['id']}/review_needed",
                                         {"acting_owner": "gpt"}, admin=True)
        self.assertEqual((status, review["lifecycle"]), (200, "review_needed"))
        status, preview, _ = self.request("POST", "/api/admin/merge/preview", {"memory_ids": [self.a["id"], self.b["id"]]}, admin=True)
        self.assertEqual((status, preview["executable"]), (200, True))
        draft = {"content": "合并后的一条。", "category": "preference/food"}
        self.assertEqual(self.request("POST", "/api/admin/merge/execute",
                                      {"memory_ids": [self.a["id"], self.b["id"]], "acting_owner": "gpt", "draft": draft},
                                      admin=True)[0], 400, "merge needs explicit confirm")
        status, merged, _ = self.request("POST", "/api/admin/merge/execute",
                                         {"memory_ids": [self.a["id"], self.b["id"]], "acting_owner": "gpt",
                                          "draft": draft, "confirm": True}, admin=True)
        self.assertEqual(status, 200)
        new_id = merged["merged_id"]
        self.assertEqual(self.request("POST", f"/api/admin/memory/{new_id}/forget",
                                      {"acting_owner": "gpt", "confirm_id": "wrong"}, admin=True)[0], 400)
        self.assertEqual(self.request("POST", f"/api/admin/memory/{new_id}/forget",
                                      {"acting_owner": "gpt", "confirm_id": new_id}, admin=True)[0], 200)
        self.assertEqual(self.request("GET", f"/api/admin/memory/{new_id}", admin=True)[0], 404)


class EntryPointTests(unittest.TestCase):
    def test_existing_atrium_command_starts_unified_ui(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memory-ui-entry-") as tmp:
            root = Path(tmp) / "中文 项目根"
            (root / "memory").mkdir(parents=True)
            entry = importlib.util.find_spec("memory_atrium").origin  # source tree or installed package
            proc = subprocess.Popen([sys.executable, entry, "--root", str(root), "--port", "0"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
            try:
                lines = [proc.stdout.readline().strip() for _ in range(2)]
                self.assertEqual(lines[0], "Memory UI:")
                match = re.fullmatch(r"http://(127\.0\.0\.1|localhost):(\d+)/", lines[1])
                self.assertIsNotNone(match, lines)
                host, port = match.group(1), int(match.group(2))
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
                conn.request("GET", "/", headers={"Host": f"{host}:{port}"})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn("重复检查", response.read().decode("utf-8"))
                conn.close()
            finally:
                proc.terminate()
                proc.wait(timeout=30)
                proc.stdout.close()

    def test_standalone_manager_still_works(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memory-ui-standalone-") as tmp:
            MemoryStore(tmp, "gpt").remember("独立模式。", "life/daily")
            server, token = create_manager_server(tmp, port=0)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
                conn.request("GET", "/api/memories", headers={"Host": f"127.0.0.1:{port}", "X-Memory-UI": "1",
                                                              "X-Manager-Token": token})
                response = conn.getresponse()
                self.assertEqual((response.status, len(json.loads(response.read()))), (200, 1))
                conn.close()
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
