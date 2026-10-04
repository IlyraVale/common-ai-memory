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
/* ---------- shared visual language ---------- */
html:not([data-theme]),html[data-theme="mono"]{
  color-scheme:light;
  --cam-bg:#f2f2ee;
  --cam-surface:#fcfcfa;
  --cam-surface-2:#f7f7f3;
  --cam-text:#11110f;
  --cam-muted:#777770;
  --cam-line:#d6d6cf;
  --cam-line-strong:#b9b9b1;
  --cam-accent:#11110f;
  --cam-accent-text:#ffffff;
  --cam-danger:#8d3030;
  --cam-ok:#41624c;
  --cam-radius:0px;
  --cam-control-radius:4px;
  --cam-shadow:none;
  --cam-blur:0px;

  --bg:var(--cam-bg);--fg:var(--cam-text);--text:var(--cam-text);
  --chat-text:var(--cam-text);--muted:var(--cam-muted);--line:var(--cam-line);
  --line2:var(--cam-line-strong);--panel:var(--cam-surface);--panel2:var(--cam-surface-2);
  --card:var(--cam-surface);--page-bg:var(--cam-bg);--accent:var(--cam-accent);
  --accent2:#3f3f39;--warm:#696961;--soft:#5d5d56;--warn:#7f623d;
  --topbar-bg:#f2f2ee;--topbar-text:var(--cam-text);
  --input-bg:#ffffff;--input-text:var(--cam-text);--input-placeholder:#92928a;
  --input-border:var(--cam-line-strong);
  --gpt:#181816;--claude:#5d5d56;--ok:#41624c;--danger:#8d3030;
  --gpt-bubble:#ffffff;--claude-bubble:#f8f8f4;--alice-bubble:#eeeeea;
  --gpt-border-color:#bdbdb5;--claude-border-color:#c9c9c1;--alice-border-color:#a9a9a1;
  --bubble-border-color:#bdbdb5;--bubble-opacity:100%;--bubble-border-opacity:100%;
  --bubble-shadow-opacity:0%;--gpt-name:#11110f;--claude-name:#55554e;--alice-name:#74746c;
}
html[data-theme="glass"]{
  color-scheme:light;
  --cam-bg:#fceef4;
  --cam-pink:#f8dde8;
  --cam-active:#f1cada;
  --cam-surface:rgba(255,250,253,.58);
  --cam-surface-2:rgba(248,221,232,.34);
  --cam-text:#462132;
  --cam-muted:#806675;
  --cam-line:rgba(255,255,255,.86);
  --cam-line-strong:rgba(91,55,72,.14);
  --cam-accent:#462132;
  --cam-accent-text:#ffffff;
  --cam-danger:#462132;
  --cam-ok:#806675;
  --cam-radius:18px;
  --cam-control-radius:12px;
  --cam-shadow:0 12px 30px rgba(91,55,72,.08);
  --cam-highlight:inset 0 1px 0 rgba(255,255,255,.72),inset 1px 0 0 rgba(255,255,255,.34);
  --cam-blur:26px;

  --bg:var(--cam-bg);--fg:var(--cam-text);--text:var(--cam-text);
  --chat-text:var(--cam-text);--muted:var(--cam-muted);--line:var(--cam-line-strong);
  --line2:rgba(91,55,72,.20);--panel:var(--cam-surface);--panel2:var(--cam-surface-2);
  --card:var(--cam-surface);--page-bg:var(--cam-bg);--accent:var(--cam-accent);
  --accent2:#806675;--warm:#806675;--soft:#806675;--warn:#806675;
  --topbar-bg:var(--cam-surface);--topbar-text:var(--cam-text);
  --input-bg:rgba(255,255,255,.44);--input-text:var(--cam-text);--input-placeholder:#9a8290;
  --input-border:rgba(91,55,72,.15);
  --gpt:#462132;--claude:#806675;--ok:#806675;--danger:#462132;
  --gpt-bubble:#ffffff;--claude-bubble:#ffffff;--alice-bubble:#ffffff;
  --gpt-border-color:rgba(255,255,255,.82);--claude-border-color:rgba(255,255,255,.82);
  --alice-border-color:rgba(255,255,255,.90);--bubble-border-color:rgba(255,255,255,.82);
  --bubble-opacity:42%;--bubble-border-opacity:84%;--bubble-shadow-opacity:7%;
  --gpt-name:#462132;--claude-name:#806675;--alice-name:#806675;
}

html,body{transition:background-color .16s ease,color .16s ease}
html:not([data-theme]) body,html[data-theme="mono"] body{
  background:var(--cam-bg)!important;color:var(--cam-text)!important;
}
html[data-theme="glass"] body{
  background:var(--cam-bg)!important;color:var(--cam-text)!important;
  position:relative;isolation:isolate;overflow-x:hidden;
}
html:not([data-theme]) body:before,html[data-theme="mono"] body:before{
  opacity:0!important;background:none!important;
}
html[data-theme="glass"] body:before{
  content:""!important;position:fixed!important;z-index:-1!important;pointer-events:none!important;
  width:34rem!important;height:34rem!important;left:-11rem!important;top:9vh!important;
  border-radius:46%!important;background:#f8dde8!important;opacity:.38!important;
  box-shadow:70vw -12vh 0 3rem #f1cada,36vw 68vh 0 -2rem #f8dde8!important;
  filter:blur(88px)!important;transform:translateZ(0);will-change:filter;
}

/* ---------- typography / spacing ---------- */
html:not([data-theme]) .shell,html[data-theme="mono"] .shell,
html[data-theme="glass"] .shell{max-width:1320px}
html[data-theme="mono"] .brand .eyebrow,
html[data-theme="mono"] .card-title,
html[data-theme="mono"] .game-kicker,
html[data-theme="mono"] .section-title{
  letter-spacing:.14em!important;text-transform:uppercase!important;color:#6c6c65!important
}
html[data-theme="mono"] .brand h1,
html[data-theme="mono"] .hero h2,
html[data-theme="mono"] .feed-head h3{
  color:#11110f!important;font-weight:500!important
}
html[data-theme="glass"] .brand h1,
html[data-theme="glass"] .hero h2,
html[data-theme="glass"] .feed-head h3{
  color:#462132!important
}
html[data-theme="glass"] .hero p,
html[data-theme="glass"] .content,
html[data-theme="glass"] .manual,
html[data-theme="glass"] .event-body,
html[data-theme="glass"] .quote,
html[data-theme="glass"] .game-link span{
  color:#806675!important
}
html[data-theme="glass"] .card-title,
html[data-theme="glass"] .quote small,
html[data-theme="glass"] .event-time,
html[data-theme="glass"] .feed-head span{
  color:#806675!important
}
html[data-theme="glass"] .dot,
html[data-theme="glass"] .live-pill i{
  background:#806675!important;box-shadow:none!important
}
html[data-theme="glass"] .live-pill{color:#806675!important}

/* ---------- common surfaces ---------- */
html:not([data-theme]) .card,html:not([data-theme]) .panel,
html[data-theme="mono"] .card,html[data-theme="mono"] .panel{
  background:var(--cam-surface)!important;border:1px solid var(--cam-line)!important;
  border-radius:0!important;box-shadow:none!important;backdrop-filter:none!important;
}
html[data-theme="glass"] .card,html[data-theme="glass"] .panel{
  background:var(--cam-surface)!important;border:1px solid var(--cam-line)!important;
  border-radius:var(--cam-radius)!important;box-shadow:var(--cam-shadow),var(--cam-highlight)!important;
  backdrop-filter:blur(var(--cam-blur)) saturate(112%) brightness(1.02)!important;
}
html[data-theme="mono"] .hero{
  background:transparent!important;border:0!important;border-top:1px solid #bfbfb7!important;
  border-bottom:1px solid #bfbfb7!important;border-radius:0!important;box-shadow:none!important;
  padding-left:0!important;padding-right:0!important;
}
html[data-theme="glass"] .hero{
  background:var(--cam-surface)!important;border:1px solid var(--cam-line)!important;
  border-radius:26px!important;box-shadow:var(--cam-shadow),var(--cam-highlight)!important;
  backdrop-filter:blur(var(--cam-blur)) saturate(112%) brightness(1.02)!important;
}
html[data-theme="mono"] .hero:after{display:none!important}
html[data-theme="glass"] .hero:after{color:rgba(255,255,255,.28)!important}

/* ---------- forms / controls ---------- */
html:not([data-theme]) input,html:not([data-theme]) textarea,html:not([data-theme]) select,
html[data-theme="mono"] input,html[data-theme="mono"] textarea,html[data-theme="mono"] select{
  background:#fff!important;color:var(--cam-text)!important;border:1px solid var(--cam-line-strong)!important;
  border-radius:var(--cam-control-radius)!important;box-shadow:none!important;
}
html[data-theme="glass"] input,html[data-theme="glass"] textarea,html[data-theme="glass"] select{
  background:var(--input-bg)!important;color:var(--cam-text)!important;border:1px solid var(--cam-line)!important;
  border-radius:var(--cam-control-radius)!important;box-shadow:none!important;
  backdrop-filter:blur(18px)!important;
}
html:not([data-theme]) button,html[data-theme="mono"] button,html[data-theme="glass"] button{
  box-shadow:none;transition:background .14s ease,border-color .14s ease,color .14s ease,transform .14s ease;
}
html[data-theme="mono"] button{border-radius:2px!important}
html[data-theme="glass"] button{border-radius:11px!important}
html:not([data-theme]) button.primary,html[data-theme="mono"] button.primary,html[data-theme="glass"] button.primary,
html:not([data-theme]) .search button,html[data-theme="mono"] .search button,html[data-theme="glass"] .search button{
  background:var(--cam-accent)!important;color:var(--cam-accent-text)!important;border-color:var(--cam-accent)!important;
}
html[data-theme="mono"] button:hover{background:#ecece7}
html[data-theme="mono"] button.primary:hover,html[data-theme="mono"] .search button:hover{background:#2b2b27!important}
html[data-theme="glass"] button:hover{background:rgba(255,255,255,.46)}
html[data-theme="glass"] button.primary:hover,html[data-theme="glass"] .search button:hover{background:#462132!important}

/* ---------- Atrium / Manager ---------- */
html[data-theme="mono"] .grid{gap:0!important}
html[data-theme="mono"] .grid>.card,
html[data-theme="mono"] .side-stack>.card{border-left-width:0!important}
html[data-theme="mono"] .grid>.card:first-child{border-left-width:1px!important}
html[data-theme="mono"] .side-stack{gap:0!important}
html[data-theme="mono"] .memory{
  background:transparent!important;border:0!important;border-top:1px solid #deded7!important;
  border-radius:0!important;padding:18px 0!important;
}
html[data-theme="mono"] .memory:first-child{border-top:0!important}
html[data-theme="glass"] .memory{
  background:rgba(255,255,255,.30)!important;border:1px solid rgba(255,255,255,.62)!important;
  border-radius:14px!important;backdrop-filter:blur(16px) brightness(1.02)!important;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.58)!important;
}
html[data-theme="mono"] .section{border-radius:0!important;border:0!important;border-left:2px solid transparent!important}
html[data-theme="mono"] .section:hover{background:#ededE8!important}
html[data-theme="mono"] .section.active{background:#e5e5df!important;border-left-color:#11110f!important}
html[data-theme="glass"] .section:hover,html[data-theme="glass"] .section.active{
  background:var(--cam-active)!important;border-color:rgba(255,255,255,.86)!important;
}
html[data-theme="mono"] .owner{
  background:transparent!important;border:1px solid #c8c8c0!important;color:#51514b!important;
  border-radius:999px!important
}
html[data-theme="mono"] .owner.active{background:#11110f!important;color:#fff!important;border-color:#11110f!important}
html[data-theme="glass"] .owner{
  background:rgba(255,255,255,.30)!important;border-color:rgba(255,255,255,.80)!important;color:#806675!important
}
html[data-theme="glass"] .owner.active{background:var(--cam-active)!important;color:#462132!important;border-color:rgba(255,255,255,.86)!important}
html[data-theme="mono"] .game-link{
  background:transparent!important;border:1px solid #d0d0c8!important;border-radius:0!important;
}
html[data-theme="glass"] .game-link{
  background:rgba(255,255,255,.32)!important;border:1px solid rgba(255,255,255,.62)!important;border-radius:14px!important;
  backdrop-filter:blur(16px)!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.54)!important;
}
html[data-theme="mono"] table{background:transparent!important}
html[data-theme="mono"] td,html[data-theme="mono"] th{border-color:#d8d8d1!important}
html[data-theme="mono"] tr.row:hover,html[data-theme="mono"] tr.row.sel{background:#ecece7!important}
html[data-theme="glass"] table{background:rgba(255,255,255,.28)!important;border-radius:14px!important;overflow:hidden;backdrop-filter:blur(18px)!important;box-shadow:var(--cam-highlight)!important}
html[data-theme="glass"] tr.row:hover,html[data-theme="glass"] tr.row.sel{background:var(--cam-active)!important}
html[data-theme="mono"] .group{background:transparent!important;border-radius:0!important;border-color:#d6d6cf!important}
html[data-theme="glass"] .group{background:rgba(255,255,255,.30)!important;border-color:rgba(255,255,255,.62)!important;border-radius:14px!important;backdrop-filter:blur(18px)!important;box-shadow:var(--cam-highlight)!important}

/* ---------- Lounge ---------- */
html[data-theme="mono"] .topbar,
html[data-theme="mono"] .composer-wrap{
  background:#f2f2ee!important;border-color:#cfcfc7!important;backdrop-filter:none!important;
}
html[data-theme="glass"] .topbar,
html[data-theme="glass"] .composer-wrap{
  background:var(--cam-surface)!important;border-color:rgba(255,255,255,.86)!important;
  backdrop-filter:blur(24px) saturate(112%) brightness(1.02)!important;
  box-shadow:var(--cam-shadow),var(--cam-highlight)!important;
}
html[data-theme="mono"] .topbar{height:64px!important}
html[data-theme="mono"] .topbar{color:#11110f!important}
html[data-theme="glass"] .topbar{color:#462132!important}
html[data-theme="mono"] .tagline{letter-spacing:.16em!important;color:#777770!important}
html[data-theme="glass"] .tagline{color:#806675!important}
html[data-theme="mono"] .person,html[data-theme="mono"] .mode-btn,html[data-theme="mono"] .bridge-state,
html[data-theme="mono"] .updated{color:#5d5d56!important}
html[data-theme="glass"] .person,html[data-theme="glass"] .mode-btn,html[data-theme="glass"] .bridge-state,
html[data-theme="glass"] .updated{color:#806675!important}
html[data-theme="mono"] .bridge-mode{background:transparent!important;border-color:#c8c8c0!important;border-radius:999px!important}
html[data-theme="glass"] .bridge-mode{background:rgba(255,255,255,.28)!important;border-color:rgba(255,255,255,.62)!important;border-radius:999px!important}
html[data-theme="mono"] .mode-btn.active{background:#11110f!important;color:#fff!important}
html[data-theme="glass"] .mode-btn.active{background:var(--cam-active)!important;color:#462132!important}
html[data-theme="mono"] .welcome{color:#777770!important}
html[data-theme="mono"] .welcome:before{content:"—"!important;color:#11110f!important;font-size:14px!important}
html[data-theme="glass"] .welcome{color:#806675!important}
html[data-theme="glass"] .welcome:before{color:#806675!important}
html[data-theme="mono"] .bubble{
  color:#11110f!important;border-radius:2px!important;box-shadow:none!important;
}
html[data-theme="mono"] .gpt .bubble{background:#fff!important;border-color:#bdbdb5!important}
html[data-theme="mono"] .claude .bubble{background:#f7f7f3!important;border-color:#c9c9c1!important}
html[data-theme="mono"] .alice .bubble{background:#ecece7!important;border-color:#b8b8b0!important}
html[data-theme="glass"] .bubble{
  color:#462132!important;background:rgba(255,250,253,.58)!important;
  border-color:rgba(255,255,255,.86)!important;box-shadow:var(--cam-shadow),var(--cam-highlight)!important;
  backdrop-filter:blur(20px) saturate(110%) brightness(1.02)!important;
}
html[data-theme="mono"] .gpt .who{color:#11110f!important}
html[data-theme="mono"] .claude .who{color:#55554e!important}
html[data-theme="mono"] .alice .who{color:#65655e!important}
html[data-theme="glass"] .gpt .who{color:#462132!important}
html[data-theme="glass"] .claude .who{color:#806675!important}
html[data-theme="glass"] .alice .who{color:#806675!important}
html[data-theme="mono"] .composer{
  background:#fff!important;border-color:#bdbdb5!important;border-radius:2px!important;box-shadow:none!important;
}
html[data-theme="glass"] .composer{
  background:rgba(255,250,253,.58)!important;border-color:rgba(255,255,255,.86)!important;
  border-radius:18px!important;box-shadow:var(--cam-shadow),var(--cam-highlight)!important;
  backdrop-filter:blur(24px) saturate(112%) brightness(1.02)!important;
}
html[data-theme="glass"] .composer textarea{
  background:transparent!important;color:#462132!important
}
html[data-theme="glass"] .drawer input,html[data-theme="glass"] .drawer textarea,
html[data-theme="glass"] .drawer select{
  background:rgba(255,255,255,.48)!important;color:#462132!important
}
html[data-theme="mono"] .gear,html[data-theme="mono"] .attach-btn,html[data-theme="mono"] .send{
  background:transparent!important;border-color:#c8c8c0!important;border-radius:2px!important;color:#22221f!important
}
html[data-theme="glass"] .gear,html[data-theme="glass"] .attach-btn,html[data-theme="glass"] .send{
  background:rgba(255,255,255,.30)!important;border-color:rgba(255,255,255,.76)!important;color:#806675!important
}
html[data-theme="mono"] .drawer{
  background:#f6f6f2!important;border-color:#d2d2ca!important;box-shadow:-18px 0 42px rgba(0,0,0,.06)!important;
}
html[data-theme="glass"] .drawer{
  background:rgba(255,250,253,.58)!important;border-color:rgba(255,255,255,.86)!important;
  box-shadow:-18px 0 50px rgba(91,55,72,.10),var(--cam-highlight)!important;
  backdrop-filter:blur(28px) saturate(112%) brightness(1.02)!important;
}

/* ---------- Game Hall ---------- */
html[data-theme="mono"] .toolbar{
  background:transparent!important;border:0!important;border-top:1px solid #cfcfc7!important;
  border-bottom:1px solid #cfcfc7!important;border-radius:0!important;padding-left:0!important;padding-right:0!important;
  backdrop-filter:none!important;
}
html[data-theme="glass"] .toolbar{
  background:rgba(255,255,255,.34)!important;border:1px solid rgba(255,255,255,.66)!important;
  border-radius:16px!important;box-shadow:var(--cam-shadow),var(--cam-highlight)!important;
  backdrop-filter:blur(22px) saturate(112%) brightness(1.02)!important;
}
html[data-theme="mono"] .arena,html[data-theme="mono"] .side .panel{background:#fcfcfa!important}
html[data-theme="mono"] .stat,html[data-theme="mono"] .bj-player{
  background:transparent!important;border-color:#d5d5ce!important;border-radius:0!important;
}
html[data-theme="glass"] .stat,html[data-theme="glass"] .bj-player{
  background:rgba(255,255,255,.28)!important;border-color:rgba(255,255,255,.58)!important;border-radius:13px!important;
}
html[data-theme="mono"] .tag{
  background:transparent!important;border-color:#c8c8c0!important;color:#55554f!important;border-radius:999px!important;
}
html[data-theme="glass"] .tag{
  background:rgba(255,255,255,.28)!important;border-color:rgba(255,255,255,.62)!important;border-radius:999px!important;
}
/* Game pieces remain materially recognizable; theme only owns the surrounding product chrome. */
html[data-theme] .sea-card .sea-name,
html[data-theme] .sea-card .gpt,
html[data-theme] .sea-card .claude{color:#dce8e1!important}
html[data-theme] .sea-card .ready{color:#9fb5aa!important}
html[data-theme] .sea-card .ready.yes{color:#8fc7a5!important}
html[data-theme] .dealer-zone .section-title,
html[data-theme] .dealer-zone .score,
html[data-theme] .dealer-zone .score b{color:#dce8e1!important}
html[data-theme] .poker-table .gpt,
html[data-theme] .poker-table .claude,
html[data-theme] .poker-table .muted,
html[data-theme] .poker-table .stack{color:#c5d5cc!important}

/* ---------- responsive ---------- */
@media(max-width:720px){
  html[data-theme="mono"] .grid{gap:10px!important}
  html[data-theme="mono"] .grid>.card,
  html[data-theme="mono"] .side-stack>.card{border-left-width:1px!important}
  html[data-theme="mono"] .side-stack{gap:10px!important}
}
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
html[data-theme="glass"]{--bg:#fceef4;--fg:#462132;--muted:#806675;--line:rgba(91,55,72,.14);--panel:rgba(255,250,253,.58);--accent:#462132}
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
