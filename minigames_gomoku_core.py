from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from memory_store import MemoryStore


BOARD_SIZE = 15
COLS = "ABCDEFGHJKLMNOP"  # Go-style coordinates: skip I.
EMPTY = "."
STONE = {"black": "X", "white": "O"}


class MiniGameError(RuntimeError):
    pass


class MiniGames:
    """Persistent local two-agent mini-game referee with table talk."""

    def __init__(self, project_root: str | Path, agent_id: str) -> None:
        self.project_root = Path(project_root).resolve()
        self.root = self.project_root / ".games" / "minigames"
        self.root.mkdir(parents=True, exist_ok=True)
        self.agent_id = (agent_id or "").strip().lower()
        if not self.agent_id:
            raise ValueError("agent_id is required")
        # A completed built-in match becomes one shared long-term memory.
        # The identity that actually ends the match owns the record, preserving
        # Common AI Memory's owner-write isolation.
        self.memory_store = MemoryStore(self.project_root, self.agent_id)

    def list_games(self) -> dict[str, Any]:
        return {
            "gomoku": {
                "name": "五子棋",
                "status": "available",
                "board": "15x15",
                "commands": [
                    "create <opponent>",
                    "status <match_id>",
                    "move <match_id> <coord>",
                    "move <match_id> <coord> :: <short table-talk>",
                    "say <match_id> <message>",
                    "wait <match_id> [seconds]",
                    "resign <match_id>",
                    "active",
                ],
            },
            "tic-tac-toe": {"name": "井字棋", "status": "planned"},
            "reversi": {"name": "黑白棋", "status": "planned"},
            "go": {"name": "围棋", "status": "planned"},
        }

    def _gomoku_dir(self) -> Path:
        path = self.root / "gomoku"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _match_path(self, match_id: str) -> Path:
        safe = "".join(ch for ch in match_id.lower() if ch.isalnum() or ch in "-_")
        if not safe or safe != match_id.lower():
            raise MiniGameError("invalid match id")
        return self._gomoku_dir() / f"{safe}.json"

    def _lock_path(self, match_id: str) -> Path:
        return self._gomoku_dir() / f"{match_id}.lock"

    def _acquire_lock(self, match_id: str, timeout: float = 5.0) -> Path:
        lock = self._lock_path(match_id)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {time.time()}".encode("ascii"))
                os.close(fd)
                return lock
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > 30:
                        lock.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise MiniGameError("game state is busy; retry shortly")
                time.sleep(0.05)

    @staticmethod
    def _release_lock(lock: Path) -> None:
        lock.unlink(missing_ok=True)

    @staticmethod
    def _new_board() -> list[list[str]]:
        return [[EMPTY for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]

    def _load(self, match_id: str) -> dict[str, Any]:
        path = self._match_path(match_id)
        if not path.exists():
            raise MiniGameError(f"match not found: {match_id}")
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            state.setdefault("chat", [])
            return state
        except Exception as exc:
            raise MiniGameError(f"corrupt match state: {match_id}") from exc

    def _save(self, state: dict[str, Any]) -> None:
        path = self._match_path(state["id"])
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    @staticmethod
    def _coord_to_rc(coord: str) -> tuple[int, int]:
        coord = coord.strip().upper()
        if len(coord) < 2:
            raise MiniGameError("invalid coordinate; use A1..P15 (I is skipped)")
        col_text = coord[0]
        row_text = coord[1:]
        if col_text not in COLS:
            raise MiniGameError("invalid column; use A..P with I skipped")
        try:
            row_num = int(row_text)
        except ValueError as exc:
            raise MiniGameError("invalid row") from exc
        if not 1 <= row_num <= BOARD_SIZE:
            raise MiniGameError("row must be 1..15")
        return row_num - 1, COLS.index(col_text)

    @staticmethod
    def _rc_to_coord(row: int, col: int) -> str:
        return f"{COLS[col]}{row + 1}"

    @staticmethod
    def _check_win(board: list[list[str]], row: int, col: int, stone: str) -> bool:
        for dr, dc in ((1, 0), (0, 1), (1, 1), (1, -1)):
            count = 1
            for sign in (1, -1):
                r, c = row + dr * sign, col + dc * sign
                while 0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE and board[r][c] == stone:
                    count += 1
                    r += dr * sign
                    c += dc * sign
            if count >= 5:
                return True
        return False

    @staticmethod
    def _color_for_agent(state: dict[str, Any], agent: str) -> str | None:
        for color in ("black", "white"):
            if state["players"].get(color) == agent:
                return color
        return None

    @staticmethod
    def _clean_message(message: str) -> str:
        message = " ".join((message or "").strip().split())
        if not message:
            raise MiniGameError("message is empty")
        if len(message) > 180:
            raise MiniGameError("table-talk is limited to 180 characters")
        return message

    def _append_chat(self, state: dict[str, Any], message: str) -> None:
        message = self._clean_message(message)
        state.setdefault("chat", []).append(
            {
                "n": len(state.get("chat", [])) + 1,
                "agent": self.agent_id,
                "message": message,
                "at": time.time(),
            }
        )
        # Keep state small even after many games.
        if len(state["chat"]) > 120:
            state["chat"] = state["chat"][-120:]

    @staticmethod
    def _finished_reason_text(state: dict[str, Any]) -> str:
        reason = str(state.get("reason") or "")
        if reason == "five-in-a-row":
            return "五子连珠"
        if reason == "draw":
            return "和棋"
        if reason.endswith(" resigned"):
            who = reason[:-9].strip()
            return f"{who} 认输"
        return reason or "正常结束"

    def _gomoku_memory_content(self, state: dict[str, Any]) -> str:
        black = state.get("players", {}).get("black", "?")
        white = state.get("players", {}).get("white", "?")
        winner = state.get("winner")
        moves = state.get("moves", [])
        last = moves[-1] if moves else None
        date_text = time.strftime("%Y-%m-%d", time.localtime(state.get("updated_at", time.time())))
        result_text = f"胜者 {winner}" if winner else "双方和棋"
        parts = [
            f"{date_text}，Common AI Memory 内置五子棋对局 {state['id']} 结束。",
            f"{black} 执黑，{white} 执白；{result_text}；结束原因：{self._finished_reason_text(state)}；共 {len(moves)} 手。",
        ]
        if last:
            parts.append(f"最后落子：{last.get('agent')} {last.get('coord')}。")
        parts.append("完整棋盘、棋谱与对局聊天保留在小游戏持久化对局记录中。")
        return " ".join(parts)

    def _archive_finished_gomoku_locked(self, state: dict[str, Any]) -> None:
        """Best-effort, idempotent long-term archive for a finished match.

        Caller must hold the per-match lock. A MemoryStore lookup by match id
        prevents duplicate memories even if the process crashes after remember()
        but before the JSON marker is saved.
        """
        if state.get("status") != "finished" or state.get("memory_recorded"):
            return

        try:
            match_id = state["id"]
            existing = self.memory_store.recall(query=match_id, owner="all", limit=20)
            for item in existing:
                if (
                    item.get("category") == "game/minigames"
                    and match_id in str(item.get("content") or "")
                ):
                    state["memory_recorded"] = True
                    state["memory_id"] = item.get("id")
                    state.pop("memory_error", None)
                    return

            saved = self.memory_store.remember(
                content=self._gomoku_memory_content(state),
                category="game/minigames",
                visibility="shared",
            )
            state["memory_recorded"] = True
            state["memory_id"] = saved.get("id") if isinstance(saved, dict) else None
            state.pop("memory_error", None)
        except Exception as exc:
            # Never invalidate a completed game because archival failed. A later
            # status/wait can retry automatically.
            state["memory_recorded"] = False
            state["memory_error"] = f"{type(exc).__name__}: {exc}"

    def _ensure_finished_gomoku_archived(self, match_id: str) -> dict[str, Any]:
        lock = self._acquire_lock(match_id)
        try:
            state = self._load(match_id)
            if state.get("status") == "finished" and not state.get("memory_recorded"):
                self._archive_finished_gomoku_locked(state)
                self._save(state)
            return state
        finally:
            self._release_lock(lock)

    def create_gomoku(self, opponent: str) -> dict[str, Any]:
        opponent = (opponent or "").strip().lower()
        if not opponent:
            raise MiniGameError("opponent is required")
        if opponent == self.agent_id:
            raise MiniGameError("opponent must be a different agent")

        match_id = f"gmk-{secrets.token_hex(3)}"
        state = {
            "id": match_id,
            "game": "gomoku",
            "created_at": time.time(),
            "updated_at": time.time(),
            "status": "playing",
            "players": {"black": self.agent_id, "white": opponent},
            "turn": "black",
            "winner": None,
            "reason": None,
            "board": self._new_board(),
            "moves": [],
            "chat": [],
        }
        self._save(state)
        return self._public_state(state)

    def _public_state(self, state: dict[str, Any], include_board: bool = True) -> dict[str, Any]:
        result = {
            "id": state["id"],
            "game": "gomoku",
            "status": state["status"],
            "players": state["players"],
            "turn": state["turn"],
            "turn_agent": state["players"].get(state["turn"]) if state["status"] == "playing" else None,
            "winner": state["winner"],
            "reason": state["reason"],
            "move_count": len(state["moves"]),
            "last_move": state["moves"][-1] if state["moves"] else None,
            "chat": state.get("chat", [])[-20:],
            "memory_recorded": bool(state.get("memory_recorded", False)),
            "memory_id": state.get("memory_id"),
        }
        if state.get("memory_error"):
            result["memory_error"] = state["memory_error"]
        color = self._color_for_agent(state, self.agent_id)
        result["you"] = {
            "agent": self.agent_id,
            "color": color,
            "your_turn": state["status"] == "playing" and color == state["turn"],
        }
        if include_board:
            result["board"] = self.render_board(state["board"])
        return result

    @staticmethod
    def render_board(board: list[list[str]]) -> str:
        header = "   " + " ".join(COLS)
        rows = [header]
        for idx, row in enumerate(board, start=1):
            rows.append(f"{idx:>2} " + " ".join(row))
        return "\n".join(rows)

    def status_gomoku(self, match_id: str) -> dict[str, Any]:
        state = self._load(match_id)
        if self._color_for_agent(state, self.agent_id) is None:
            raise MiniGameError("this agent is not a player in that match")
        if state.get("status") == "finished" and not state.get("memory_recorded"):
            state = self._ensure_finished_gomoku_archived(match_id)
        return self._public_state(state)

    def say_gomoku(self, match_id: str, message: str) -> dict[str, Any]:
        lock = self._acquire_lock(match_id)
        try:
            state = self._load(match_id)
            if self._color_for_agent(state, self.agent_id) is None:
                raise MiniGameError("this agent is not a player in that match")
            self._append_chat(state, message)
            state["updated_at"] = time.time()
            self._save(state)
            return self._public_state(state, include_board=False)
        finally:
            self._release_lock(lock)

    def move_gomoku(
        self,
        match_id: str,
        coord: str,
        message: str | None = None,
    ) -> dict[str, Any]:
        lock = self._acquire_lock(match_id)
        try:
            state = self._load(match_id)
            if state["status"] != "playing":
                raise MiniGameError("match is already finished")

            color = self._color_for_agent(state, self.agent_id)
            if color is None:
                raise MiniGameError("this agent is not a player in that match")
            if state["turn"] != color:
                raise MiniGameError("not your turn")

            row, col = self._coord_to_rc(coord)
            if state["board"][row][col] != EMPTY:
                raise MiniGameError("intersection is occupied")

            stone = STONE[color]
            state["board"][row][col] = stone
            state["moves"].append(
                {
                    "n": len(state["moves"]) + 1,
                    "agent": self.agent_id,
                    "color": color,
                    "coord": self._rc_to_coord(row, col),
                }
            )
            if message:
                self._append_chat(state, message)
            state["updated_at"] = time.time()

            if self._check_win(state["board"], row, col, stone):
                state["status"] = "finished"
                state["winner"] = self.agent_id
                state["reason"] = "five-in-a-row"
            elif len(state["moves"]) >= BOARD_SIZE * BOARD_SIZE:
                state["status"] = "finished"
                state["winner"] = None
                state["reason"] = "draw"
            else:
                state["turn"] = "white" if color == "black" else "black"

            if state.get("status") == "finished":
                self._archive_finished_gomoku_locked(state)
            self._save(state)
            return self._public_state(state)
        finally:
            self._release_lock(lock)

    def resign_gomoku(self, match_id: str) -> dict[str, Any]:
        lock = self._acquire_lock(match_id)
        try:
            state = self._load(match_id)
            if state["status"] != "playing":
                return self._public_state(state)
            color = self._color_for_agent(state, self.agent_id)
            if color is None:
                raise MiniGameError("this agent is not a player in that match")
            other = "white" if color == "black" else "black"
            state["status"] = "finished"
            state["winner"] = state["players"][other]
            state["reason"] = f"{self.agent_id} resigned"
            state["updated_at"] = time.time()
            self._archive_finished_gomoku_locked(state)
            self._save(state)
            return self._public_state(state)
        finally:
            self._release_lock(lock)

    async def wait_gomoku(self, match_id: str, seconds: int = 25) -> dict[str, Any]:
        seconds = max(1, min(int(seconds), 30))
        deadline = time.monotonic() + seconds
        while True:
            state = self._load(match_id)
            color = self._color_for_agent(state, self.agent_id)
            if color is None:
                raise MiniGameError("this agent is not a player in that match")
            if state["status"] != "playing":
                if not state.get("memory_recorded"):
                    state = self._ensure_finished_gomoku_archived(match_id)
                return self._public_state(state)
            if state["turn"] == color:
                return self._public_state(state)
            if time.monotonic() >= deadline:
                result = self._public_state(state, include_board=False)
                result["waiting"] = True
                result["message"] = "Opponent has not moved yet; call wait again."
                return result
            await asyncio.sleep(0.5)

    def active_gomoku(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for path in sorted(
            self._gomoku_dir().glob("gmk-*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if self._color_for_agent(state, self.agent_id) is None:
                continue
            if state.get("status") == "playing":
                result.append(self._public_state(state, include_board=False))
            if len(result) >= 8:
                break
        return result

