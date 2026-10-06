"""Snapshots: verifiable restore points of the user data in DATA_DIR. No model is ever involved.

A snapshot is a folder ``DATA_DIR/snapshots/<id>/`` holding a copy of the managed data and a
``manifest.json`` with every file's size and SHA256. SQLite databases are copied with SQLite's online
backup API while the project write lock is held, so the copy is consistent even with services running.

Restoring is deliberately two-step and CLI-only: ``restore-plan`` shows exactly what would change and
issues a short-lived one-time token bound to that plan; ``restore --confirm <token>`` re-verifies,
takes a safety snapshot first (and stops if that fails), stages and verifies the files, swaps them in
under the lock, checks integrity and rolls everything back if any step fails.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1
SNAPSHOT_DIR = "snapshots"
MANAGED_DIRS = ("memory", "dreams", "archive", ".lounge", ".games", ".lounge-bridge", "attachments")
MANAGED_FILES = ("owner-config.json",)
STATE_DIR = "state"
STATE_SUBDIRS = ("dream-prepared",)
SQLITE_SUFFIXES = (".sqlite3", ".sqlite", ".db")
DERIVED_STATE = ("memory-search", "memory-vectors")  # rebuildable indexes: not snapshotted, rebuilt after restore
EXCLUDED_SUFFIXES = ("-wal", "-shm", "-journal", ".lock", ".tmp", ".pyc", ".dirty")
TOKEN_FILE = Path(STATE_DIR) / "snapshot-restore-tokens.json"
TOKEN_TTL = timedelta(minutes=10)
LOCK_TIMEOUT = 30.0


class SnapshotError(RuntimeError):
    """A refused or failed snapshot operation; the message is safe to show."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _excluded(path: Path) -> bool:
    name = path.name
    return (name.endswith(EXCLUDED_SUFFIXES) or ".tmp-" in name or name.startswith(".restore-")
            or "__pycache__" in path.parts or name == ".memory-store.lock")


def _is_sqlite(path: Path) -> bool:
    return path.name.endswith(SQLITE_SUFFIXES)


def managed_files(root: Path) -> list[str]:
    """Relative POSIX paths of every managed data file currently in DATA_DIR."""
    found: list[str] = []
    for name in MANAGED_DIRS:
        base = root / name
        if base.is_dir():
            found += [p.relative_to(root).as_posix() for p in base.rglob("*") if p.is_file() and not _excluded(p)]
    for name in MANAGED_FILES:
        if (root / name).is_file():
            found.append(name)
    state = root / STATE_DIR
    if state.is_dir():
        for path in state.iterdir():
            if path.is_file() and _is_sqlite(path) and not _excluded(path) \
                    and not path.name.startswith(DERIVED_STATE) and "rebuild" not in path.name:
                found.append(path.relative_to(root).as_posix())
        for sub in STATE_SUBDIRS:
            if (state / sub).is_dir():
                found += [p.relative_to(root).as_posix() for p in (state / sub).rglob("*")
                          if p.is_file() and not _excluded(p)]
    return sorted(set(found))


def _sqlite_summary(path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
        tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {}
        for table in tables:
            try:
                counts[table] = int(db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
            except sqlite3.Error:
                counts[table] = None
    return {"integrity": integrity, "tables": counts}


def _sqlite_content_sha256(path: Path) -> str:
    """Digest of a database's logical content. File bytes are not comparable: a WAL database and its
    DELETE-journal backup differ in the header even when every row is the same."""
    digest = hashlib.sha256()
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        for line in db.iterdump():
            digest.update(line.encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def _backup_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as src, \
            closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")


class SnapshotManager:
    def __init__(self, project_root: str | Path, *, clock=_now) -> None:
        self.root = Path(project_root).resolve()
        self.base = self.root / SNAPSHOT_DIR
        self.clock = clock

    # --- locking ------------------------------------------------------------------------------
    @contextmanager
    def _locked(self) -> Iterator[Path]:
        from memory_store import MemoryStore

        store = MemoryStore(self.root, "snapshot")
        try:
            lock = store._acquire_lock(timeout=LOCK_TIMEOUT)
        except TimeoutError as exc:
            raise SnapshotError("the memory store is busy; try again in a moment") from exc
        try:
            yield lock
        finally:
            store._release_lock(lock)

    @staticmethod
    def _keep_alive(lock: Path) -> None:
        try:
            os.utime(lock, None)  # other writers treat a lock older than 30 s as stale
        except OSError:
            pass

    # --- create / list / verify -----------------------------------------------------------------
    def create(self, *, label: str = "", reason: str = "manual") -> dict[str, Any]:
        snapshot_id = "snap-" + self.clock().strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
        partial = self.base / f".partial-{snapshot_id}"
        final = self.base / snapshot_id
        partial.mkdir(parents=True, exist_ok=False)
        try:
            with self._locked() as lock:
                files, databases = [], []
                for rel in managed_files(self.root):
                    source, target = self.root / rel, partial / "data" / rel
                    self._keep_alive(lock)
                    if _is_sqlite(source):
                        _backup_sqlite(source, target)
                        databases.append({"path": rel, **_sqlite_summary(target)})
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, target)
                    entry = {"path": rel, "size": target.stat().st_size, "sha256": _sha256(target),
                             "kind": "sqlite" if _is_sqlite(source) else "file"}
                    if entry["kind"] == "sqlite":
                        entry["content_sha256"] = _sqlite_content_sha256(target)
                    files.append(entry)
                created_at = _iso(self.clock())
            manifest = {
                "schema_version": SCHEMA_VERSION, "snapshot_id": snapshot_id, "created_at": created_at,
                "label": str(label)[:120], "reason": reason, "software_version": _software_version(),
                "included_roots": list(MANAGED_DIRS) + list(MANAGED_FILES) + [f"{STATE_DIR}/*.sqlite3"]
                + [f"{STATE_DIR}/{s}" for s in STATE_SUBDIRS],
                "excluded": ["logs", "caches and models", "derived search/vector indexes", "locks, temp, WAL/SHM",
                             SNAPSHOT_DIR],
                "file_count": len(files), "total_bytes": sum(f["size"] for f in files),
                "files": files, "sqlite": databases,
            }
            (partial / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(partial, final)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        return {"ok": True, "snapshot_id": snapshot_id, "file_count": len(files),
                "total_bytes": manifest["total_bytes"], "databases": len(databases)}

    def _manifest(self, snapshot_id: str) -> dict[str, Any]:
        if not snapshot_id or "/" in snapshot_id or "\\" in snapshot_id or snapshot_id.startswith("."):
            raise SnapshotError("invalid snapshot id")
        path = self.base / snapshot_id / "manifest.json"
        if not path.is_file():
            raise SnapshotError(f"no snapshot {snapshot_id}")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SnapshotError(f"snapshot {snapshot_id} has an unreadable manifest") from exc
        if manifest.get("snapshot_id") != snapshot_id or manifest.get("schema_version") != SCHEMA_VERSION:
            raise SnapshotError(f"snapshot {snapshot_id} has an invalid manifest")
        return manifest

    def list(self) -> dict[str, Any]:
        items = []
        if self.base.is_dir():
            for folder in sorted(self.base.iterdir(), reverse=True):
                if folder.is_dir() and not folder.name.startswith("."):
                    try:
                        m = self._manifest(folder.name)
                    except SnapshotError:
                        items.append({"snapshot_id": folder.name, "status": "unreadable"})
                        continue
                    items.append({"snapshot_id": m["snapshot_id"], "created_at": m["created_at"],
                                  "label": m.get("label", ""), "reason": m.get("reason", ""),
                                  "file_count": m["file_count"], "total_bytes": m["total_bytes"]})
        return {"ok": True, "snapshots": items}

    def verify(self, snapshot_id: str) -> dict[str, Any]:
        manifest = self._manifest(snapshot_id)
        data = self.base / snapshot_id / "data"
        problems = []
        listed = {f["path"] for f in manifest["files"]}
        for entry in manifest["files"]:
            path = data / entry["path"]
            if not path.is_file():
                problems.append({"path": entry["path"], "problem": "missing"})
            elif path.stat().st_size != entry["size"] or _sha256(path) != entry["sha256"]:
                problems.append({"path": entry["path"], "problem": "content changed"})
        if data.is_dir():
            for path in data.rglob("*"):
                if path.is_file() and path.relative_to(data).as_posix() not in listed:
                    problems.append({"path": path.relative_to(data).as_posix(), "problem": "unexpected file"})
        for db in manifest["sqlite"]:
            path = data / db["path"]
            if path.is_file() and not any(p["path"] == db["path"] for p in problems):
                try:
                    if _sqlite_summary(path)["integrity"] != "ok":
                        problems.append({"path": db["path"], "problem": "sqlite integrity check failed"})
                except sqlite3.Error:
                    problems.append({"path": db["path"], "problem": "sqlite unreadable"})
        return {"ok": not problems, "snapshot_id": snapshot_id, "files_checked": len(manifest["files"]),
                "problems": problems}

    # --- restore planning --------------------------------------------------------------------------
    def restore_plan(self, snapshot_id: str) -> dict[str, Any]:
        """Read-only: what restoring this snapshot would add, overwrite, remove and how SQLite would look."""
        manifest = self._manifest(snapshot_id)
        snap = {f["path"]: f for f in manifest["files"]}
        current = {rel: self.root / rel for rel in managed_files(self.root)}
        add = sorted(set(snap) - set(current))
        remove = sorted(set(current) - set(snap))
        overwrite, unchanged = [], 0
        current_sha = {}
        data = self.base / snapshot_id / "data"
        for rel in sorted(set(snap) & set(current)):
            if snap[rel].get("kind") == "sqlite":  # compare rows, not bytes
                try:
                    current_sha[rel] = _sqlite_content_sha256(current[rel])
                    wanted = snap[rel].get("content_sha256") or _sqlite_content_sha256(data / rel)
                except sqlite3.Error:
                    current_sha[rel], wanted = _sha256(current[rel]), None
            else:
                current_sha[rel], wanted = _sha256(current[rel]), snap[rel]["sha256"]
            if current_sha[rel] == wanted:
                unchanged += 1
            else:
                overwrite.append(rel)
        for rel in remove:
            current_sha[rel] = _sha256(current[rel])
        databases = []
        for db in manifest["sqlite"]:
            now_summary = None
            if db["path"] in current:
                try:
                    now_summary = _sqlite_summary(current[db["path"]])["tables"]
                except sqlite3.Error:
                    now_summary = "unreadable"
            databases.append({"path": db["path"], "restored_tables": db["tables"], "current_tables": now_summary})
        derived = sorted(p.relative_to(self.root).as_posix() for p in (self.root / STATE_DIR).glob("*")
                         if p.is_file() and p.name.startswith(DERIVED_STATE)) if (self.root / STATE_DIR).is_dir() else []
        digest_input = {"snapshot_id": snapshot_id, "manifest": [(f["path"], f["sha256"]) for f in manifest["files"]],
                        "add": add, "overwrite": [(r, current_sha[r]) for r in overwrite],
                        "remove": [(r, current_sha[r]) for r in remove]}
        plan_digest = hashlib.sha256(json.dumps(digest_input, sort_keys=True).encode("utf-8")).hexdigest()
        return {"ok": True, "snapshot_id": snapshot_id, "created_at": manifest["created_at"],
                "add": add, "overwrite": overwrite, "remove": remove, "unchanged": unchanged,
                "sqlite": databases, "derived_indexes_rebuilt": derived, "plan_digest": plan_digest,
                "note": "Read-only plan. Restoring is CLI-only: common-ai-memory snapshot restore-plan / restore."}

    # --- one-time restore tokens ---------------------------------------------------------------------
    def _tokens(self) -> dict[str, Any]:
        path = self.root / TOKEN_FILE
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_tokens(self, tokens: dict[str, Any]) -> None:
        path = self.root / TOKEN_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        now = _iso(self.clock())
        tokens = {k: v for k, v in tokens.items() if v.get("expires_at", "") > now and not v.get("used")}
        tmp = path.with_name(path.name + f".tmp-{secrets.token_hex(3)}")
        tmp.write_text(json.dumps(tokens), encoding="utf-8")
        os.replace(tmp, path)

    def issue_restore_token(self, snapshot_id: str) -> dict[str, Any]:
        plan = self.restore_plan(snapshot_id)
        token = "rst-" + secrets.token_urlsafe(18)
        tokens = self._tokens()
        tokens[hashlib.sha256(token.encode()).hexdigest()] = {
            "snapshot_id": snapshot_id, "plan_digest": plan["plan_digest"],
            "expires_at": _iso(self.clock() + TOKEN_TTL), "used": False}
        self._save_tokens(tokens)
        return {**plan, "restore_token": token, "token_expires_in_minutes": int(TOKEN_TTL.total_seconds() // 60)}

    def _consume_token(self, snapshot_id: str, token: str) -> str:
        tokens = self._tokens()
        key = hashlib.sha256(str(token).encode()).hexdigest()
        entry = tokens.get(key)
        if not entry or entry.get("used"):
            raise SnapshotError("unknown or already used restore token; run restore-plan again")
        if entry["expires_at"] <= _iso(self.clock()):
            raise SnapshotError("restore token expired; run restore-plan again")
        if entry["snapshot_id"] != snapshot_id:
            raise SnapshotError("restore token belongs to a different snapshot")
        entry["used"] = True
        tokens[key] = entry
        path = self.root / TOKEN_FILE
        path.write_text(json.dumps(tokens), encoding="utf-8")  # mark used before anything else happens
        return entry["plan_digest"]

    # --- restore ---------------------------------------------------------------------------------------
    def restore(self, snapshot_id: str, *, confirm: str) -> dict[str, Any]:
        expected_digest = self._consume_token(snapshot_id, confirm)
        verification = self.verify(snapshot_id)
        if not verification["ok"]:
            raise SnapshotError(f"snapshot {snapshot_id} failed verification; nothing was restored")
        plan = self.restore_plan(snapshot_id)
        if plan["plan_digest"] != expected_digest:
            raise SnapshotError("data changed since the restore plan was made; run restore-plan again")
        try:
            safety = self.create(label=f"pre-restore safety before {snapshot_id}", reason="pre_restore")
        except Exception as exc:
            raise SnapshotError(f"could not create the pre-restore safety snapshot ({type(exc).__name__}); "
                                "nothing was restored") from exc

        source = self.base / snapshot_id / "data"
        stamp = secrets.token_hex(4)
        staging = self.root / f".restore-staging-{stamp}"
        rollback = self.root / f".restore-rollback-{stamp}"
        manifest = {f["path"]: f for f in self._manifest(snapshot_id)["files"]}
        try:
            for rel in plan["add"] + plan["overwrite"]:
                target = staging / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / rel, target)
                if _sha256(target) != manifest[rel]["sha256"]:
                    raise SnapshotError(f"staged copy of {rel} does not match the snapshot")
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        moved: list[tuple[Path, Path]] = []   # (live path, rollback path) for files moved aside
        placed: list[Path] = []               # live paths that received a staged file
        try:
            with self._locked() as lock:
                if self.restore_plan(snapshot_id)["plan_digest"] != expected_digest:
                    raise SnapshotError("data changed while preparing the restore; nothing was restored. "
                                        "Run restore-plan again.")
                try:
                    def aside(live: Path) -> None:
                        if live.exists():
                            keep = rollback / live.relative_to(self.root)
                            keep.parent.mkdir(parents=True, exist_ok=True)
                            os.replace(live, keep)
                            moved.append((live, keep))

                    for rel in plan["overwrite"] + plan["remove"]:
                        self._keep_alive(lock)
                        live = self.root / rel
                        aside(live)
                        if _is_sqlite(live):
                            for suffix in ("-wal", "-shm", "-journal"):
                                aside(live.with_name(live.name + suffix))
                    for rel in plan["derived_indexes_rebuilt"]:
                        aside(self.root / rel)
                    for rel in plan["add"] + plan["overwrite"]:
                        self._keep_alive(lock)
                        live = self.root / rel
                        live.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(staging / rel, live)
                        placed.append(live)
                    for rel in plan["add"] + plan["overwrite"]:
                        live = self.root / rel
                        if _sha256(live) != manifest[rel]["sha256"]:
                            raise SnapshotError(f"restored {rel} does not match the snapshot")
                        if _is_sqlite(live) and _sqlite_summary(live)["integrity"] != "ok":
                            raise SnapshotError(f"restored database {rel} failed its integrity check")
                except BaseException:
                    for live in reversed(placed):
                        live.unlink(missing_ok=True)
                    for live, keep in reversed(moved):
                        live.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(keep, live)
                    raise
        except SnapshotError:
            raise
        except Exception as exc:
            raise SnapshotError(f"restore failed and was rolled back ({type(exc).__name__}: {exc}). "
                                "Stop running services before restoring.") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(rollback, ignore_errors=True)
        return {"ok": True, "restored": snapshot_id, "safety_snapshot": safety["snapshot_id"],
                "added": len(plan["add"]), "overwritten": len(plan["overwrite"]), "removed": len(plan["remove"]),
                "derived_indexes_removed": len(plan["derived_indexes_rebuilt"]),
                "next": "Rebuild the search index (common-ai-memory-search rebuild) and, if used, the vector index."}


def snapshot_events(project_root: str | Path) -> list[dict[str, Any]]:
    """Read-only rows for the timeline."""
    manager = SnapshotManager(project_root)
    return [s for s in manager.list()["snapshots"] if "created_at" in s]


def _software_version() -> str | None:
    try:
        import config

        return getattr(config, "__version__", None)
    except Exception:
        return None


def cli(argv: list[str] | None = None) -> int:
    from config import data_root, load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(prog="common-ai-memory snapshot",
                                     description="Restore points for Common AI Memory data (no model involved)")
    parser.add_argument("--root", default=str(data_root()))
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="create a snapshot now")
    create.add_argument("--label", default="")
    sub.add_parser("list", help="list snapshots")
    for name, text in (("verify", "re-check a snapshot's files and databases"),
                       ("restore-plan", "show what a restore would change and issue a one-time token")):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("snapshot_id")
    restore = sub.add_parser("restore", help="restore a snapshot (needs a token from restore-plan)")
    restore.add_argument("snapshot_id")
    restore.add_argument("--confirm", required=True, metavar="RESTORE_TOKEN")
    args = parser.parse_args(argv)
    manager = SnapshotManager(args.root)
    try:
        if args.command == "create":
            result = manager.create(label=args.label)
        elif args.command == "list":
            result = manager.list()
        elif args.command == "verify":
            result = manager.verify(args.snapshot_id)
        elif args.command == "restore-plan":
            result = manager.issue_restore_token(args.snapshot_id)
        else:
            result = manager.restore(args.snapshot_id, confirm=args.confirm)
    except SnapshotError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(cli())
