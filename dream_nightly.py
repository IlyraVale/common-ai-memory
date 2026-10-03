from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from dreams import (
    DEFAULT_TIMEZONE,
    DREAM_RUNTIME_DB,
    OWNER_CONFIG_FILENAME,
    DreamCommitError,
    DreamPreparer,
    DreamStore,
    PreparedDreamStore,
    _cross_process_file_lock,
    _parse_timestamp,
    claim_on_wake_dream,
    commit_claimed_dream,
    load_owner_config,
    most_recent_complete_dream_date,
    run_configured_dream,
    safe_dream_paths,
    validate_owner,
)
from dream_cli_runner import DreamCliFailure, PreferredCliRunner, resolve_executable, runner_for
from config import data_root, load_dotenv

# The existing grace rule lets one wake claim at most the previous day (if it is
# still owed) and then the latest complete day. The nightly CLI follows exactly
# the same rule, so it never claims more than these two dates in one run.
MAX_CLAIMS_PER_RUN = 2

EXIT_OK = 0
EXIT_INFRASTRUCTURE = 2
EXIT_BUSY = 3
EXIT_PARTIAL_FALLBACK = 4
EXIT_ALL_FALLBACK = 5

LOG_FIELDS = (
    "timestamp", "owner", "dream_date", "mode", "runner", "action", "status", "outcome",
    "generation_id", "error_type", "error_class", "duration_seconds", "error",
)


def configured_owners(project_root: Path, explicit: list[str]) -> list[str]:
    owners: list[str] = []
    path = project_root / OWNER_CONFIG_FILENAME
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        values = raw.get("owners", {}) if isinstance(raw, dict) else {}
        if not isinstance(values, dict):
            raise ValueError("owner configuration 'owners' must be a JSON object")
        owners.extend(values.keys())
    owners.extend(explicit)
    result: list[str] = []
    for owner in owners:
        owner = validate_owner(owner)
        if owner not in result:
            result.append(owner)
    if not result:
        raise ValueError("no owners configured; pass --owner or add owners to owner-config.json")
    return result


def _log(root: Path, event: dict[str, Any]) -> None:
    directory = root / "state" / "dream-scheduler"
    directory.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).date().isoformat()
    safe = {key: event.get(key) for key in LOG_FIELDS if event.get(key) is not None}
    with (directory / f"{day}.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n")


def _utc_clock() -> datetime:
    return datetime.now(timezone.utc)


def run_preferred_cli(
    root: Path, owner: str, *, timezone_name: str, now: datetime,
    runner: PreferredCliRunner | None = None, clock: Callable[[], datetime] = _utc_clock,
) -> dict[str, Any]:
    """Act as a wake for this owner: claim, let the fixed local CLI write, commit.

    Every CLI problem is a fallback: nothing is committed, the prepared packet is
    kept, and the claim lease expires so a later on_wake can claim the same date.
    """
    config = load_owner_config(root, owner)
    store = DreamStore(root)
    cli = runner or runner_for(config, max_output_chars=store.config.max_dream_chars)
    latest = most_recent_complete_dream_date(now, timezone_name)
    claims: list[dict[str, Any]] = []
    for _ in range(MAX_CLAIMS_PER_RUN):
        pending = claim_on_wake_dream(root, owner, now=now, timezone_name=timezone_name)
        if pending is None:
            break
        started = time.monotonic()
        claim = {"dream_date": pending["dream_date"], "generation_id": pending["generation_id"]}
        try:
            content = cli.generate(pending, pending["instruction"])
        except DreamCliFailure as exc:
            claims.append({**claim, "status": "fallback", "error_class": exc.error_class,
                           "duration_seconds": round(time.monotonic() - started, 1)})
            break
        try:
            result = commit_claimed_dream(
                root, owner, pending["dream_date"], pending["claim_token"], content,
                now=clock(), runner=cli.name,
            )
        except DreamCommitError as exc:
            claims.append({**claim, "status": "fallback", "error_class": f"commit_rejected:{exc.code}",
                           "duration_seconds": round(time.monotonic() - started, 1)})
            break
        claims.append({**claim, "status": result["status"],
                       "duration_seconds": round(time.monotonic() - started, 1)})
    if store.existing(owner, latest) is None:
        # Keep the existing nightly guarantee: the next wake finds a prepared packet.
        PreparedDreamStore(root).prepare(
            DreamPreparer(root, scraps_enabled=config.dream_scraps_enabled,
                          scrap_max_per_dream=config.dream_scrap_max_per_dream),
            owner, latest, timezone_name=timezone_name, now=now,
        )
    if any(item["status"] == "fallback" for item in claims):
        status = "fallback"
    elif claims:
        status = "committed"
    elif store.existing(owner, latest) is not None:
        status = "already_exists"
    else:
        status = "no_claim"
    return {"owner": owner, "dream_date": latest, "mode": config.dream_mode, "runner": cli.name,
            "action": "cli", "status": status, "claims": claims}


def run_owner(
    root: Path, owner: str, *, timezone_name: str, now: datetime,
    runner: PreferredCliRunner | None = None, clock: Callable[[], datetime] = _utc_clock,
) -> dict[str, Any]:
    config = load_owner_config(root, owner)
    if config.dream_preferred_runner == "cli":
        return run_preferred_cli(root, owner, timezone_name=timezone_name, now=now, runner=runner, clock=clock)
    day = most_recent_complete_dream_date(now, timezone_name)
    store = DreamStore(root)
    existing = store.recover_current(owner, day)
    if existing is not None:
        PreparedDreamStore(root).delete(owner, day)
        return {"owner": owner, "dream_date": day, "mode": config.dream_mode,
                "action": "skip", "status": "already_exists", "generation_id": existing["generation_id"]}
    if config.dream_mode == "on_wake":
        packet, created = PreparedDreamStore(root).prepare(
            DreamPreparer(root, scraps_enabled=config.dream_scraps_enabled,
                          scrap_max_per_dream=config.dream_scrap_max_per_dream),
            owner, day, timezone_name=timezone_name, now=now
        )
        return {"owner": owner, "dream_date": day, "mode": config.dream_mode,
                "action": "prepare", "status": "created" if created else "reused",
                "generation_id": packet["generation_id"]}
    result = run_configured_dream(root, owner, now=now, timezone_name=timezone_name)
    return {"owner": owner, "dream_date": day, "mode": config.dream_mode,
            "action": "generate", "status": "created" if result.get("created") else "reused",
            "generation_id": result.get("generation_id")}


def _exit_code(results: list[dict[str, Any]]) -> int:
    if any(item.get("status") == "error" for item in results):
        return EXIT_INFRASTRUCTURE
    cli = [item for item in results if item.get("action") == "cli"]
    fallbacks = [item for item in cli if item.get("status") == "fallback"]
    if cli and len(fallbacks) == len(cli):
        return EXIT_ALL_FALLBACK
    if fallbacks:
        return EXIT_PARTIAL_FALLBACK
    return EXIT_OK


def run_batch(
    root: Path, owners: list[str], *, timezone_name: str, now: datetime,
    runners: dict[str, PreferredCliRunner] | None = None, clock: Callable[[], datetime] = _utc_clock,
) -> tuple[list[dict[str, Any]], int]:
    results: list[dict[str, Any]] = []
    lock = root / "state" / ".dream-nightly.lock"
    try:
        with _cross_process_file_lock(lock, blocking=False):
            for owner in owners:
                timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                try:
                    event = run_owner(root, owner, timezone_name=timezone_name, now=now,
                                      runner=(runners or {}).get(owner), clock=clock)
                    event["timestamp"] = timestamp
                except Exception as exc:
                    event = {"timestamp": timestamp, "owner": owner, "mode": None,
                             "action": "run", "status": "error",
                             "error_type": type(exc).__name__, "error": str(exc)[:300]}
                for claim in event.get("claims", []):
                    _log(root, {"timestamp": timestamp, "owner": owner, "mode": event.get("mode"),
                                "runner": event.get("runner"), "action": "cli", **claim,
                                "outcome": claim.get("status")})
                _log(root, {key: value for key, value in event.items() if key != "claims"})
                results.append(event)
    except (OSError, BlockingIOError):
        return [{"status": "busy", "action": "batch"}], EXIT_BUSY
    return results, _exit_code(results)


def _readonly_runtime(root: Path) -> sqlite3.Connection | None:
    path = root / DREAM_RUNTIME_DB
    if not path.is_file():
        return None
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def dry_run(root: Path, owners: list[str], *, timezone_name: str, now: datetime) -> list[dict[str, Any]]:
    """Report what a run would do. Reads only: no prepare, claim, commit, or log."""
    report = []
    db = _readonly_runtime(root)
    try:
        for owner in owners:
            config = load_owner_config(root, owner)
            latest = most_recent_complete_dream_date(now, timezone_name)
            grace = (date.fromisoformat(latest) - timedelta(days=1)).isoformat()
            days = {}
            for day in (grace, latest):
                _, dated = safe_dream_paths(root, owner, day)
                lease_state, committed = "none", False
                if db is not None:
                    committed = db.execute("SELECT 1 FROM dream_commits WHERE owner=? AND dream_date=?",
                                           (owner, day)).fetchone() is not None
                    lease = db.execute("SELECT expires_at FROM dream_leases WHERE owner=? AND dream_date=?",
                                       (owner, day)).fetchone()
                    if lease is not None:
                        expires = _parse_timestamp(str(lease["expires_at"])).astimezone(timezone.utc)
                        lease_state = "active" if expires > now.astimezone(timezone.utc) else "expired"
                days[day] = {"dream_exists": dated.is_file(), "committed": committed, "lease": lease_state,
                             "packet_exists": PreparedDreamStore(root).path_for(owner, day).is_file()}
            g = days[grace]
            would_claim = None
            if config.dream_mode == "on_wake" and not (not g["dream_exists"] and g["lease"] == "active"):
                candidate = grace if (not g["dream_exists"] and not g["committed"]
                                      and (g["lease"] == "expired" or g["packet_exists"])) else latest
                would_claim = None if days[candidate]["dream_exists"] else candidate
            preferred = config.dream_preferred_runner == "cli"
            report.append({
                "owner": owner, "mode": config.dream_mode,
                "selected_runner": f"cli:{config.dream_cli_profile}" if preferred else
                                   ("on_wake (prepare only)" if config.dream_mode == "on_wake" else config.dream_mode),
                "cli_available": (resolve_executable(config.dream_cli_executable) is not None) if preferred else None,
                "latest_complete": latest, "grace_date": grace, "dates": days,
                "would_claim_first": would_claim,
            })
    finally:
        if db is not None:
            db.close()
    return report


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Prepare or generate the latest complete Dream day")
    parser.add_argument("--project-root", default=str(data_root()))
    parser.add_argument("--owner", action="append", default=[])
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--dry-run", action="store_true",
                        help="report owners, dates, packets, commits and CLI availability without side effects")
    args = parser.parse_args(argv)
    root = Path(args.project_root).resolve()
    owners = configured_owners(root, args.owner)
    if args.dry_run:
        report = dry_run(root, owners, timezone_name=args.timezone, now=datetime.now(timezone.utc))
        print(json.dumps({"ok": True, "dry_run": True, "owners": report}, ensure_ascii=False, indent=2))
        return EXIT_OK
    results, code = run_batch(root, owners, timezone_name=args.timezone, now=datetime.now(timezone.utc))
    outcome = {item.get("owner"): item.get("status") for item in results if item.get("owner")}
    print(json.dumps({"ok": code == EXIT_OK, "exit_code": code, "outcome": outcome, "results": results},
                     ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:300]}), file=sys.stderr)
        raise SystemExit(EXIT_INFRASTRUCTURE)
