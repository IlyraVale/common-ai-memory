"""Unified local memory UI: Atrium, manager, duplicate check, game hall and lounge on one localhost origin.

One server, one URL. Each original page is reused rather than rewritten:

* the read-only Atrium is served unchanged at /atrium (its data routes stay put);
* the manager and duplicate check reuse memory_manager under /api/admin;
* the game hall spectator reuses minigames_viewer under /games and /api/games;
* the lounge reuses lounge_viewer under /lounge and /api/lounge.

A shell page at / holds the navigation and shows each page in a same-origin frame.
The lounge bridge stays its own process on 127.0.0.1:8879 (the browser extension
and the MCP lounge_wake_ack tool address it there); this server can optionally
supervise it so nothing else needs starting. A failing module answers 5xx with its
error class only and never takes the other pages down.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import memory_atrium
from config import __version__, env_path, load_dotenv
from memory_manager import (
    ManagerApi, MemoryAdmin, bind_server, dispatch_api, render_page, request_problem, send, send_json,
)

APP_NAME = "Common AI Memory UI"
APP_VERSION = __version__
DEFAULT_PORT = 8877
BRIDGE_URL = os.getenv("LOUNGE_BRIDGE_URL", "http://localhost:8879").rstrip("/")
ADMIN_PREFIX = "/api/admin"
GAMES_PREFIX = "/api/games"
LOUNGE_PREFIX = "/api/lounge"
PAGES = {"atrium": "/atrium", "manage": "/manage", "duplicates": "/duplicates", "games": "/games", "lounge": "/lounge"}

SHELL = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Common AI Memory</title>
<style>
:root{--bg:#f6f4ef;--fg:#222;--muted:#777;--line:#ddd;--accent:#7a3b2e}
@media (prefers-color-scheme:dark){:root{--bg:#1b1a19;--fg:#eee;--muted:#aaa;--line:#3a3836;--accent:#e0a090}}
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,"Microsoft YaHei",sans-serif}
nav{display:flex;align-items:center;gap:4px;height:44px;padding:0 12px;border-bottom:1px solid var(--line);overflow-x:auto;white-space:nowrap}
nav b{margin-right:12px;font-weight:600;letter-spacing:.04em}
nav a{color:var(--muted);text-decoration:none;padding:10px 12px;border-bottom:2px solid transparent}
nav a[aria-current="page"]{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}
nav a:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
iframe{display:block;width:100%;height:calc(100% - 45px);border:0}
@media (max-width:600px){nav b{display:none}nav a{padding:10px 7px}}
</style></head><body>
<nav aria-label="Memory UI"><b>Common AI Memory</b>
<a href="#atrium" data-page="atrium">中庭</a><a href="#manage" data-page="manage">管理</a><a href="#duplicates" data-page="duplicates">重复检查</a><a href="#games" data-page="games">游戏厅</a><a href="#lounge" data-page="lounge">聊天室</a></nav>
<iframe id="view" title="Memory UI"></iframe>
<script nonce="__NONCE__">
const PAGES = {atrium: "/atrium", manage: "/manage", duplicates: "/duplicates", games: "/games", lounge: "/lounge"};
function show() {
  const page = PAGES[location.hash.slice(1)] ? location.hash.slice(1) : "atrium";
  document.querySelectorAll("nav a").forEach(a => a.setAttribute("aria-current", a.dataset.page === page ? "page" : "false"));
  const frame = document.getElementById("view");
  if (frame.getAttribute("src") !== PAGES[page]) frame.setAttribute("src", PAGES[page]);
}
window.addEventListener("hashchange", show); show();
</script></body></html>"""

VIEWER_CSP = ("default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; img-src 'self' data: blob:; "
              "font-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")


def render_shell() -> tuple[bytes, str]:
    nonce = secrets.token_urlsafe(16)
    csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; frame-src 'self'; "
           "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    return SHELL.replace("__NONCE__", nonce).encode("utf-8"), csp


def atrium_page() -> bytes:
    """The Atrium page as is; only its links to the retired standalone viewers point at the shell."""
    html = memory_atrium.HTML
    for placeholder, page in (("__GAME_HALL_URL__", "games"), ("__LOUNGE_URL__", "lounge")):
        html = html.replace(f'href="{placeholder}" target="_blank"', f'href="/#{page}" target="_top"')
    return html.encode("utf-8")


def viewer_page(html: str, replacements: tuple[tuple[str, str], ...]) -> tuple[bytes, str]:
    """A reused viewer page with its API paths prefixed and its one script given a CSP nonce."""
    nonce = secrets.token_urlsafe(16)
    for old, new in replacements:
        html = html.replace(old, new)
    html = html.replace("<script>", f'<script nonce="{nonce}">')
    return html.encode("utf-8"), VIEWER_CSP.format(nonce=nonce)


def _module(name: str):
    try:
        return __import__(name)
    except Exception:  # optional component unavailable
        return None


def bridge_status(timeout: float = 1.5) -> dict[str, Any]:
    try:
        with urlopen(Request(BRIDGE_URL + "/v1/status"), timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return {"running": True, "mode": data.get("mode") if isinstance(data, dict) else None}
    except (URLError, OSError, ValueError):
        return {"running": False, "mode": None}


class BridgeSupervisor:
    """Keeps the lounge bridge worker running as a child process, in manual mode on start.

    A bridge started by someone else is left alone. Restarts are rate limited.
    """

    MAX_RESTARTS = 5
    WINDOW_SECONDS = 600

    def __init__(self, root: Path, interval: float = 10.0) -> None:
        self.root = root
        self.interval = interval
        self.child: subprocess.Popen | None = None
        self.starts: list[float] = []
        self._stop = threading.Event()
        self.state = "idle"

    def _spawn(self) -> None:
        logs = self.root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        with (logs / "lounge-bridge.out.log").open("ab") as out, (logs / "lounge-bridge.err.log").open("ab") as err:
            self.child = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve().parent / "lounge_bridge.py"),
                 "--root", str(self.root), "--mode", "manual"],
                cwd=str(self.root), stdout=out, stderr=err, stdin=subprocess.DEVNULL, creationflags=flags,
            )
        self.starts.append(time.monotonic())

    def check(self) -> None:
        if bridge_status(timeout=1.0)["running"]:
            self.state = "running"
            return
        if self.child is not None and self.child.poll() is None:
            self.state = "starting"
            return
        recent = [t for t in self.starts if time.monotonic() - t < self.WINDOW_SECONDS]
        if len(recent) >= self.MAX_RESTARTS:
            self.state = "gave_up"
            return
        self._spawn()
        self.state = "starting"

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check()
            except Exception:
                self.state = "error"
            self._stop.wait(self.interval)

    def start(self) -> None:
        threading.Thread(target=self._loop, name="lounge-bridge-supervisor", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self.child is not None and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.child.kill()


class _BodyReader:
    """rfile wrapper that counts what a handler read of the request body, so the rest can be drained."""

    DRAIN_LIMIT = 1 << 20

    def __init__(self, raw: Any, content_length: str | None) -> None:
        self.raw = raw
        try:
            self.remaining = max(int(content_length or 0), 0)
        except ValueError:
            self.remaining = 0

    def read(self, size: int = -1) -> bytes:
        size = self.remaining if size is None or size < 0 else min(size, self.remaining)
        data = self.raw.read(size) if size else b""
        self.remaining -= len(data)
        return data

    def readline(self, size: int = -1) -> bytes:
        size = self.remaining if size is None or size < 0 else min(size, self.remaining)
        data = self.raw.readline(size) if size else b""
        self.remaining -= len(data)
        return data

    def drain(self) -> bool:
        """Read what is left of the body (bounded); False when the connection should be closed instead."""
        if self.remaining > self.DRAIN_LIMIT:
            return False
        try:
            while self.remaining > 0:
                if not self.read(min(self.remaining, 65536)):
                    return False
        except OSError:
            return False
        return True

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw, name)


class _ViewerHeaders:
    """Brings reused viewer responses up to the UI standard: no-store unless they chose a cache policy, nosniff."""

    def send_header(self, keyword: str, value: str) -> None:
        self.__dict__.setdefault("_sent_headers", set()).add(keyword.lower())
        super().send_header(keyword, value)

    def end_headers(self) -> None:
        seen = self.__dict__.get("_sent_headers", set())
        if "cache-control" not in seen:
            super().send_header("Cache-Control", "no-store")
        if "x-content-type-options" not in seen:
            super().send_header("X-Content-Type-Options", "nosniff")
        self.__dict__["_sent_headers"] = set()
        super().end_headers()


def _delegate(handler: Any, target_cls: type, path: str) -> Any:
    """Run a reused viewer handler on this connection with a rewritten path."""
    delegate = object.__new__(target_cls)
    for name in ("rfile", "wfile", "headers", "command", "request_version", "requestline", "raw_requestline",
                 "client_address", "server", "connection", "request", "close_connection"):
        if hasattr(handler, name):
            setattr(delegate, name, getattr(handler, name))
    delegate.path = path
    return delegate


def make_ui_handler(atrium: Any, api: ManagerApi, port: int, root: Path):
    games = _module("minigames_viewer")
    lounge = _module("lounge_viewer")
    games_cls = type("UnifiedGamesHandler", (_ViewerHeaders, games.Handler), {"root": root}) if games else None
    lounge_cls = type("UnifiedLoungeHandler", (_ViewerHeaders, lounge.Handler), {"root": root}) if lounge else None
    origin_ok = {f"http://{h}" for h in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}")}

    class Handler(memory_atrium.Handler):
        app = atrium
        server_version = "MemoryUI"

        def log_message(self, fmt: str, *args: Any) -> None:  # no paths or content in logs
            return

        def _guarded(self) -> bool:
            problem = request_problem(self.headers, port)
            if problem:
                send_json(self, problem[0], {"error": problem[1]})
                return False
            return True

        def _isolated(self, component: str, fn) -> None:
            try:
                fn()
            except Exception as exc:  # module failure must not take the UI down
                try:
                    send_json(self, 500, {"ok": False, "component": component, "error": type(exc).__name__})
                except Exception:
                    self.close_connection = True

        def _viewer(self, cls: type | None, component: str, method: str, path: str) -> None:
            if cls is None:
                send_json(self, 503, {"ok": False, "component": component, "error": "unavailable"})
                return
            delegate = _delegate(self, cls, path)
            self._isolated(component, getattr(delegate, method))
            self.close_connection = getattr(delegate, "close_connection", self.close_connection)

        def _health(self) -> None:
            # Short probe: a refused localhost connect can take over a second on Windows,
            # and health must stay fast enough for single-instance detection.
            bridge = bridge_status(timeout=0.4)
            send_json(self, 200, {
                "ok": True, "app": APP_NAME, "version": APP_VERSION, "pid": os.getpid(),
                "components": {"atrium": True, "manager": True, "duplicates": True,
                               "games": games_cls is not None, "lounge": lounge_cls is not None},
                "lounge_bridge": bridge,
            })

        def do_GET(self) -> None:
            if not self._guarded():
                return
            path = urlparse(self.path).path
            query = urlparse(self.path).query
            if path == "/health":
                self._isolated("health", self._health)
                return
            if path == "/":
                page, csp = render_shell()
                send(self, 200, page, "text/html; charset=utf-8", {"Content-Security-Policy": csp, "X-Frame-Options": "DENY"})
                return
            if path == PAGES["atrium"]:
                send(self, 200, atrium_page(), "text/html; charset=utf-8",
                     {"Content-Security-Policy": "frame-ancestors 'self'", "X-Frame-Options": "SAMEORIGIN"})
                return
            if path in (PAGES["manage"], PAGES["duplicates"]):
                page, csp = render_page(ADMIN_PREFIX, initial_tab="dups" if path == PAGES["duplicates"] else "list",
                                        embedded=True)
                send(self, 200, page, "text/html; charset=utf-8", {"Content-Security-Policy": csp, "X-Frame-Options": "SAMEORIGIN"})
                return
            if path in (PAGES["games"], PAGES["lounge"]):
                module = games if path == PAGES["games"] else lounge
                if module is None:
                    send_json(self, 503, {"ok": False, "error": "unavailable"})
                    return
                prefix = GAMES_PREFIX if path == PAGES["games"] else LOUNGE_PREFIX
                replacements = (('"/api/', f'"{prefix}/'), ('"/attachments/', f'"{LOUNGE_PREFIX}/attachments/'))
                page, csp = viewer_page(module.HTML, replacements)
                send(self, 200, page, "text/html; charset=utf-8", {"Content-Security-Policy": csp, "X-Frame-Options": "SAMEORIGIN"})
                return
            if path == ADMIN_PREFIX or path.startswith(ADMIN_PREFIX + "/"):
                self._isolated("manager", lambda: dispatch_api(self, api, "GET", ADMIN_PREFIX))
                return
            if path.startswith(GAMES_PREFIX + "/"):
                self._viewer(games_cls, "games", "do_GET", "/api/" + path[len(GAMES_PREFIX) + 1:] + (f"?{query}" if query else ""))
                return
            if path.startswith(LOUNGE_PREFIX + "/"):
                rest = path[len(LOUNGE_PREFIX) + 1:]
                inner = "/" + rest if rest.startswith("attachments/") else "/api/" + rest
                self._viewer(lounge_cls, "lounge", "do_GET", inner)
                return
            self._isolated("atrium", super().do_GET)  # Atrium data routes and assets, unchanged

        def do_POST(self) -> None:
            # Requests refused before their body is read (bad host or origin, no token, unknown path)
            # must still have it consumed: closing a socket with unread data resets the connection and
            # can discard the reply the client is about to read.
            body = _BodyReader(self.rfile, self.headers.get("Content-Length"))
            self.rfile = body
            try:
                self._route_post()
            finally:
                if not body.drain():
                    self.close_connection = True
                self.rfile = body.raw

        def _route_post(self) -> None:
            if not self._guarded():
                return
            path = urlparse(self.path).path
            if path.startswith(ADMIN_PREFIX + "/"):
                self._isolated("manager", lambda: dispatch_api(self, api, "POST", ADMIN_PREFIX))
                return
            if path.startswith(LOUNGE_PREFIX + "/"):
                # The lounge posts as the human and switches bridge modes: same-origin pages only.
                if self.headers.get("Origin") not in origin_ok:
                    send_json(self, 403, {"error": "same-origin request required"})
                    return
                self._viewer(lounge_cls, "lounge", "do_POST", "/api/" + path[len(LOUNGE_PREFIX) + 1:])
                return
            send_json(self, 404, {"error": "not found"})

    return Handler


class UIServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        # Never print tracebacks (they can carry message or memory text); class name only.
        exc = sys.exc_info()[1]
        print(f"memory-ui: request error {type(exc).__name__}", file=sys.stderr, flush=True)


def create_ui_server(root: str | Path, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                     admin_owners: tuple[str, ...] | None = None) -> tuple[ThreadingHTTPServer, ManagerApi]:
    root = Path(root).resolve()
    atrium = memory_atrium.MemoryAtrium(root)
    api = ManagerApi(MemoryAdmin(root, admin_owners))
    server = bind_server(host, port, lambda p: make_ui_handler(atrium, api, p, root))
    server.__class__ = UIServer
    return server, api


def running_instance(host: str, port: int, timeout: float = 2.0) -> dict[str, Any] | None:
    """The health of a Memory UI already serving on host:port, or None."""
    try:
        request = Request(f"http://{host}:{port}/health", headers={"Host": f"{host}:{port}"})
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (URLError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("app") == APP_NAME else None


def serve(root: str | Path, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          admin_owners: tuple[str, ...] | None = None, supervise_bridge: bool = False) -> int:
    existing = running_instance(host, port) if port else None
    if existing:
        print(f"Memory UI already running: http://{host}:{port}/ (pid {existing.get('pid')})", flush=True)
        return 0
    server, _ = create_ui_server(root, host=host, port=port, admin_owners=admin_owners)
    supervisor = BridgeSupervisor(Path(root).resolve()) if supervise_bridge else None
    if supervisor:
        supervisor.start()
    print("Memory UI:", flush=True)
    print(f"http://{host}:{server.server_address[1]}/", flush=True)
    print("(localhost only; Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if supervisor:
            supervisor.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Unified local memory UI (Atrium, manager, duplicates, games, lounge).")
    parser.add_argument("--root", default=str(env_path("DATA_DIR", "./runtime")))
    parser.add_argument("--host", default="127.0.0.1", help="localhost only (127.0.0.1, localhost or ::1)")
    parser.add_argument("--port", type=int, default=int(os.getenv("MEMORY_UI_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--owners", default="", help="owners the manager may change (default: owners found in memory)")
    parser.add_argument("--supervise-bridge", action="store_true",
                        help="keep the lounge bridge worker running (started in manual mode)")
    args = parser.parse_args(argv)
    owners = tuple(o.strip().lower() for o in args.owners.split(",") if o.strip()) or None
    return serve(args.root, host=args.host, port=args.port, admin_owners=owners, supervise_bridge=args.supervise_bridge)


if __name__ == "__main__":
    raise SystemExit(main())
