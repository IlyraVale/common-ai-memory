"""Nightly preferred-CLI Dream automation. All writes stay in TemporaryDirectory.

A stand-in CLI (a Python script run through the runner's test launcher) plays
Claude Code and Codex: it records argv/prompt and can succeed, fail auth, run out
of quota, exit nonzero, return empty/malformed/oversized output, or hang with a
grandchild process.
"""

from __future__ import annotations

import ctypes
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import dream_nightly
from dream_cli_runner import (
    DreamCliFailure, MAX_PREFERRED_CLI_TIMEOUT_SECONDS, PROMPT_FIELDS, PreferredCliRunner, resolve_executable,
)
from dream_nightly import (
    EXIT_ALL_FALLBACK, EXIT_INFRASTRUCTURE, EXIT_OK, EXIT_PARTIAL_FALLBACK, dry_run, run_batch,
)
from dreams import (
    DreamStore, PreparedDreamStore, DreamPreparer, PENDING_DREAM_INSTRUCTION, claim_on_wake_dream,
    dream_commit_result, load_owner_config,
)
from memory_store import MemoryStore

HERE = Path(__file__).resolve().parent
SCRIPTS = next(p / "scripts" for p in (HERE, HERE.parent) if (p / "scripts" / "run-nightly-dreams.ps1").is_file())
NOW = datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc)  # 12:00 Asia/Shanghai
LATEST, GRACE = "2026-09-29", "2026-09-28"
TZ = "Asia/Shanghai"
SECRET_MATERIAL = "MATERIAL-BODY-不应进入日志"

FAKE_CLI = r'''
import json, os, subprocess, sys, time
here = os.path.dirname(os.path.abspath(__file__))
profile = "claude" if "-p" in sys.argv[1:3] else "codex"
mode = open(os.path.join(here, "mode-" + profile + ".txt"), encoding="utf-8").read().strip()
prompt = sys.stdin.buffer.read().decode("utf-8")
with open(os.path.join(here, "calls-" + profile + ".jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt, "stdin_tty": sys.stdin.isatty()}, ensure_ascii=False) + "\n")
body = profile + " dream body"
def out_file():
    return sys.argv[sys.argv.index("-o") + 1]
def claude(result, **extra):
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": result, **extra}))
if mode == "success":
    if profile == "claude":
        claude("\x1b[1m" + body + "\x1b[0m", session_id="s", usage={})
    else:
        print("codex progress chatter SECRET-STDOUT")
        open(out_file(), "w", encoding="utf-8").write(body + "\n")
    sys.exit(0)
if mode == "auth":
    sys.stderr.write("Error: Not logged in. Please run /login SECRET-STDERR\n"); sys.exit(1)
if mode == "quota":
    sys.stderr.write("You have hit your usage limit. SECRET-STDERR\n"); sys.exit(1)
if mode == "nonzero":
    sys.stderr.write("unexpected crash SECRET-STDERR\n"); sys.exit(3)
if mode == "empty":
    if profile == "claude":
        claude("   ")
    else:
        open(out_file(), "w", encoding="utf-8").write("  \n")
    sys.exit(0)
if mode == "malformed":
    print("not json and no output file SECRET-STDOUT"); sys.exit(0)
if mode == "too_long":
    if profile == "claude":
        claude("x" * 9000)
    else:
        open(out_file(), "w", encoding="utf-8").write("x" * 9000)
    sys.exit(0)
if mode == "is_error":
    print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                      "result": "API Error: rate limit exceeded SECRET-STDOUT"})); sys.exit(0)
if mode == "timeout":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    open(os.path.join(here, "grandchild-" + profile + ".pid"), "w").write(str(child.pid))
    time.sleep(120)
sys.exit(9)
'''


def process_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    kernel = ctypes.windll.kernel32
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


def _record(root: Path, memory_id: str, owner: str, day: str) -> None:
    created = f"{day}T03:00:00Z"
    record = {"id": memory_id, "owner": owner, "scope": "agent", "category": "plan/general",
              "created_at": created, "updated_at": created, "content": f"{SECRET_MATERIAL} {memory_id}"}
    path = root / "memory" / "plan" / "general" / f"{day}_{memory_id}__{owner}__agent.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(MemoryStore._serialize(record), encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="dream-cli-")
        # Chinese path segments on purpose: the production project lives under one.
        self.root = Path(self.temp.name) / "梦境根目录"
        self.bin = Path(self.temp.name) / "命令行工具"
        self.root.mkdir()
        self.bin.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        for owner in ("claude", "gpt"):
            _record(self.root, f"{owner}-latest", owner, LATEST)
        self.fake = self.bin / "fake_cli.py"
        self.fake.write_text(FAKE_CLI, encoding="utf-8")
        self.write_config()
        self.set_mode("claude", "success")
        self.set_mode("codex", "success")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_config(self, **overrides) -> None:
        owners = {
            "claude": {"dream_mode": "on_wake", "dream_preferred_runner": "cli",
                       "dream_cli_profile": "claude_code", "dream_cli_executable": str(self.fake),
                       "dream_timeout_seconds": 300},
            "gpt": {"dream_mode": "on_wake", "dream_preferred_runner": "cli",
                    "dream_cli_profile": "codex", "dream_cli_executable": str(self.fake),
                    "dream_timeout_seconds": 300},
        }
        for owner, values in overrides.items():
            owners[owner] = values
        (self.root / "owner-config.json").write_text(json.dumps({"owners": owners}), encoding="utf-8")

    def set_mode(self, profile: str, mode: str) -> None:
        (self.bin / f"mode-{profile}.txt").write_text(mode, encoding="utf-8")

    def calls(self, profile: str) -> list[dict]:
        path = self.bin / f"calls-{profile}.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def runners(self, timeout: int = 60) -> dict[str, PreferredCliRunner]:
        return {
            "claude": PreferredCliRunner("claude_code", str(self.fake), timeout, 8000, (sys.executable,)),
            "gpt": PreferredCliRunner("codex", str(self.fake), timeout, 8000, (sys.executable,)),
        }

    def run_batch(self, *, now: datetime = NOW, timeout: int = 60, owners=("claude", "gpt")):
        return run_batch(self.root, list(owners), timezone_name=TZ, now=now,
                         runners=self.runners(timeout), clock=lambda: now + timedelta(minutes=1))

    def commits(self, owner: str | None = None) -> list[tuple]:
        db = sqlite3.connect(self.root / "state" / "dream-runtime.sqlite3")
        try:
            rows = db.execute("SELECT owner,dream_date,generation_id FROM dream_commits ORDER BY 1,2").fetchall()
        finally:
            db.close()
        return [row for row in rows if owner is None or row[0] == owner]

    def dream(self, owner: str, day: str = LATEST) -> dict | None:
        return DreamStore(self.root).existing(owner, day)

    def log_text(self) -> str:
        directory = self.root / "state" / "dream-scheduler"
        return "\n".join(p.read_text(encoding="utf-8") for p in directory.glob("*.jsonl")) if directory.is_dir() else ""

    def by_owner(self, results) -> dict[str, dict]:
        return {item["owner"]: item for item in results}


class SuccessAndFallbackTests(Base):
    def test_both_cli_success_commit_with_runner_attribution(self) -> None:
        results, code = self.run_batch()
        self.assertEqual(code, EXIT_OK)
        out = self.by_owner(results)
        self.assertEqual((out["claude"]["status"], out["gpt"]["status"]), ("committed", "committed"))
        self.assertEqual(self.dream("claude")["content"], "claude dream body")  # ANSI stripped, JSON unwrapped
        self.assertEqual(self.dream("gpt")["content"], "codex dream body")       # only the -o file, not stdout
        self.assertEqual(self.dream("claude")["runner"], "cli:claude_code")
        self.assertEqual(self.dream("gpt")["runner"], "cli:codex")
        self.assertEqual(len(self.commits()), 2)
        self.assertFalse(PreparedDreamStore(self.root).path_for("claude", LATEST).exists())

    def test_owner_cli_mapping_is_fixed(self) -> None:
        self.run_batch()
        claude_argv, codex_argv = self.calls("claude")[0]["argv"], self.calls("codex")[0]["argv"]
        self.assertIn("-p", claude_argv)
        self.assertEqual(codex_argv[0], "exec")
        self.assertIn(f"{LATEST}", json.dumps(self.calls("claude")[0]["prompt"]))
        self.assertIn("claude-latest", self.calls("claude")[0]["prompt"])
        self.assertNotIn("gpt-latest", self.calls("claude")[0]["prompt"])
        self.assertIn("gpt-latest", self.calls("codex")[0]["prompt"])
        self.assertNotIn("claude-latest", self.calls("codex")[0]["prompt"])

    def test_cli_flags_isolate_tools_settings_and_persistence(self) -> None:
        self.run_batch()
        claude = self.calls("claude")[0]["argv"]
        for flag in ("--output-format", "json", "--tools", "", "--strict-mcp-config", "--setting-sources",
                     "--no-session-persistence"):
            self.assertIn(flag, claude)
        self.assertEqual(claude[claude.index("--tools") + 1], "")
        self.assertEqual(claude[claude.index("--setting-sources") + 1], "")
        codex = self.calls("codex")[0]["argv"]
        for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "-o"):
            self.assertIn(flag, codex)
        self.assertEqual(codex[codex.index("--sandbox") + 1], "read-only")
        self.assertFalse(self.calls("claude")[0]["stdin_tty"])

    def test_prompt_contains_only_packet_materials_and_instruction(self) -> None:
        self.run_batch()
        prompt = self.calls("claude")[0]["prompt"]
        self.assertTrue(prompt.startswith(PENDING_DREAM_INSTRUCTION))
        package = json.loads(prompt.split("INPUT PACKAGE (JSON):\n", 1)[1])
        self.assertEqual(set(package), set(PROMPT_FIELDS) | {"derived", "factual_authority"})
        self.assertEqual((package["derived"], package["factual_authority"]), (True, False))
        self.assertEqual((package["owner"], package["dream_date"]), ("claude", LATEST))
        for forbidden in ("claim_token", "expires_at", "claimed_at", "exposure_episode_id", "api_key", "oauth"):
            self.assertNotIn(forbidden, prompt.lower().replace("_", "_"))

    def test_claude_fails_gpt_succeeds(self) -> None:
        self.set_mode("claude", "auth")
        results, code = self.run_batch()
        out = self.by_owner(results)
        self.assertEqual(code, EXIT_PARTIAL_FALLBACK)
        self.assertEqual((out["claude"]["status"], out["gpt"]["status"]), ("fallback", "committed"))
        self.assertEqual(out["claude"]["claims"][0]["error_class"], "auth_failure")
        self.assertIsNone(self.dream("claude"))
        self.assertTrue(PreparedDreamStore(self.root).path_for("claude", LATEST).exists(), "packet kept")
        self.assertIsNotNone(self.dream("gpt"))

    def test_gpt_fails_claude_succeeds(self) -> None:
        self.set_mode("codex", "quota")
        results, code = self.run_batch()
        out = self.by_owner(results)
        self.assertEqual(code, EXIT_PARTIAL_FALLBACK)
        self.assertEqual((out["claude"]["status"], out["gpt"]["status"]), ("committed", "fallback"))
        self.assertEqual(out["gpt"]["claims"][0]["error_class"], "quota_exhausted")

    def test_both_fail_then_on_wake_claims_and_commits(self) -> None:
        self.set_mode("claude", "nonzero")
        self.set_mode("codex", "nonzero")
        results, code = self.run_batch()
        self.assertEqual(code, EXIT_ALL_FALLBACK)
        self.assertEqual(self.commits(), [])
        for owner in ("claude", "gpt"):
            # While the CLI's claim lease is live, a wake gets nothing; afterwards it can claim.
            self.assertIsNone(claim_on_wake_dream(self.root, owner, now=NOW + timedelta(minutes=2)))
            pending = claim_on_wake_dream(self.root, owner, now=NOW + timedelta(minutes=30))
            self.assertEqual(pending["dream_date"], LATEST)
            result = dream_commit_result(self.root, owner, LATEST, pending["claim_token"], f"{owner} on_wake dream",
                                         now=NOW + timedelta(minutes=31))
            self.assertEqual(result["status"], "committed")
            self.assertEqual(self.dream(owner)["runner"], "on_wake")
        self.assertEqual(len(self.commits()), 2)

    def test_one_owner_infrastructure_error_does_not_block_other(self) -> None:
        self.write_config(claude={"dream_mode": "on_wake", "dream_preferred_runner": "cli",
                                  "dream_cli_profile": "not-a-profile", "dream_cli_executable": "x"})
        results, code = run_batch(self.root, ["claude", "gpt"], timezone_name=TZ, now=NOW,
                                  runners={"gpt": self.runners()["gpt"]}, clock=lambda: NOW)
        out = self.by_owner(results)
        self.assertEqual(code, EXIT_INFRASTRUCTURE)
        self.assertEqual((out["claude"]["status"], out["gpt"]["status"]), ("error", "committed"))


class FailureClassTests(Base):
    def assert_fallback(self, mode: str, expected: str, *, profile: str = "claude", timeout: int = 60) -> dict:
        self.set_mode(profile, mode)
        owner = "claude" if profile == "claude" else "gpt"
        results, code = self.run_batch(owners=(owner,), timeout=timeout)
        event = results[0]
        self.assertEqual(code, EXIT_ALL_FALLBACK)
        self.assertEqual(event["status"], "fallback")
        self.assertEqual(event["claims"][0]["error_class"], expected)
        self.assertIsNone(self.dream(owner))
        self.assertEqual(self.commits(), [])
        self.assertTrue(PreparedDreamStore(self.root).path_for(owner, LATEST).exists())
        return event

    def test_auth_failure(self) -> None:
        self.assert_fallback("auth", "auth_failure")
        self.assert_fallback("auth", "auth_failure", profile="codex")

    def test_quota_and_nonzero(self) -> None:
        self.assert_fallback("quota", "quota_exhausted")
        self.assert_fallback("nonzero", "nonzero_exit", profile="codex")

    def test_claude_reported_error_in_json(self) -> None:
        self.assert_fallback("is_error", "quota_exhausted")

    def test_empty_malformed_and_oversized_output(self) -> None:
        for profile in ("claude", "codex"):
            for mode, expected in (("empty", "empty_output"), ("malformed", "malformed_output"),
                                   ("too_long", "output_too_long")):
                with self.subTest(profile=profile, mode=mode):
                    self.tearDown(); self.setUp()
                    self.assert_fallback(mode, expected, profile=profile)

    def test_executable_missing_and_launch_failure(self) -> None:
        self.write_config(claude={"dream_mode": "on_wake", "dream_preferred_runner": "cli",
                                  "dream_cli_profile": "claude_code",
                                  "dream_cli_executable": str(self.bin / "no-such-claude.exe")})
        results, code = run_batch(self.root, ["claude"], timezone_name=TZ, now=NOW, clock=lambda: NOW)
        self.assertEqual(results[0]["claims"][0]["error_class"], "executable_missing")
        self.assertEqual(code, EXIT_ALL_FALLBACK)
        runner = PreferredCliRunner("codex", str(self.fake), 60, 8000, (str(self.bin / "missing-launcher.exe"),))
        with self.assertRaises(DreamCliFailure) as raised:
            runner.generate({"owner": "gpt", "materials": []}, "instruction")
        self.assertEqual(raised.exception.error_class, "launch_failure")

    def test_timeout_kills_child_process_tree(self) -> None:
        started = time.monotonic()
        self.assert_fallback("timeout", "timeout", timeout=3)
        self.assertLess(time.monotonic() - started, 60)
        pid = int((self.bin / "grandchild-claude.pid").read_text())
        deadline = time.monotonic() + 10
        while process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertFalse(process_alive(pid), "grandchild must be terminated with the CLI")

    def test_logs_hold_only_safe_fields(self) -> None:
        self.set_mode("claude", "auth")
        self.set_mode("codex", "malformed")
        self.run_batch()
        text = self.log_text()
        self.assertIn("auth_failure", text)
        self.assertIn("malformed_output", text)
        for secret in ("SECRET-STDERR", "SECRET-STDOUT", SECRET_MATERIAL, "claim_token", "Not logged in"):
            self.assertNotIn(secret, text)
        for line in text.splitlines():
            self.assertTrue(set(json.loads(line)) <= set(dream_nightly.LOG_FIELDS))

    def test_no_api_runner_is_ever_used(self) -> None:
        self.set_mode("claude", "auth")
        with patch("dreams.ApiRunner", side_effect=AssertionError("API used")), \
                patch("dream_nightly.run_configured_dream", side_effect=AssertionError("cli/api mode used")):
            results, code = self.run_batch()
        self.assertEqual(self.by_owner(results)["claude"]["status"], "fallback")
        self.assertEqual(code, EXIT_PARTIAL_FALLBACK)


class IdempotencyTests(Base):
    def test_already_committed_is_skipped_without_calling_cli(self) -> None:
        self.run_batch()
        before = (len(self.calls("claude")), len(self.calls("codex")))
        results, code = self.run_batch()
        self.assertEqual(code, EXIT_OK)
        self.assertEqual({e["status"] for e in results}, {"already_exists"})
        self.assertEqual((len(self.calls("claude")), len(self.calls("codex"))), before)
        self.assertEqual(len(self.commits()), 2)

    def test_repeated_scheduler_runs_are_idempotent(self) -> None:
        for _ in range(3):
            self.run_batch(now=NOW + timedelta(hours=1))
        self.assertEqual(len(self.commits()), 2)
        self.assertEqual(len(list((self.root / "dreams" / "claude" / "2026-09").glob("*.md"))), 1)
        self.assertEqual(len(self.calls("claude")), 1)

    def test_existing_packet_is_reused(self) -> None:
        packet, _ = PreparedDreamStore(self.root).prepare(DreamPreparer(self.root), "claude", LATEST,
                                                           timezone_name=TZ, now=NOW)
        self.run_batch(owners=("claude",))
        self.assertEqual(self.commits("claude")[0][2], packet["generation_id"])

    def test_crash_after_commit_rerun_detects_commit(self) -> None:
        real = dream_nightly.commit_claimed_dream

        def commit_then_crash(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError("process died after commit")

        with patch("dream_nightly.commit_claimed_dream", side_effect=commit_then_crash):
            results, code = self.run_batch(owners=("claude",))
        self.assertEqual((results[0]["status"], code), ("error", EXIT_INFRASTRUCTURE))
        results, code = self.run_batch(owners=("claude",))
        self.assertEqual((results[0]["status"], code), ("already_exists", EXIT_OK))
        self.assertEqual(len(self.commits("claude")), 1)
        self.assertEqual(len(self.calls("claude")), 1)

    def test_stale_packet_after_commit_creates_no_second_dream(self) -> None:
        self.run_batch(owners=("claude",))
        dated = self.root / "dreams" / "claude" / "2026-09" / f"{LATEST}.md"
        before = dated.read_bytes()
        PreparedDreamStore(self.root).prepare(DreamPreparer(self.root), "claude", LATEST, timezone_name=TZ, now=NOW)
        results, code = self.run_batch(owners=("claude",))
        self.assertEqual(results[0]["status"], "already_exists")
        self.assertEqual(dated.read_bytes(), before)
        self.assertEqual(len(self.commits("claude")), 1)

    def test_current_packet_after_fallback_is_retried_on_next_run(self) -> None:
        self.set_mode("claude", "nonzero")
        self.run_batch(owners=("claude",))
        generation = json.loads(PreparedDreamStore(self.root).path_for("claude", LATEST).read_text())["generation_id"]
        self.set_mode("claude", "success")
        results, code = self.run_batch(owners=("claude",), now=NOW + timedelta(minutes=30))
        self.assertEqual((results[0]["status"], code), ("committed", EXIT_OK))
        self.assertEqual(self.commits("claude")[0][2], generation)

    def test_grace_day_then_latest_at_most_two_and_older_untouched(self) -> None:
        _record(self.root, "claude-grace", "claude", GRACE)
        _record(self.root, "claude-old", "claude", "2026-09-25")
        store = PreparedDreamStore(self.root)
        store.prepare(DreamPreparer(self.root), "claude", GRACE, timezone_name=TZ, now=NOW)
        store.prepare(DreamPreparer(self.root), "claude", "2026-09-25", timezone_name=TZ, now=NOW)
        results, _ = self.run_batch(owners=("claude",))
        self.assertEqual([c["dream_date"] for c in results[0]["claims"]], [GRACE, LATEST])
        self.assertEqual([row[1] for row in self.commits("claude")], [GRACE, LATEST])
        self.assertTrue(store.path_for("claude", "2026-09-25").exists(), "beyond grace: left as is")
        self.assertIsNone(self.dream("claude", "2026-09-25"))

    def test_concurrent_runs_are_serialized(self) -> None:
        lock = self.root / "state" / ".dream-nightly.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        from dreams import _cross_process_file_lock

        with _cross_process_file_lock(lock):
            results, code = self.run_batch()
        self.assertEqual((results[0]["status"], code), ("busy", 3))
        self.assertFalse((self.root / "dreams").exists())
        self.assertEqual((self.calls("claude"), self.calls("codex")), ([], []))


class ConfigDryRunAndEntryTests(Base):
    def test_config_validation(self) -> None:
        cases = [
            {"dream_mode": "cli", "dream_preferred_runner": "cli", "dream_cli_profile": "codex",
             "dream_cli_executable": "codex"},
            {"dream_mode": "on_wake", "dream_preferred_runner": "api"},
            {"dream_mode": "on_wake", "dream_preferred_runner": "cli", "dream_cli_profile": "codex",
             "dream_cli_executable": "codex", "dream_timeout_seconds": MAX_PREFERRED_CLI_TIMEOUT_SECONDS + 1},
            {"dream_mode": "on_wake", "dream_cli_profile": "codex"},
        ]
        for values in cases:
            with self.subTest(values=values):
                self.write_config(gpt=values)
                with self.assertRaises(ValueError):
                    load_owner_config(self.root, "gpt")
        self.write_config()
        config = load_owner_config(self.root, "gpt")
        self.assertEqual((config.dream_mode, config.dream_preferred_runner, config.dream_cli_profile),
                         ("on_wake", "cli", "codex"))

    def test_on_wake_owner_without_cli_keeps_old_nightly_behavior(self) -> None:
        self.write_config(gpt={"dream_mode": "on_wake"})
        results, code = self.run_batch(owners=("gpt",))
        self.assertEqual((results[0]["action"], results[0]["status"], code), ("prepare", "created", EXIT_OK))
        self.assertEqual(self.calls("codex"), [])

    def test_resolve_executable_glob_picks_newest(self) -> None:
        older, newer = self.bin / "v1" / "tool.exe", self.bin / "v2" / "tool.exe"
        for path in (older, newer):
            path.parent.mkdir()
            path.write_bytes(b"")
        os.utime(older, (time.time() - 100, time.time() - 100))
        self.assertEqual(resolve_executable(str(self.bin / "v*" / "tool.exe")), str(newer))
        self.assertIsNone(resolve_executable(str(self.bin / "nothing*" / "tool.exe")))

    def test_dry_run_reports_without_side_effects(self) -> None:
        PreparedDreamStore(self.root).prepare(DreamPreparer(self.root), "claude", LATEST, timezone_name=TZ, now=NOW)
        before = sorted(str(p) for p in self.root.rglob("*") if p.is_file() and ".git" not in p.parts)
        report = {item["owner"]: item for item in dry_run(self.root, ["claude", "gpt"], timezone_name=TZ, now=NOW)}
        after = sorted(str(p) for p in self.root.rglob("*") if p.is_file() and ".git" not in p.parts)
        self.assertEqual(before, after)
        self.assertEqual(report["claude"]["selected_runner"], "cli:claude_code")
        self.assertTrue(report["claude"]["cli_available"])
        self.assertTrue(report["claude"]["dates"][LATEST]["packet_exists"])
        self.assertEqual(report["claude"]["would_claim_first"], LATEST)
        self.assertEqual(self.calls("claude"), [])

    @unittest.skipUnless(os.name == "nt", "Windows Task Scheduler entry script")
    def test_entry_script_exit_codes(self) -> None:
        script = SCRIPTS / "run-nightly-dreams.ps1"

        def run(owners: str) -> int:
            return subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                 "-Owners", owners, "-Timezone", TZ, "-ProjectRoot", str(self.root), "-Python", sys.executable],
                capture_output=True, timeout=300,
            ).returncode

        self.write_config(claude={"dream_mode": "on_wake"}, gpt={"dream_mode": "on_wake"})
        self.assertEqual(run("claude,gpt"), EXIT_OK)
        missing = {"dream_mode": "on_wake", "dream_preferred_runner": "cli", "dream_cli_profile": "codex",
                   "dream_cli_executable": str(self.bin / "absent.exe")}
        self.write_config(claude={"dream_mode": "on_wake"}, gpt=missing)
        self.assertEqual(run("claude,gpt"), EXIT_ALL_FALLBACK)
        (self.root / "owner-config.json").write_text("{ broken", encoding="utf-8")
        self.assertEqual(run("claude,gpt"), EXIT_INFRASTRUCTURE)


if __name__ == "__main__":
    unittest.main()
