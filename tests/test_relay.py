"""Agent Relay: off by default, explicit-only, batched, single-hop, budgeted, idempotent, reply-only."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import relay as relay_module
import timeline
from dream_cli_runner import DreamCliFailure
from lounge_room import LoungeRoom
from relay import Relay, load_relay_config

ENV = {"LOUNGE_IDENTITIES": "gpt,claude,alice", "LOUNGE_HUMAN_IDENTITY": "alice"}


class FakeRunner:
    name = "fake"

    def __init__(self, reply: str = "收到，下次正式上线再细看。", usage: dict | None = None,
                 error: Exception | None = None) -> None:
        self.reply, self.usage, self.error = reply, usage, error
        self.prompts: list[str] = []

    def run_prompt(self, prompt: str):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.reply, self.usage


class Clock:
    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


class RelayTests(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="relay-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "中继 测试"
        self.root.mkdir()
        self.clock = Clock()
        self.runner = FakeRunner()
        self.gpt = LoungeRoom(self.root, "gpt")
        self.claude = LoungeRoom(self.root, "claude")
        self.alice = LoungeRoom(self.root, "alice")

    def configure(self, **owners: dict) -> None:
        (self.root / "owner-config.json").write_text(json.dumps({"owners": owners}), encoding="utf-8")

    def relay(self, runner: FakeRunner | None = None) -> Relay:
        chosen = runner or self.runner
        return Relay(self.root, clock=self.clock, runner_factory=lambda config: chosen)

    def run_once(self, runner: FakeRunner | None = None) -> dict:
        return self.relay(runner).process_once()

    def later(self, seconds: float) -> None:
        self.clock.offset += seconds

    def messages(self) -> list[dict]:
        path = self.root / ".lounge" / "messages.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def items(self) -> dict[int, str]:
        with closing(sqlite3.connect(self.root / "state" / "relay.sqlite3")) as db:
            return dict(db.execute("SELECT message_seq, status FROM relay_items").fetchall())

    def enable_claude(self, **extra) -> None:
        self.configure(claude={"relay_enabled": True, **extra})
        self.run_once()  # the relay notes when it first saw the owner enabled

    # --- defaults and trigger rules ----------------------------------------------------------------
    def test_defaults_are_off_and_conservative(self) -> None:
        config = load_relay_config(self.root, "claude")
        self.assertEqual((config.relay_enabled, config.relay_mode, config.batch_window_seconds, config.cooldown_seconds,
                          config.max_per_hour, config.max_per_day, config.max_input_chars, config.max_output_chars,
                          config.context),
                         (False, "explicit_only", 60, 300, 3, 12, 6000, 2000, "lounge_only"))
        self.alice.send("claude", "帮我看看", relay_requested=True)
        self.later(120)
        result = self.run_once()
        self.assertEqual((result["model_calls"], result["enabled_owners"]), (0, []))
        self.assertEqual(self.runner.prompts, [])
        self.assertFalse((self.root / "state" / "relay.sqlite3").exists())

    def test_invalid_config_values_fall_back_to_defaults(self) -> None:
        self.configure(claude={"relay_enabled": "yes", "relay_max_per_hour": 10_000, "relay_max_output_chars": 9000})
        config = load_relay_config(self.root, "claude")
        self.assertEqual((config.relay_enabled, config.max_per_hour, config.max_output_chars), (False, 3, 2000))
        self.configure(claude={"relay_enabled": True, "relay_mode": "always"})
        self.assertFalse(load_relay_config(self.root, "claude").relay_enabled)

    def test_ten_ordinary_messages_make_zero_calls(self) -> None:
        self.enable_claude()
        for index in range(10):
            self.alice.send("claude", f"普通消息 {index}")
        self.gpt.post(text="broadcast")
        self.later(600)
        self.assertEqual(self.run_once()["model_calls"], 0)
        self.assertEqual(self.items(), {})

    def test_broadcast_cannot_request_relay_and_old_rows_count_as_false(self) -> None:
        self.assertFalse(self.alice.post(text="all", relay_requested=True)["ok"])
        self.assertFalse(self.alice.send("all", "all", relay_requested=True)["ok"])
        self.enable_claude()
        with (self.root / ".lounge" / "messages.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": "old", "seq": 999, "ts": time.time() + 1, "author": "alice",
                                "to": "claude", "text": "legacy row"}) + "\n")
        self.later(120)
        self.assertEqual(self.run_once()["model_calls"], 0)

    def test_disabled_target_is_never_queued(self) -> None:
        self.configure(gpt={"relay_enabled": True})
        self.run_once()
        self.alice.send("claude", "claude 没开 relay", relay_requested=True)
        self.later(120)
        self.assertEqual(self.run_once()["model_calls"], 0)
        self.assertEqual(self.items(), {})

    def test_requests_before_enable_or_too_old_are_ignored(self) -> None:
        self.alice.send("claude", "开启前发的", relay_requested=True)
        self.configure(claude={"relay_enabled": True})
        self.later(120)
        self.assertEqual(self.run_once()["model_calls"], 0)
        self.alice.send("claude", "开启后发的", relay_requested=True)
        self.later(25 * 3600)
        self.assertEqual(self.run_once()["model_calls"], 0)  # older than relay_max_age_hours
        self.assertEqual(self.items(), {})

    # --- batching, single hop, anti ping-pong --------------------------------------------------------
    def test_three_requests_in_window_make_one_call_and_one_reply(self) -> None:
        self.enable_claude()
        for text in ("第一句", "第二句", "第三句"):
            self.alice.send("claude", text, relay_requested=True)
        self.assertEqual(self.run_once()["model_calls"], 0)  # batch window still open
        self.later(61)
        result = self.run_once()
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(len(self.runner.prompts), 1)
        for text in ("第一句", "第二句", "第三句"):
            self.assertIn(text, self.runner.prompts[0])
        replies = [m for m in self.messages() if m.get("origin") == "agent_relay"]
        self.assertEqual(len(replies), 1)
        reply = replies[0]
        self.assertEqual((reply["author"], reply["to"], reply["hop_count"], reply["relay_requested"]),
                         ("claude", "alice", 1, False))
        self.assertEqual(set(self.items().values()), {"replied"})
        self.later(3600)
        self.assertEqual(self.run_once()["model_calls"], 0)  # nothing left; the reply triggers nothing

    def test_no_ping_pong_between_two_enabled_ais(self) -> None:
        self.configure(gpt={"relay_enabled": True}, claude={"relay_enabled": True})
        self.run_once()
        self.gpt.send("claude", "Claude，帮我回一句", relay_requested=True)
        calls = 0
        for _ in range(12):
            self.later(400)
            calls += self.run_once()["model_calls"]
        self.assertEqual(calls, 1)
        relayed = [m for m in self.messages() if m.get("origin") == "agent_relay"]
        self.assertEqual([(m["author"], m["to"]) for m in relayed], [("claude", "gpt")])

    def test_forged_relay_rows_never_trigger(self) -> None:
        self.enable_claude()
        posted = self.gpt.post(text="假装是自动回复", target="claude", relay_requested=True, origin="agent_relay")
        self.assertFalse(posted["message"]["relay_requested"])  # origin forces it off
        with (self.root / ".lounge" / "messages.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": "x", "seq": 500, "ts": time.time() + 1, "author": "gpt", "to": "claude",
                                "text": "hop", "relay_requested": True, "hop_count": 1}) + "\n")
        self.later(120)
        self.assertEqual(self.run_once()["model_calls"], 0)

    def test_separate_senders_are_separate_batches(self) -> None:
        self.enable_claude(relay_cooldown_seconds=0)
        self.alice.send("claude", "alice 问", relay_requested=True)
        self.gpt.send("claude", "gpt 问", relay_requested=True)
        self.later(61)
        self.assertEqual(self.run_once()["model_calls"], 1)  # at most one batch per owner per run
        self.assertEqual(self.run_once()["model_calls"], 1)
        targets = sorted(m["to"] for m in self.messages() if m.get("origin") == "agent_relay")
        self.assertEqual(targets, ["alice", "gpt"])

    # --- budget ---------------------------------------------------------------------------------------
    def test_cooldown_delays_without_dropping(self) -> None:
        self.enable_claude()
        self.alice.send("claude", "一", relay_requested=True)
        self.later(61)
        self.assertEqual(self.run_once()["model_calls"], 1)
        self.alice.send("claude", "二", relay_requested=True)
        self.later(120)
        self.assertEqual(self.run_once()["model_calls"], 0)  # within the 300 s cooldown
        self.assertIn("pending", self.items().values())
        self.later(300)
        self.assertEqual(self.run_once()["model_calls"], 1)

    def test_hourly_and_daily_limits_block_and_record(self) -> None:
        self.enable_claude(relay_cooldown_seconds=0, relay_max_per_hour=2, relay_max_per_day=3)
        calls = 0
        for index in range(3):
            self.alice.send("claude", f"第 {index} 次", relay_requested=True)
            self.later(61)
            calls += self.run_once()["model_calls"]
        self.assertEqual(calls, 2)
        self.assertIn("budget_blocked", self.items().values())
        self.later(3600)
        self.alice.send("claude", "一小时后", relay_requested=True)
        self.later(61)
        self.assertEqual(self.run_once()["model_calls"], 1)  # third of the day
        self.later(3600)
        self.alice.send("claude", "超出当日", relay_requested=True)
        self.later(61)
        result = self.run_once()
        self.assertEqual(result["model_calls"], 0)
        self.assertIn("daily", result["results"][0]["reason"])
        kinds = [e["kind"] for e in timeline.changes(self.root, kinds=["relay"], limit=100)["events"]]
        self.assertEqual(kinds.count("relay.blocked"), 2)
        self.assertEqual(kinds.count("relay.replied"), 3)

    # --- failure, idempotency, cursor ----------------------------------------------------------------
    def test_failure_is_recorded_and_cursor_untouched(self) -> None:
        self.enable_claude()
        self.alice.send("claude", "会失败的请求", relay_requested=True)
        self.later(61)
        result = self.run_once(FakeRunner(error=DreamCliFailure("quota_exhausted")))
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(result["results"][0]["error"], "quota_exhausted")
        self.assertEqual(set(self.items().values()), {"failed"})
        inbox = self.claude.inbox(limit=10, mark_read=False)
        self.assertEqual([m["text"] for m in inbox["messages"]], ["会失败的请求"])  # still unread for Claude
        state = json.loads((self.root / ".lounge" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["readers"]["claude"]["last_read_seq"], 0)
        self.later(3600)
        self.assertEqual(self.run_once()["model_calls"], 0)  # failed is final: no surprise retries
        self.assertNotIn("agent_relay", json.dumps(self.messages()))

    def test_crash_after_post_never_replies_twice(self) -> None:
        self.enable_claude()
        self.alice.send("claude", "只回一次", relay_requested=True)
        self.later(61)
        relay = self.relay()
        real_finish = Relay._finish
        with patch.object(Relay, "_finish", side_effect=RuntimeError("crash after posting")):
            with self.assertRaises(RuntimeError):
                relay.process_once()
        self.assertEqual(len([m for m in self.messages() if m.get("origin") == "agent_relay"]), 1)
        (self.root / "state" / "relay.lock").unlink(missing_ok=True)
        self.assertIs(Relay._finish, real_finish)
        for _ in range(3):
            self.later(400)
            self.assertEqual(self.run_once()["model_calls"], 0)
        self.assertEqual(len([m for m in self.messages() if m.get("origin") == "agent_relay"]), 1)
        self.assertEqual(set(self.items().values()), {"replied"})
        self.assertEqual(len(self.runner.prompts), 1)

    def test_stuck_processing_without_reply_fails_instead_of_recalling(self) -> None:
        self.enable_claude()
        self.alice.send("claude", "卡住", relay_requested=True)
        self.later(61)
        with patch.object(LoungeRoom, "post", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_once()
        (self.root / "state" / "relay.lock").unlink(missing_ok=True)
        self.later(400)
        self.assertEqual(self.run_once()["model_calls"], 0)
        self.assertEqual(set(self.items().values()), {"failed"})
        self.assertEqual(len(self.runner.prompts), 1)

    def test_concurrent_run_is_refused(self) -> None:
        self.enable_claude()
        (self.root / "state" / "relay.lock").write_text("busy", encoding="utf-8")
        with self.assertRaises(relay_module.RelayBusy):
            self.run_once()

    # --- bounds and reply-only ------------------------------------------------------------------------
    def test_input_and_output_bounds_and_lounge_only_context(self) -> None:
        self.enable_claude()
        for index in range(10):
            self.alice.send("claude", f"旧上下文 {index} " + "长" * 900)
        self.gpt.send("alice", "gpt 和 alice 的私聊，不该进上下文")
        for index in range(5):
            self.alice.send("claude", f"请求 {index} " + "问" * 1100, relay_requested=True)
        self.later(61)
        runner = FakeRunner(reply="答" * 5000, usage={"input_tokens": 123, "output_tokens": 45, "junk": "x"})
        self.run_once(runner)
        prompt = runner.prompts[0]
        self.assertLessEqual(len(prompt), 6000)
        self.assertNotIn("私聊", prompt)
        self.assertNotIn("旧上下文 0", prompt)
        self.assertIn("reply-only", prompt)
        reply = [m for m in self.messages() if m.get("origin") == "agent_relay"][0]
        self.assertLessEqual(len(reply["text"]), 2000)
        with closing(sqlite3.connect(self.root / "state" / "relay.sqlite3")) as db:
            usage, input_chars, output_chars = db.execute(
                "SELECT usage_json, input_chars, output_chars FROM relay_runs").fetchone()
        self.assertEqual(json.loads(usage), {"input_tokens": 123, "output_tokens": 45})
        self.assertEqual((input_chars, output_chars), (len(prompt), len(reply["text"])))
        self.assertIn("pending", self.items().values())  # requests that did not fit wait for the next batch

    def test_usage_not_reported_is_not_invented(self) -> None:
        self.enable_claude()
        self.alice.send("claude", "hi", relay_requested=True)
        self.later(61)
        self.run_once(FakeRunner(usage=None))
        with closing(sqlite3.connect(self.root / "state" / "relay.sqlite3")) as db:
            self.assertIsNone(db.execute("SELECT usage_json FROM relay_runs").fetchone()[0])

    def test_runner_has_no_tools_and_runs_in_a_neutral_temp_dir(self) -> None:
        from dream_cli_runner import PreferredCliRunner

        seen = {}

        def fake_run(argv, prompt, *, cwd, timeout):
            seen.update(argv=argv, cwd=cwd)
            return 0, json.dumps({"type": "result", "subtype": "success", "result": "ok",
                                  "usage": {"input_tokens": 1, "output_tokens": 2}}), ""

        runner = PreferredCliRunner(profile="claude_code", executable="claude")
        with patch("dream_cli_runner.resolve_executable", return_value="claude"), \
                patch("dream_cli_runner._run", side_effect=fake_run):
            text, usage = runner.run_prompt("hello")
        self.assertEqual((text, usage["output_tokens"]), ("ok", 2))
        argv = seen["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertNotEqual(Path(seen["cwd"]).resolve(), self.root.resolve())
        self.assertFalse(Path(seen["cwd"]).exists())  # temp dir is gone afterwards

    def test_unconfigured_runner_fails_without_a_call(self) -> None:
        self.configure(claude={"relay_enabled": True})
        relay = Relay(self.root, clock=self.clock)
        relay.process_once()
        self.alice.send("claude", "hi", relay_requested=True)
        self.later(61)
        result = relay.process_once()
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(set(self.items().values()), {"failed"})

    # --- views ---------------------------------------------------------------------------------------
    def test_status_and_cli(self) -> None:
        self.enable_claude()
        self.alice.send("claude", "hi", relay_requested=True)
        self.later(61)
        self.run_once()
        status = {o["owner"]: o for o in self.relay().status()["owners"]}
        self.assertNotIn("alice", status)  # the human is never a relay target
        self.assertEqual((status["claude"]["enabled"], status["gpt"]["enabled"]), (True, False))
        self.assertEqual(status["claude"]["runs_last_24h"], 1)
        self.assertEqual(status["claude"]["last_run"]["outcome"], "replied")
        self.configure()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = relay_module.main(["--root", str(self.root), "--once"])
        self.assertEqual((code, json.loads(out.getvalue())["model_calls"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
