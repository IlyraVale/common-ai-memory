from __future__ import annotations

import asyncio
import itertools
import json
import os
import random
import secrets
import time
from collections import Counter
from pathlib import Path
from typing import Any

from minigames_gomoku_core import MiniGameError, MiniGames as _GomokuMiniGames


SEA_COLS = "ABCDEFGHIJ"
SEA_SIZE = 10
BATTLESHIP_SPECS = [
    ("carrier", 5),
    ("battleship", 4),
    ("cruiser", 3),
    ("submarine", 3),
    ("destroyer", 2),
]

RANKS = "23456789TJQKA"
SUITS = "SHDC"
SUIT_SYMBOL = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
RANK_VALUE = {r: i + 2 for i, r in enumerate(RANKS)}
POKER_HAND_NAMES = {
    8: "straight-flush",
    7: "four-of-a-kind",
    6: "full-house",
    5: "flush",
    4: "straight",
    3: "three-of-a-kind",
    2: "two-pair",
    1: "one-pair",
    0: "high-card",
}


class MiniGames(_GomokuMiniGames):
    """Common AI Memory built-in games.

    Gomoku stays byte-for-byte compatible through the inherited proven core.
    New games use the same fixed AI identity, persistent local referee state,
    short table-talk, and shared long-term finish summaries.
    """

    # ------------------------------------------------------------------
    # Catalog
    # ------------------------------------------------------------------

    def list_games(self) -> dict[str, Any]:
        return {
            "gomoku": {
                "name": "五子棋",
                "status": "available",
                "type": "complete-information strategy",
                "commands": [
                    "create <opponent>",
                    "status <match_id>",
                    "move <match_id> <coord> (optional table talk: use game_action.table_talk)",
                    "say <match_id> <message>",
                    "wait <match_id> [seconds]",
                    "resign <match_id>",
                    "active",
                ],
            },
            "battleship": {
                "name": "海战棋",
                "status": "available",
                "type": "hidden-map deduction",
                "board": "10x10",
                "privacy": "Opponent ship cells are never returned through that opponent's MCP view while the match is active.",
                "commands": [
                    "create <opponent>",
                    "fleet <match_id> auto",
                    "status <match_id>",
                    "fire <match_id> <coord> (optional table talk: use game_action.table_talk)",
                    "say <match_id> <message>",
                    "wait <match_id> [seconds]",
                    "resign <match_id>",
                    "active",
                ],
            },
            "blackjack": {
                "name": "21点",
                "status": "available",
                "type": "cards / probability / dealer",
                "money": "No wagering. This is a zero-stakes rules simulation.",
                "privacy": "Other player's cards and dealer hole card stay hidden until the round finishes.",
                "commands": [
                    "create <opponent>",
                    "status <match_id>",
                    "hit <match_id> (optional table talk: use game_action.table_talk)",
                    "stand <match_id> (optional table talk: use game_action.table_talk)",
                    "say <match_id> <message>",
                    "wait <match_id> [seconds]",
                    "resign <match_id>",
                    "active",
                ],
            },
            "holdem": {
                "name": "德州扑克",
                "status": "available",
                "type": "hidden-hand strategy / betting / bluff",
                "format": "heads-up, one hand, 200 play chips each, blinds 1/2",
                "money": "Play chips only. No real-money wagering or cash-out.",
                "privacy": "Opponent hole cards stay hidden unless a showdown occurs.",
                "commands": [
                    "create <opponent>",
                    "status <match_id>",
                    "check <match_id> (optional table talk: use game_action.table_talk)",
                    "call <match_id> (optional table talk: use game_action.table_talk)",
                    "raise <match_id> <street-total> (optional table talk: use game_action.table_talk)",
                    "fold <match_id> (optional table talk: use game_action.table_talk)",
                    "say <match_id> <message>",
                    "wait <match_id> [seconds]",
                    "active",
                ],
            },
            "tic-tac-toe": {"name": "井字棋", "status": "planned"},
            "reversi": {"name": "黑白棋", "status": "planned"},
            "go": {"name": "围棋", "status": "planned"},
        }

    # ------------------------------------------------------------------
    # Shared helpers for the new games
    # ------------------------------------------------------------------

    def _game_dir(self, game: str) -> Path:
        path = self.root / game
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _safe_match_id(match_id: str) -> str:
        match_id = (match_id or "").strip().lower()
        safe = "".join(ch for ch in match_id if ch.isalnum() or ch in "-_")
        if not safe or safe != match_id:
            raise MiniGameError("invalid match id")
        return safe

    def _state_path(self, game: str, match_id: str) -> Path:
        return self._game_dir(game) / f"{self._safe_match_id(match_id)}.json"

    def _state_lock_path(self, game: str, match_id: str) -> Path:
        return self._game_dir(game) / f"{self._safe_match_id(match_id)}.lock"

    def _acquire_state_lock(self, game: str, match_id: str, timeout: float = 5.0) -> Path:
        lock = self._state_lock_path(game, match_id)
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
    def _release_state_lock(lock: Path) -> None:
        lock.unlink(missing_ok=True)

    def _load_state(self, game: str, match_id: str) -> dict[str, Any]:
        path = self._state_path(game, match_id)
        if not path.exists():
            raise MiniGameError(f"match not found: {match_id}")
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            state.setdefault("chat", [])
            return state
        except Exception as exc:
            raise MiniGameError(f"corrupt match state: {match_id}") from exc

    def _save_state(self, game: str, state: dict[str, Any]) -> None:
        path = self._state_path(game, state["id"])
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    @staticmethod
    def _new_match_id(prefix: str) -> str:
        return f"{prefix}-{secrets.token_hex(3)}"

    @staticmethod
    def _validate_opponent(agent: str, opponent: str) -> str:
        opponent = (opponent or "").strip().lower()
        if not opponent:
            raise MiniGameError("opponent is required")
        if opponent == agent:
            raise MiniGameError("opponent must be a different agent")
        return opponent

    def _is_player(self, state: dict[str, Any], agent: str | None = None) -> bool:
        agent = agent or self.agent_id
        players = state.get("players")
        if isinstance(players, dict):
            return agent in players.values()
        if isinstance(players, list):
            return agent in players
        return False

    def _other_agent(self, state: dict[str, Any], agent: str | None = None) -> str:
        agent = agent or self.agent_id
        players = state.get("player_order") or list((state.get("players") or {}).values())
        for candidate in players:
            if candidate != agent:
                return candidate
        raise MiniGameError("opponent is missing")

    def _append_chat_new(self, state: dict[str, Any], message: str) -> None:
        self._append_chat(state, message)

    def _archive_finished_state_locked(self, state: dict[str, Any], content: str) -> None:
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
                content=content,
                category="game/minigames",
                visibility="shared",
            )
            state["memory_recorded"] = True
            state["memory_id"] = saved.get("id") if isinstance(saved, dict) else None
            state.pop("memory_error", None)
        except Exception as exc:
            state["memory_recorded"] = False
            state["memory_error"] = f"{type(exc).__name__}: {exc}"

    def _new_game_common(self, match_id: str, game: str, opponent: str) -> dict[str, Any]:
        now = time.time()
        return {
            "id": match_id,
            "game": game,
            "created_at": now,
            "updated_at": now,
            "player_order": [self.agent_id, opponent],
            "players": {"first": self.agent_id, "second": opponent},
            "chat": [],
            "memory_recorded": False,
        }

    @staticmethod
    def _memory_meta(state: dict[str, Any]) -> dict[str, Any]:
        out = {
            "memory_recorded": bool(state.get("memory_recorded")),
            "memory_id": state.get("memory_id"),
        }
        if state.get("memory_error"):
            out["memory_error"] = state.get("memory_error")
        return out

    async def _wait_new_game(
        self,
        game: str,
        match_id: str,
        public_state,
        action_needed,
        seconds: int = 25,
    ) -> dict[str, Any]:
        seconds = max(1, min(int(seconds), 30))
        deadline = time.monotonic() + seconds
        while True:
            state = self._load_state(game, match_id)
            if not self._is_player(state):
                raise MiniGameError("this agent is not a player in that match")
            if state.get("status") == "finished" or action_needed(state):
                return public_state(state)
            if time.monotonic() >= deadline:
                result = public_state(state)
                result["waiting"] = True
                result["message"] = "Opponent has not completed the needed action yet; call wait again."
                return result
            await asyncio.sleep(0.5)

    def _active_new_game(self, game: str, public_state, limit: int = 8) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in sorted(
            self._game_dir(game).glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not self._is_player(state):
                continue
            if state.get("status") != "finished":
                rows.append(public_state(state, compact=True))
            if len(rows) >= limit:
                break
        return rows

    # ------------------------------------------------------------------
    # Battleship
    # ------------------------------------------------------------------

    @staticmethod
    def _sea_coord(coord: str) -> tuple[int, int, str]:
        coord = (coord or "").strip().upper()
        if len(coord) < 2 or coord[0] not in SEA_COLS:
            raise MiniGameError("invalid coordinate; use A1..J10")
        try:
            row = int(coord[1:])
        except ValueError as exc:
            raise MiniGameError("invalid coordinate; use A1..J10") from exc
        if not 1 <= row <= SEA_SIZE:
            raise MiniGameError("row must be 1..10")
        col = SEA_COLS.index(coord[0])
        return row - 1, col, f"{coord[0]}{row}"

    @staticmethod
    def _sea_coord_from_rc(row: int, col: int) -> str:
        return f"{SEA_COLS[col]}{row + 1}"

    def _random_fleet(self) -> list[dict[str, Any]]:
        occupied: set[str] = set()
        ships: list[dict[str, Any]] = []
        for name, size in BATTLESHIP_SPECS:
            for _ in range(2000):
                horizontal = random.choice([True, False])
                max_row = SEA_SIZE - (1 if horizontal else size)
                max_col = SEA_SIZE - (size if horizontal else 1)
                row = random.randint(0, max_row)
                col = random.randint(0, max_col)
                cells = [
                    self._sea_coord_from_rc(row + (0 if horizontal else i), col + (i if horizontal else 0))
                    for i in range(size)
                ]
                if occupied.isdisjoint(cells):
                    occupied.update(cells)
                    ships.append({"name": name, "size": size, "cells": cells})
                    break
            else:
                raise MiniGameError("failed to place fleet")
        return ships

    @staticmethod
    def _ship_cells(fleet: list[dict[str, Any]]) -> set[str]:
        return {cell for ship in fleet for cell in ship.get("cells", [])}

    @staticmethod
    def _shot_map(events: list[dict[str, Any]]) -> dict[str, str]:
        return {str(e.get("coord")): str(e.get("result")) for e in events}

    def _render_sea(self, own_fleet: list[dict[str, Any]] | None, incoming: list[dict[str, Any]], outgoing: list[dict[str, Any]]) -> tuple[str, str]:
        own_cells = self._ship_cells(own_fleet or [])
        incoming_map = self._shot_map(incoming)
        outgoing_map = self._shot_map(outgoing)
        header = "   " + " ".join(SEA_COLS)
        own_rows = [header]
        target_rows = [header]
        for row in range(SEA_SIZE):
            own_line = []
            target_line = []
            for col in range(SEA_SIZE):
                coord = self._sea_coord_from_rc(row, col)
                if coord in incoming_map:
                    own_line.append("X" if coord in own_cells else "o")
                else:
                    own_line.append("S" if coord in own_cells else ".")
                result = outgoing_map.get(coord)
                target_line.append("X" if result in {"hit", "sunk"} else ("o" if result == "miss" else "."))
            own_rows.append(f"{row+1:>2} " + " ".join(own_line))
            target_rows.append(f"{row+1:>2} " + " ".join(target_line))
        return "\n".join(own_rows), "\n".join(target_rows)

    def create_battleship(self, opponent: str) -> dict[str, Any]:
        opponent = self._validate_opponent(self.agent_id, opponent)
        state = self._new_game_common(self._new_match_id("sea"), "battleship", opponent)
        state.update(
            {
                "status": "setup",
                "phase": "fleet-setup",
                "turn_agent": None,
                "fleets": {
                    self.agent_id: {"ready": False, "ships": []},
                    opponent: {"ready": False, "ships": []},
                },
                "shots": {self.agent_id: [], opponent: []},
                "winner": None,
                "reason": None,
            }
        )
        self._save_state("battleship", state)
        return self._public_battleship(state)

    def fleet_auto_battleship(self, match_id: str) -> dict[str, Any]:
        lock = self._acquire_state_lock("battleship", match_id)
        try:
            state = self._load_state("battleship", match_id)
            if not self._is_player(state):
                raise MiniGameError("this agent is not a player in that match")
            if state.get("status") != "setup":
                raise MiniGameError("fleet setup is already closed")
            mine = state["fleets"][self.agent_id]
            if mine.get("ready"):
                raise MiniGameError("your fleet is already ready")
            mine["ships"] = self._random_fleet()
            mine["ready"] = True
            state["updated_at"] = time.time()
            if all(state["fleets"][a].get("ready") for a in state["player_order"]):
                state["status"] = "playing"
                state["phase"] = "battle"
                state["turn_agent"] = state["player_order"][0]
            self._save_state("battleship", state)
            return self._public_battleship(state)
        finally:
            self._release_state_lock(lock)

    def _battleship_sunk_ship(self, fleet: list[dict[str, Any]], hit_coords: set[str], coord: str) -> dict[str, Any] | None:
        for ship in fleet:
            cells = set(ship.get("cells", []))
            if coord in cells and cells.issubset(hit_coords):
                return ship
        return None

    def fire_battleship(self, match_id: str, coord: str, message: str | None = None) -> dict[str, Any]:
        lock = self._acquire_state_lock("battleship", match_id)
        try:
            state = self._load_state("battleship", match_id)
            if state.get("status") != "playing":
                raise MiniGameError("battle is not active")
            if not self._is_player(state):
                raise MiniGameError("this agent is not a player in that match")
            if state.get("turn_agent") != self.agent_id:
                raise MiniGameError("not your turn")
            _, _, coord = self._sea_coord(coord)
            my_shots = state["shots"][self.agent_id]
            if any(e.get("coord") == coord for e in my_shots):
                raise MiniGameError("coordinate already fired upon")

            opponent = self._other_agent(state)
            enemy_fleet = state["fleets"][opponent]["ships"]
            enemy_cells = self._ship_cells(enemy_fleet)
            result = "hit" if coord in enemy_cells else "miss"

            previous_hits = {
                e["coord"] for e in my_shots if e.get("result") in {"hit", "sunk"}
            }
            hit_coords = set(previous_hits)
            if result == "hit":
                hit_coords.add(coord)
            sunk_ship = self._battleship_sunk_ship(enemy_fleet, hit_coords, coord) if result == "hit" else None
            if sunk_ship:
                result = "sunk"

            event = {
                "n": len(my_shots) + 1,
                "agent": self.agent_id,
                "coord": coord,
                "result": result,
                "sunk": sunk_ship.get("name") if sunk_ship else None,
                "at": time.time(),
            }
            my_shots.append(event)
            if message:
                self._append_chat_new(state, message)
            state["updated_at"] = time.time()

            all_enemy_cells = self._ship_cells(enemy_fleet)
            hit_coords = {
                e["coord"] for e in my_shots if e.get("result") in {"hit", "sunk"}
            }
            if all_enemy_cells and all_enemy_cells.issubset(hit_coords):
                state["status"] = "finished"
                state["phase"] = "finished"
                state["winner"] = self.agent_id
                state["reason"] = "all-enemy-ships-sunk"
                state["turn_agent"] = None
                self._archive_finished_state_locked(state, self._battleship_memory(state))
            else:
                state["turn_agent"] = opponent

            self._save_state("battleship", state)
            out = self._public_battleship(state)
            out["shot_result"] = event
            return out
        finally:
            self._release_state_lock(lock)

    def _battleship_memory(self, state: dict[str, Any]) -> str:
        a, b = state["player_order"]
        counts = {p: len(state["shots"].get(p, [])) for p in (a, b)}
        date = time.strftime("%Y-%m-%d", time.localtime(state.get("updated_at", time.time())))
        return (
            f"{date}，Common AI Memory 海战棋对局 {state['id']} 结束。"
            f"{a} 对 {b}；胜者 {state.get('winner')}；结束原因：{state.get('reason')}；"
            f"双方分别进行了 {counts[a]} / {counts[b]} 次炮击。"
            "舰队完整布局保留在小游戏持久化记录中。"
        )

    def _public_battleship(self, state: dict[str, Any], compact: bool = False) -> dict[str, Any]:
        if not self._is_player(state):
            raise MiniGameError("this agent is not a player in that match")
        opponent = self._other_agent(state)
        mine = state["fleets"][self.agent_id]
        incoming = state["shots"].get(opponent, [])
        outgoing = state["shots"].get(self.agent_id, [])
        own_board, target_board = self._render_sea(mine.get("ships", []), incoming, outgoing)
        my_hits = {e["coord"] for e in incoming if e.get("result") in {"hit", "sunk"}}
        own_ships = []
        for ship in mine.get("ships", []):
            cells = set(ship.get("cells", []))
            own_ships.append(
                {
                    "name": ship.get("name"),
                    "size": ship.get("size"),
                    "hits": len(cells & my_hits),
                    "sunk": bool(cells) and cells.issubset(my_hits),
                }
            )
        result = {
            "id": state["id"],
            "game": "battleship",
            "status": state["status"],
            "phase": state.get("phase"),
            "players": state["players"],
            "turn_agent": state.get("turn_agent"),
            "winner": state.get("winner"),
            "reason": state.get("reason"),
            "fleet_ready": {a: bool(state["fleets"][a].get("ready")) for a in state["player_order"]},
            "your_fleet": own_ships,
            "enemy_sunk": [e.get("sunk") for e in outgoing if e.get("sunk")],
            "last_shot": outgoing[-1] if outgoing else None,
            "last_enemy_shot": incoming[-1] if incoming else None,
            "chat": state.get("chat", [])[-20:],
            "you": {
                "agent": self.agent_id,
                "opponent": opponent,
                "your_turn": state.get("turn_agent") == self.agent_id,
                "action_required": (
                    "fleet auto"
                    if state.get("status") == "setup" and not mine.get("ready")
                    else ("fire" if state.get("turn_agent") == self.agent_id else None)
                ),
            },
            **self._memory_meta(state),
        }
        if not compact:
            result["your_ocean"] = own_board
            result["target_ocean"] = target_board
        return result

    def status_battleship(self, match_id: str) -> dict[str, Any]:
        return self._public_battleship(self._load_state("battleship", match_id))

    def say_battleship(self, match_id: str, message: str) -> dict[str, Any]:
        lock = self._acquire_state_lock("battleship", match_id)
        try:
            state = self._load_state("battleship", match_id)
            if not self._is_player(state):
                raise MiniGameError("this agent is not a player in that match")
            self._append_chat_new(state, message)
            state["updated_at"] = time.time()
            self._save_state("battleship", state)
            return self._public_battleship(state, compact=True)
        finally:
            self._release_state_lock(lock)

    def resign_battleship(self, match_id: str) -> dict[str, Any]:
        lock = self._acquire_state_lock("battleship", match_id)
        try:
            state = self._load_state("battleship", match_id)
            if not self._is_player(state):
                raise MiniGameError("this agent is not a player in that match")
            if state.get("status") == "finished":
                return self._public_battleship(state)
            state["status"] = "finished"
            state["phase"] = "finished"
            state["winner"] = self._other_agent(state)
            state["reason"] = f"{self.agent_id} resigned"
            state["turn_agent"] = None
            state["updated_at"] = time.time()
            self._archive_finished_state_locked(state, self._battleship_memory(state))
            self._save_state("battleship", state)
            return self._public_battleship(state)
        finally:
            self._release_state_lock(lock)

    def _battleship_action_needed(self, state: dict[str, Any]) -> bool:
        if state.get("status") == "setup":
            return not state["fleets"][self.agent_id].get("ready")
        return state.get("turn_agent") == self.agent_id

    async def wait_battleship(self, match_id: str, seconds: int = 25) -> dict[str, Any]:
        return await self._wait_new_game(
            "battleship", match_id, self._public_battleship, self._battleship_action_needed, seconds
        )

    def active_battleship(self) -> list[dict[str, Any]]:
        return self._active_new_game("battleship", self._public_battleship)

    # ------------------------------------------------------------------
    # Blackjack
    # ------------------------------------------------------------------

    @staticmethod
    def _new_deck() -> list[str]:
        deck = [r + s for r in RANKS for s in SUITS]
        random.shuffle(deck)
        return deck

    @staticmethod
    def _card_label(card: str) -> str:
        return f"{card[0]}{SUIT_SYMBOL.get(card[1], card[1])}"

    @staticmethod
    def _blackjack_score(cards: list[str]) -> tuple[int, bool]:
        total = 0
        aces = 0
        for card in cards:
            rank = card[0]
            if rank == "A":
                aces += 1
                total += 11
            elif rank in {"T", "J", "Q", "K"}:
                total += 10
            else:
                total += int(rank)
        while total > 21 and aces:
            total -= 10
            aces -= 1
        soft = aces > 0
        return total, soft

    @classmethod
    def _is_blackjack(cls, cards: list[str]) -> bool:
        return len(cards) == 2 and cls._blackjack_score(cards)[0] == 21

    @staticmethod
    def _draw(state: dict[str, Any]) -> str:
        if not state["deck"]:
            raise MiniGameError("deck is empty")
        return state["deck"].pop()

    def create_blackjack(self, opponent: str) -> dict[str, Any]:
        opponent = self._validate_opponent(self.agent_id, opponent)
        state = self._new_game_common(self._new_match_id("bj"), "blackjack", opponent)
        state.update(
            {
                "status": "playing",
                "phase": "players",
                "deck": self._new_deck(),
                "hands": {self.agent_id: [], opponent: []},
                "dealer": [],
                "decisions": {self.agent_id: "playing", opponent: "playing"},
                "turn_agent": self.agent_id,
                "results": {},
                "winner": None,
                "reason": None,
            }
        )
        for _ in range(2):
            for p in state["player_order"]:
                state["hands"][p].append(self._draw(state))
            state["dealer"].append(self._draw(state))

        for p in state["player_order"]:
            if self._is_blackjack(state["hands"][p]):
                state["decisions"][p] = "stand"
        self._blackjack_advance_or_finish(state)
        self._save_state("blackjack", state)
        return self._public_blackjack(state)

    def _blackjack_next_player(self, state: dict[str, Any]) -> str | None:
        if state.get("turn_agent") in state["player_order"]:
            start = state["player_order"].index(state["turn_agent"]) + 1
        else:
            start = 0
        for idx in range(start, len(state["player_order"])):
            p = state["player_order"][idx]
            if state["decisions"][p] == "playing":
                return p
        for idx in range(0, start):
            p = state["player_order"][idx]
            if state["decisions"][p] == "playing":
                return p
        return None

    def _blackjack_finish(self, state: dict[str, Any]) -> None:
        state["phase"] = "dealer"
        dealer_score, _ = self._blackjack_score(state["dealer"])
        while dealer_score < 17:
            state["dealer"].append(self._draw(state))
            dealer_score, _ = self._blackjack_score(state["dealer"])

        dealer_bj = self._is_blackjack(state["dealer"])
        results: dict[str, str] = {}
        for p in state["player_order"]:
            cards = state["hands"][p]
            score, _ = self._blackjack_score(cards)
            player_bj = self._is_blackjack(cards)
            if score > 21 or state["decisions"][p] == "bust":
                result = "bust"
            elif player_bj and not dealer_bj:
                result = "blackjack"
            elif dealer_bj and not player_bj:
                result = "lose"
            elif dealer_score > 21:
                result = "win"
            elif score > dealer_score:
                result = "win"
            elif score < dealer_score:
                result = "lose"
            else:
                result = "push"
            results[p] = result

        state["results"] = results
        winners = [p for p, r in results.items() if r in {"win", "blackjack"}]
        state["winner"] = winners[0] if len(winners) == 1 else None
        state["status"] = "finished"
        state["phase"] = "finished"
        state["turn_agent"] = None
        state["reason"] = "dealer-resolution"
        state["updated_at"] = time.time()
        self._archive_finished_state_locked(state, self._blackjack_memory(state))

    def _blackjack_advance_or_finish(self, state: dict[str, Any]) -> None:
        playing = [p for p in state["player_order"] if state["decisions"][p] == "playing"]
        if not playing:
            self._blackjack_finish(state)
            return
        if state.get("turn_agent") not in playing:
            state["turn_agent"] = playing[0]

    def hit_blackjack(self, match_id: str, message: str | None = None) -> dict[str, Any]:
        lock = self._acquire_state_lock("blackjack", match_id)
        try:
            state = self._load_state("blackjack", match_id)
            self._require_blackjack_turn(state)
            state["hands"][self.agent_id].append(self._draw(state))
            if message:
                self._append_chat_new(state, message)
            score, _ = self._blackjack_score(state["hands"][self.agent_id])
            if score > 21:
                state["decisions"][self.agent_id] = "bust"
                state["turn_agent"] = self._blackjack_next_player(state)
            elif score == 21:
                state["decisions"][self.agent_id] = "stand"
                state["turn_agent"] = self._blackjack_next_player(state)
            state["updated_at"] = time.time()
            self._blackjack_advance_or_finish(state)
            self._save_state("blackjack", state)
            return self._public_blackjack(state)
        finally:
            self._release_state_lock(lock)

    def stand_blackjack(self, match_id: str, message: str | None = None) -> dict[str, Any]:
        lock = self._acquire_state_lock("blackjack", match_id)
        try:
            state = self._load_state("blackjack", match_id)
            self._require_blackjack_turn(state)
            state["decisions"][self.agent_id] = "stand"
            if message:
                self._append_chat_new(state, message)
            state["turn_agent"] = self._blackjack_next_player(state)
            state["updated_at"] = time.time()
            self._blackjack_advance_or_finish(state)
            self._save_state("blackjack", state)
            return self._public_blackjack(state)
        finally:
            self._release_state_lock(lock)

    def _require_blackjack_turn(self, state: dict[str, Any]) -> None:
        if not self._is_player(state):
            raise MiniGameError("this agent is not a player in that match")
        if state.get("status") != "playing":
            raise MiniGameError("round is already finished")
        if state.get("turn_agent") != self.agent_id:
            raise MiniGameError("not your turn")
        if state["decisions"].get(self.agent_id) != "playing":
            raise MiniGameError("your hand is already settled")

    def resign_blackjack(self, match_id: str) -> dict[str, Any]:
        lock = self._acquire_state_lock("blackjack", match_id)
        try:
            state = self._load_state("blackjack", match_id)
            if not self._is_player(state):
                raise MiniGameError("this agent is not a player in that match")
            if state.get("status") == "finished":
                return self._public_blackjack(state)
            state["decisions"][self.agent_id] = "bust"
            if state.get("turn_agent") == self.agent_id:
                state["turn_agent"] = self._blackjack_next_player(state)
            state["updated_at"] = time.time()
            self._blackjack_advance_or_finish(state)
            self._save_state("blackjack", state)
            return self._public_blackjack(state)
        finally:
            self._release_state_lock(lock)

    def _blackjack_memory(self, state: dict[str, Any]) -> str:
        a, b = state["player_order"]
        da = state["results"].get(a, "?")
        db = state["results"].get(b, "?")
        dealer_score = self._blackjack_score(state["dealer"])[0]
        date = time.strftime("%Y-%m-%d", time.localtime(state.get("updated_at", time.time())))
        return (
            f"{date}，Common AI Memory 21点对局 {state['id']} 结束。"
            f"{a}：{da}；{b}：{db}；庄家最终 {dealer_score} 点。"
            "本局无下注，仅为零赌注规则游戏。"
        )

    def _public_blackjack(self, state: dict[str, Any], compact: bool = False) -> dict[str, Any]:
        if not self._is_player(state):
            raise MiniGameError("this agent is not a player in that match")
        finished = state.get("status") == "finished"
        opponent = self._other_agent(state)
        mine = state["hands"][self.agent_id]
        mine_score, mine_soft = self._blackjack_score(mine)
        dealer_visible = state["dealer"] if finished else state["dealer"][:1] + ["??"]
        players_view: dict[str, Any] = {}
        for p in state["player_order"]:
            cards = state["hands"][p]
            if p == self.agent_id or finished:
                score, soft = self._blackjack_score(cards)
                shown = [self._card_label(c) for c in cards]
                players_view[p] = {
                    "cards": shown,
                    "score": score,
                    "soft": soft,
                    "decision": state["decisions"][p],
                    "result": state.get("results", {}).get(p),
                }
            else:
                players_view[p] = {
                    "cards": ["??"] * len(cards),
                    "card_count": len(cards),
                    "decision": state["decisions"][p],
                }
        result = {
            "id": state["id"],
            "game": "blackjack",
            "status": state["status"],
            "phase": state.get("phase"),
            "players": players_view,
            "dealer": {
                "cards": [self._card_label(c) if c != "??" else "??" for c in dealer_visible],
                "score": self._blackjack_score(state["dealer"])[0] if finished else None,
            },
            "turn_agent": state.get("turn_agent"),
            "results": state.get("results") if finished else None,
            "chat": state.get("chat", [])[-20:],
            "you": {
                "agent": self.agent_id,
                "opponent": opponent,
                "score": mine_score,
                "soft": mine_soft,
                "your_turn": state.get("turn_agent") == self.agent_id,
                "legal_actions": (
                    ["hit", "stand"] if state.get("turn_agent") == self.agent_id else []
                ),
            },
            **self._memory_meta(state),
        }
        if compact:
            result.pop("players", None)
            result["your_score"] = mine_score
        return result

    def status_blackjack(self, match_id: str) -> dict[str, Any]:
        return self._public_blackjack(self._load_state("blackjack", match_id))

    def say_blackjack(self, match_id: str, message: str) -> dict[str, Any]:
        lock = self._acquire_state_lock("blackjack", match_id)
        try:
            state = self._load_state("blackjack", match_id)
            if not self._is_player(state):
                raise MiniGameError("this agent is not a player in that match")
            self._append_chat_new(state, message)
            state["updated_at"] = time.time()
            self._save_state("blackjack", state)
            return self._public_blackjack(state, compact=True)
        finally:
            self._release_state_lock(lock)

    def _blackjack_action_needed(self, state: dict[str, Any]) -> bool:
        return state.get("turn_agent") == self.agent_id

    async def wait_blackjack(self, match_id: str, seconds: int = 25) -> dict[str, Any]:
        return await self._wait_new_game(
            "blackjack", match_id, self._public_blackjack, self._blackjack_action_needed, seconds
        )

    def active_blackjack(self) -> list[dict[str, Any]]:
        return self._active_new_game("blackjack", self._public_blackjack)

    # ------------------------------------------------------------------
    # Heads-up Texas Hold'em (play chips only)
    # ------------------------------------------------------------------

    @staticmethod
    def _poker_five_value(cards: tuple[str, ...]) -> tuple[int, tuple[int, ...]]:
        values = sorted((RANK_VALUE[c[0]] for c in cards), reverse=True)
        counts = Counter(values)
        groups = sorted(((count, value) for value, count in counts.items()), reverse=True)
        flush = len({c[1] for c in cards}) == 1
        unique = sorted(set(values), reverse=True)
        if 14 in unique:
            unique.append(1)
        straight_high = None
        for i in range(len(unique) - 4):
            window = unique[i:i+5]
            if window[0] - window[4] == 4 and len(set(window)) == 5:
                straight_high = window[0]
                break
        if flush and straight_high is not None:
            return 8, (straight_high,)
        if groups[0][0] == 4:
            quad = groups[0][1]
            kicker = max(v for v in values if v != quad)
            return 7, (quad, kicker)
        triples = sorted((v for v, c in counts.items() if c == 3), reverse=True)
        pairs = sorted((v for v, c in counts.items() if c >= 2), reverse=True)
        if triples and len([v for v in pairs if v != triples[0]]) >= 1:
            pair = max(v for v in pairs if v != triples[0])
            return 6, (triples[0], pair)
        if flush:
            return 5, tuple(values)
        if straight_high is not None:
            return 4, (straight_high,)
        if triples:
            t = triples[0]
            kickers = sorted((v for v in values if v != t), reverse=True)[:2]
            return 3, (t, *kickers)
        exact_pairs = sorted((v for v, c in counts.items() if c == 2), reverse=True)
        if len(exact_pairs) >= 2:
            hi, lo = exact_pairs[:2]
            kicker = max(v for v in values if v not in {hi, lo})
            return 2, (hi, lo, kicker)
        if len(exact_pairs) == 1:
            p = exact_pairs[0]
            kickers = sorted((v for v in values if v != p), reverse=True)[:3]
            return 1, (p, *kickers)
        return 0, tuple(values)

    @classmethod
    def _poker_best(cls, cards: list[str]) -> tuple[tuple[int, tuple[int, ...]], list[str]]:
        if len(cards) < 5:
            raise MiniGameError("not enough cards for a poker hand")
        best_value = None
        best_cards: list[str] = []
        for combo in itertools.combinations(cards, 5):
            value = cls._poker_five_value(combo)
            if best_value is None or value > best_value:
                best_value = value
                best_cards = list(combo)
        assert best_value is not None
        return best_value, best_cards

    @classmethod
    def _poker_best_name(cls, cards: list[str]) -> str | None:
        if len(cards) < 5:
            return None
        return POKER_HAND_NAMES[cls._poker_best(cards)[0][0]]

    @staticmethod
    def _poker_pay(state: dict[str, Any], agent: str, amount: int) -> None:
        amount = int(amount)
        if amount < 0 or amount > state["stacks"][agent]:
            raise MiniGameError("invalid chip amount")
        state["stacks"][agent] -= amount
        state["street_contrib"][agent] += amount
        state["total_contrib"][agent] += amount

    def create_holdem(self, opponent: str) -> dict[str, Any]:
        opponent = self._validate_opponent(self.agent_id, opponent)
        state = self._new_game_common(self._new_match_id("he"), "holdem", opponent)
        deck = self._new_deck()
        state.update(
            {
                "status": "playing",
                "street": "preflop",
                "deck": deck,
                "hole": {self.agent_id: [deck.pop(), deck.pop()], opponent: [deck.pop(), deck.pop()]},
                "board": [],
                "dealer": self.agent_id,
                "small_blind": self.agent_id,
                "big_blind": opponent,
                "stacks": {self.agent_id: 200, opponent: 200},
                "street_contrib": {self.agent_id: 0, opponent: 0},
                "total_contrib": {self.agent_id: 0, opponent: 0},
                "current_bet": 0,
                "last_raise_size": 1,
                "acted_since_raise": [],
                "turn_agent": self.agent_id,
                "actions": [],
                "winner": None,
                "reason": None,
                "showdown": False,
                "payout": None,
            }
        )
        self._poker_pay(state, self.agent_id, 1)
        self._poker_pay(state, opponent, 2)
        state["current_bet"] = 2
        self._save_state("holdem", state)
        return self._public_holdem(state)

    def _poker_to_call(self, state: dict[str, Any], agent: str) -> int:
        return max(0, int(state["current_bet"]) - int(state["street_contrib"][agent]))

    def _poker_max_total(self, state: dict[str, Any], agent: str) -> int:
        opponent = self._other_agent(state, agent)
        own_max = state["street_contrib"][agent] + state["stacks"][agent]
        opp_max = state["street_contrib"][opponent] + state["stacks"][opponent]
        return min(own_max, opp_max)

    def _require_poker_turn(self, state: dict[str, Any]) -> None:
        if not self._is_player(state):
            raise MiniGameError("this agent is not a player in that match")
        if state.get("status") != "playing":
            raise MiniGameError("hand is already finished")
        if state.get("turn_agent") != self.agent_id:
            raise MiniGameError("not your turn")

    def _poker_log_action(self, state: dict[str, Any], action: str, amount: int | None = None) -> None:
        state["actions"].append(
            {
                "n": len(state["actions"]) + 1,
                "agent": self.agent_id,
                "street": state["street"],
                "action": action,
                "amount": amount,
                "at": time.time(),
            }
        )
        if len(state["actions"]) > 120:
            state["actions"] = state["actions"][-120:]

    def _poker_round_complete(self, state: dict[str, Any]) -> bool:
        a, b = state["player_order"]
        return (
            set(state["acted_since_raise"]) == {a, b}
            and state["street_contrib"][a] == state["street_contrib"][b]
        )

    def _poker_runout_and_showdown(self, state: dict[str, Any]) -> None:
        while len(state["board"]) < 5:
            state["board"].append(self._draw(state))
        self._poker_showdown(state)

    def _poker_advance_street(self, state: dict[str, Any]) -> None:
        if any(state["stacks"][p] == 0 for p in state["player_order"]):
            self._poker_runout_and_showdown(state)
            return

        street = state["street"]
        if street == "preflop":
            state["board"].extend([self._draw(state), self._draw(state), self._draw(state)])
            state["street"] = "flop"
        elif street == "flop":
            state["board"].append(self._draw(state))
            state["street"] = "turn"
        elif street == "turn":
            state["board"].append(self._draw(state))
            state["street"] = "river"
        elif street == "river":
            self._poker_showdown(state)
            return
        else:
            raise MiniGameError(f"invalid poker street: {street}")

        for p in state["player_order"]:
            state["street_contrib"][p] = 0
        state["current_bet"] = 0
        state["last_raise_size"] = 2
        state["acted_since_raise"] = []
        # Heads-up postflop: non-dealer acts first.
        state["turn_agent"] = self._other_agent(state, state["dealer"])

    def _poker_award(self, state: dict[str, Any], winner: str | None, reason: str, showdown: bool) -> None:
        pot = sum(state["total_contrib"].values())
        if winner is None:
            a, b = state["player_order"]
            half = pot // 2
            state["stacks"][a] += half
            state["stacks"][b] += pot - half
            payout = {"split": pot}
        else:
            state["stacks"][winner] += pot
            payout = {"winner": winner, "amount": pot}
        state["status"] = "finished"
        state["street"] = "showdown" if showdown else "finished"
        state["turn_agent"] = None
        state["winner"] = winner
        state["reason"] = reason
        state["showdown"] = showdown
        state["payout"] = payout
        state["pot_final"] = pot
        state["updated_at"] = time.time()
        self._archive_finished_state_locked(state, self._holdem_memory(state))

    def _poker_showdown(self, state: dict[str, Any]) -> None:
        a, b = state["player_order"]
        va, _ = self._poker_best(state["hole"][a] + state["board"])
        vb, _ = self._poker_best(state["hole"][b] + state["board"])
        if va > vb:
            winner = a
        elif vb > va:
            winner = b
        else:
            winner = None
        self._poker_award(state, winner, "showdown", True)

    def check_holdem(self, match_id: str, message: str | None = None) -> dict[str, Any]:
        lock = self._acquire_state_lock("holdem", match_id)
        try:
            state = self._load_state("holdem", match_id)
            self._require_poker_turn(state)
            if self._poker_to_call(state, self.agent_id) != 0:
                raise MiniGameError("cannot check while facing a bet; call, raise, or fold")
            self._poker_log_action(state, "check")
            if message:
                self._append_chat_new(state, message)
            if self.agent_id not in state["acted_since_raise"]:
                state["acted_since_raise"].append(self.agent_id)
            opponent = self._other_agent(state)
            if self._poker_round_complete(state):
                self._poker_advance_street(state)
            else:
                state["turn_agent"] = opponent
            state["updated_at"] = time.time()
            self._save_state("holdem", state)
            return self._public_holdem(state)
        finally:
            self._release_state_lock(lock)

    def call_holdem(self, match_id: str, message: str | None = None) -> dict[str, Any]:
        lock = self._acquire_state_lock("holdem", match_id)
        try:
            state = self._load_state("holdem", match_id)
            self._require_poker_turn(state)
            to_call = self._poker_to_call(state, self.agent_id)
            if to_call <= 0:
                raise MiniGameError("nothing to call; use check or raise")
            if to_call > state["stacks"][self.agent_id]:
                raise MiniGameError("call exceeds stack; this should not occur under effective-stack raise limits")
            self._poker_pay(state, self.agent_id, to_call)
            self._poker_log_action(state, "call", to_call)
            if message:
                self._append_chat_new(state, message)
            if self.agent_id not in state["acted_since_raise"]:
                state["acted_since_raise"].append(self.agent_id)
            opponent = self._other_agent(state)
            if self._poker_round_complete(state):
                self._poker_advance_street(state)
            else:
                state["turn_agent"] = opponent
            state["updated_at"] = time.time()
            self._save_state("holdem", state)
            return self._public_holdem(state)
        finally:
            self._release_state_lock(lock)

    def raise_holdem(self, match_id: str, street_total: int, message: str | None = None) -> dict[str, Any]:
        lock = self._acquire_state_lock("holdem", match_id)
        try:
            state = self._load_state("holdem", match_id)
            self._require_poker_turn(state)
            try:
                target = int(street_total)
            except Exception as exc:
                raise MiniGameError("raise target must be an integer street-total") from exc

            current = int(state["current_bet"])
            max_total = self._poker_max_total(state, self.agent_id)
            min_total = current + max(1, int(state["last_raise_size"]))
            if target <= current:
                raise MiniGameError(f"raise target must exceed current bet {current}")
            if target > max_total:
                raise MiniGameError(f"raise target exceeds effective-stack maximum {max_total}")
            if target < min_total and target != max_total:
                raise MiniGameError(f"minimum raise-to is {min_total}; all-in effective maximum is {max_total}")

            pay = target - state["street_contrib"][self.agent_id]
            if pay > state["stacks"][self.agent_id]:
                raise MiniGameError("insufficient play chips")
            previous_bet = current
            self._poker_pay(state, self.agent_id, pay)
            state["current_bet"] = target
            state["last_raise_size"] = target - previous_bet
            state["acted_since_raise"] = [self.agent_id]
            self._poker_log_action(state, "raise-to", target)
            if message:
                self._append_chat_new(state, message)
            state["turn_agent"] = self._other_agent(state)
            state["updated_at"] = time.time()
            self._save_state("holdem", state)
            return self._public_holdem(state)
        finally:
            self._release_state_lock(lock)

    def fold_holdem(self, match_id: str, message: str | None = None) -> dict[str, Any]:
        lock = self._acquire_state_lock("holdem", match_id)
        try:
            state = self._load_state("holdem", match_id)
            self._require_poker_turn(state)
            if message:
                self._append_chat_new(state, message)
            self._poker_log_action(state, "fold")
            self._poker_award(state, self._other_agent(state), f"{self.agent_id} folded", False)
            self._save_state("holdem", state)
            return self._public_holdem(state)
        finally:
            self._release_state_lock(lock)

    def _holdem_memory(self, state: dict[str, Any]) -> str:
        a, b = state["player_order"]
        date = time.strftime("%Y-%m-%d", time.localtime(state.get("updated_at", time.time())))
        winner = state.get("winner") or "split-pot"
        return (
            f"{date}，Common AI Memory 双人德州扑克对局 {state['id']} 结束。"
            f"{a} 对 {b}；结果 {winner}；结束原因：{state.get('reason')}；"
            f"最终底池 {state.get('pot_final', sum(state.get('total_contrib', {}).values()))} 枚游戏筹码。"
            "本游戏仅使用不可兑现的本地游戏筹码。"
        )

    def _public_holdem(self, state: dict[str, Any], compact: bool = False) -> dict[str, Any]:
        if not self._is_player(state):
            raise MiniGameError("this agent is not a player in that match")
        opponent = self._other_agent(state)
        showdown = bool(state.get("showdown"))
        finished = state.get("status") == "finished"
        hole_view = {}
        for p in state["player_order"]:
            if p == self.agent_id or showdown:
                hole_view[p] = [self._card_label(c) for c in state["hole"][p]]
            else:
                hole_view[p] = ["??", "??"]

        to_call = self._poker_to_call(state, self.agent_id) if not finished else 0
        legal: list[str] = []
        if state.get("turn_agent") == self.agent_id and not finished:
            if to_call:
                legal.extend(["call", "fold"])
            else:
                legal.append("check")
            max_total = self._poker_max_total(state, self.agent_id)
            if max_total > state["current_bet"] and state["stacks"][self.agent_id] > 0:
                legal.append(f"raise-to {max(state['current_bet'] + state['last_raise_size'], state['current_bet'] + 1)}..{max_total}")

        own_cards = state["hole"][self.agent_id] + state["board"]
        result = {
            "id": state["id"],
            "game": "holdem",
            "status": state["status"],
            "street": state.get("street"),
            "players": state["players"],
            "dealer": state.get("dealer"),
            "blinds": {"small": state.get("small_blind"), "big": state.get("big_blind"), "values": [1, 2]},
            "hole": hole_view,
            "board": [self._card_label(c) for c in state.get("board", [])],
            "stacks": state["stacks"],
            "pot": state.get("pot_final", sum(state["total_contrib"].values())),
            "street_contrib": state["street_contrib"],
            "current_bet": state["current_bet"],
            "turn_agent": state.get("turn_agent"),
            "winner": state.get("winner"),
            "reason": state.get("reason"),
            "showdown": showdown,
            "payout": state.get("payout"),
            "last_action": state["actions"][-1] if state.get("actions") else None,
            "chat": state.get("chat", [])[-20:],
            "you": {
                "agent": self.agent_id,
                "opponent": opponent,
                "your_turn": state.get("turn_agent") == self.agent_id,
                "to_call": to_call,
                "legal_actions": legal,
                "your_best": self._poker_best_name(own_cards),
            },
            **self._memory_meta(state),
        }
        if compact:
            result.pop("hole", None)
            result.pop("chat", None)
        return result

    def status_holdem(self, match_id: str) -> dict[str, Any]:
        return self._public_holdem(self._load_state("holdem", match_id))

    def say_holdem(self, match_id: str, message: str) -> dict[str, Any]:
        lock = self._acquire_state_lock("holdem", match_id)
        try:
            state = self._load_state("holdem", match_id)
            if not self._is_player(state):
                raise MiniGameError("this agent is not a player in that match")
            self._append_chat_new(state, message)
            state["updated_at"] = time.time()
            self._save_state("holdem", state)
            return self._public_holdem(state, compact=True)
        finally:
            self._release_state_lock(lock)

    def _holdem_action_needed(self, state: dict[str, Any]) -> bool:
        return state.get("turn_agent") == self.agent_id

    async def wait_holdem(self, match_id: str, seconds: int = 25) -> dict[str, Any]:
        return await self._wait_new_game(
            "holdem", match_id, self._public_holdem, self._holdem_action_needed, seconds
        )

    def active_holdem(self) -> list[dict[str, Any]]:
        return self._active_new_game("holdem", self._public_holdem)

