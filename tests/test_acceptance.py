from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

import pytest

from config import load_dotenv
from game_hall import GameHall
from lounge_bridge import DEFAULT_CONFIG, LoungeBridge, load_config
from lounge_room import LoungeRoom
from lounge_viewer import Handler as LoungeHandler
from memory_atrium import Handler as AtriumHandler, MemoryAtrium
from memory_store import MemoryStore


def browser_config(mode="active"):
    value = dict(DEFAULT_CONFIG)
    value.update({"mode": mode, "cooldown_seconds": 0.01, "retry_base_seconds": 0.01,
                  "retry_max_seconds": 1.0, "ack_timeout_seconds": 10.0,
                  "adapters": {"gpt": {"type": "browser"}, "claude": {"type": "browser"}}})
    return value


def test_concurrent_memory_writes_are_not_lost(tmp_path, monkeypatch):
    monkeypatch.setattr(MemoryStore, "_git_commit", lambda *_: "disabled-in-test")
    stores = [MemoryStore(tmp_path, "gpt"), MemoryStore(tmp_path, "claude")]
    errors = []
    def write(store, prefix):
        try:
            for index in range(100): store.remember(f"{prefix}-{index}", "project/general", "shared")
        except Exception as exc: errors.append(exc)
    threads = [threading.Thread(target=write, args=(stores[0], "g")), threading.Thread(target=write, args=(stores[1], "c"))]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert not errors
    assert len(stores[0].recent(1000)) == 200


def test_owner_isolation_and_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(MemoryStore, "_git_commit", lambda *_: "disabled-in-test")
    gpt, claude = MemoryStore(tmp_path, "gpt"), MemoryStore(tmp_path, "claude")
    own = gpt.remember("private-to-author writes", "project/general", "agent")
    shared = claude.remember("shared read", "project/general", "shared")
    assert {x["id"] for x in claude.recall("private-to-author")} == {own["id"]}
    assert {x["id"] for x in gpt.recent(owner="shared")} == {shared["id"]}
    with pytest.raises(PermissionError): claude.update(own["id"], "forbidden")
    with pytest.raises(PermissionError): claude.forget(own["id"])
    assert gpt.update(own["id"], "updated")["content"] == "updated"
    assert gpt.forget(own["id"])["ok"]


def test_game_turn_and_state(tmp_path):
    async def scenario():
        gpt, claude = GameHall(tmp_path, "gpt"), GameHall(tmp_path, "claude")
        created = await gpt.game_action("minigames", "gomoku", "create claude")
        match_id = created["result"]["id"]
        first = await gpt.game_action("minigames", "gomoku", f"move {match_id} H8")
        assert first["ok"] and first["result"]["turn_agent"] == "claude"
        repeated = await gpt.game_action("minigames", "gomoku", f"move {match_id} H9")
        assert not repeated["ok"]
        second = await claude.game_action("minigames", "gomoku", f"move {match_id} H9")
        assert second["ok"] and second["result"]["turn_agent"] == "gpt"
    asyncio.run(scenario())


def test_lounge_say_read_and_unread(tmp_path):
    alice, gpt, claude = (LoungeRoom(tmp_path, x) for x in ("alice", "gpt", "claude"))
    alice.action("main", "say :: hello")
    before = gpt.status(mark_read=False)
    assert before["unread_count"] == 1 and before["unread"][0]["author"] == "alice"
    assert gpt.status(mark_read=True)["unread_count"] == 1
    assert gpt.status(mark_read=False)["unread_count"] == 0
    claude.action("main", "say :: reply")
    assert gpt.status(mark_read=False)["unread_count"] == 1


def test_browser_delivery_sequence_ack_retry_lease_dedupe_and_targets(tmp_path):
    bridge = LoungeBridge(tmp_path, browser_config(), replay_existing=True)
    LoungeRoom(tmp_path, "alice").action("main", "say :: wake both")
    clock = time.time()
    bridge.process_once(now=clock)
    gpt_event = bridge.browser_wake("gpt", now=clock + 1)
    claude_event = bridge.browser_wake("claude", now=clock + 1)
    assert gpt_event["sequence"] == claude_event["sequence"] == 1
    assert gpt_event["target"] == "gpt" and claude_event["target"] == "claude"
    assert bridge.browser_result("gpt", 1, gpt_event["delivery_attempt"], False, "delivery failed")
    assert bridge.status()["pending_count"] == 2
    retry = bridge.browser_wake("gpt", now=clock + 2)
    assert retry and retry["sequence"] == 1
    assert bridge.browser_result("gpt", 1, retry["delivery_attempt"], True, "delivered")
    assert bridge.explicit_ack("gpt", 1)
    assert not bridge.browser_result("gpt", 1, retry["delivery_attempt"], True, "duplicate")
    assert bridge.status()["pending_count"] == 1
    assert bridge.browser_wake("claude", now=clock + 2) is None
    assert bridge.browser_wake("claude", now=clock + 102) is None
    assert bridge.status()["pending_count"] == 1
    leased_again = bridge.browser_wake("claude", now=clock + 223)
    assert leased_again and leased_again["target"] == "claude"
    assert bridge.browser_result("claude", 1, leased_again["delivery_attempt"], True, "delivered")
    assert bridge.explicit_ack("claude", 1)
    assert bridge.status()["pending_count"] == 0


def test_gpt_lost_browser_result_retries_with_higher_attempt(tmp_path):
    bridge = LoungeBridge(tmp_path, browser_config("ai-chat"), replay_existing=True)
    LoungeRoom(tmp_path, "claude").action("main", "say :: wake GPT")
    clock = time.time()
    bridge.process_once(now=clock)
    first = bridge.browser_wake("gpt", now=clock + 1)
    assert first and first["delivery_attempt"] == 1
    assert bridge.browser_wake("gpt", now=clock + 22) is None
    second = bridge.browser_wake("gpt", now=clock + 24)
    assert second and second["delivery_attempt"] > first["delivery_attempt"]
    assert bridge.browser_result("gpt", second["sequence"], second["delivery_attempt"], True, "delivered")
    assert bridge.explicit_ack("gpt", second["sequence"])
    assert bridge.status()["pending_count"] == 0


def test_lounge_bridge_environment_port_overrides_config(tmp_path, monkeypatch):
    path = tmp_path / "bridge.json"
    path.write_text(json.dumps({"status_port": 8879}), encoding="utf-8")
    monkeypatch.setenv("LOUNGE_BRIDGE_PORT", "9988")
    assert load_config(path)["status_port"] == 9988


def test_healthcheck_loads_dotenv_before_reading_ports():
    source = (Path(__file__).parents[1] / "scripts" / "healthcheck.py").read_text(encoding="utf-8")
    assert source.index("load_dotenv()") < source.index('os.getenv("MEMORY_ATRIUM_PORT"')


def test_lounge_viewer_uses_configured_human_display_name(tmp_path, monkeypatch):
    monkeypatch.setenv("LOUNGE_HUMAN_DISPLAY_NAME", "Example Human")
    LoungeHandler.root = tmp_path
    server = ThreadingHTTPServer(("localhost", 0), LoungeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        page = urlopen(f"http://localhost:{server.server_port}/", timeout=2).read().decode()
        assert "Example Human" in page
        assert "__HUMAN_DISPLAY_NAME__" not in page
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_configuration_loading(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("MEMORY_PORT=9123\nLOUNGE_HUMAN_IDENTITY=alice\n", encoding="utf-8")
    monkeypatch.delenv("MEMORY_PORT", raising=False)
    load_dotenv(env)
    assert __import__("os").environ["MEMORY_PORT"] == "9123"


def test_http_atrium_and_mcp_tool_surface(tmp_path, monkeypatch):
    monkeypatch.setattr(MemoryStore, "_git_commit", lambda *_: "disabled-in-test")
    MemoryStore(tmp_path, "gpt").remember("HTTP integration", "project/general", "shared")
    AtriumHandler.app = MemoryAtrium(tmp_path)
    server = ThreadingHTTPServer(("localhost", 0), AtriumHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        home = json.loads(urlopen(f"http://localhost:{server.server_port}/api/home", timeout=2).read())
        assert home["total"] == 1
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    monkeypatch.setenv("AI_MEMORY_AGENT", "gpt"); monkeypatch.setenv("DATA_DIR", str(tmp_path))
    module = importlib.import_module("server")
    for name in ("remember", "recall", "recent", "update_memory", "forget", "game_list",
                 "game_open", "game_status", "game_action", "game_close", "lounge_wake_ack"):
        assert callable(getattr(module, name))
    saved = module.remember("MCP integration", "project/general", "shared")
    assert saved["ok"] and module.recall("MCP integration", "all", 5)[0]["id"] == saved["id"]
    assert any(game["id"] == "minigames" for game in module.game_list()["games"])


def test_memory_results_do_not_expose_filesystem_path(tmp_path, monkeypatch):
    monkeypatch.setattr(MemoryStore, "_git_commit", lambda *_: "disabled-in-test")
    store = MemoryStore(tmp_path, "gpt")
    saved = store.remember("private filesystem boundary", "project/general", "shared")
    assert "path" not in saved
    assert all("path" not in item for item in store.recall("filesystem boundary"))
    assert all("path" not in item for item in store.recent())
    updated = store.update(saved["id"], "updated filesystem boundary")
    assert "path" not in updated

    house = tmp_path / "memory" / "_house"
    house.mkdir(parents=True)
    (house / "README.md").write_text("Manual record", encoding="utf-8")
    assert all("path" not in item for item in store.recent(owner="human"))

    AtriumHandler.app = MemoryAtrium(tmp_path)
    atrium_server = ThreadingHTTPServer(("localhost", 0), AtriumHandler)
    thread = threading.Thread(target=atrium_server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.loads(
            urlopen(f"http://localhost:{atrium_server.server_port}/api/memories", timeout=2).read()
        )
        assert all("path" not in item for item in payload["items"])
    finally:
        atrium_server.shutdown(); atrium_server.server_close(); thread.join(timeout=2)

    module = importlib.import_module("server")
    monkeypatch.setattr(module, "store", store)
    mcp_saved = module.remember("MCP filesystem boundary", "project/general", "shared")
    assert "path" not in mcp_saved
    assert all("path" not in item for item in module.recall("MCP filesystem boundary", "all", 5))

    assert store.forget(saved["id"])["ok"]
    assert not store.recall("updated filesystem boundary")
