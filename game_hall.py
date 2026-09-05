from __future__ import annotations

from pathlib import Path
from typing import Any

from external_games import ExternalGameAdapter
from lounge_room import LoungeRoom
from minigames import MiniGameError, MiniGames


BUILTIN_GAMES = {
    "lounge": {"name": "AI Lounge", "status": "available", "description": "Shared multi-AI room with read state and table talk."},
    "minigames": {"name": "Built-in Minigames", "status": "available", "description": "Gomoku, Battleship, Blackjack, and heads-up Holdem."},
}


class GameHall:
    """Production Game Hall with built-ins and isolated optional adapters."""

    def __init__(self, project_root: str | Path, agent_id: str, adapters: dict[str, ExternalGameAdapter] | None = None) -> None:
        self.project_root = Path(project_root).resolve()
        self.agent_id = (agent_id or "").strip().lower()
        if not self.agent_id:
            raise ValueError("agent_id is required")
        self.minigames = MiniGames(self.project_root, self.agent_id)
        self._lounge = LoungeRoom(self.project_root, self.agent_id)
        self._opened: set[str] = set()
        self.adapters = dict(adapters or {})

    def game_list(self) -> dict:
        games = [{"id": game_id, **info} for game_id, info in BUILTIN_GAMES.items()]
        games.extend({"id": game_id, "name": adapter.name, "status": "available", "description": adapter.description}
                     for game_id, adapter in sorted(self.adapters.items()))
        return {"ok": True, "agent": self.agent_id, "games": games}

    async def game_open(self, game: str) -> dict:
        game = (game or "").strip().lower()
        if game == "lounge":
            return self._lounge.open()
        if game == "minigames":
            self._opened.add(game)
            return {"ok": True, "game": game, "name": BUILTIN_GAMES[game]["name"], "agent": self.agent_id,
                    "games": self.minigames.list_games(), "active": self._active_minigames(),
                    "continuous_play_protocol": "Stay in the same match; act only on your turn, then wait for the opponent."}
        adapter = self.adapters.get(game)
        if adapter:
            self._opened.add(game)
            return await adapter.open(self.agent_id)
        return {"ok": False, "error": f"unknown or unconfigured game: {game}"}

    async def game_status(self, game: str) -> dict:
        game = (game or "").strip().lower()
        if game == "lounge":
            return self._lounge.status(mark_read=True)
        if game == "minigames":
            self._opened.add(game)
            return {"ok": True, "game": game, "agent": self.agent_id,
                    "games": self.minigames.list_games(), "active": self._active_minigames()}
        adapter = self.adapters.get(game)
        if adapter:
            return await adapter.status(self.agent_id)
        return {"ok": False, "error": f"unknown or unconfigured game: {game}"}

    def _active_minigames(self) -> dict[str, Any]:
        return {"gomoku": self.minigames.active_gomoku(), "battleship": self.minigames.active_battleship(),
                "blackjack": self.minigames.active_blackjack(), "holdem": self.minigames.active_holdem()}

    async def _minigame_action(self, area: str, command: str) -> dict:
        area = (area or "").strip().lower()
        command = (command or "").strip()
        aliases = {
            "五子棋": "gomoku",
            "海战棋": "battleship",
            "21点": "blackjack",
            "21": "blackjack",
            "扑克": "holdem",
            "德州扑克": "holdem",
            "poker": "holdem",
            "texas-holdem": "holdem",
        }
        area = aliases.get(area, area)
        allowed = {"gomoku", "battleship", "blackjack", "holdem"}
        if area not in allowed:
            return {
                "ok": False,
                "error": f"unknown minigame area: {area}",
                "allowed_areas": sorted(allowed),
            }
        if not command:
            return {"ok": False, "error": "command is required"}

        try:
            if command.lower().startswith("say "):
                parts = command.split(maxsplit=2)
                if len(parts) != 3:
                    raise MiniGameError("usage: say <match_id> <message>")
                match_id, message = parts[1], parts[2]
                if area == "gomoku":
                    result = self.minigames.say_gomoku(match_id, message)
                elif area == "battleship":
                    result = self.minigames.say_battleship(match_id, message)
                elif area == "blackjack":
                    result = self.minigames.say_blackjack(match_id, message)
                else:
                    result = self.minigames.say_holdem(match_id, message)
            else:
                main_command, separator, table_talk = command.partition(" :: ")
                parts = main_command.split()
                op = parts[0].lower()
                msg = table_talk if separator else None

                if area == "gomoku":
                    if op == "create" and len(parts) == 2:
                        result = self.minigames.create_gomoku(parts[1])
                    elif op == "status" and len(parts) == 2:
                        result = self.minigames.status_gomoku(parts[1])
                    elif op == "move" and len(parts) == 3:
                        result = self.minigames.move_gomoku(parts[1], parts[2], msg)
                    elif op == "wait" and len(parts) in {2, 3}:
                        result = await self.minigames.wait_gomoku(parts[1], int(parts[2]) if len(parts) == 3 else 25)
                    elif op == "resign" and len(parts) == 2:
                        result = self.minigames.resign_gomoku(parts[1])
                    elif op in {"active", "list"} and len(parts) == 1:
                        result = {"matches": self.minigames.active_gomoku()}
                    else:
                        raise MiniGameError("gomoku: create/status/move/say/wait/resign/active")

                elif area == "battleship":
                    if op == "create" and len(parts) == 2:
                        result = self.minigames.create_battleship(parts[1])
                    elif op == "fleet" and len(parts) == 3 and parts[2].lower() == "auto":
                        result = self.minigames.fleet_auto_battleship(parts[1])
                    elif op == "status" and len(parts) == 2:
                        result = self.minigames.status_battleship(parts[1])
                    elif op == "fire" and len(parts) == 3:
                        result = self.minigames.fire_battleship(parts[1], parts[2], msg)
                    elif op == "wait" and len(parts) in {2, 3}:
                        result = await self.minigames.wait_battleship(parts[1], int(parts[2]) if len(parts) == 3 else 25)
                    elif op == "resign" and len(parts) == 2:
                        result = self.minigames.resign_battleship(parts[1])
                    elif op in {"active", "list"} and len(parts) == 1:
                        result = {"matches": self.minigames.active_battleship()}
                    else:
                        raise MiniGameError("battleship: create/fleet auto/status/fire/say/wait/resign/active")

                elif area == "blackjack":
                    if op == "create" and len(parts) == 2:
                        result = self.minigames.create_blackjack(parts[1])
                    elif op == "status" and len(parts) == 2:
                        result = self.minigames.status_blackjack(parts[1])
                    elif op == "hit" and len(parts) == 2:
                        result = self.minigames.hit_blackjack(parts[1], msg)
                    elif op == "stand" and len(parts) == 2:
                        result = self.minigames.stand_blackjack(parts[1], msg)
                    elif op == "wait" and len(parts) in {2, 3}:
                        result = await self.minigames.wait_blackjack(parts[1], int(parts[2]) if len(parts) == 3 else 25)
                    elif op == "resign" and len(parts) == 2:
                        result = self.minigames.resign_blackjack(parts[1])
                    elif op in {"active", "list"} and len(parts) == 1:
                        result = {"matches": self.minigames.active_blackjack()}
                    else:
                        raise MiniGameError("blackjack: create/status/hit/stand/say/wait/resign/active")

                else:  # holdem
                    if op == "create" and len(parts) == 2:
                        result = self.minigames.create_holdem(parts[1])
                    elif op == "status" and len(parts) == 2:
                        result = self.minigames.status_holdem(parts[1])
                    elif op == "check" and len(parts) == 2:
                        result = self.minigames.check_holdem(parts[1], msg)
                    elif op == "call" and len(parts) == 2:
                        result = self.minigames.call_holdem(parts[1], msg)
                    elif op == "raise" and len(parts) == 3:
                        result = self.minigames.raise_holdem(parts[1], int(parts[2]), msg)
                    elif op == "fold" and len(parts) == 2:
                        result = self.minigames.fold_holdem(parts[1], msg)
                    elif op == "wait" and len(parts) in {2, 3}:
                        result = await self.minigames.wait_holdem(parts[1], int(parts[2]) if len(parts) == 3 else 25)
                    elif op in {"active", "list"} and len(parts) == 1:
                        result = {"matches": self.minigames.active_holdem()}
                    else:
                        raise MiniGameError("holdem: create/status/check/call/raise/fold/say/wait/active")

            self._opened.add("minigames")
            return {
                "ok": True,
                "game": "minigames",
                "agent": self.agent_id,
                "area": area,
                "command": command,
                "result": result,
            }

        except (MiniGameError, ValueError) as exc:
            return {
                "ok": False,
                "game": "minigames",
                "agent": self.agent_id,
                "area": area,
                "command": command,
                "error": str(exc),
            }


    async def game_action(self, game: str, area: str, command: str) -> dict:
        game, area, command = (game or "").strip().lower(), (area or "").strip().lower(), (command or "").strip()
        if game == "lounge":
            return self._lounge.action(area=area, command=command)
        if game == "minigames":
            return await self._minigame_action(area, command)
        adapter = self.adapters.get(game)
        if adapter:
            return await adapter.action(self.agent_id, area, command)
        return {"ok": False, "error": f"unknown or unconfigured game: {game}"}

    def game_close(self, game: str) -> dict:
        game = (game or "").strip().lower()
        if game == "lounge":
            return self._lounge.close()
        self._opened.discard(game)
        return {"ok": True, "game": game, "agent": self.agent_id,
                "message": "Closed locally. Persistent game progress is unchanged."}
