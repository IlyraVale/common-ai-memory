"""Games and lounge inside the unified UI, health, failure isolation, single instance, launcher.

All writes stay in TemporaryDirectory (under a Chinese-named folder on purpose).
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import lounge_viewer
import memory_services
import memory_ui
import minigames_viewer
from config import human_identity
from lounge_room import LoungeRoom
from memory_store import MemoryStore
from memory_ui import create_ui_server, running_instance

HERE = Path(__file__).resolve().parent
SECRET = "私密聊天内容-SECRET"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(port: int, path: str, *, host: str | None = None, method: str = "GET", body: bytes | None = None,
        headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request(method, path, body=body, headers={"Host": host or f"127.0.0.1:{port}", **(headers or {})})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data, dict(response.getheaders())


def legacy(handler_cls: type, root: Path, path: str):
    cls = type("Legacy", (handler_cls,), {"root": root})
    server = ThreadingHTTPServer(("127.0.0.1", 0), cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        return get(server.server_address[1], path)
    finally:
        server.shutdown()
        server.server_close()


class FakeBridge(BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *args):
        return

    def _reply(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._reply({"ok": True, "mode": "manual", "pending_count": 0} if self.path == "/v1/status" else {"ok": False}, 200 if self.path == "/v1/status" else 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        FakeBridge.calls.append((self.path, body))
        self._reply({"ok": True, "mode": body.get("mode", "manual")})


def stop_instance(port: int) -> None:
    """Stop a UI started by the launcher test and wait (bounded) until it is gone."""
    health = running_instance("127.0.0.1", port)
    if not health:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(health["pid"]), "/T", "/F"], capture_output=True)
    else:
        os.kill(health["pid"], 9)
    deadline = time.monotonic() + 15
    while memory_services.port_open(port, timeout=0.2) and time.monotonic() < deadline:
        time.sleep(0.1)
    time.sleep(0.5)  # the venv launcher parent exits right after its child and releases the log handles


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="memory-services-")
        self.root = Path(self.temp.name) / "统一入口根目录"
        (self.root / "memory").mkdir(parents=True)
        MemoryStore(self.root, "gpt").remember("一条记忆。", "life/daily")
        games = self.root / ".games" / "minigames" / "holdem"
        games.mkdir(parents=True)
        (games / "hdm-abc123.json").write_text(json.dumps({
            "id": "hdm-abc123", "game": "holdem", "status": "playing", "street": "flop",
            "players": {"gpt": {"hole": ["As", "Kd"]}, "claude": {"hole": ["2c", "7h"]}}, "updated_at": 1,
        }), encoding="utf-8")
        LoungeRoom(self.root, human_identity()).post(text=SECRET)
        self.server, self.api = create_ui_server(self.root, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()


class IntegrationTests(Base):
    def test_shell_has_games_and_lounge(self) -> None:
        page = get(self.port, "/")[1].decode()
        self.assertIn('href="#games"', page)
        self.assertIn('href="#lounge"', page)
        self.assertIn("游戏厅", page)
        self.assertIn("聊天室", page)

    def test_games_page_and_api_match_legacy(self) -> None:
        status, page, headers = get(self.port, "/games")
        self.assertEqual(status, 200)
        self.assertIn(b'fetch("/api/games/matches"', page)
        self.assertIn("script-src 'nonce-", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "SAMEORIGIN")
        for path in ("/api/matches", "/api/match/hdm-abc123", "/api/match/nope"):
            expected = legacy(minigames_viewer.Handler, self.root, path)
            actual = get(self.port, "/api/games/" + path[len("/api/"):])
            self.assertEqual((actual[0], json.loads(actual[1])), (expected[0], json.loads(expected[1])), path)
        for path in ("/api/games/matches", "/api/games/match/nope", "/api/lounge/state", "/api/lounge/attachments/" + "0" * 32):
            headers = get(self.port, path)[2]
            self.assertIn("no-store", headers.get("Cache-Control", ""), path)
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff", path)
        state = json.loads(get(self.port, "/api/games/match/hdm-abc123")[1])
        self.assertNotIn("As", json.dumps(state), "hidden cards stay redacted")

    def test_lounge_page_state_and_attachment_paths(self) -> None:
        status, page, headers = get(self.port, "/lounge")
        self.assertEqual(status, 200)
        for path in (b'"/api/lounge/state"', b'"/api/lounge/messages"', b'"/api/lounge/bridge/status"', b'"/api/lounge/attachments/'):
            self.assertIn(path, page)
        self.assertNotIn(b'fetch("/api/state"', page)
        self.assertIn("img-src 'self' data: blob:", headers["Content-Security-Policy"])
        expected = json.loads(legacy(lounge_viewer.Handler, self.root, "/api/state")[1])
        self.assertEqual(json.loads(get(self.port, "/api/lounge/state")[1]), expected)
        self.assertEqual(get(self.port, "/api/lounge/attachments/" + "0" * 32)[0], 404)

    def test_lounge_post_requires_same_origin_and_appends(self) -> None:
        body = json.dumps({"text": "来自统一入口的一句话"}).encode()
        headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        self.assertEqual(get(self.port, "/api/lounge/messages", method="POST", body=body, headers=headers)[0], 403)
        self.assertEqual(get(self.port, "/api/lounge/messages", method="POST", body=body,
                             headers={**headers, "Origin": "http://evil.example"})[0], 403)
        status, data, _ = get(self.port, "/api/lounge/messages", method="POST", body=body,
                              headers={**headers, "Origin": self.origin})
        self.assertEqual(status, 200, data)
        rows = (self.root / ".lounge" / "messages.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(rows[-1])["text"], "来自统一入口的一句话")
        self.assertEqual(json.loads(rows[-1])["author"], human_identity())

    def test_refused_posts_always_get_their_reply(self) -> None:
        body = json.dumps({"text": "x" * 200_000}).encode()
        headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        cases = (("/api/lounge/messages", {"Origin": "http://evil.example"}, 403),
                 ("/api/lounge/messages", {}, 403),
                 ("/api/admin/memories", {"Origin": "http://evil.example"}, 403),
                 ("/api/admin/memories", {"X-Memory-UI": "1"}, 403),
                 ("/api/home", {}, 404))
        for _ in range(30):
            for path, extra, expected in cases:
                self.assertEqual(get(self.port, path, method="POST", body=body, headers={**headers, **extra})[0],
                                 expected, path)
        self.assertEqual(get(self.port, "/health")[0], 200)

    def test_bridge_status_and_mode_go_to_the_internal_bridge(self) -> None:
        offline = f"http://127.0.0.1:{free_port()}"
        with patch.object(lounge_viewer, "BRIDGE_URL", offline), patch.object(memory_ui, "BRIDGE_URL", offline):
            self.assertEqual(get(self.port, "/api/lounge/bridge/status")[0], 503, "bridge offline reported, UI fine")
            self.assertEqual(json.loads(get(self.port, "/health")[1])["lounge_bridge"]["running"], False)
        bridge = ThreadingHTTPServer(("127.0.0.1", 0), FakeBridge)
        threading.Thread(target=bridge.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{bridge.server_address[1]}"
        try:
            with patch.object(lounge_viewer, "BRIDGE_URL", url), patch.object(memory_ui, "BRIDGE_URL", url):
                self.assertEqual(json.loads(get(self.port, "/api/lounge/bridge/status")[1])["mode"], "manual")
                body = json.dumps({"mode": "active"}).encode()
                status, _, _ = get(self.port, "/api/lounge/bridge/mode", method="POST", body=body,
                                   headers={"Content-Type": "application/json", "Content-Length": str(len(body)),
                                            "Origin": self.origin})
                self.assertEqual(status, 200)
                self.assertEqual(FakeBridge.calls[-1], ("/v1/mode", {"mode": "active"}))
                health = json.loads(get(self.port, "/health")[1])
                self.assertEqual(health["lounge_bridge"], {"running": True, "mode": "manual"})
        finally:
            bridge.shutdown()
            bridge.server_close()

    def test_health_is_safe(self) -> None:
        status, data, headers = get(self.port, "/health")
        health = json.loads(data)
        self.assertEqual(status, 200)
        self.assertEqual((health["ok"], health["app"]), (True, memory_ui.APP_NAME))
        self.assertEqual(set(health["components"]), {"atrium", "manager", "duplicates", "games", "lounge"})
        self.assertTrue(all(health["components"].values()))
        text = data.decode()
        for leak in (str(self.root), self.temp.name, SECRET, self.api.token, "owner-config"):
            self.assertNotIn(leak, text)
        self.assertEqual(get(self.port, "/health", host="evil.example")[0], 421)

    def test_module_failures_are_isolated(self) -> None:
        with patch.object(minigames_viewer, "iter_matches", side_effect=RuntimeError(SECRET)):
            status, data, _ = get(self.port, "/api/games/matches")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(data), {"ok": False, "component": "games", "error": "RuntimeError"})
        with patch.object(lounge_viewer, "read_json", side_effect=OSError(SECRET)):
            status, data, _ = get(self.port, "/api/lounge/state")
        self.assertEqual((status, json.loads(data)["error"]), (500, "OSError"))
        self.assertNotIn(SECRET.encode(), data)
        self.assertEqual(get(self.port, "/health")[0], 200)
        self.assertEqual(get(self.port, "/atrium")[0], 200)
        self.assertEqual(get(self.port, "/api/home")[0], 200)

    def test_atrium_links_point_to_integrated_pages(self) -> None:
        page = get(self.port, "/atrium")[1]
        self.assertNotIn(b"__GAME_HALL_URL__", page)
        self.assertNotIn(b"__LOUNGE_URL__", page)
        self.assertIn(b'href="/#games"', page)
        self.assertIn(b'href="/#lounge"', page)

    def test_no_access_logs(self) -> None:
        import contextlib
        import io

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            get(self.port, "/api/lounge/state")
            get(self.port, "/api/games/matches")
        self.assertEqual(err.getvalue(), "")


class SingleInstanceAndLauncherTests(unittest.TestCase):
    def test_serve_detects_running_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server, _ = create_ui_server(tmp, port=0)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                self.assertEqual(running_instance("127.0.0.1", port)["pid"], os.getpid())
                with patch.object(memory_ui, "create_ui_server", side_effect=AssertionError("second bind")):
                    self.assertEqual(memory_ui.serve(tmp, port=port), 0)
                with patch.object(memory_services.subprocess, "Popen", side_effect=AssertionError("second start")):
                    self.assertEqual(memory_services.start(Path(tmp), port, use_task=False), 0)
            finally:
                server.shutdown()
                server.server_close()

    def test_launcher_refuses_foreign_port(self) -> None:
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        try:
            self.assertEqual(memory_services.start(Path(tempfile.gettempdir()), holder.getsockname()[1], use_task=False), 2)
        finally:
            holder.close()

    def test_launcher_starts_hidden_once_then_reports_running(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memory-launcher-", ignore_cleanup_errors=True) as tmp:
            root = Path(tmp) / "中文 启动根"
            (root / "memory").mkdir(parents=True)
            port = free_port()
            command = memory_services.ui_command(root, port)
            if os.name == "nt":
                self.assertTrue(command[0].lower().endswith("pythonw.exe"), "hidden: no console window")
            self.assertIn("--supervise-bridge", command)
            self.addCleanup(stop_instance, port)
            with patch.object(memory_services, "ui_command",
                              lambda r, p: [sys.executable, str(Path(memory_ui.__file__).resolve()), "--root", str(r), "--port", str(p)]):
                self.assertEqual(memory_services.start(root, port, use_task=False), 0)
            try:
                first = running_instance("127.0.0.1", port)
                self.assertIsNotNone(first)
                with patch.object(memory_services.subprocess, "Popen", side_effect=AssertionError("duplicate")):
                    self.assertEqual(memory_services.start(root, port, use_task=False), 0)
                self.assertEqual(running_instance("127.0.0.1", port)["pid"], first["pid"])
                rows = {name: state for name, _, state in memory_services.status_rows(port)}
                self.assertTrue(rows["Memory UI"].startswith("running"))
                self.assertTrue(rows["Games"].startswith("integrated"))
                self.assertTrue(rows["Lounge"].startswith("integrated"))
            finally:
                stop_instance(port)

    def test_task_definition(self) -> None:
        definition = memory_services.task_definition(Path("C:/somewhere/common-ai-memory"), 8766)
        self.assertEqual(definition["name"], "Common AI Memory - UI")
        self.assertEqual(definition["multiple_instances"], "IgnoreNew")
        self.assertIn("--supervise-bridge", definition["arguments"])
        self.assertIn("8766", definition["arguments"])
        self.assertEqual(definition["working_directory"], str(Path("C:/somewhere/common-ai-memory")))
        found = [p for p in (HERE / "scripts" / "install-memory-ui-task.ps1", HERE.parent / "scripts" / "install-memory-ui-task.ps1") if p.is_file()]
        if not found:
            self.skipTest("optional Windows helper script not shipped in this install")
        script = found[0].read_text(encoding="utf-8")
        for needle in ("-AtLogOn", "pythonw.exe", "IgnoreNew", "-Hidden", "RunLevel Limited", "--supervise-bridge",
                       "-RestartCount 3"):
            self.assertIn(needle, script)
        self.assertNotIn("Nightly Dream", script)

    def test_bridge_supervisor_starts_manual_and_respects_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = memory_ui.BridgeSupervisor(Path(tmp), interval=0.1)
            with patch.object(memory_ui, "bridge_status", return_value={"running": True, "mode": "ai-chat"}), \
                    patch.object(memory_ui.subprocess, "Popen", side_effect=AssertionError("must not start")):
                supervisor.check()
            self.assertEqual(supervisor.state, "running")
            started = []

            class FakeProc:
                def poll(self):
                    return 1

            with patch.object(memory_ui, "bridge_status", return_value={"running": False, "mode": None}), \
                    patch.object(memory_ui.subprocess, "Popen", side_effect=lambda argv, **kw: started.append(argv) or FakeProc()):
                for _ in range(8):
                    supervisor.check()
            self.assertEqual(len(started), memory_ui.BridgeSupervisor.MAX_RESTARTS, "restarts are rate limited")
            self.assertEqual(started[0][-2:], ["--mode", "manual"])
            self.assertEqual(supervisor.state, "gave_up")


class BoundaryTests(unittest.TestCase):
    def test_new_modules_never_kill_processes(self) -> None:
        sources = "".join((HERE / name).read_text(encoding="utf-8") if (HERE / name).is_file()
                          else Path(__import__(name[:-3]).__file__).read_text(encoding="utf-8")
                          for name in ("memory_ui.py", "memory_services.py"))
        self.assertNotRegex(sources, r"Stop-Process|taskkill")
        self.assertNotIn("Nightly Dream", sources)


if __name__ == "__main__":
    unittest.main()
