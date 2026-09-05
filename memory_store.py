from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory_house import decorate_record, room_dir, validate_category_shape


_FRONTMATTER_KEYS = (
    "id",
    "owner",
    "scope",
    "category",
    "created_at",
    "updated_at",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _date_from_iso(value: str) -> str:
    value = str(value or "")
    if len(value) >= 10 and value[4:5] == "-" and value[7:8] == "-":
        return value[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _safe_agent(value: str) -> str:
    value = (value or "").strip().lower()
    safe = "".join(ch for ch in value if ch.isalnum() or ch in "-_")
    if not safe or safe != value:
        raise ValueError(f"invalid agent id: {value!r}")
    return safe


def _safe_memory_id(value: str) -> str:
    value = (value or "").strip()
    safe = "".join(ch for ch in value if ch.isalnum() or ch in "-_:")
    if not safe or safe != value:
        raise ValueError(f"invalid memory id: {value!r}")
    return safe


class MemoryStore:
    """Room-first Markdown memory store.

    Physical layout:
      memory/<section>/<subject>/<date>_<id>__<owner>__<scope>.md

    Theme comes first in the filesystem. Ownership remains metadata-enforced:
    every AI can read readable records, but update/forget require owner==agent_id.
    """

    def __init__(self, project_root: str | Path, agent_id: str) -> None:
        self.project_root = Path(project_root).resolve()
        self.memory_root = self.project_root / "memory"
        self.memory_root.mkdir(parents=True, exist_ok=True)
        self.agent_id = _safe_agent(agent_id)
        self.lock_path = self.project_root / ".memory-store.lock"

    # ---------- public API ----------

    def remember(self, content: str, category: str, visibility: str = "agent") -> dict[str, Any]:
        content = self._clean_content(content)
        category = validate_category_shape(category)
        visibility = (visibility or "agent").strip().lower()
        if visibility not in {"agent", "shared"}:
            raise ValueError("visibility must be 'agent' or 'shared'")

        record = {
            "id": secrets.token_hex(16),
            "owner": self.agent_id,
            "scope": visibility,
            "category": category,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "content": content,
        }

        lock = self._acquire_lock()
        try:
            path = self._write_record(record)
        finally:
            self._release_lock(lock)

        git = self._git_commit(
            [path],
            f"memory: {self.agent_id} remember {category}",
        )
        result = decorate_record(record)
        result["ok"] = True
        result["git"] = git
        return result

    def recall(self, query: str = "", owner: str = "all", limit: int = 10) -> list[dict[str, Any]]:
        query = (query or "").strip().lower()
        items = self._filtered(owner=owner)

        if query:
            scored: list[tuple[int, str, dict[str, Any]]] = []
            terms = [t for t in query.split() if t]
            for item in items:
                hay = " ".join(
                    [
                        str(item.get("content") or ""),
                        str(item.get("category") or ""),
                        str(item.get("owner") or ""),
                        str(item.get("location") or ""),
                    ]
                ).lower()
                if query in hay:
                    score = 100 + hay.count(query)
                elif terms and all(t in hay for t in terms):
                    score = 50 + sum(hay.count(t) for t in terms)
                else:
                    continue
                scored.append((score, str(item.get("updated_at") or ""), item))
            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
            items = [x[2] for x in scored]
        else:
            items.sort(key=self._sort_key, reverse=True)

        return items[: self._safe_limit(limit)]

    def recent(self, limit: int = 10, owner: str = "all") -> list[dict[str, Any]]:
        items = self._filtered(owner=owner)
        items.sort(key=self._sort_key, reverse=True)
        return items[: self._safe_limit(limit)]

    def update(self, memory_id: str, content: str, category: str | None = None) -> dict[str, Any]:
        memory_id = _safe_memory_id(memory_id)
        content = self._clean_content(content)
        found = self._find_record(memory_id)
        if found is None:
            raise KeyError(f"memory not found: {memory_id}")

        old_path, record = found
        if record.get("owner") != self.agent_id:
            raise PermissionError("cannot update a memory owned by another identity")

        new_category = (
            validate_category_shape(category)
            if category is not None
            else str(record.get("category") or "")
        )
        record["content"] = content
        record["category"] = new_category
        record["updated_at"] = _now_iso()

        lock = self._acquire_lock()
        try:
            new_path = self._path_for_record(record)
            new_path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(new_path, self._serialize(record))
            if old_path.resolve() != new_path.resolve():
                old_path.unlink(missing_ok=True)
        finally:
            self._release_lock(lock)

        git = self._git_commit(
            [old_path, new_path],
            f"memory: {self.agent_id} update {memory_id[:12]}",
        )
        result = decorate_record(record)
        result["ok"] = True
        result["git"] = git
        return result

    def forget(self, memory_id: str) -> dict[str, Any]:
        memory_id = _safe_memory_id(memory_id)
        found = self._find_record(memory_id)
        if found is None:
            raise KeyError(f"memory not found: {memory_id}")

        path, record = found
        if record.get("owner") != self.agent_id:
            raise PermissionError("cannot delete a memory owned by another identity")

        lock = self._acquire_lock()
        try:
            path.unlink(missing_ok=False)
        finally:
            self._release_lock(lock)

        git = self._git_commit(
            [path],
            f"memory: {self.agent_id} forget {memory_id[:12]}",
        )
        return {
            "ok": True,
            "id": memory_id,
            "owner": self.agent_id,
            "category": record.get("category"),
            "location": decorate_record(record).get("location"),
            "git": git,
        }

    def import_record(self, record: dict[str, Any]) -> Path:
        """Migration-only helper. Preserves id/owner/scope/timestamps."""
        clean = {
            "id": _safe_memory_id(str(record["id"])),
            "owner": _safe_agent(str(record["owner"])),
            "scope": str(record.get("scope") or "agent").strip().lower(),
            "category": validate_category_shape(str(record["category"])),
            "created_at": str(record.get("created_at") or _now_iso()),
            "updated_at": str(record.get("updated_at") or record.get("created_at") or _now_iso()),
            "content": self._clean_content(str(record.get("content") or "")),
        }
        if clean["scope"] not in {"agent", "shared"}:
            raise ValueError(f"invalid imported scope: {clean['scope']!r}")
        return self._write_record(clean)

    # ---------- record I/O ----------

    def _iter_memory_paths(self):
        for path in self.memory_root.rglob("*.md"):
            rel = path.relative_to(self.memory_root)
            if not rel.parts:
                continue
            if rel.parts[0].startswith("_"):
                continue
            if path.name.startswith("_"):
                continue
            yield path

    def _read_all(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in self._iter_memory_paths():
            try:
                record = self._parse(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not record:
                continue
            items.append(decorate_record(record))

        # Human-maintained house documents remain readable as manuals.
        house = self.memory_root / "_house"
        if house.exists():
            for path in sorted(house.glob("*.md")):
                try:
                    stat = path.stat()
                    content = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                stamp = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
                items.append(
                    decorate_record(
                        {
                            "id": f"human:_house/{path.name}",
                            "owner": "human",
                            "scope": "human",
                            "category": "manual",
                            "created_at": stamp,
                            "updated_at": stamp,
                            "content": content,
                        }
                    )
                )
        return items

    def _filtered(self, owner: str) -> list[dict[str, Any]]:
        owner = (owner or "all").strip().lower()
        items = self._read_all()
        if owner == "all":
            return items
        if owner == "shared":
            return [x for x in items if str(x.get("scope") or "").lower() == "shared"]
        if owner == "human":
            return [x for x in items if str(x.get("owner") or "").lower() == "human"]
        return [x for x in items if str(x.get("owner") or "").lower() == owner]

    def _find_record(self, memory_id: str) -> tuple[Path, dict[str, Any]] | None:
        for path in self._iter_memory_paths():
            try:
                record = self._parse(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if record and str(record.get("id")) == memory_id:
                return path, record
        return None

    def _path_for_record(self, record: dict[str, Any]) -> Path:
        category = validate_category_shape(str(record["category"]))
        owner = _safe_agent(str(record["owner"]))
        scope = str(record["scope"]).strip().lower()
        memory_id = _safe_memory_id(str(record["id"]))
        date = _date_from_iso(str(record.get("created_at") or ""))
        filename = f"{date}_{memory_id}__{owner}__{scope}.md"
        return room_dir(self.memory_root, category) / filename

    def _write_record(self, record: dict[str, Any]) -> Path:
        path = self._path_for_record(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(f"memory file already exists: {path}")
        self._atomic_write(path, self._serialize(record))
        return path

    @staticmethod
    def _serialize(record: dict[str, Any]) -> str:
        lines = ["---"]
        for key in _FRONTMATTER_KEYS:
            lines.append(f"{key}: {json.dumps(str(record.get(key) or ''), ensure_ascii=False)}")
        lines.extend(["---", "", str(record.get("content") or "").strip(), ""])
        return "\n".join(lines)

    @staticmethod
    def _parse(text: str) -> dict[str, Any] | None:
        if not text.startswith("---\n"):
            return None
        end = text.find("\n---\n", 4)
        if end < 0:
            return None
        header = text[4:end]
        content = text[end + 5 :].lstrip("\n").rstrip()
        meta: dict[str, str] = {}
        for line in header.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            if key not in _FRONTMATTER_KEYS:
                continue
            meta[key] = str(json.loads(value.strip()))
        if any(not meta.get(k) for k in _FRONTMATTER_KEYS):
            raise ValueError("incomplete memory frontmatter")
        meta["content"] = content
        return meta

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{secrets.token_hex(3)}")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _clean_content(content: str) -> str:
        content = (content or "").strip()
        if not content:
            raise ValueError("memory content is empty")
        if len(content) > 20000:
            raise ValueError("memory content is too long")
        return content

    @staticmethod
    def _safe_limit(limit: int) -> int:
        try:
            value = int(limit)
        except Exception:
            value = 10
        return max(1, min(value, 1000))

    @staticmethod
    def _sort_key(item: dict[str, Any]) -> str:
        return str(item.get("updated_at") or item.get("created_at") or "")

    # ---------- cross-process write lock ----------

    def _acquire_lock(self, timeout: float = 8.0) -> Path:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {time.time()}".encode("ascii"))
                os.close(fd)
                return self.lock_path
            except FileExistsError:
                try:
                    if time.time() - self.lock_path.stat().st_mtime > 30:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError("memory store is busy; retry shortly")
                time.sleep(0.05)

    @staticmethod
    def _release_lock(lock: Path) -> None:
        lock.unlink(missing_ok=True)

    # ---------- git ----------

    def _git_commit(self, paths: list[Path], message: str) -> str:
        git_dir = self.project_root / ".git"
        if not git_dir.exists():
            return "skipped-no-git"

        rels: list[str] = []
        for path in paths:
            try:
                rels.append(str(path.resolve().relative_to(self.project_root)))
            except Exception:
                continue
        rels = sorted(set(rels))
        if not rels:
            return "skipped"

        try:
            add = subprocess.run(
                ["git", "add", "-A", "--", *rels],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if add.returncode != 0:
                return "error-add"

            diff = subprocess.run(
                ["git", "diff", "--cached", "--quiet", "--", *rels],
                cwd=self.project_root,
                check=False,
            )
            if diff.returncode == 0:
                return "clean"

            commit = subprocess.run(
                ["git", "commit", "-m", message, "--", *rels],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                check=False,
            )
            return "committed" if commit.returncode == 0 else "error-commit"
        except OSError:
            return "unavailable"
