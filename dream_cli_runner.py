"""Preferred local-CLI Dream generation for on_wake owners.

The nightly runner acts as a wake on the owner's behalf: it claims through the
existing on_wake protocol, asks a fixed local CLI to write the dream from the
prepared materials only, and commits with the claim token. Any CLI problem is a
fallback, never a fake dream: the claim's lease simply expires and a later
on_wake can claim the same date.

Logs and errors carry only an error class, never CLI output or materials.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CLI_PROFILES = frozenset({"claude_code", "codex"})
# Below the 10-minute claim lease so a generation can never outlive its claim.
MAX_PREFERRED_CLI_TIMEOUT_SECONDS = 480
DEFAULT_PREFERRED_CLI_TIMEOUT_SECONDS = 300
PROMPT_FIELDS = ("owner", "dream_date", "timezone", "generation_id", "truncated", "materials")

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_AUTH_RE = re.compile(
    r"not logged in|log ?in required|please (log|sign) ?in|unauthori[sz]ed|authenticat|\b401\b|"
    r"invalid (api )?key|token (has )?expired|re-?login",
    re.IGNORECASE,
)
_QUOTA_RE = re.compile(
    r"quota|usage limit|rate.?limit|\b429\b|limit reached|credit balance|out of credits|"
    r"weekly limit|exceeded your|too many requests",
    re.IGNORECASE,
)


class DreamCliFailure(Exception):
    """A CLI generation failure. ``error_class`` is the only detail ever recorded."""

    ERROR_CLASSES = frozenset({
        "executable_missing", "launch_failure", "auth_failure", "quota_exhausted",
        "nonzero_exit", "timeout", "empty_output", "malformed_output", "output_too_long",
    })

    def __init__(self, error_class: str) -> None:
        if error_class not in self.ERROR_CLASSES:
            raise ValueError(f"unknown CLI error class: {error_class}")
        super().__init__(f"dream CLI failure: {error_class}")
        self.error_class = error_class


def resolve_executable(spec: str) -> str | None:
    """Resolve an absolute path, a PATH name, or a glob (newest match by mtime)."""
    expanded = os.path.expandvars(os.path.expanduser(spec))
    if any(ch in expanded for ch in "*?["):
        matches = [path for path in glob.glob(expanded) if os.path.isfile(path)]
        return max(matches, key=os.path.getmtime) if matches else None
    if os.path.isabs(expanded):
        return expanded if os.path.isfile(expanded) else None
    return shutil.which(expanded)


def prompt_package(pending: dict[str, Any]) -> dict[str, Any]:
    """Only the prepared materials and their framing; never claim tokens or lease data."""
    package = {key: pending[key] for key in PROMPT_FIELDS if key in pending}
    package["derived"] = True
    package["factual_authority"] = False
    return package


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        try:
            os.killpg(proc.pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _run(argv: list[str], prompt: str, *, cwd: str, timeout: int) -> tuple[int, str, str]:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, shell=False, **kwargs,
        )
    except OSError as exc:
        raise DreamCliFailure("launch_failure") from exc
    try:
        stdout, stderr = proc.communicate(prompt.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        raise DreamCliFailure("timeout") from exc
    return proc.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


def _classify_failure(*texts: str) -> str:
    joined = "\n".join(text for text in texts if text)
    if _AUTH_RE.search(joined):
        return "auth_failure"
    if _QUOTA_RE.search(joined):
        return "quota_exhausted"
    return "nonzero_exit"


def _clean(text: str, max_chars: int) -> str:
    body = _ANSI_RE.sub("", text).replace("\r\n", "\n").strip()
    if not body:
        raise DreamCliFailure("empty_output")
    if len(body) > max_chars:
        raise DreamCliFailure("output_too_long")
    return body


@dataclass(frozen=True)
class PreferredCliRunner:
    profile: str
    executable: str
    timeout_seconds: int = DEFAULT_PREFERRED_CLI_TIMEOUT_SECONDS
    max_output_chars: int = 8000
    launcher: tuple[str, ...] = ()  # tests run a script stand-in; empty in production

    def __post_init__(self) -> None:
        if self.profile not in CLI_PROFILES:
            raise ValueError(f"unknown dream CLI profile: {self.profile!r}")
        if not 1 <= int(self.timeout_seconds) <= MAX_PREFERRED_CLI_TIMEOUT_SECONDS:
            raise ValueError("preferred CLI timeout must stay below the claim lease")

    @property
    def name(self) -> str:
        return f"cli:{self.profile}"

    def available(self) -> bool:
        return resolve_executable(self.executable) is not None

    def generate(self, package: dict[str, Any], instruction: str) -> str:
        binary = resolve_executable(self.executable)
        if binary is None:
            raise DreamCliFailure("executable_missing")
        prompt = instruction + "\n\nINPUT PACKAGE (JSON):\n" + json.dumps(
            prompt_package(package), ensure_ascii=False, sort_keys=True
        )
        with tempfile.TemporaryDirectory(prefix="dream-cli-") as workdir:
            if self.profile == "claude_code":
                return self._claude(binary, prompt, workdir)
            return self._codex(binary, prompt, workdir)

    def _claude(self, binary: str, prompt: str, workdir: str) -> str:
        # No tools, no MCP servers, no user/project settings or instruction files.
        argv = [*self.launcher, binary, "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config",
                "--setting-sources", "", "--no-session-persistence"]
        code, stdout, stderr = _run(argv, prompt, cwd=workdir, timeout=self.timeout_seconds)
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            data = None
        if code != 0:
            hint = json.dumps(data.get("result")) if isinstance(data, dict) else ""
            raise DreamCliFailure(_classify_failure(stderr, hint, stdout[:2000]))
        if not isinstance(data, dict) or data.get("type") != "result":
            raise DreamCliFailure("malformed_output")
        result = data.get("result")
        if data.get("is_error") or data.get("subtype") != "success":
            raise DreamCliFailure(_classify_failure(str(result or ""), str(data.get("api_error_status") or "")))
        if not isinstance(result, str):
            raise DreamCliFailure("malformed_output")
        return _clean(result, self.max_output_chars)

    def _codex(self, binary: str, prompt: str, workdir: str) -> str:
        out_file = Path(workdir) / "last-message.txt"
        # Read-only sandbox, no user config/rules, nothing persisted; final message to a file.
        argv = [*self.launcher, binary, "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "--sandbox", "read-only", "--skip-git-repo-check", "--color", "never",
                "-o", str(out_file), "-"]
        code, stdout, stderr = _run(argv, prompt, cwd=workdir, timeout=self.timeout_seconds)
        if code != 0:
            raise DreamCliFailure(_classify_failure(stderr, stdout[-2000:]))
        if not out_file.is_file():
            raise DreamCliFailure("malformed_output")
        return _clean(out_file.read_text(encoding="utf-8", errors="replace"), self.max_output_chars)


def runner_for(config: Any, *, max_output_chars: int) -> PreferredCliRunner:
    return PreferredCliRunner(
        profile=config.dream_cli_profile,
        executable=config.dream_cli_executable,
        timeout_seconds=config.dream_timeout_seconds,
        max_output_chars=max_output_chars,
    )

