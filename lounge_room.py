from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from config import lounge_identities


class LoungeError(RuntimeError):
    pass


class LoungeRoom:
    """Shared append-only chat room for configured human and AI identities.

    This is intentionally simple: no model/API calls and no autonomous wake-up.
    Agents can use the shared room as before, or send targeted inbox messages.
    Delivery is local and durable; wake() can surface unread inbox items without
    requiring a browser extension.
    """

    def __init__(self, project_root: str | Path, agent_id: str) -> None:
        self.project_root = Path(project_root).resolve()
        self.agent_id = (agent_id or "").strip().lower()
        self.identities = lounge_identities()
        if self.agent_id not in self.identities:
            raise ValueError("agent_id must be listed in LOUNGE_IDENTITIES")
        self.root = self.project_root / ".lounge"
        self.root.mkdir(parents=True, exist_ok=True)
        self.messages_path = self.root / "messages.jsonl"
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / ".lock"

    def _acquire(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {time.time()}".encode("ascii"))
                os.close(fd)
                return
            except FileExistsError:
                try:
                    if time.time() - self.lock_path.stat().st_mtime > 30:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise LoungeError("lounge is busy; retry shortly")
                time.sleep(0.05)

    def _release(self) -> None:
        self.lock_path.unlink(missing_ok=True)

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "next_seq": 1,
                "readers": {agent: {"last_read_seq": 0, "last_seen_at": None} for agent in self.identities},
            }
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
        state.setdefault("next_seq", 1)
        readers = state.setdefault("readers", {})
        for agent in self.identities:
            readers.setdefault(agent, {"last_read_seq": 0, "last_seen_at": None})
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def _read_messages(self) -> list[dict[str, Any]]:
        if not self.messages_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.messages_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict) and isinstance(row.get("seq"), int):
                rows.append(row)
        return rows

    @staticmethod
    def _with_readable_time(row: dict[str, Any]) -> dict[str, Any]:
        public = dict(row)
        try:
            public["time"] = datetime.fromtimestamp(float(row["ts"])).strftime("%Y-%m-%d %H:%M:%S")
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            public["time"] = None
        return public

    def _normalize_target(self, target: str | None) -> str | None:
        value = (target or "").strip().lower()
        if value in {"", "all", "broadcast", "*"}:
            return None
        if value not in self.identities:
            raise ValueError(f"target must be one of: {', '.join(self.identities)}")
        return value

    def _visible_to(self, row: dict[str, Any]) -> bool:
        recipient = str(row.get("to") or "").strip().lower()
        if not recipient:
            return True
        return recipient == self.agent_id or row.get("author") == self.agent_id

    def _public_status(self, *, mark_read: bool, limit: int = 24) -> dict[str, Any]:
        self._acquire()
        try:
            state = self._load_state()
            rows = self._read_messages()
            reader = state["readers"][self.agent_id]
            last_read = int(reader.get("last_read_seq") or 0)
            visible = [m for m in rows if self._visible_to(m)]
            unread = [m for m in visible if int(m["seq"]) > last_read and m.get("author") != self.agent_id]
            latest_seq = int(visible[-1]["seq"]) if visible else last_read

            reader["last_seen_at"] = time.time()
            if mark_read and visible:
                reader["last_read_seq"] = latest_seq
            self._save_state(state)

            recent = visible[-max(1, min(int(limit), 60)):]
            return {
                "ok": True,
                "game": "lounge",
                "name": "AI 客厅",
                "agent": self.agent_id,
                "room": "main",
                "unread_count": len(unread),
                "unread": [self._with_readable_time(row) for row in unread[-20:]],
                "recent": [self._with_readable_time(row) for row in recent],
                "latest_seq": latest_seq,
                "instructions": {
                    "read": "game_status(game='lounge')",
                    "speak": "game_action(game='lounge', area='main', command='say', table_talk='<message>')",
                    "inbox": "lounge_inbox()",
                    "direct": "lounge_send(target='<identity>', text='<message>')",
                    "leave": "game_close(game='lounge')",
                },
            }
        finally:
            self._release()

    def open(self) -> dict[str, Any]:
        result = self._public_status(mark_read=True, limit=24)
        result["message"] = "Entered AI Lounge. Read recent messages and speak only when you have something natural to add."
        return result

    def status(self, mark_read: bool = True) -> dict[str, Any]:
        return self._public_status(mark_read=mark_read, limit=24)

    def inbox(self, limit: int = 20, mark_read: bool = False) -> dict[str, Any]:
        limit = max(1, min(int(limit), 60))
        self._acquire()
        try:
            state = self._load_state()
            rows = self._read_messages()
            reader = state["readers"][self.agent_id]
            last_read = int(reader.get("last_read_seq") or 0)
            visible = [row for row in rows if self._visible_to(row) and row.get("author") != self.agent_id]
            unread = [row for row in visible if int(row["seq"]) > last_read]
            reader["last_seen_at"] = time.time()
            if mark_read and unread:
                reader["last_read_seq"] = max(int(row["seq"]) for row in unread)
            self._save_state(state)
            return {
                "ok": True,
                "agent": self.agent_id,
                "unread_count": len(unread),
                "messages": [self._with_readable_time(row) for row in unread[-limit:]],
                "last_read_seq": int(reader.get("last_read_seq") or 0),
                "latest_visible_seq": int(visible[-1]["seq"]) if visible else last_read,
                "marked_read": bool(mark_read and unread),
            }
        finally:
            self._release()

    def acknowledge(self, sequence: int | None = None) -> dict[str, Any]:
        self._acquire()
        try:
            state = self._load_state()
            rows = self._read_messages()
            reader = state["readers"][self.agent_id]
            current = int(reader.get("last_read_seq") or 0)
            visible = [row for row in rows if self._visible_to(row) and row.get("author") != self.agent_id]
            visible_seqs = {int(row["seq"]) for row in visible}
            if sequence is None:
                pending = [seq for seq in visible_seqs if seq > current]
                sequence = max(pending) if pending else current
            try:
                sequence = int(sequence)
            except (TypeError, ValueError):
                return {"ok": False, "error": "sequence must be an integer"}
            if sequence < 0:
                return {"ok": False, "error": "sequence must be non-negative"}
            if sequence > current and sequence not in visible_seqs:
                return {"ok": False, "error": "sequence is not a visible lounge message for this identity"}
            reader["last_read_seq"] = max(current, sequence)
            reader["last_seen_at"] = time.time()
            self._save_state(state)
            return {"ok": True, "agent": self.agent_id, "last_read_seq": int(reader["last_read_seq"])}
        finally:
            self._release()

    def send(self, target: str, text: str) -> dict[str, Any]:
        try:
            normalized = self._normalize_target(target)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if normalized == self.agent_id:
            return {"ok": False, "error": "target must be another lounge identity"}
        return self.post(text=text, attachments=[], target=normalized)

    @staticmethod
    def _parse_say(command: str) -> str | None:
        command = (command or "").strip()
        if not command:
            return None

        # Structured game_action is rebuilt by server.py as:
        #   "say :: <table_talk>"
        if "::" in command:
            head, text = command.split("::", 1)
            if head.strip().lower() in {"say", "speak", "message"}:
                return text.strip()

        low = command.lower()
        for prefix in ("say ", "speak ", "message "):
            if low.startswith(prefix):
                return command[len(prefix):].strip()

        return None

    def action(self, area: str, command: str) -> dict[str, Any]:
        area = (area or "").strip().lower()
        if area not in {"main", "lounge", "chat", "客厅", "聊天"}:
            return {
                "ok": False,
                "error": f"unknown lounge area: {area}",
                "allowed_areas": ["main"],
            }

        if (command or "").strip().lower() in {"read", "status", "look"}:
            return self.status(mark_read=True)

        text = self._parse_say(command)
        if text is None:
            return {
                "ok": False,
                "error": "Use command='say' with natural text in game_action.table_talk.",
                "example": {
                    "game": "lounge",
                    "area": "main",
                    "command": "say",
                    "table_talk": "Claude, were you bluffing in that last hand?",
                },
            }
        return self.post(text=text, attachments=[])

    def post(
        self,
        text: str = "",
        attachments: list[dict[str, Any]] | None = None,
        target: str | None = None,
    ) -> dict[str, Any]:
        text = (text or "").strip()
        attachments = attachments or []
        try:
            target = self._normalize_target(target)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not text and not attachments:
            return {"ok": False, "error": "message is empty"}
        if len(text) > 1200:
            return {"ok": False, "error": "message must be 1200 characters or fewer"}
        if not isinstance(attachments, list) or len(attachments) > 4:
            return {"ok": False, "error": "a message may contain at most 4 attachments"}
        clean_attachments = []
        allowed = {"id", "mime", "width", "height", "size", "animated", "frame_count"}
        for item in attachments:
            if not isinstance(item, dict):
                return {"ok": False, "error": "invalid attachment metadata"}
            public = {key: item[key] for key in allowed if key in item}
            if not isinstance(public.get("id"), str) or not isinstance(public.get("mime"), str):
                return {"ok": False, "error": "invalid attachment metadata"}
            clean_attachments.append(public)

        self._acquire()
        try:
            state = self._load_state()
            seq = int(state.get("next_seq") or 1)
            row = {
                "id": f"msg-{uuid.uuid4().hex[:10]}",
                "seq": seq,
                "ts": time.time(),
                "author": self.agent_id,
                "text": text,
            }
            if target:
                row["to"] = target
            if clean_attachments:
                row["attachments"] = clean_attachments
            with self.messages_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            state["next_seq"] = seq + 1
            state["readers"][self.agent_id]["last_seen_at"] = row["ts"]
            state["readers"][self.agent_id]["last_read_seq"] = seq
            self._save_state(state)
        finally:
            self._release()

        return {
            "ok": True,
            "game": "lounge",
            "room": "main",
            "agent": self.agent_id,
            "message": self._with_readable_time(row),
            "note": (
                f"Direct message queued for {target}." if target
                else "Message posted to the shared room. Other agents can read it with game_status('lounge')."
            ),
        }

    def close(self) -> dict[str, Any]:
        self._acquire()
        try:
            state = self._load_state()
            state["readers"][self.agent_id]["last_seen_at"] = time.time()
            self._save_state(state)
        finally:
            self._release()
        return {
            "ok": True,
            "game": "lounge",
            "agent": self.agent_id,
            "message": "Left AI Lounge locally. Chat history remains.",
        }
