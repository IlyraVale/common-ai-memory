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

THEME_STORAGE_KEY = "common-ai-memory-theme"

# Unified visual tokens are injected after each reused page's own CSS.  This keeps
# the legacy viewers independently runnable while letting the integrated shell
# present one coherent theme across Atrium, Manager, Games and Lounge.
THEME_CORE_CSS = r"""
<style id="cam-theme-core">
html:not([data-theme]),html[data-theme="mono"]{
  color-scheme:light;
  --cam-bg:#f3f3ef;--cam-surface:#ffffff;--cam-surface-2:#f8f8f5;
  --cam-text:#11110f;--cam-muted:#74746e;--cam-line:#d7d7d1;
  --cam-line-strong:#b9b9b2;--cam-accent:#11110f;--cam-accent-text:#ffffff;
  --cam-danger:#9b2f2f;--cam-ok:#356b48;--cam-radius:10px;
  --cam-shadow:none;--cam-blur:0px;
  --bg:var(--cam-bg);--fg:var(--cam-text);--text:var(--cam-text);
  --chat-text:var(--cam-text);--muted:var(--cam-muted);--line:var(--cam-line);
  --line2:var(--cam-line-strong);--panel:var(--cam-surface);--panel2:var(--cam-surface-2);
  --card:var(--cam-surface);--page-bg:var(--cam-bg);--accent:var(--cam-accent);
  --accent2:#40403b;--warm:#60605a;--soft:#5c5c56;--warn:#8b5a22;
  --topbar-bg:#f3f3ef;--topbar-text:var(--cam-text);--input-bg:#ffffff;
  --input-text:var(--cam-text);--input-placeholder:#92928b;--input-border:var(--cam-line-strong);
  --gpt:#20201d;--claude:#55554f;--ok:#356b48;--danger:#9b2f2f;
  --gpt-bubble:#ffffff;--claude-bubble:#ffffff;--alice-bubble:#f0f0ec;
  --gpt-border-color:#bdbdb6;--claude-border-color:#bdbdb6;--alice-border-color:#a9a9a2;
  --bubble-border-color:#bdbdb6;--bubble-opacity:100%;--bubble-border-opacity:100%;
  --bubble-shadow-opacity:0%;--gpt-name:#11110f;--claude-name:#44443f;--alice-name:#686861;
}
html[data-theme="glass"]{
  color-scheme:light;
  --cam-bg:#dfe5df;--cam-surface:rgba(255,255,255,.56);--cam-surface-2:rgba(255,255,255,.36);
  --cam-text:#1c211e;--cam-muted:#69716c;--cam-line:rgba(255,255,255,.62);
  --cam-line-strong:rgba(67,78,71,.18);--cam-accent:#46564d;--cam-accent-text:#ffffff;
  --cam-danger:#985858;--cam-ok:#50725d;--cam-radius:18px;
  --cam-shadow:0 18px 50px rgba(55,69,60,.10);--cam-blur:22px;
  --bg:var(--cam-bg);--fg:var(--cam-text);--text:var(--cam-text);
  --chat-text:var(--cam-text);--muted:var(--cam-muted);--line:var(--cam-line-strong);
  --line2:rgba(67,78,71,.25);--panel:var(--cam-surface);--panel2:var(--cam-surface-2);
  --card:var(--cam-surface);--page-bg:var(--cam-bg);--accent:var(--cam-accent);
  --accent2:#718078;--warm:#78837d;--soft:#5d6962;--warn:#8b6b47;
  --topbar-bg:#e8ede9;--topbar-text:var(--cam-text);--input-bg:rgba(255,255,255,.58);
  --input-text:var(--cam-text);--input-placeholder:#7f8983;--input-border:rgba(67,78,71,.20);
  --gpt:#435149;--claude:#68746e;--ok:#50725d;--danger:#985858;
  --gpt-bubble:#ffffff;--claude-bubble:#ffffff;--alice-bubble:#ffffff;
  --gpt-border-color:rgba(255,255,255,.72);--claude-border-color:rgba(255,255,255,.72);
  --alice-border-color:rgba(255,255,255,.82);--bubble-border-color:rgba(255,255,255,.72);
  --bubble-opacity:48%;--bubble-border-opacity:75%;--bubble-shadow-opacity:8%;
  --gpt-name:#445249;--claude-name:#657169;--alice-name:#55635b;
}
html:not([data-theme]) body,html[data-theme="mono"] body,html[data-theme="glass"] body{
  background:var(--cam-bg)!important;color:var(--cam-text)!important;
}
html[data-theme="glass"] body{
  background:
    radial-gradient(circle at 14% 8%,rgba(255,255,255,.34),transparent 27%),
    radial-gradient(circle at 86% 20%,rgba(255,255,255,.24),transparent 24%),
    var(--cam-bg)!important;
}
html:not([data-theme]) body:before,html[data-theme="mono"] body:before,html[data-theme="glass"] body:before{
  opacity:0!important;background:none!important;
}
html:not([data-theme]) .card,html:not([data-theme]) .panel,
html[data-theme="mono"] .card,html[data-theme="mono"] .panel,
html[data-theme="glass"] .card,html[data-theme="glass"] .panel{
  background:var(--cam-surface)!important;border-color:var(--cam-line)!important;
  border-radius:var(--cam-radius)!important;box-shadow:var(--cam-shadow)!important;
  backdrop-filter:blur(var(--cam-blur));
}
html:not([data-theme]) .hero,html[data-theme="mono"] .hero{
  background:var(--cam-surface)!important;border-color:var(--cam-line)!important;border-radius:0!important;
  box-shadow:none!important;
}
html[data-theme="glass"] .hero{
  background:var(--cam-surface)!important;border-color:var(--cam-line)!important;
  border-radius:24px!important;box-shadow:var(--cam-shadow)!important;backdrop-filter:blur(var(--cam-blur));
}
html:not([data-theme]) .memory,html[data-theme="mono"] .memory,
html[data-theme="glass"] .memory,
html:not([data-theme]) .group,html[data-theme="mono"] .group,html[data-theme="glass"] .group,
html:not([data-theme]) table,html[data-theme="mono"] table,html[data-theme="glass"] table{
  background:var(--cam-surface-2)!important;border-color:var(--cam-line)!important;color:var(--cam-text)!important;
}
html[data-theme="mono"] .memory,html[data-theme="mono"] .group{border-radius:0!important}
html[data-theme="glass"] .memory,html[data-theme="glass"] .group{border-radius:14px!important;backdrop-filter:blur(var(--cam-blur))}
html:not([data-theme]) input,html:not([data-theme]) textarea,html:not([data-theme]) select,
html[data-theme="mono"] input,html[data-theme="mono"] textarea,html[data-theme="mono"] select,
html[data-theme="glass"] input,html[data-theme="glass"] textarea,html[data-theme="glass"] select{
  background:var(--input-bg)!important;color:var(--cam-text)!important;border-color:var(--cam-line-strong)!important;
  box-shadow:none!important;
}
html[data-theme="glass"] input,html[data-theme="glass"] textarea,html[data-theme="glass"] select{
  backdrop-filter:blur(var(--cam-blur));
}
html:not([data-theme]) button,html[data-theme="mono"] button,html[data-theme="glass"] button{
  border-color:var(--cam-line-strong);box-shadow:none;
}
html:not([data-theme]) button.primary,html[data-theme="mono"] button.primary,html[data-theme="glass"] button.primary,
html:not([data-theme]) .search button,html[data-theme="mono"] .search button,html[data-theme="glass"] .search button{
  background:var(--cam-accent)!important;color:var(--cam-accent-text)!important;border-color:var(--cam-accent)!important;
}
html[data-theme="mono"] .section:hover,html[data-theme="mono"] .section.active,
html[data-theme="mono"] tr.row:hover,html[data-theme="mono"] tr.row.sel{
  background:#ecece7!important;border-color:#cfcfc8!important;
}
html[data-theme="glass"] .section:hover,html[data-theme="glass"] .section.active,
html[data-theme="glass"] tr.row:hover,html[data-theme="glass"] tr.row.sel{
  background:rgba(255,255,255,.34)!important;border-color:rgba(255,255,255,.58)!important;
}
html:not([data-theme]) .owner,html[data-theme="mono"] .owner{
  background:#fff!important;border-color:#cfcfc8!important;color:#4f4f49!important;border-radius:999px!important;
}
html[data-theme="mono"] .owner.active{background:#11110f!important;color:#fff!important;border-color:#11110f!important}
html[data-theme="glass"] .owner{background:rgba(255,255,255,.34)!important;border-color:rgba(255,255,255,.66)!important;color:#59655e!important}
html[data-theme="glass"] .owner.active{background:rgba(70,86,77,.86)!important;color:#fff!important;border-color:transparent!important}
html:not([data-theme]) .game-link,html[data-theme="mono"] .game-link{
  background:#fff!important;border-color:#cfcfc8!important;border-radius:0!important;
}
html[data-theme="glass"] .game-link{
  background:rgba(255,255,255,.34)!important;border-color:rgba(255,255,255,.62)!important;border-radius:14px!important;
}
html:not([data-theme]) .topbar,html:not([data-theme]) .composer-wrap,
html[data-theme="mono"] .topbar,html[data-theme="mono"] .composer-wrap,
html[data-theme="glass"] .topbar,html[data-theme="glass"] .composer-wrap{
  background:color-mix(in srgb,var(--topbar-bg) 78%,transparent)!important;border-color:var(--cam-line)!important;
}
html[data-theme="mono"] .topbar,html[data-theme="mono"] .composer-wrap{backdrop-filter:none!important}
html[data-theme="glass"] .topbar,html[data-theme="glass"] .composer-wrap{backdrop-filter:blur(24px)!important}
html:not([data-theme]) .composer,html[data-theme="mono"] .composer,
html[data-theme="glass"] .composer{
  background:var(--cam-surface)!important;border-color:var(--cam-line-strong)!important;box-shadow:var(--cam-shadow)!important;
}
html[data-theme="mono"] .composer{border-radius:4px!important}
html[data-theme="glass"] .composer{border-radius:20px!important;backdrop-filter:blur(var(--cam-blur))}
html:not([data-theme]) .bubble,html[data-theme="mono"] .bubble,
html[data-theme="glass"] .bubble{color:var(--cam-text)!important}
html[data-theme="mono"] .bubble{border-radius:3px!important;box-shadow:none!important}
html[data-theme="glass"] .bubble{box-shadow:0 12px 32px rgba(55,69,60,.08)!important;backdrop-filter:blur(16px)}
html:not([data-theme]) .toolbar,html[data-theme="mono"] .toolbar{
  background:#fff!important;border-color:#d7d7d1!important;border-radius:0!important;backdrop-filter:none!important;
}
html[data-theme="glass"] .toolbar{
  background:rgba(255,255,255,.42)!important;border-color:rgba(255,255,255,.64)!important;
  border-radius:16px!important;box-shadow:var(--cam-shadow)!important;backdrop-filter:blur(var(--cam-blur))!important;
}
html:not([data-theme]) .stat,html[data-theme="mono"] .stat,
html:not([data-theme]) .bj-player,html[data-theme="mono"] .bj-player{
  background:#fff!important;border-color:#d7d7d1!important;border-radius:0!important;
}
html[data-theme="glass"] .stat,html[data-theme="glass"] .bj-player{
  background:rgba(255,255,255,.32)!important;border-color:rgba(255,255,255,.58)!important;border-radius:14px!important;
}
html:not([data-theme]) .drawer,html[data-theme="mono"] .drawer{
  background:#f6f6f2!important;border-color:#d7d7d1!important;box-shadow:-18px 0 42px rgba(0,0,0,.08)!important;
}
html[data-theme="glass"] .drawer{
  background:rgba(242,246,243,.78)!important;border-color:rgba(255,255,255,.62)!important;
  box-shadow:-18px 0 50px rgba(55,69,60,.14)!important;backdrop-filter:blur(26px);
}
html[data-theme="mono"] .welcome:before{color:#11110f!important}
html[data-theme="glass"] .welcome:before{color:#66736b!important}
html[data-theme="mono"] .hero:after{color:#11110f08!important}
html[data-theme="glass"] .hero:after{color:#ffffff24!important}
html[data-theme="mono"] .footer{color:#7b7b74!important}
html[data-theme="glass"] .footer{color:#6e7872!important}
</style>
"""


def inject_theme_css(html: str) -> str:
    """Append the shared theme layer after a reused page's legacy styles."""
    if 'id="cam-theme-core"' in html:
        return html
    if "</head>" not in html:
        return html
    return html.replace("</head>", THEME_CORE_CSS + "</head>", 1)

SHELL = r"""<!doctype html>
<html lang="zh-CN" data-theme="mono"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Common AI Memory</title>
<style>
:root{--bg:#f3f3ef;--fg:#11110f;--muted:#74746e;--line:#d7d7d1;--panel:#fff;--accent:#11110f}
html[data-theme="glass"]{--bg:#dfe5df;--fg:#1c211e;--muted:#69716c;--line:rgba(67,78,71,.18);--panel:rgba(255,255,255,.54);--accent:#46564d}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:13px/1.4 Inter,system-ui,"Microsoft YaHei",sans-serif}
body{transition:background .16s ease,color .16s ease}
nav{display:flex;align-items:center;gap:2px;height:52px;padding:0 14px;border-bottom:1px solid var(--line);overflow-x:auto;white-space:nowrap;background:var(--panel)}
html[data-theme="glass"] nav{background:rgba(255,255,255,.42);backdrop-filter:blur(22px)}
nav b{margin-right:16px;font-size:12px;font-weight:700;letter-spacing:.08em}
nav a{color:var(--muted);text-decoration:none;padding:17px 11px 15px;border-bottom:1px solid transparent}
nav a[aria-current="page"]{color:var(--fg);border-bottom-color:var(--fg)}
nav a:focus-visible,.theme-switch button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.spacer{flex:1}
.theme-switch{display:flex;gap:2px;padding:3px;border:1px solid var(--line);border-radius:999px;background:color-mix(in srgb,var(--panel) 72%,transparent)}
.theme-switch button{border:0;background:transparent;color:var(--muted);font:600 10px/1 system-ui,sans-serif;letter-spacing:.06em;padding:7px 9px;border-radius:999px;cursor:pointer}
.theme-switch button[aria-pressed="true"]{background:var(--fg);color:var(--bg)}
iframe{display:block;width:100%;height:calc(100% - 53px);border:0;background:var(--bg)}
@media (max-width:600px){nav{padding:0 8px}nav b{display:none}nav a{padding:17px 7px 15px}.theme-switch button{padding:7px}}
</style></head><body>
<nav aria-label="Memory UI"><b>COMMON AI MEMORY</b>
<a href="#atrium" data-page="atrium">中庭</a><a href="#manage" data-page="manage">管理</a><a href="#duplicates" data-page="duplicates">重复检查</a><a href="#games" data-page="games">游戏厅</a><a href="#lounge" data-page="lounge">聊天室</a>
<span class="spacer"></span>
<div class="theme-switch" role="group" aria-label="界面主题">
<button type="button" data-theme-choice="mono" aria-pressed="true">MONO</button>
<button type="button" data-theme-choice="glass" aria-pressed="false">GLASS</button>
</div></nav>
<iframe id="view" title="Memory UI"></iframe>
<script nonce="__NONCE__">
const PAGES = {atrium: "/atrium", manage: "/manage", duplicates: "/duplicates", games: "/games", lounge: "/lounge"};
const THEME_KEY = "common-ai-memory-theme", THEMES = new Set(["mono","glass"]);
const frame = document.getElementById("view");
function savedTheme(){
  try { const value = localStorage.getItem(THEME_KEY); return THEMES.has(value) ? value : "mono"; }
  catch (_) { return "mono"; }
}
function syncFrameTheme(){
  const theme = document.documentElement.dataset.theme || "mono";
  try { if (frame.contentDocument) frame.contentDocument.documentElement.dataset.theme = theme; } catch (_) {}
}
function setTheme(theme, persist=true){
  theme = THEMES.has(theme) ? theme : "mono";
  document.documentElement.dataset.theme = theme;
  document.querySelectorAll("[data-theme-choice]").forEach(button =>
    button.setAttribute("aria-pressed", String(button.dataset.themeChoice === theme)));
  if (persist) { try { localStorage.setItem(THEME_KEY, theme); } catch (_) {} }
  syncFrameTheme();
}
document.querySelectorAll("[data-theme-choice]").forEach(button =>
  button.addEventListener("click", () => setTheme(button.dataset.themeChoice)));
frame.addEventListener("load", syncFrameTheme);
function show() {
  const page = PAGES[location.hash.slice(1)] ? location.hash.slice(1) : "atrium";
  document.querySelectorAll("nav a").forEach(a => a.setAttribute("aria-current", a.dataset.page === page ? "page" : "false"));
  if (frame.getAttribute("src") !== PAGES[page]) frame.setAttribute("src", PAGES[page]);
  else syncFrameTheme();
}
setTheme(savedTheme(), false);
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
    return inject_theme_css(html).encode("utf-8")


def viewer_page(html: str, replacements: tuple[tuple[str, str], ...]) -> tuple[bytes, str]:
    """A reused viewer page with its API paths prefixed and its one script given a CSP nonce."""
    nonce = secrets.token_urlsafe(16)
    for old, new in replacements:
        html = html.replace(old, new)
    html = inject_theme_css(html)
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
                themed = inject_theme_css(page.decode("utf-8")).encode("utf-8")
                send(self, 200, themed, "text/html; charset=utf-8", {"Content-Security-Policy": csp, "X-Frame-Options": "SAMEORIGIN"})
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
