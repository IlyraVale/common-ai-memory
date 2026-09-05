from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import env_path, human_identity, load_dotenv, lounge_identities


MODES = {"manual", "active", "ai-chat"}
AGENTS = {"gpt", "claude"}
DEFAULT_CONFIG = {
    "schema_version": 1,
    "mode": "manual",
    "poll_seconds": 0.75,
    "cooldown_seconds": 8.0,
    "retry_base_seconds": 3.0,
    "retry_max_seconds": 60.0,
    "wake_timeout_seconds": 90.0,
    "ack_timeout_seconds": 180.0,
    "max_ai_rounds": 8,
    "status_port": 8879,
    "adapters": {"gpt": {"type": "disabled"}, "claude": {"type": "disabled"}},
}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else dict(default)
    except Exception:
        return dict(default)


def load_config(path: Path) -> dict[str, Any]:
    raw = load_json(path, DEFAULT_CONFIG)
    config = dict(DEFAULT_CONFIG)
    config.update({key: value for key, value in raw.items() if key != "adapters"})
    config["adapters"] = dict(DEFAULT_CONFIG["adapters"])
    config["adapters"].update(raw.get("adapters", {}))
    if config["mode"] not in MODES:
        raise ValueError("mode must be manual, active, or ai-chat")
    for key in ("poll_seconds", "cooldown_seconds", "retry_base_seconds", "retry_max_seconds", "wake_timeout_seconds", "ack_timeout_seconds"):
        config[key] = float(config[key])
        if config[key] <= 0:
            raise ValueError(f"{key} must be positive")
    config["max_ai_rounds"] = int(config["max_ai_rounds"])
    if os.getenv("LOUNGE_BRIDGE_PORT"):
        config["status_port"] = os.environ["LOUNGE_BRIDGE_PORT"]
    config["status_port"] = int(config["status_port"])
    if config["max_ai_rounds"] < 1 or not 1 <= config["status_port"] <= 65535:
        raise ValueError("invalid max_ai_rounds or status_port")
    for agent in AGENTS:
        adapter = config["adapters"].get(agent, {})
        if adapter.get("type", "disabled") not in {"disabled", "command", "http", "browser"}:
            raise ValueError(f"invalid adapter type for {agent}")
        if adapter.get("type") == "command":
            command = adapter.get("command")
            if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
                raise ValueError(f"{agent} command adapter requires a non-empty string array")
        if adapter.get("type") == "http":
            url = str(adapter.get("url", ""))
            if not url.startswith("http://localhost:"):
                raise ValueError(f"{agent} HTTP adapter must use loopback")
    return config


@dataclass
class WakeResult:
    ok: bool
    detail: str


class WakeRunner:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def __call__(self, delivery: dict[str, Any]) -> WakeResult:
        agent = delivery["target"]
        adapter = self.config["adapters"].get(agent, {"type": "disabled"})
        payload = {
            "version": 1,
            "type": "lounge_wake",
            "target": agent,
            "sequence": delivery["seq"],
            "source": delivery["source"],
            "prompt": (
                f"[AI Lounge Bridge seq={delivery['seq']}] AI Lounge 有新消息。"
                "请先调用 game_status(\"lounge\") 读取最新上下文；若有自然、必要的回应，"
                "请通过现有 game_action 在 lounge/main 发言。不要复述本提示，也不要为了续聊而强行回复。"
            ),
        }
        kind = adapter.get("type", "disabled")
        if kind == "disabled":
            return WakeResult(False, f"{agent} adapter is disabled")
        try:
            if kind == "command":
                completed = subprocess.run(
                    adapter["command"],
                    input=json.dumps(payload, ensure_ascii=False) + "\n",
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    timeout=self.config["wake_timeout_seconds"],
                    shell=False,
                    cwd=adapter.get("cwd") or None,
                )
                detail = (completed.stderr or completed.stdout or "").strip()[-500:]
                return WakeResult(completed.returncode == 0, detail or f"exit={completed.returncode}")
            request = Request(
                adapter["url"],
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            with urlopen(request, timeout=self.config["wake_timeout_seconds"]) as response:
                return WakeResult(200 <= response.status < 300, f"http={response.status}")
        except (subprocess.SubprocessError, OSError, HTTPError, URLError) as exc:
            return WakeResult(False, str(exc)[-500:])
        return WakeResult(False, "unsupported adapter")


class LoungeBridge:
    def __init__(
        self,
        root: str | Path,
        config: dict[str, Any],
        wake: Callable[[dict[str, Any]], WakeResult] | None = None,
        *,
        replay_existing: bool = False,
    ) -> None:
        self.root = Path(root).resolve()
        self.human_id = human_identity()
        self.identities = set(lounge_identities())
        self.lounge = self.root / ".lounge"
        self.runtime = self.root / ".lounge-bridge"
        self.state_path = self.runtime / "state.json"
        self.config = config
        self.wake = wake or WakeRunner(config)
        self.guard = threading.RLock()
        self.stop_event = threading.Event()
        self.state = self._load_state(replay_existing)

    def _messages(self) -> list[dict[str, Any]]:
        path = self.lounge / "messages.jsonl"
        rows: list[dict[str, Any]] = []
        if not path.exists():
            return rows
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict) and isinstance(row.get("seq"), int) and row.get("author") in self.identities:
                rows.append(row)
        rows.sort(key=lambda item: item["seq"])
        return rows

    def _new_state(self, replay_existing: bool) -> dict[str, Any]:
        rows = self._messages()
        latest = rows[-1]["seq"] if rows and not replay_existing else 0
        return {
            "schema_version": 1,
            "mode": self.config["mode"],
            "last_scanned_seq": latest,
            "pending": {},
            "recent_acks": [],
            "last_wake_at": {"gpt": 0.0, "claude": 0.0},
            "conversation": {"generation": 0, "ai_messages": 0, "ai_rounds": 0, "last_ai_author": None, "paused": False, "paused_at_seq": None},
            "metrics": {"queued": 0, "acked": 0, "failed_attempts": 0, "suppressed": 0},
            "started_at": time.time(),
            "updated_at": time.time(),
        }

    def _load_state(self, replay_existing: bool) -> dict[str, Any]:
        if not self.state_path.exists():
            state = self._new_state(replay_existing)
            atomic_json(self.state_path, state)
            return state
        state = load_json(self.state_path, {})
        if state.get("schema_version") != 1:
            raise ValueError("unsupported Lounge Bridge state schema")
        state["mode"] = state.get("mode") if state.get("mode") in MODES else self.config["mode"]
        state.setdefault("pending", {})
        state.setdefault("recent_acks", [])
        state.setdefault("last_wake_at", {"gpt": 0.0, "claude": 0.0})
        state.setdefault("metrics", {"queued": 0, "acked": 0, "failed_attempts": 0, "suppressed": 0})
        state.setdefault("conversation", {"generation": 0, "ai_messages": 0, "ai_rounds": 0, "last_ai_author": None, "paused": False, "paused_at_seq": None})
        for delivery in state["pending"].values():
            if delivery.get("inflight_at"):
                delivery["inflight_at"] = None
                delivery["next_attempt_at"] = 0.0
        return state

    def _save(self) -> None:
        self.state["updated_at"] = time.time()
        atomic_json(self.state_path, self.state)

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError("mode must be manual, active, or ai-chat")
        if mode != "manual":
            missing = [agent for agent in sorted(AGENTS) if self.config["adapters"].get(agent, {}).get("type", "disabled") == "disabled"]
            if missing:
                raise ValueError(f"automatic mode requires configured adapters: {', '.join(missing)}")
        with self.guard:
            self.state["mode"] = mode
            if mode == "manual":
                self._suppress_pending("mode_manual")
            elif mode == "active":
                for key, item in list(self.state["pending"].items()):
                    if item["source"] in AGENTS:
                        self._ack(key, item, "suppressed_mode_active")
            self._save()

    def _ack(self, key: str, delivery: dict[str, Any], outcome: str) -> None:
        self.state["pending"].pop(key, None)
        self.state["recent_acks"].append({"seq": delivery["seq"], "target": delivery["target"], "outcome": outcome, "at": time.time()})
        self.state["recent_acks"] = self.state["recent_acks"][-100:]

    def _suppress_pending(self, reason: str) -> None:
        for key, item in list(self.state["pending"].items()):
            self._ack(key, item, reason)
            self.state["metrics"]["suppressed"] += 1

    def _queue(self, row: dict[str, Any], target: str) -> None:
        key = f"{row['seq']}:{target}"
        if key in self.state["pending"] or any(x["seq"] == row["seq"] and x["target"] == target for x in self.state["recent_acks"]):
            return
        self.state["pending"][key] = {
            "seq": row["seq"], "target": target, "source": row["author"],
            "generation": self.state["conversation"]["generation"], "attempts": 0,
            "next_attempt_at": 0.0, "inflight_at": None, "lease_until": 0.0,
            "status": "queued", "delivered_at": None, "last_error": None,
        }
        self.state["metrics"]["queued"] += 1

    def _ingest(self) -> None:
        last = int(self.state.get("last_scanned_seq", 0))
        for row in (item for item in self._messages() if item["seq"] > last):
            author = row["author"]
            convo = self.state["conversation"]
            if author == self.human_id:
                self._suppress_pending("superseded_by_human")
                convo.update({"generation": int(convo.get("generation", 0)) + 1, "ai_messages": 0, "ai_rounds": 0, "last_ai_author": None, "paused": False, "paused_at_seq": None})
            else:
                convo["ai_messages"] = int(convo.get("ai_messages", 0)) + 1
                convo["ai_rounds"] = (convo["ai_messages"] + 1) // 2
                convo["last_ai_author"] = author
                if convo["ai_messages"] >= self.config["max_ai_rounds"] * 2:
                    convo["paused"] = True
                    convo["paused_at_seq"] = row["seq"]
                    self._suppress_pending("ai_round_limit")

            mode = self.state["mode"]
            targets: list[str] = []
            if mode in {"active", "ai-chat"} and author == self.human_id:
                targets = ["gpt", "claude"]
            elif mode == "ai-chat" and author in AGENTS and not convo["paused"]:
                targets = ["claude" if author == "gpt" else "gpt"]
            else:
                self.state["metrics"]["suppressed"] += 1
            for target in targets:
                self._queue(row, target)
            self.state["last_scanned_seq"] = row["seq"]
            self._save()

    def _deliver(self, now: float) -> None:
        items = sorted(self.state["pending"].items(), key=lambda pair: (pair[1]["seq"], pair[1]["target"]))
        for key, delivery in items:
            target = delivery["target"]
            if self.config["adapters"].get(target, {}).get("type") == "browser":
                continue
            if delivery.get("status") == "awaiting_ack" and now < float(delivery.get("next_attempt_at", 0)):
                continue
            if delivery.get("status") == "awaiting_ack":
                delivery["status"] = "queued"
            if now < float(delivery.get("next_attempt_at", 0)):
                continue
            if now - float(self.state["last_wake_at"].get(target, 0)) < self.config["cooldown_seconds"]:
                continue
            delivery["attempts"] += 1
            delivery["inflight_at"] = now
            self._save()
            result = self.wake(dict(delivery))
            delivery = self.state["pending"].get(key)
            if delivery is None:
                continue
            delivery["inflight_at"] = None
            if result.ok:
                self.state["last_wake_at"][target] = now
                delivery["status"] = "awaiting_ack"
                delivery["delivered_at"] = now
                delivery["next_attempt_at"] = now + self.config["ack_timeout_seconds"]
                delivery["last_error"] = None
            else:
                delivery["last_error"] = result.detail
                delay = min(self.config["retry_max_seconds"], self.config["retry_base_seconds"] * (2 ** min(delivery["attempts"] - 1, 8)))
                delivery["next_attempt_at"] = now + delay
                self.state["metrics"]["failed_attempts"] += 1
            self._save()

    def browser_wake(self, target: str, now: float | None = None) -> dict[str, Any] | None:
        if target not in AGENTS or self.config["adapters"].get(target, {}).get("type") != "browser":
            return None
        current = time.time() if now is None else now
        with self.guard:
            candidates = sorted(
                (item for item in self.state["pending"].values() if item["target"] == target),
                key=lambda item: item["seq"],
            )
            # CLAUDE_GUARD_SERIALIZE: keep only the oldest unresolved Claude wake.
            if target == "claude" and candidates:
                candidates = candidates[:1]
            for item in candidates:
                if target == "gpt" and item.get("status") == "leased":
                    lease_until = float(item.get("lease_until", 0))
                    if current < lease_until:
                        continue
                    attempts = int(item.get("attempts", 0)) + 1
                    item["attempts"] = attempts
                    item["lease_until"] = 0.0
                    item["status"] = "queued"
                    item["last_error"] = "gpt_lease_expired"
                    delay = min(
                        self.config["retry_max_seconds"],
                        self.config["retry_base_seconds"] * (2 ** min(attempts - 1, 8)),
                    )
                    item["next_attempt_at"] = current + delay
                    self.state["metrics"]["failed_attempts"] += 1
                    self._save()
                    continue
                # CLAUDE_GUARD_LEASE_EXPIRED
                if target == "claude" and item.get("status") == "leased":
                    lease_until = float(item.get("lease_until", 0))
                    if current < lease_until:
                        continue
                    attempts = int(item.get("attempts", 0)) + 1
                    item["attempts"] = attempts
                    item["lease_until"] = 0.0
                    item["last_error"] = "claude_lease_expired"
                    self.state["metrics"]["failed_attempts"] += 1
                    max_attempts = int(self.config.get("claude_max_delivery_attempts", 2))
                    if attempts >= max_attempts:
                        key = f"{item['seq']}:{target}"
                        self._ack(key, item, "claude_lease_expired")
                        self.state["metrics"]["suppressed"] += 1
                        self._save()
                        continue
                    item["status"] = "queued"
                    item["next_attempt_at"] = current + float(
                        self.config.get("claude_retry_pause_seconds", 120.0)
                    )
                    self._save()
                    continue
                if item.get("status") == "awaiting_ack" and current < float(item.get("next_attempt_at", 0)):
                    continue
                if item.get("status") == "awaiting_ack":
                    # CLAUDE_GUARD_ACK_TIMEOUT
                    if target == "claude":
                        attempts = int(item.get("attempts", 0))
                        max_attempts = int(self.config.get("claude_max_delivery_attempts", 2))
                        if attempts >= max_attempts:
                            key = f"{item['seq']}:{target}"
                            self._ack(key, item, "claude_retry_exhausted")
                            self.state["metrics"]["suppressed"] += 1
                            self._save()
                            continue
                        item["status"] = "queued"
                        item["next_attempt_at"] = current + float(
                            self.config.get("claude_retry_pause_seconds", 120.0)
                        )
                        item["last_error"] = "claude_ack_timeout_guard"
                        self._save()
                        continue
                    item["status"] = "queued"
                if current < float(item.get("next_attempt_at", 0)) or current < float(item.get("lease_until", 0)):
                    continue
                # CLAUDE_GUARD_WAKE_GAP
                wake_gap = float(self.config["cooldown_seconds"])
                if target == "claude":
                    wake_gap = max(
                        wake_gap,
                        float(self.config.get("claude_min_wake_gap_seconds", 20.0)),
                    )
                if current - float(self.state["last_wake_at"].get(target, 0)) < wake_gap:
                    continue
                item["status"] = "leased"
                # CLAUDE_GUARD_LEASE_SECONDS
                lease_seconds = 20.0
                if target == "claude":
                    lease_seconds = float(self.config.get("claude_lease_seconds", 90.0))
                item["lease_until"] = current + lease_seconds
                attempt = int(item.get("attempts", 0)) + 1
                self._save()
                return {
                    "sequence": item["seq"], "target": target, "source": item["source"],
                    "delivery_attempt": attempt,
                    "message": (
                        f"AI Lounge wake: seq={item['seq']}, target={target}. "
                        "请先调用 game_status(\"lounge\") 读取最新消息。"
                        "若需要回应，必须调用 game_action("
                        "game=\"lounge\", area=\"main\", command=\"say\", "
                        "table_talk=\"<回复正文>\") 将回复发送到 AI 客厅；"
                        "不要只在当前 ChatGPT/Claude 对话中输出回复，"
                        "也不要把当前对话中的普通回复视为已经回应客厅。"
                        "如确实无需回应，可以不调用 game_action。"
                        "若需要回应但 game_action 失败，不要用当前对话的文本回复代替，且不要 ACK。"
                        f"完成本次检查后调用 lounge_wake_ack(sequence={item['seq']})。"
                    ),
                }
            return None

    def browser_result(self, target: str, sequence: int, attempt: int, ok: bool, detail: str = "") -> bool:
        key = f"{sequence}:{target}"
        now = time.time()
        with self.guard:
            item = self.state["pending"].get(key)
            if not item or target not in AGENTS or attempt != int(item.get("attempts", 0)) + 1:
                return False
            item["attempts"] = attempt
            item["lease_until"] = 0.0
            if ok:
                item["status"] = "awaiting_ack"
                item["delivered_at"] = now
                item["next_attempt_at"] = now + self.config["ack_timeout_seconds"]
                item["last_error"] = None
                self.state["last_wake_at"][target] = now
            else:
                # CLAUDE_GUARD_DELIVERY_FAIL
                item["last_error"] = detail[-500:]
                self.state["metrics"]["failed_attempts"] += 1
                if target == "claude":
                    max_attempts = int(self.config.get("claude_max_delivery_attempts", 2))
                    if attempt >= max_attempts:
                        self._ack(key, item, "claude_delivery_failed")
                        self.state["metrics"]["suppressed"] += 1
                    else:
                        item["status"] = "queued"
                        item["next_attempt_at"] = now + float(
                            self.config.get("claude_retry_pause_seconds", 120.0)
                        )
                else:
                    item["status"] = "queued"
                    delay = min(
                        self.config["retry_max_seconds"],
                        self.config["retry_base_seconds"] * (2 ** min(attempt - 1, 8)),
                    )
                    item["next_attempt_at"] = now + delay
            self._save()
            return True

    def explicit_ack(self, target: str, sequence: int) -> bool:
        key = f"{sequence}:{target}"
        with self.guard:
            item = self.state["pending"].get(key)
            if not item:
                return any(x["seq"] == sequence and x["target"] == target and x["outcome"] == "explicit_ack" for x in self.state["recent_acks"])
            self.state["metrics"]["acked"] += 1
            self._ack(key, item, "explicit_ack")
            self._save()
            return True

    def process_once(self, now: float | None = None) -> None:
        with self.guard:
            self._ingest()
            self._deliver(time.time() if now is None else now)

    def status(self) -> dict[str, Any]:
        with self.guard:
            latest_rows = self._messages()
            latest_seq = latest_rows[-1]["seq"] if latest_rows else 0
            adapters = {agent: self.config["adapters"].get(agent, {}).get("type", "disabled") for agent in sorted(AGENTS)}
            automatic_ready = all(kind != "disabled" for kind in adapters.values())
            pending = list(self.state["pending"].values())
            return {
                "ok": True, "service": "ai-lounge-bridge", "schema_version": 1,
                "mode": self.state["mode"], "latest_seq": latest_seq,
                "last_scanned_seq": self.state["last_scanned_seq"], "pending_count": len(pending),
                "pending": [{"seq": x["seq"], "target": x["target"], "status": x.get("status", "queued"), "attempts": x["attempts"], "next_attempt_at": x["next_attempt_at"], "last_error": x["last_error"]} for x in pending],
                "conversation": dict(self.state["conversation"]), "max_ai_rounds": self.config["max_ai_rounds"],
                "adapters": adapters, "automatic_ready": automatic_ready,
                "health": "ok" if self.state["mode"] == "manual" or automatic_ready else "degraded",
                "metrics": dict(self.state["metrics"]),
                "updated_at": self.state["updated_at"],
            }

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.process_once()
            except Exception as exc:
                with self.guard:
                    self.state["last_loop_error"] = str(exc)[-500:]
                    self._save()
            self.stop_event.wait(self.config["poll_seconds"])


class StatusHandler(BaseHTTPRequestHandler):
    bridge: LoungeBridge

    def _json(self, value: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/v1/status":
            self._json(self.bridge.status())
        elif self.path.startswith("/v1/browser-wake/"):
            target = self.path.rsplit("/", 1)[-1]
            self._json({"ok": True, "event": self.bridge.browser_wake(target)})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path not in {"/v1/mode", "/v1/browser-result", "/v1/ack"}:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1024:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if self.path == "/v1/mode":
                self.bridge.set_mode(str(payload.get("mode", "")))
                self._json(self.bridge.status())
            elif self.path == "/v1/browser-result":
                accepted = self.bridge.browser_result(str(payload.get("target", "")), int(payload.get("sequence", 0)), int(payload.get("delivery_attempt", 0)), bool(payload.get("ok")), str(payload.get("detail", "")))
                self._json({"ok": accepted}, 200 if accepted else 409)
            else:
                accepted = self.bridge.explicit_ack(str(payload.get("target", "")), int(payload.get("sequence", 0)))
                self._json({"ok": accepted}, 200 if accepted else 404)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._json({"ok": False, "error": str(exc)}, 400)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Independent local AI Lounge wake bridge")
    parser.add_argument("--root", default=str(env_path("DATA_DIR", "./runtime")))
    parser.add_argument("--config", default="")
    parser.add_argument("--mode", choices=sorted(MODES))
    parser.add_argument("--replay-existing", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    config_path = Path(args.config).resolve() if args.config else root / ".lounge-bridge" / "config.json"
    config = load_config(config_path)
    if args.mode:
        config["mode"] = args.mode
    bridge = LoungeBridge(root, config, replay_existing=args.replay_existing)
    if args.mode:
        bridge.set_mode(args.mode)
    if args.once:
        bridge.process_once()
        print(json.dumps(bridge.status(), ensure_ascii=False))
        return
    StatusHandler.bridge = bridge
    host = os.getenv("CAM_BIND_HOST", "localhost")
    server = ThreadingHTTPServer((host, config["status_port"]), StatusHandler)
    worker = threading.Thread(target=bridge.run, name="lounge-bridge", daemon=True)
    worker.start()
    print(f"AI Lounge Bridge: http://{host}:{config['status_port']}/v1/status", flush=True)
    try:
        server.serve_forever()
    finally:
        bridge.stop_event.set()
        server.server_close()
        worker.join(timeout=5)


if __name__ == "__main__":
    main()
