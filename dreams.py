from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import re
import secrets
import sqlite3
import subprocess
import tempfile
import time as time_module
import unicodedata
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Protocol, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from memory_feedback import VALID_SOURCES, VALID_VERDICTS
from memory_store import MemoryStore, _read_memory_text, _with_sharing_retry
from dream_scraps import DreamScrapStore
from memory_witness import MemoryWitnessStore


log = logging.getLogger("dreams")
SCHEMA_VERSION = 1
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_DREAM_MODE = "on_wake"
OWNER_CONFIG_FILENAME = "owner-config.json"
DREAM_RUNTIME_DB = Path("state") / "dream-runtime.sqlite3"
DREAM_PREPARED_DIR = Path("state") / "dream-prepared"
DREAM_MODES = frozenset({"cli", "api", "on_wake"})
DEFAULT_DREAM_LEASE_TTL = timedelta(minutes=10)
PENDING_DREAM_INSTRUCTION = (
    "Using only the supplied materials, organize a concise dream body for this date. "
    "You may compress, reorganize, and make natural associations, but do not add facts absent from the materials; "
    "do not upgrade inferred content into user statements or observations; do not create relationship conclusions, "
    "open items, or long-term memories; and do not modify source memories or write the dream into ordinary memory. "
    "This dream is derived shadow material and is not a factual source. Do not role-play, introduce a project persona, "
    "or pursue poetic language. Materials marked ephemeral_scrap are short-lived associative fragments, never facts; "
    "do not expand them into factual claims. Return only the dream body suitable for dream_commit."
)
OWNER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
QUERY_WORD_RE = re.compile(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]+")


@dataclass(frozen=True)
class DreamConfig:
    new_limit: int = 6
    retrieval_limit: int = 6
    historical_limit: int = 6
    dream_cooldown_days: int = 7
    recent_history_days: int = 30
    historical_category_cap: int = 2
    historical_older_slots: int = 1
    total_chars: int = 6000
    item_chars: int = 1200
    max_dream_chars: int = 8000
    wake_chars: int = 1200
    runner_timeout_seconds: int = 180
    episode_cap_per_memory: int = 3
    retrieval_bucket_minutes: int = 30
    scheduled_time: str = "02:30"
    scrap_max_per_dream: int = 1


DEFAULT_CONFIG = DreamConfig()


DreamMode = Literal["cli", "api", "on_wake"]


@dataclass(frozen=True)
class OwnerConfig:
    owner: str
    dream_mode: DreamMode = DEFAULT_DREAM_MODE
    dream_cli: tuple[str, ...] | None = None
    dream_api_adapter: str | None = None
    dream_api_base_url: str | None = None
    dream_api_key_env: str | None = None
    dream_model: str | None = None
    dream_timeout_seconds: int = DEFAULT_CONFIG.runner_timeout_seconds
    dream_preferred_runner: str | None = None
    dream_cli_profile: str | None = None
    dream_cli_executable: str | None = None
    dream_scraps_enabled: bool = True
    dream_scrap_ttl_hours: int = 72
    dream_scrap_max_per_dream: int = 1
    independent_witness_enabled: bool = True
    passive_recall_enabled: bool = True


@dataclass(frozen=True)
class DreamClaim:
    owner: str
    dream_date: str
    claim_token: str
    claimed_at: str
    expires_at: str


class DreamCommitError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def get_timezone(name: str):
    """Resolve an IANA zone, with a dependency-free Windows fallback for the default."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name)
        if name in {"UTC", "Etc/UTC"}:
            return timezone.utc
        raise ValueError(f"timezone data is unavailable for {name!r}")


def validate_owner(owner: str) -> str:
    owner = str(owner or "").strip().lower()
    if not OWNER_RE.fullmatch(owner):
        raise ValueError("owner must match ^[a-z0-9][a-z0-9_-]{0,63}$")
    return owner


def load_owner_config(project_root: str | Path, owner: str) -> OwnerConfig:
    """Read one owner's validated settings without changing pipeline behavior."""
    owner = validate_owner(owner)
    path = Path(project_root).resolve() / OWNER_CONFIG_FILENAME
    if not path.is_file():
        return OwnerConfig(owner=owner)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid owner configuration: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError("owner configuration must be a JSON object")
    owners = raw.get("owners", {})
    if not isinstance(owners, dict):
        raise ValueError("owner configuration 'owners' must be a JSON object")
    values = owners.get(owner, {})
    if not isinstance(values, dict):
        raise ValueError(f"owner configuration for {owner!r} must be a JSON object")
    mode = values.get("dream_mode", DEFAULT_DREAM_MODE)
    if not isinstance(mode, str) or mode not in DREAM_MODES:
        allowed = ", ".join(sorted(DREAM_MODES))
        raise ValueError(f"invalid dream_mode for owner {owner!r}: {mode!r}; expected one of: {allowed}")
    cli = values.get("dream_cli")
    if cli is not None and (
        not isinstance(cli, list) or not cli or not all(isinstance(item, str) and item for item in cli)
    ):
        raise ValueError(f"dream_cli for owner {owner!r} must be a non-empty array of strings")
    api_adapter = values.get("dream_api_adapter")
    if api_adapter is not None and (not isinstance(api_adapter, str) or not api_adapter):
        raise ValueError(f"dream_api_adapter for owner {owner!r} must be a non-empty string")
    api_base_url = values.get("dream_api_base_url")
    if api_base_url is not None and (
        not isinstance(api_base_url, str) or not api_base_url.startswith(("https://", "http://localhost", "http://127.0.0.1"))
    ):
        raise ValueError(f"dream_api_base_url for owner {owner!r} must be HTTPS or a localhost URL")
    api_key_env = values.get("dream_api_key_env")
    if api_key_env is not None and (
        not isinstance(api_key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env)
    ):
        raise ValueError(f"dream_api_key_env for owner {owner!r} must be an environment variable name")
    model = values.get("dream_model")
    if model is not None and (not isinstance(model, str) or not model):
        raise ValueError(f"dream_model for owner {owner!r} must be a non-empty string or null")
    timeout = values.get("dream_timeout_seconds", DEFAULT_CONFIG.runner_timeout_seconds)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ValueError(f"dream_timeout_seconds for owner {owner!r} must be a positive integer")
    scraps_enabled = values.get("dream_scraps_enabled", True)
    witness_enabled = values.get("independent_witness_enabled", True)
    passive_enabled = values.get("passive_recall_enabled", True)
    if not all(isinstance(value, bool) for value in (scraps_enabled, witness_enabled, passive_enabled)):
        raise ValueError("dream scraps, witness, and passive recall flags must be booleans")
    preferred = values.get("dream_preferred_runner")
    cli_profile = values.get("dream_cli_profile")
    cli_executable = values.get("dream_cli_executable")
    if preferred is not None:
        from dream_cli_runner import (
            CLI_PROFILES, DEFAULT_PREFERRED_CLI_TIMEOUT_SECONDS, MAX_PREFERRED_CLI_TIMEOUT_SECONDS,
        )

        if preferred != "cli":
            raise ValueError(f"dream_preferred_runner for owner {owner!r} must be 'cli' or null")
        if mode != "on_wake":
            raise ValueError("dream_preferred_runner requires dream_mode=on_wake so on_wake stays the fallback")
        if cli_profile not in CLI_PROFILES:
            raise ValueError(f"dream_cli_profile for owner {owner!r} must be one of: {', '.join(sorted(CLI_PROFILES))}")
        if not isinstance(cli_executable, str) or not cli_executable.strip():
            raise ValueError(f"dream_cli_executable for owner {owner!r} must be a non-empty string")
        if "dream_timeout_seconds" not in values:
            timeout = DEFAULT_PREFERRED_CLI_TIMEOUT_SECONDS
        if timeout > MAX_PREFERRED_CLI_TIMEOUT_SECONDS:
            raise ValueError(f"dream_timeout_seconds for a preferred CLI must be at most {MAX_PREFERRED_CLI_TIMEOUT_SECONDS}")
    elif cli_profile is not None or cli_executable is not None:
        raise ValueError(f"dream_cli_profile/dream_cli_executable for owner {owner!r} require dream_preferred_runner='cli'")
    scrap_ttl = values.get("dream_scrap_ttl_hours", 72)
    scrap_max = values.get("dream_scrap_max_per_dream", 1)
    if isinstance(scrap_ttl, bool) or not isinstance(scrap_ttl, int) or not 24 <= scrap_ttl <= 168:
        raise ValueError("dream_scrap_ttl_hours must be between 24 and 168")
    if isinstance(scrap_max, bool) or scrap_max not in {0, 1}:
        raise ValueError("dream_scrap_max_per_dream must be 0 or 1")
    return OwnerConfig(
        owner=owner,
        dream_mode=cast(DreamMode, mode),
        dream_cli=tuple(cli) if cli is not None else None,
        dream_api_adapter=api_adapter,
        dream_api_base_url=api_base_url,
        dream_api_key_env=api_key_env,
        dream_model=model,
        dream_timeout_seconds=timeout,
        dream_preferred_runner=preferred,
        dream_cli_profile=cli_profile,
        dream_cli_executable=cli_executable,
        dream_scraps_enabled=scraps_enabled,
        dream_scrap_ttl_hours=scrap_ttl,
        dream_scrap_max_per_dream=scrap_max,
        independent_witness_enabled=witness_enabled,
        passive_recall_enabled=passive_enabled,
    )


def _utc_now(now: datetime | None = None) -> datetime:
    value = datetime.now(timezone.utc) if now is None else now
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("lease time must be timezone-aware")
    return value.astimezone(timezone.utc)


@contextmanager
def _cross_process_file_lock(path: Path, *, blocking: bool = True):
    """Crash-safe OS lock on one byte of a persistent, non-secret lock file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            msvcrt.locking(stream.fileno(), mode, 1)
        else:
            import fcntl
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            fcntl.flock(stream.fileno(), flags)
        yield
    finally:
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def _utc_iso(value: datetime) -> str:
    return _utc_now(value).isoformat().replace("+00:00", "Z")


def most_recent_complete_dream_date(now: datetime, timezone_name: str = DEFAULT_TIMEZONE) -> str:
    """Return the previous local calendar day using an aware instant."""
    local_now = _utc_now(now).astimezone(get_timezone(timezone_name))
    return (local_now.date() - timedelta(days=1)).isoformat()


def _runtime_commit_package(package: dict[str, Any]) -> dict[str, Any]:
    """Keep only metadata needed to build/repair a committed dream, never material bodies."""
    return {
        "schema_version": package["schema_version"],
        "owner": package["owner"],
        "dream_date": package["dream_date"],
        "timezone": package["timezone"],
        "generation_id": package["generation_id"],
        "source_memory_ids": package["source_memory_ids"],
        "source_event_range": package["source_event_range"],
        "truncated": package["truncated"],
    }


class DreamLeaseStore:
    """Cross-process runtime leases for one owner and dream date."""

    def __init__(
        self,
        project_root: str | Path,
        db_path: str | Path | None = None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.db_path = Path(db_path) if db_path is not None else self.project_root / DREAM_RUNTIME_DB
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))

    _INIT_LOCK_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16)

    @staticmethod
    def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
        message = str(exc).lower()
        return "database is locked" in message or "database is busy" in message

    def _connect_once(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.db_path), timeout=self.busy_timeout_ms / 1000)
        try:
            db.row_factory = sqlite3.Row
            # Apply the handler before WAL/schema work; sqlite's connect timeout alone
            # does not reliably cover the first journal-mode transition on Windows.
            db.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            db.execute("PRAGMA journal_mode = WAL")
            db.execute(
                """CREATE TABLE IF NOT EXISTS dream_leases (
                       owner TEXT NOT NULL,
                       dream_date TEXT NOT NULL,
                       claim_token TEXT NOT NULL,
                       claimed_at TEXT NOT NULL,
                       expires_at TEXT NOT NULL,
                       package_json TEXT,
                       PRIMARY KEY (owner, dream_date)
                   )"""
            )
            columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(dream_leases)")}
            if "package_json" not in columns:
                db.execute("ALTER TABLE dream_leases ADD COLUMN package_json TEXT")
            db.execute(
                """CREATE TABLE IF NOT EXISTS dream_commits (
                       owner TEXT NOT NULL,
                       dream_date TEXT NOT NULL,
                       claim_token TEXT NOT NULL,
                       content_sha256 TEXT NOT NULL,
                       generation_id TEXT NOT NULL,
                       package_json TEXT NOT NULL,
                       committed_at TEXT NOT NULL,
                       PRIMARY KEY (owner, dream_date)
                   )"""
            )
            db.commit()
            return db
        except Exception:
            db.close()
            raise

    def _connect(self) -> sqlite3.Connection:
        for delay in (*self._INIT_LOCK_RETRY_DELAYS, None):
            try:
                return self._connect_once()
            except sqlite3.OperationalError as exc:
                if delay is None or not self._is_locked_error(exc):
                    raise
                time_module.sleep(delay)
        raise AssertionError("unreachable")

    @staticmethod
    def _claim_from_row(row: sqlite3.Row) -> DreamClaim:
        return DreamClaim(
            owner=str(row["owner"]),
            dream_date=str(row["dream_date"]),
            claim_token=str(row["claim_token"]),
            claimed_at=str(row["claimed_at"]),
            expires_at=str(row["expires_at"]),
        )

    def claim(
        self,
        owner: str,
        dream_date: str,
        ttl: timedelta,
        now: datetime | None = None,
        package: dict[str, Any] | None = None,
    ) -> DreamClaim | None:
        owner = validate_owner(owner)
        day = validate_dream_date(dream_date)
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
            raise ValueError("lease ttl must be a positive timedelta")
        claimed = _utc_now(now)
        expires = claimed + ttl
        token = secrets.token_urlsafe(32)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM dream_leases WHERE owner = ? AND dream_date = ?",
                (owner, day),
            ).fetchone()
            if row is not None and _parse_timestamp(row["expires_at"]).astimezone(timezone.utc) > claimed:
                db.rollback()
                return None
            db.execute(
                """INSERT INTO dream_leases
                   (owner, dream_date, claim_token, claimed_at, expires_at, package_json)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(owner, dream_date) DO UPDATE SET
                       claim_token = excluded.claim_token,
                       claimed_at = excluded.claimed_at,
                       expires_at = excluded.expires_at,
                       package_json = excluded.package_json""",
                (
                    owner, day, token, _utc_iso(claimed), _utc_iso(expires),
                    None if package is None else json.dumps(
                        _runtime_commit_package(package), ensure_ascii=False, sort_keys=True
                    ),
                ),
            )
            db.commit()
            return DreamClaim(owner, day, token, _utc_iso(claimed), _utc_iso(expires))
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def current(
        self,
        owner: str,
        dream_date: str,
        now: datetime | None = None,
    ) -> DreamClaim | None:
        owner = validate_owner(owner)
        day = validate_dream_date(dream_date)
        current = _utc_now(now)
        db = self._connect()
        try:
            row = db.execute(
                "SELECT * FROM dream_leases WHERE owner = ? AND dream_date = ?",
                (owner, day),
            ).fetchone()
        finally:
            db.close()
        if row is None or _parse_timestamp(row["expires_at"]).astimezone(timezone.utc) <= current:
            return None
        return self._claim_from_row(row)

    def recovery_state(
        self,
        owner: str,
        dream_date: str,
        now: datetime | None = None,
    ) -> Literal["none", "active", "expired", "committed"]:
        """Describe persisted history for the single date considered by grace recovery."""
        owner = validate_owner(owner)
        day = validate_dream_date(dream_date)
        current = _utc_now(now)
        db = self._connect()
        try:
            committed = db.execute(
                "SELECT 1 FROM dream_commits WHERE owner = ? AND dream_date = ?",
                (owner, day),
            ).fetchone()
            if committed is not None:
                return "committed"
            lease = db.execute(
                "SELECT expires_at FROM dream_leases WHERE owner = ? AND dream_date = ?",
                (owner, day),
            ).fetchone()
        finally:
            db.close()
        if lease is None:
            return "none"
        expires = _parse_timestamp(str(lease["expires_at"])).astimezone(timezone.utc)
        return "active" if expires > current else "expired"


def validate_dream_date(value: str | date) -> str:
    raw = value.isoformat() if isinstance(value, date) else str(value)
    if not DATE_RE.fullmatch(raw):
        raise ValueError("dream_date must be YYYY-MM-DD")
    date.fromisoformat(raw)
    return raw


def safe_dream_paths(project_root: str | Path, owner: str, dream_date: str) -> tuple[Path, Path]:
    root = Path(project_root).resolve()
    owner = validate_owner(owner)
    dream_date = validate_dream_date(dream_date)
    dream_root = (root / "dreams").resolve()
    owner_root = (dream_root / owner).resolve()
    month_root = (owner_root / dream_date[:7]).resolve()
    for candidate in (owner_root, month_root):
        try:
            candidate.relative_to(dream_root)
        except ValueError:
            raise ValueError("dream path escapes project dreams root")
    return owner_root / "current.md", month_root / f"{dream_date}.md"


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def local_day_bounds(day: str, timezone_name: str) -> tuple[datetime, datetime]:
    zone = get_timezone(timezone_name)
    local_date = date.fromisoformat(validate_dream_date(day))
    start = datetime.combine(local_date, time.min, zone)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def normalize_query_fingerprint(query: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(query or "")).casefold()
    tokens: list[str] = []
    for token in QUERY_WORD_RE.findall(normalized):
        tokens.append(token)
    canonical = " ".join(tokens)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def _ordered_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        value = str(value)
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _readonly_feedback(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.is_file():
        return None
    uri = db_path.resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only = ON")
    return db


def _feedback_for_events(
    db: sqlite3.Connection,
    retrieval_ids: list[str],
    owner: str,
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    if not retrieval_ids:
        return result
    placeholders = ",".join("?" for _ in retrieval_ids)
    rows = db.execute(
        f"SELECT memory_id, verdict, source FROM retrieval_feedback "
        f"WHERE agent_id=? AND retrieval_id IN ({placeholders})",
        (owner, *retrieval_ids),
    ).fetchall()
    for row in rows:
        if row["verdict"] not in VALID_VERDICTS or row["source"] not in VALID_SOURCES:
            log.warning("ignoring invalid feedback while preparing dream")
            continue
        if row["memory_id"]:
            result.setdefault(str(row["memory_id"]), set()).add(str(row["verdict"]))
    return result


def effective_retrievals(
    db: sqlite3.Connection,
    owner: str,
    day: str,
    timezone_name: str = DEFAULT_TIMEZONE,
    config: DreamConfig = DEFAULT_CONFIG,
) -> tuple[dict[str, int], list[str], dict[str, set[str]], dict[str, str | None]]:
    owner = validate_owner(owner)
    start, end = local_day_bounds(day, timezone_name)
    rows = db.execute(
        "SELECT retrieval_id, query, result_ids_json, created_at FROM retrieval_events "
        "WHERE agent_id=? AND created_at>=? AND created_at<? ORDER BY created_at, retrieval_id",
        (owner, start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")),
    ).fetchall()
    episodes: set[tuple[str, str, str, int]] = set()
    event_ids: list[str] = []
    event_times: list[str] = []
    for row in rows:
        event_ids.append(str(row["retrieval_id"]))
        event_times.append(str(row["created_at"]))
        fingerprint = normalize_query_fingerprint(str(row["query"] or ""))
        bucket = int(_parse_timestamp(row["created_at"]).timestamp()) // (config.retrieval_bucket_minutes * 60)
        try:
            memory_ids = json.loads(row["result_ids_json"])
        except (TypeError, json.JSONDecodeError):
            log.warning("ignoring malformed retrieval event %s", row["retrieval_id"])
            continue
        for memory_id in _ordered_unique(memory_ids):
            episodes.add((owner, memory_id, fingerprint, bucket))
    counts: dict[str, int] = {}
    for _, memory_id, _, _ in sorted(episodes):
        counts[memory_id] = min(config.episode_cap_per_memory, counts.get(memory_id, 0) + 1)
    feedback = _feedback_for_events(db, event_ids, owner)
    event_range = {
        "start": min(event_times) if event_times else None,
        "end": max(event_times) if event_times else None,
    }
    return counts, event_ids, feedback, event_range


def _score(reasons: list[str], effective_count: int, feedback: set[str]) -> float:
    value = 2.0 if "new" in reasons else 0.0
    value += math.log1p(effective_count) * 1.5
    if "used" in feedback:
        value += 3.0
    if "ignored" in feedback:
        value -= 0.25
    if "stale" in feedback:
        value -= 4.0
    return round(value, 6)


INACTIVE_LIFECYCLES = frozenset({"stale", "superseded"})
HISTORICAL_BLOCKING_VERDICTS = ("corrected", "stale")


def historical_feedback_blocks(db: sqlite3.Connection, owner: str, before: datetime) -> set[str]:
    """Memory ids this owner ever marked corrected or stale before ``before`` (read-only)."""
    try:
        rows = db.execute(
            "SELECT DISTINCT memory_id FROM retrieval_feedback WHERE agent_id=? AND verdict IN (?, ?) "
            "AND created_at<?",
            (owner, *HISTORICAL_BLOCKING_VERDICTS, before.isoformat().replace("+00:00", "Z")),
        ).fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[0]) for row in rows}


def dream_readable_corpus(project_root: str | Path, owner: str) -> list[dict[str, Any]]:
    """Memories a Dream for ``owner`` may draw on.

    That is the owner's own memories (agent or shared scope) plus every shared memory written by
    another identity. Another identity's agent-scope memories, human house manuals and anything
    that is not an ordinary memory record are never included. Records come from MemoryStore, with
    lifecycle normalized the same way recall sees it; nothing is modified.
    """
    owner = validate_owner(owner)
    corpus: list[dict[str, Any]] = []
    for row in MemoryStore(project_root, owner)._filtered("all", include_inactive=True):
        memory_id = str(row.get("id") or "")
        if not memory_id or not row.get("content") or memory_id.startswith("human:"):
            continue
        row_owner = str(row.get("owner") or "").strip().lower()
        scope = str(row.get("scope") or "").strip().lower()
        if row_owner == "human" or scope not in {"agent", "shared"}:
            continue
        if row_owner == owner or scope == "shared":
            corpus.append(row)
    return corpus


def recent_dream_sources(
    project_root: str | Path, owner: str, dream_date: str, days: int
) -> dict[str, dict[str, Any]]:
    """Source memory ids of this owner's committed Dreams on the ``days`` dates before ``dream_date``.

    Returns ``{memory_id: {"last": newest date it was dreamed, "count": dreams it appeared in}}``.
    Missing, unreadable or malformed Dream files are skipped: cooldown is a preference, never a
    reason for preparation to fail.
    """
    owner = validate_owner(owner)
    day = date.fromisoformat(validate_dream_date(dream_date))
    seen: dict[str, dict[str, Any]] = {}
    for offset in range(1, max(0, int(days)) + 1):
        previous = (day - timedelta(days=offset)).isoformat()
        try:
            _, dated = safe_dream_paths(project_root, owner, previous)
            if not dated.is_file():
                continue
            metadata = parse_dream(dated.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if metadata.get("owner") != owner:
            continue
        for memory_id in _ordered_unique(metadata.get("source_memory_ids", [])):
            entry = seen.setdefault(memory_id, {"last": previous, "count": 0})
            entry["count"] += 1
            entry["last"] = max(entry["last"], previous)
    return seen


class DreamPreparer:
    def __init__(
        self,
        project_root: str | Path,
        *,
        feedback_db: str | Path | None = None,
        config: DreamConfig = DEFAULT_CONFIG,
        scraps_enabled: bool = True,
        scrap_max_per_dream: int = 1,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.feedback_db = Path(feedback_db) if feedback_db else self.project_root / "state" / "memory-feedback.sqlite3"
        self.config = config
        self.scraps_enabled = bool(scraps_enabled)
        self.scrap_max_per_dream = scrap_max_per_dream

    def prepare(
        self,
        owner: str,
        *,
        dream_date: str | None = None,
        timezone_name: str = DEFAULT_TIMEZONE,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        owner = validate_owner(owner)
        zone = get_timezone(timezone_name)
        current = now.astimezone(zone) if now else datetime.now(zone)
        day = validate_dream_date(dream_date or current.date())
        start, end = local_day_bounds(day, timezone_name)
        records = [
            row for row in dream_readable_corpus(self.project_root, owner)
            if str(row.get("lifecycle") or "active") not in INACTIVE_LIFECYCLES
        ]
        records_by_id = {str(row["id"]): row for row in records}
        counts: dict[str, int] = {}
        feedback: dict[str, set[str]] = {}
        event_range: dict[str, str | None] = {"start": None, "end": None}
        history_blocked: set[str] = set()
        db = _readonly_feedback(self.feedback_db)
        try:
            if db is not None:
                counts, _, feedback, event_range = effective_retrievals(
                    db, owner, day, timezone_name, self.config
                )
                history_blocked = historical_feedback_blocks(db, owner, end)
        finally:
            if db is not None:
                db.close()

        def created(row: dict[str, Any]) -> datetime | None:
            try:
                return _parse_timestamp(str(row.get("created_at") or "")).astimezone(timezone.utc)
            except (TypeError, ValueError):
                return None

        def is_today(row: dict[str, Any]) -> bool:
            stamp = created(row)
            return stamp is not None and start <= stamp < end

        excluded = {memory_id for memory_id, values in feedback.items() if "corrected" in values}
        cooldown = recent_dream_sources(self.project_root, owner, day, self.config.dream_cooldown_days)

        def category(row: dict[str, Any]) -> str:
            return str(row.get("category") or "")

        new_rows = [row for row in records if is_today(row) and str(row["id"]) not in excluded]
        new_rows.sort(key=lambda row: (str(row.get("created_at") or ""), str(row["id"])))
        retrieval_rows = [
            records_by_id[memory_id] for memory_id in counts
            if memory_id in records_by_id and memory_id not in excluded
        ]
        retrieval_rows.sort(
            key=lambda row: (
                -_score([], counts.get(str(row["id"]), 0), feedback.get(str(row["id"]), set())),
                str(row["id"]),
            )
        )

        # Historical background: active, not an open item, created before this Dream day, never
        # corrected or marked stale. ``status`` may be missing or "done".
        recent_cutoff = start - timedelta(days=max(0, self.config.recent_history_days))
        historical_rows = [
            row for row in records
            if row.get("status") != "open"
            and str(row.get("lifecycle") or "active") == "active"
            and str(row["id"]) not in excluded
            and str(row["id"]) not in history_blocked
            and "stale" not in feedback.get(str(row["id"]), set())
            and (created(row) is None or created(row) < start)
        ]
        historical_ids = {str(row["id"]) for row in historical_rows}
        rng = random.Random(f"{owner}:{day}")

        def shuffled(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            ordered = sorted(rows, key=lambda row: str(row["id"]))
            rng.shuffle(ordered)
            return ordered

        fresh = [row for row in historical_rows if str(row["id"]) not in cooldown]
        recent_pool = shuffled([row for row in fresh if created(row) is not None and created(row) >= recent_cutoff])
        older_pool = shuffled([row for row in fresh if created(row) is None or created(row) < recent_cutoff])
        # Reuse after cooldown: longest since last dreamed first, then least often dreamed.
        cooled_pool = sorted(
            (row for row in historical_rows if str(row["id"]) in cooldown),
            key=lambda row: (cooldown[str(row["id"])]["last"], cooldown[str(row["id"])]["count"], str(row["id"])),
        )

        selected: list[dict[str, Any]] = []
        selected_ids: set[str] = set()

        def category_count(name: str) -> int:
            return sum(1 for item in selected if item.get("category") == name)

        def take(row: dict[str, Any], source_kind: str) -> None:
            memory_id = str(row["id"])
            reasons = [source_kind]
            if source_kind != "historical" and memory_id in historical_ids:
                reasons.append("historical")
            selected_ids.add(memory_id)
            selected.append({
                "id": memory_id,
                "category": row.get("category"),
                "created_at": row.get("created_at"),
                "status": row.get("status"),
                "source": row.get("source"),
                "reasons": reasons,
                "effective_retrieval_count": counts.get(memory_id, 0),
                "feedback": sorted(feedback.get(memory_id, set())),
                "score": _score(reasons, counts.get(memory_id, 0), feedback.get(memory_id, set())),
                "content": str(row.get("content") or ""),
            })

        def merge_reason(memory_id: str, source_kind: str) -> None:
            for item in selected:
                if item["id"] == memory_id and source_kind not in item["reasons"]:
                    item["reasons"].append(source_kind)
                    item["score"] = _score(item["reasons"], counts.get(memory_id, 0), feedback.get(memory_id, set()))

        # Today's new memories: highest priority, never held back by cooldown or category caps.
        added = 0
        for row in new_rows:
            if added >= self.config.new_limit:
                break
            take(row, "new")
            added += 1

        # Today's real retrievals: never excluded by cooldown; within one score, prefer memories not
        # dreamed recently and categories not yet present.
        for row in retrieval_rows:
            if str(row["id"]) in selected_ids:
                merge_reason(str(row["id"]), "retrieved")
        pending = [row for row in retrieval_rows if str(row["id"]) not in selected_ids]
        added = 0
        while pending and added < self.config.retrieval_limit:
            best_score = -_score([], counts.get(str(pending[0]["id"]), 0), feedback.get(str(pending[0]["id"]), set()))
            group = [row for row in pending
                     if -_score([], counts.get(str(row["id"]), 0), feedback.get(str(row["id"]), set())) == best_score]
            choice = min(group, key=lambda row: (str(row["id"]) in cooldown, category_count(category(row)), str(row["id"])))
            pending.remove(choice)
            take(choice, "retrieved")
            added += 1

        # Historical background with per-category cap: recent (last N days) first, one slot kept for
        # an older memory, then progressively relax: cap off, then memories dreamed during cooldown.
        limit = max(0, self.config.historical_limit)
        cap = max(1, self.config.historical_category_cap)
        historical_added = 0

        def fill(pool: list[dict[str, Any]], quota: int, capped: bool) -> None:
            nonlocal historical_added
            for row in pool:
                if historical_added >= limit or quota <= 0:
                    return
                memory_id = str(row["id"])
                if memory_id in selected_ids:
                    continue
                if capped and category_count(category(row)) >= cap:
                    continue
                take(row, "historical")
                historical_added += 1
                quota -= 1

        older_reserved = min(max(0, self.config.historical_older_slots), limit) if older_pool else 0
        fill(recent_pool, limit - older_reserved, capped=True)
        fill(older_pool, limit, capped=True)
        fill(recent_pool, limit, capped=True)
        fill(recent_pool + older_pool, limit, capped=False)
        fill(cooled_pool, limit, capped=True)
        fill(cooled_pool, limit, capped=False)

        used_chars = 0
        truncated = False
        bounded: list[dict[str, Any]] = []
        for item in selected:
            available = self.config.total_chars - used_chars
            if available <= 0:
                truncated = True
                break
            content = item["content"]
            allowance = min(self.config.item_chars, available)
            if len(content) > allowance:
                content = content[: max(0, allowance - 1)] + "…"
                truncated = True
            bounded.append({**item, "content": content})
            used_chars += len(content)
        if self.scraps_enabled and self.scrap_max_per_dream == 1:
            scraps = DreamScrapStore(self.project_root).eligible(
                owner, cutoff=end, now=current
            )
            if scraps and used_chars < self.config.total_chars:
                scrap_rng = random.Random(f"scrap:{owner}:{day}")
                scrap = scraps[scrap_rng.randrange(len(scraps))]
                available = min(self.config.item_chars, self.config.total_chars - used_chars)
                scrap_content = str(scrap["content"])
                if len(scrap_content) > available:
                    scrap_content = scrap_content[: max(0, available - 1)] + "…"
                    truncated = True
                bounded.append({
                    "id": f"scrap:{scrap['scrap_id']}",
                    "material_source": "ephemeral_scrap",
                    "content": scrap_content,
                    "created_at": scrap["created_at"],
                    "derived": True,
                    "ephemeral": True,
                    "factual_authority": False,
                    "score": -1.0,
                    "reasons": ["ephemeral_scrap"],
                })
        source_ids = _ordered_unique(
            item["id"] for item in bounded if item.get("material_source") != "ephemeral_scrap"
        )
        digest_material = {
            "owner": owner,
            "dream_date": day,
            "timezone": timezone_name,
            "items": [
                {"id": item["id"], "sha256": hashlib.sha256(item["content"].encode("utf-8")).hexdigest()}
                for item in bounded
            ],
            "source_event_range": event_range,
        }
        generation_id = hashlib.sha256(
            json.dumps(digest_material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:32]
        return {
            "schema_version": SCHEMA_VERSION,
            "owner": owner,
            "dream_date": day,
            "timezone": timezone_name,
            "generation_id": generation_id,
            "source_memory_ids": source_ids,
            "source_event_range": event_range,
            "truncated": truncated,
            "limits": asdict(self.config),
            "materials": bounded,
        }


class PreparedDreamStore:
    """Atomic derived packets. SQLite remains the authority for claims and commits."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.root = (self.project_root / DREAM_PREPARED_DIR).resolve()
        self.lock_path = self.project_root / "state" / ".dream-prepared.lock"

    def path_for(self, owner: str, dream_date: str) -> Path:
        owner = validate_owner(owner)
        day = validate_dream_date(dream_date)
        path = self.root / owner / f"{day}.json"
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("prepared dream path escapes runtime root") from exc
        return path

    @staticmethod
    def _validate(packet: Any, owner: str, day: str) -> dict[str, Any]:
        if not isinstance(packet, dict):
            raise ValueError("prepared dream packet must be a JSON object")
        required = {
            "schema_version", "owner", "dream_date", "generation_id", "prepared_at",
            "timezone", "source_memory_ids", "source_event_range", "truncated", "limits", "materials",
        }
        missing = sorted(required - packet.keys())
        if missing:
            raise ValueError(f"prepared dream packet is missing: {', '.join(missing)}")
        if packet["owner"] != owner or packet["dream_date"] != day:
            raise ValueError("prepared dream packet owner/date mismatch")
        if packet.get("derived") is not True or packet.get("factual_authority") is not False:
            raise ValueError("prepared dream packet authority markers are invalid")
        if not isinstance(packet["materials"], list) or not isinstance(packet["generation_id"], str):
            raise ValueError("prepared dream packet materials/generation_id are invalid")
        return packet

    def load(self, owner: str, dream_date: str) -> dict[str, Any] | None:
        owner = validate_owner(owner)
        day = validate_dream_date(dream_date)
        path = self.path_for(owner, day)
        if not path.is_file():
            return None
        try:
            raw = json.loads(_read_memory_text(path))
        except FileNotFoundError:
            # Removed by a concurrent post-commit cleanup after the is_file() check:
            # the same answer as if the check itself had found no packet.
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid prepared dream packet: {path}") from exc
        return self._validate(raw, owner, day)

    def prepare(
        self,
        preparer: DreamPreparer,
        owner: str,
        dream_date: str,
        *,
        timezone_name: str = DEFAULT_TIMEZONE,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], bool]:
        owner = validate_owner(owner)
        day = validate_dream_date(dream_date)
        with _cross_process_file_lock(self.lock_path):
            existing = self.load(owner, day)
            if existing is not None:
                return existing, False
            package = preparer.prepare(
                owner, dream_date=day, timezone_name=timezone_name, now=now
            )
            prepared_at = _utc_iso(_utc_now(now))
            packet = {
                **package,
                "prepared_at": prepared_at,
                "derived": True,
                "factual_authority": False,
            }
            path = self.path_for(owner, day)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f".{day}.", suffix=".tmp", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(packet, stream, ensure_ascii=False, sort_keys=True, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                _with_sharing_retry(lambda: os.replace(temp_name, path))
            finally:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
            return packet, True

    def delete(self, owner: str, dream_date: str) -> bool:
        path = self.path_for(owner, dream_date)
        with _cross_process_file_lock(self.lock_path):
            try:
                _with_sharing_retry(path.unlink)
                return True
            except FileNotFoundError:
                return False

    def cleanup(self, owner: str, dream_date: str) -> bool:
        """Best-effort removal after the dream is already durable.

        The dream commit has succeeded by the time this runs, so a cleanup failure
        must not turn it into a failure. A leftover packet is inert: claims skip
        dates with a dated dream, and recovery/nightly cleanup removes it later.
        """
        try:
            self.delete(owner, dream_date)
            return True
        except OSError as exc:
            log.warning(
                "prepared dream packet cleanup deferred for owner=%s date=%s: %s",
                validate_owner(owner), validate_dream_date(dream_date), type(exc).__name__,
            )
            return False


def claim_on_wake_dream(
    project_root: str | Path,
    owner: str,
    *,
    now: datetime,
    timezone_name: str = DEFAULT_TIMEZONE,
    ttl: timedelta = DEFAULT_DREAM_LEASE_TTL,
    preparer: DreamPreparer | None = None,
    lease_store: DreamLeaseStore | None = None,
    prepared_store: PreparedDreamStore | None = None,
) -> dict[str, Any] | None:
    """Prepare and lease the most recent completed local day without generating a dream."""
    root = Path(project_root).resolve()
    owner = validate_owner(owner)
    owner_config = load_owner_config(root, owner)
    if owner_config.dream_mode != "on_wake":
        return None
    zone = get_timezone(timezone_name)
    current = _utc_now(now).astimezone(zone)
    latest_complete = date.fromisoformat(most_recent_complete_dream_date(current, timezone_name))
    grace_date = (latest_complete - timedelta(days=1)).isoformat()
    leases = lease_store or DreamLeaseStore(root)
    packets = prepared_store or PreparedDreamStore(root)
    _, grace_dated_path = safe_dream_paths(root, owner, grace_date)
    grace_state = leases.recovery_state(owner, grace_date, now)
    grace_packet = packets.load(owner, grace_date)
    if not grace_dated_path.is_file() and grace_state == "active":
        return None
    dream_date = (
        grace_date
        if not grace_dated_path.is_file()
        and grace_state != "committed"
        and (grace_state == "expired" or grace_packet is not None)
        else latest_complete.isoformat()
    )
    _, dated_path = safe_dream_paths(root, owner, dream_date)
    if dated_path.is_file():
        return None
    prepare = preparer or DreamPreparer(
        root,
        scraps_enabled=owner_config.dream_scraps_enabled,
        scrap_max_per_dream=owner_config.dream_scrap_max_per_dream,
    )
    package = packets.load(owner, dream_date)
    if package is None:
        package, _ = packets.prepare(
            prepare, owner, dream_date, timezone_name=timezone_name, now=current
        )
    if dated_path.is_file():
        return None
    claim = leases.claim(owner, dream_date, ttl, now, package=package)
    if claim is None:
        return None
    episode_id = "dream-" + secrets.token_urlsafe(18)
    if owner_config.independent_witness_enabled:
        MemoryWitnessStore(root).expose(
            owner, list(package.get("source_memory_ids", [])), episode_id,
            source="wake", context_kind="dream_material", now=now,
        )
    return {
        **package,
        "claim_token": claim.claim_token,
        "claimed_at": claim.claimed_at,
        "expires_at": claim.expires_at,
        "exposure_episode_id": episode_id,
        "instruction": PENDING_DREAM_INSTRUCTION,
    }


class Runner(Protocol):
    name: str
    model: str | None

    def run(self, package: dict[str, Any]) -> str: ...


class DreamApiAdapter(Protocol):
    name: str
    model: str | None

    def generate(self, prompt: str, *, timeout: int, metadata: dict[str, Any]) -> str: ...


def dream_instruction() -> str:
    """One deliberately plain instruction shared by every generation mode."""
    return PENDING_DREAM_INSTRUCTION


def build_prompt(package: dict[str, Any]) -> str:
    return dream_instruction() + "\n\nINPUT PACKAGE (JSON):\n" + json.dumps(package, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class RunnerConfig:
    name: str
    executable: str
    args: tuple[str, ...] = ()
    model: str | None = None
    timeout_seconds: int = DEFAULT_CONFIG.runner_timeout_seconds
    max_output_chars: int = DEFAULT_CONFIG.max_dream_chars
    input_mode: str = "stdin"
    explicit_command: bool = False


class CliRunner:
    def __init__(self, config: RunnerConfig) -> None:
        self.config = config
        self.name = config.name
        self.model = config.model
        if config.input_mode not in {"stdin", "tempfile"}:
            raise ValueError("input_mode must be stdin or tempfile")
        if config.explicit_command:
            self.argv = [config.executable, *config.args]
        elif config.name == "codex" and not config.args:
            self.argv = [
                config.executable, "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "--sandbox", "read-only", "--skip-git-repo-check", "-",
            ]
        elif config.args:
            self.argv = [config.executable, *config.args]
        else:
            raise ValueError("custom runners require an explicit argument template")

    def run(self, package: dict[str, Any]) -> str:
        prompt = build_prompt(package)
        temp_path: Path | None = None
        argv = list(self.argv)
        stdin_text: str | None = prompt
        working_dir: str | None = None
        temp_dir = tempfile.TemporaryDirectory(prefix="dream-runner-")
        try:
            working_dir = temp_dir.name
            if self.config.input_mode == "tempfile":
                temp_path = Path(temp_dir.name) / "input.txt"
                temp_path.write_text(prompt, encoding="utf-8")
                os.chmod(temp_path, 0o600)
                if not any("{input_file}" in value for value in argv):
                    raise ValueError("tempfile runner args must contain {input_file}")
                argv = [value.replace("{input_file}", str(temp_path)) for value in argv]
                stdin_text = None
            elif any("{input_file}" in value for value in argv):
                raise ValueError("{input_file} is only valid with tempfile input_mode")
            completed = subprocess.run(
                argv,
                input=stdin_text,
                cwd=working_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=self.config.timeout_seconds,
                shell=False,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(f"dream runner exited {completed.returncode}")
            output = completed.stdout.strip()
            if not output:
                raise ValueError("dream runner returned empty output")
            if len(output) > self.config.max_output_chars:
                raise ValueError("dream runner output exceeds configured maximum")
            return output
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"dream runner timed out after {self.config.timeout_seconds}s") from exc
        finally:
            temp_dir.cleanup()


class ApiRunner:
    def __init__(
        self,
        adapter: DreamApiAdapter,
        *,
        timeout_seconds: int = DEFAULT_CONFIG.runner_timeout_seconds,
        max_output_chars: int = DEFAULT_CONFIG.max_dream_chars,
        model: str | None = None,
    ) -> None:
        self.adapter = adapter
        self.name = f"api:{adapter.name}"
        self.model = model if model is not None else adapter.model
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars

    def run(self, package: dict[str, Any]) -> str:
        metadata = {
            "owner": package["owner"],
            "dream_date": package["dream_date"],
            "generation_id": package["generation_id"],
            "schema_version": package["schema_version"],
        }
        try:
            output = self.adapter.generate(
                build_prompt(package), timeout=self.timeout_seconds, metadata=metadata
            )
        except TimeoutError:
            raise
        except Exception as exc:
            raise RuntimeError(f"dream API adapter {self.adapter.name!r} failed") from exc
        if not isinstance(output, str) or not output.strip():
            raise ValueError("dream API adapter returned empty output")
        output = output.strip()
        if len(output) > self.max_output_chars:
            raise ValueError("dream API adapter output exceeds configured maximum")
        return output


def _json_scalar(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def serialize_dream(metadata: dict[str, Any], body: str) -> str:
    ids = _ordered_unique(metadata.get("source_memory_ids", []))
    event_range = metadata.get("source_event_range") or {"start": None, "end": None}
    lines = [
        "---",
        f"schema_version: {int(metadata['schema_version'])}",
        f"owner: {_json_scalar(validate_owner(metadata['owner']))}",
        f"dream_date: {_json_scalar(validate_dream_date(metadata['dream_date']))}",
        f"generated_at: {_json_scalar(str(metadata['generated_at']))}",
        f"timezone: {_json_scalar(str(metadata['timezone']))}",
        f"runner: {_json_scalar(str(metadata['runner']))}",
        f"model: {_json_scalar(metadata.get('model'))}",
        f"generation_id: {_json_scalar(str(metadata['generation_id']))}",
        "derived: true",
        "factual_authority: false",
        "source_memory_ids:",
    ]
    lines.extend(f"  - {_json_scalar(memory_id)}" for memory_id in ids)
    lines.extend([
        "source_event_range:",
        f"  start: {_json_scalar(event_range.get('start'))}",
        f"  end: {_json_scalar(event_range.get('end'))}",
        f"truncated: {'true' if metadata.get('truncated') else 'false'}",
        "---", "", body.strip(), "",
    ])
    return "\n".join(lines)


def parse_dream(text: str) -> dict[str, Any]:
    if not text.startswith("---\n"):
        raise ValueError("dream frontmatter is missing")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("dream frontmatter is incomplete")
    lines = text[4:end].splitlines()
    data: dict[str, Any] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if line == "source_memory_ids:":
            values: list[str] = []
            index += 1
            while index < len(lines) and lines[index].startswith("  - "):
                values.append(str(json.loads(lines[index][4:])))
                index += 1
            data["source_memory_ids"] = _ordered_unique(values)
            continue
        if line == "source_event_range:":
            values: dict[str, Any] = {}
            index += 1
            while index < len(lines) and lines[index].startswith("  "):
                key, raw = lines[index].strip().split(":", 1)
                values[key] = json.loads(raw.strip())
                index += 1
            data["source_event_range"] = values
            continue
        if ":" not in line or line.startswith(" "):
            raise ValueError("invalid dream frontmatter line")
        key, raw = line.split(":", 1)
        data[key] = json.loads(raw.strip())
        index += 1
    required = {
        "schema_version", "owner", "dream_date", "generated_at", "timezone", "runner", "model",
        "generation_id", "derived", "factual_authority", "source_memory_ids",
        "source_event_range", "truncated",
    }
    if set(data) != required or data["schema_version"] != 1:
        raise ValueError("invalid dream frontmatter fields")
    validate_owner(data["owner"])
    validate_dream_date(data["dream_date"])
    _parse_timestamp(data["generated_at"])
    get_timezone(data["timezone"])
    if data["derived"] is not True or data["factual_authority"] is not False:
        raise ValueError("dream authority flags are invalid")
    body = text[end + 5 :].strip()
    if not body:
        raise ValueError("dream body is empty")
    return {**data, "content": body}


class DreamStore:
    def __init__(self, project_root: str | Path, config: DreamConfig = DEFAULT_CONFIG) -> None:
        self.project_root = Path(project_root).resolve()
        self.config = config

    def existing(self, owner: str, dream_date: str) -> dict[str, Any] | None:
        _, dated = safe_dream_paths(self.project_root, owner, dream_date)
        if not dated.is_file():
            return None
        parsed = parse_dream(_read_memory_text(dated))
        if parsed["owner"] != validate_owner(owner) or parsed["dream_date"] != validate_dream_date(dream_date):
            raise ValueError("dream path/frontmatter mismatch")
        return parsed

    def _repair_current_locked(
        self,
        owner: str,
        dream_date: str,
        writer: Callable[[Path, str], None],
    ) -> dict[str, Any] | None:
        current_path, dated_path = safe_dream_paths(self.project_root, owner, dream_date)
        if not dated_path.is_file():
            return None
        dated_text = dated_path.read_text(encoding="utf-8")
        parsed = parse_dream(dated_text)
        if parsed["owner"] != owner or parsed["dream_date"] != dream_date:
            raise ValueError("dream path/frontmatter mismatch")
        try:
            current_matches = current_path.read_text(encoding="utf-8") == dated_text
        except (OSError, UnicodeError):
            current_matches = False
        if not current_matches:
            current_path.parent.mkdir(parents=True, exist_ok=True)
            writer(current_path, dated_text)
        return parsed

    def recover_current(
        self,
        owner: str,
        dream_date: str,
        *,
        atomic_writer: Callable[[Path, str], None] | None = None,
    ) -> dict[str, Any] | None:
        owner = validate_owner(owner)
        day = validate_dream_date(dream_date)
        writer = atomic_writer or MemoryStore._atomic_write
        lock_store = MemoryStore(self.project_root, owner)
        with lock_store._write_lock():
            return self._repair_current_locked(owner, day, writer)

    def commit(
        self,
        package: dict[str, Any],
        body: str,
        *,
        runner: str,
        model: str | None,
        force: bool = False,
        generated_at: datetime | None = None,
        atomic_writer: Callable[[Path, str], None] | None = None,
    ) -> dict[str, Any]:
        owner = validate_owner(package["owner"])
        day = validate_dream_date(package["dream_date"])
        if not body or not body.strip():
            raise ValueError("dream output is empty")
        if len(body) > self.config.max_dream_chars:
            raise ValueError("dream output exceeds configured maximum")
        body.encode("utf-8", errors="strict")
        zone = get_timezone(package["timezone"])
        generated = generated_at.astimezone(zone) if generated_at else datetime.now(zone)
        metadata = {
            "schema_version": 1,
            "owner": owner,
            "dream_date": day,
            "generated_at": generated.isoformat(),
            "timezone": package["timezone"],
            "runner": runner,
            "model": model,
            "generation_id": package["generation_id"],
            "derived": True,
            "factual_authority": False,
            "source_memory_ids": package["source_memory_ids"],
            "source_event_range": package["source_event_range"],
            "truncated": bool(package["truncated"]),
        }
        text = serialize_dream(metadata, body)
        current_path, dated_path = safe_dream_paths(self.project_root, owner, day)
        writer = atomic_writer or MemoryStore._atomic_write
        lock_store = MemoryStore(self.project_root, owner)
        with lock_store._write_lock():
            if dated_path.exists() and not force:
                existing = self._repair_current_locked(owner, day, writer)
                return {"created": False, "idempotent": True, "path": str(dated_path), **existing}
            old_dated = dated_path.read_text(encoding="utf-8") if dated_path.exists() else None
            dated_path.parent.mkdir(parents=True, exist_ok=True)
            current_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                writer(dated_path, text)
                writer(current_path, text)
            except Exception:
                if old_dated is None:
                    dated_path.unlink(missing_ok=True)
                else:
                    MemoryStore._atomic_write(dated_path, old_dated)
                raise
        return {"created": True, "idempotent": False, "path": str(dated_path), **parse_dream(text)}

    def read_current(self, owner: str, max_chars: int | None = None) -> dict[str, Any] | None:
        owner = validate_owner(owner)
        current, _ = safe_dream_paths(self.project_root, owner, date.today().isoformat())
        if not current.is_file():
            return None
        try:
            parsed = parse_dream(_read_memory_text(current))
            if parsed["owner"] != owner:
                raise ValueError("current dream owner mismatch")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            log.warning("ignoring invalid current dream for %s: %s", owner, exc)
            return None
        limit = self.config.wake_chars if max_chars is None else max(1, int(max_chars))
        content = parsed["content"]
        truncated = len(content) > limit
        if truncated:
            content = content[: max(0, limit - 1)] + "…"
        return {
            "label": "梦境 / 派生内容 / 不作为事实依据",
            "owner": parsed["owner"],
            "dream_date": parsed["dream_date"],
            "derived": True,
            "factual_authority": False,
            "content": content,
            "truncated": truncated,
        }

    def list_months(self, owner: str) -> list[dict[str, Any]]:
        owner = validate_owner(owner)
        owner_root, _ = safe_dream_paths(self.project_root, owner, "2000-01-01")
        root = owner_root.parent
        result = []
        if not root.is_dir():
            return result
        for month in sorted((path for path in root.iterdir() if path.is_dir() and re.fullmatch(r"\d{4}-\d{2}", path.name)), reverse=True):
            days = sorted(path.stem for path in month.glob("????-??-??.md"))
            result.append({"month": month.name, "days": days})
        return result

    def open_day(self, owner: str, dream_date: str) -> dict[str, Any]:
        result = self.existing(owner, dream_date)
        if result is None:
            raise KeyError(f"dream not found: {owner}/{dream_date}")
        return result

    def open_current(self, owner: str) -> dict[str, Any] | None:
        owner = validate_owner(owner)
        current, _ = safe_dream_paths(self.project_root, owner, "2000-01-01")
        if not current.is_file():
            return None
        parsed = parse_dream(_read_memory_text(current))
        if parsed["owner"] != owner:
            raise ValueError("current dream owner mismatch")
        return parsed


def dream_get_result(
    project_root: str | Path,
    owner: str,
    dream_date: str | None = None,
) -> dict[str, Any]:
    store = DreamStore(project_root)
    try:
        dream = store.open_current(owner) if dream_date is None else store.existing(owner, validate_dream_date(dream_date))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "status": "error", "dream": None, "error": {"code": "invalid_dream", "message": str(exc)}}
    if dream is None:
        return {"ok": True, "status": "not_found", "dream": None, "error": None}
    return {"ok": True, "status": "found", "dream": dream, "error": None}


def _commit_error(code: str, message: str) -> DreamCommitError:
    return DreamCommitError(code, message)


def commit_claimed_dream(
    project_root: str | Path,
    owner: str,
    dream_date: str,
    claim_token: str,
    content: str,
    *,
    now: datetime | None = None,
    lease_store: DreamLeaseStore | None = None,
    dream_store: DreamStore | None = None,
    runner: str = "on_wake",
    model: str | None = None,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    owner = validate_owner(owner)
    day = validate_dream_date(dream_date)
    if load_owner_config(root, owner).dream_mode != "on_wake":
        raise _commit_error("wrong_mode", "dream_commit requires dream_mode=on_wake")
    token = str(claim_token or "")
    if not token:
        raise _commit_error("invalid_claim", "claim_token is required")
    body = str(content or "").strip()
    store = dream_store or DreamStore(root)
    if not body:
        raise _commit_error("invalid_content", "dream content is empty")
    if len(body) > store.config.max_dream_chars:
        raise _commit_error("invalid_content", "dream content exceeds configured maximum")
    body.encode("utf-8", errors="strict")
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    timestamp = _utc_now(now)
    leases = lease_store or DreamLeaseStore(root)
    packets = PreparedDreamStore(root)
    db = leases._connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        committed = db.execute(
            "SELECT * FROM dream_commits WHERE owner = ? AND dream_date = ?",
            (owner, day),
        ).fetchone()
        lease = db.execute(
            "SELECT * FROM dream_leases WHERE owner = ? AND dream_date = ?",
            (owner, day),
        ).fetchone()
        if committed is not None:
            if str(committed["claim_token"]) != token:
                raise _commit_error("invalid_claim", "claim_token does not match the committed dream")
            if str(committed["content_sha256"]) != content_hash:
                raise _commit_error("content_conflict", "a different dream is already committed for this date")
            package = json.loads(str(committed["package_json"]))
            existing = store.existing(owner, day)
            if existing is None or existing["generation_id"] != str(committed["generation_id"]) or existing["content"] != body:
                raise _commit_error("content_conflict", "dated dream does not match committed runtime state")
            store.commit(package, body, runner="on_wake", model=None)
            db.commit()
            packets.cleanup(owner, day)
            return {"ok": True, "status": "already_committed", "already_committed": True, "dream": existing, "error": None}
        if lease is None or str(lease["claim_token"]) != token:
            raise _commit_error("invalid_claim", "claim_token does not match owner and dream_date")
        package_raw = lease["package_json"]
        if not package_raw:
            raise _commit_error("invalid_claim", "claim has no prepared material package")
        package = json.loads(str(package_raw))
        if package.get("owner") != owner or package.get("dream_date") != day:
            raise _commit_error("invalid_claim", "claim package does not match owner and dream_date")
        existing = store.existing(owner, day)
        if existing is not None:
            if existing["generation_id"] != package.get("generation_id") or existing["content"] != body:
                raise _commit_error("content_conflict", "a different dream is already committed for this date")
            store.commit(package, body, runner="on_wake", model=None)
            status = "already_committed"
        else:
            expires = _parse_timestamp(str(lease["expires_at"])).astimezone(timezone.utc)
            if expires <= timestamp:
                raise _commit_error("expired_claim", "claim_token has expired")
            store.commit(
                package,
                body,
                runner=runner,
                model=model,
                generated_at=timestamp,
            )
            status = "committed"
        db.execute(
            """INSERT INTO dream_commits
               (owner, dream_date, claim_token, content_sha256, generation_id, package_json, committed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                owner, day, token, content_hash, str(package["generation_id"]),
                json.dumps(package, ensure_ascii=False, sort_keys=True), _utc_iso(timestamp),
            ),
        )
        db.commit()
        packets.cleanup(owner, day)
        dream = store.existing(owner, day)
        return {"ok": True, "status": status, "already_committed": status == "already_committed", "dream": dream, "error": None}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def dream_commit_result(
    project_root: str | Path,
    owner: str,
    dream_date: str,
    claim_token: str,
    content: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        return commit_claimed_dream(
            project_root, owner, dream_date, claim_token, content, now=now
        )
    except DreamCommitError as exc:
        return {"ok": False, "status": "error", "already_committed": False, "dream": None, "error": {"code": exc.code, "message": str(exc)}}
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "status": "error", "already_committed": False, "dream": None, "error": {"code": "invalid_request", "message": str(exc)}}


def attach_dream_to_wake(
    packet: dict[str, Any],
    *,
    project_root: str | Path,
    owner: str,
    include_dream: bool = True,
    max_chars: int = DEFAULT_CONFIG.wake_chars,
) -> dict[str, Any]:
    """Add a bounded, explicitly non-authoritative dream section when available."""
    if not include_dream:
        return packet
    dream = DreamStore(project_root).read_current(owner, max_chars=max_chars)
    if dream is not None:
        packet["dream"] = dream
    return packet


class DreamPipeline:
    def __init__(self, preparer: DreamPreparer, store: DreamStore, runner: Runner | None) -> None:
        self.preparer = preparer
        self.store = store
        self.runner = runner

    def generate(
        self,
        owner: str,
        *,
        dream_date: str | None = None,
        timezone_name: str = DEFAULT_TIMEZONE,
        force: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        zone = get_timezone(timezone_name)
        current = now.astimezone(zone) if now else datetime.now(zone)
        day = validate_dream_date(dream_date or current.date())
        existing = self.store.recover_current(owner, day)
        if existing is not None and not force:
            return {"created": False, "idempotent": True, **existing}
        if self.runner is None:
            raise RuntimeError("dream runner is not configured")
        package = self.preparer.prepare(
            owner, dream_date=day, timezone_name=timezone_name, now=current
        )
        output = self.runner.run(package)
        return self.store.commit(
            package, output, runner=self.runner.name, model=self.runner.model,
            force=force, generated_at=current,
        )


def run_configured_dream(
    project_root: str | Path,
    owner: str,
    *,
    now: datetime,
    timezone_name: str = DEFAULT_TIMEZONE,
    api_adapters: dict[str, DreamApiAdapter] | None = None,
    preparer: DreamPreparer | None = None,
    store: DreamStore | None = None,
) -> dict[str, Any]:
    """Run one active cli/api dream for the latest complete day; on_wake remains passive."""
    root = Path(project_root).resolve()
    owner = validate_owner(owner)
    config = load_owner_config(root, owner)
    day = most_recent_complete_dream_date(now, timezone_name)
    dream_store = store or DreamStore(root)
    existing = dream_store.recover_current(owner, day)
    if existing is not None:
        PreparedDreamStore(root).cleanup(owner, day)
        return {"created": False, "idempotent": True, **existing}
    if config.dream_mode == "on_wake":
        return {"created": False, "idempotent": False, "skipped": True, "reason": "on_wake"}
    if config.dream_mode == "cli":
        if config.dream_cli is None:
            raise ValueError(f"dream_cli is required for cli owner {owner!r}")
        runner: Runner = CliRunner(RunnerConfig(
            name="configured_cli",
            executable=config.dream_cli[0],
            args=config.dream_cli[1:],
            model=config.dream_model,
            timeout_seconds=config.dream_timeout_seconds,
            explicit_command=True,
        ))
    else:
        if config.dream_api_adapter is None:
            raise ValueError(f"dream_api_adapter is required for api owner {owner!r}")
        adapter = (api_adapters or {}).get(config.dream_api_adapter)
        if adapter is None:
            raise ValueError(
                "configured API mode requires an installed/registered adapter: "
                f"{config.dream_api_adapter!r}"
            )
        runner = ApiRunner(
            adapter,
            timeout_seconds=config.dream_timeout_seconds,
            model=config.dream_model,
        )
    packets = PreparedDreamStore(root)
    configured_preparer = preparer or DreamPreparer(
        root,
        scraps_enabled=config.dream_scraps_enabled,
        scrap_max_per_dream=config.dream_scrap_max_per_dream,
    )
    package, _ = packets.prepare(
        configured_preparer, owner, day, timezone_name=timezone_name, now=now
    )
    output = runner.run(package)
    result = dream_store.commit(
        package, output, runner=runner.name, model=runner.model,
        generated_at=_utc_now(now).astimezone(get_timezone(timezone_name)),
    )
    packets.cleanup(owner, day)
    return result


def catch_up_due(
    owner: str,
    *,
    project_root: str | Path,
    timezone_name: str = DEFAULT_TIMEZONE,
    scheduled_time: str = DEFAULT_CONFIG.scheduled_time,
    enabled: bool = True,
    now: datetime | None = None,
) -> bool:
    if not enabled:
        return False
    owner = validate_owner(owner)
    zone = get_timezone(timezone_name)
    current = now.astimezone(zone) if now else datetime.now(zone)
    hour, minute = (int(part) for part in scheduled_time.split(":", 1))
    if current.timetz().replace(tzinfo=None) < time(hour, minute):
        return False
    _, dated = safe_dream_paths(project_root, owner, current.date().isoformat())
    return not dated.is_file()
