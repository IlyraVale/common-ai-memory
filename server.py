from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import env_path, load_dotenv

try:
    from mcp.server import MCPServer
except ImportError:  # compatibility for development environments on MCP 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer
from mcp.types import ImageContent

from category_policy import CategoryPolicy
from game_hall import GameHall
from lounge_attachments import LoungeAttachmentStore
from memory_audit import MemoryAuditLog
from memory_store import MemoryStore


load_dotenv()
PROJECT_ROOT = env_path("DATA_DIR", os.getenv("AI_MEMORY_ROOT", "./runtime"))
AGENT_ID = os.getenv("AI_MEMORY_AGENT", "").strip()
if not AGENT_ID:
    raise RuntimeError(
        "AI_MEMORY_AGENT is required. Start this server with a fixed identity, "
        "for example: $env:AI_MEMORY_AGENT='gpt'"
    )
LOUNGE_BRIDGE_URL = os.getenv("LOUNGE_BRIDGE_URL", os.getenv("AI_MEMORY_LOUNGE_BRIDGE_URL", "http://localhost:8879")).rstrip("/")

store = MemoryStore(PROJECT_ROOT, AGENT_ID)
audit_log = MemoryAuditLog(PROJECT_ROOT, AGENT_ID)
category_policy = CategoryPolicy(PROJECT_ROOT)
game_hall = GameHall(PROJECT_ROOT, AGENT_ID)
attachment_store = LoungeAttachmentStore(PROJECT_ROOT)
mcp = MCPServer("Common AI Memory")



def _format_game_exception(exc: BaseException) -> str:
    """Flatten ExceptionGroup / TaskGroup wrappers into useful error details."""
    parts = []
    seen = set()

    def walk(err, depth=0):
        if err is None or id(err) in seen or depth > 8:
            return
        seen.add(id(err))

        text = str(err).strip() or repr(err)
        parts.append(f"{type(err).__name__}: {text}")

        children = getattr(err, "exceptions", None)
        if children:
            for child in children:
                walk(child, depth + 1)
            return

        cause = getattr(err, "__cause__", None)
        if cause is not None:
            walk(cause, depth + 1)
            return

        context = getattr(err, "__context__", None)
        if context is not None:
            walk(context, depth + 1)

    walk(exc)
    return " -> ".join(parts)[:2000]


@mcp.tool()
def remember(content: str, category: str = "general", visibility: str = "agent") -> dict:
    """Save a memory for this AI identity.

    category must be an indexed value from memory/_house/CATEGORY_INDEX.md.
    Legacy category names are normalized to their current standard category.
    visibility='agent' writes to this AI's private-by-owner namespace.
    visibility='shared' appends a shared memory carrying this AI as owner.
    Other AIs may read it but cannot edit or delete it.
    """
    category = category_policy.normalize(category)
    return store.remember(content=content, category=category, visibility=visibility)


@mcp.tool()
def recall(query: str = "", owner: str = "all", limit: int = 10) -> list[dict]:
    """Search memories across the shared memory house. Each result includes its physical room location.

    owner can be 'all', 'human', 'shared', or a specific agent id such as
    'gpt' or 'claude'. Reading across agents is allowed; writing is not.
    """
    items = store.recall(query=query, owner=owner, limit=limit)
    audit_log.log_read(tool="recall", query=query, owner=owner, limit=limit, items=items)
    return items


@mcp.tool()
def recent(limit: int = 10, owner: str = "all") -> list[dict]:
    """Walk through the newest readable memories in the shared house. Each record includes its room location."""
    items = store.recent(limit=limit, owner=owner)
    audit_log.log_read(tool="recent", owner=owner, limit=limit, items=items)
    return items


@mcp.tool()
def update_memory(memory_id: str, content: str, category: str | None = None) -> dict:
    """Update a memory only if it was written by this AI identity.

    If category is supplied, it must be indexed in CATEGORY_INDEX.md.
    """
    category = category_policy.normalize_optional(category)
    return store.update(memory_id=memory_id, content=content, category=category)


@mcp.tool()
def forget(memory_id: str) -> dict:
    """Delete a memory only if it was written by this AI identity."""
    return store.forget(memory_id=memory_id)


@mcp.tool()
def game_list() -> dict:
    """List games available through the shared game hall."""
    return game_hall.game_list()


@mcp.tool()
async def game_open(game: str) -> dict:
    """Open a game and return a concise current-state summary."""
    try:
        return await game_hall.game_open(game)
    except Exception as exc:
        return {
            "ok": False,
            "game": game,
            "operation": "game_open",
            "error_type": type(exc).__name__,
            "error": _format_game_exception(exc),
        }


@mcp.tool()
async def game_status(game: str) -> dict:
    """Read a concise current status for a connected game."""
    try:
        return await game_hall.game_status(game)
    except Exception as exc:
        return {
            "ok": False,
            "game": game,
            "operation": "game_status",
            "error_type": type(exc).__name__,
            "error": _format_game_exception(exc),
        }


@mcp.tool()
def lounge_attachment_open(attachment_id: str) -> list[ImageContent]:
    """Open one image referenced by an AI Lounge message as real MCP image content.

    Use the lightweight attachment id returned by game_status('lounge'). Animated
    GIF/APNG results include the original plus a JPEG contact sheet of sampled frames.
    """
    return attachment_store.image_content(attachment_id)


@mcp.tool()
async def game_action(game: str, area: str, command: str, table_talk: str = "") -> dict:
    """Perform one real game command through an allowlisted game area.

    Keep command machine-readable and concise. Do not embed natural-language
    table talk inside command. For minigames, pass optional conversation through
    the separate table_talk field; the server converts it to the legacy internal
    `command :: table-talk` format only after the MCP call has been accepted.

    Optional external games are available only when an operator injects a
    separately installed adapter. No external implementation is bundled.
    """
    try:
        command = (command or "").strip()
        table_talk = (table_talk or "").strip()
        if not command:
            raise ValueError("command is required")
        if len(table_talk) > 240:
            raise ValueError("table_talk must be 240 characters or fewer")
        legacy_command = command
        if table_talk:
            legacy_command = f"{command} :: {table_talk}"
        return await game_hall.game_action(game=game, area=area, command=legacy_command)
    except Exception as exc:
        return {
            "ok": False,
            "game": game,
            "area": area,
            "command": command if isinstance(command, str) else str(command),
            "operation": "game_action",
            "error_type": type(exc).__name__,
            "error": _format_game_exception(exc),
        }


@mcp.tool()
def game_close(game: str) -> dict:
    """Close the local game-hall session without changing persistent progress."""
    return game_hall.game_close(game)


@mcp.tool()
def lounge_wake_ack(sequence: int) -> dict:
    """Acknowledge a delivered AI Lounge wake for this AI identity.

    Call this only after actually reading game_status('lounge') for this wake
    and, if natural, replying through game_action. The AI Lounge Bridge treats
    a delivered wake as unacknowledged until this is called, so it keeps
    retrying delivery otherwise. Do not call this speculatively or for a
    sequence you have not actually checked.
    """
    if AGENT_ID not in {"gpt", "claude"}:
        raise ValueError("lounge_wake_ack is only available to the gpt or claude identity")
    if not isinstance(sequence, int) or sequence <= 0:
        raise ValueError("sequence must be a positive integer")
    payload = json.dumps({"target": AGENT_ID, "sequence": sequence}).encode("utf-8")
    request = Request(
        f"{LOUNGE_BRIDGE_URL}/v1/ack",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
            return {"ok": bool(body.get("ok")), "sequence": sequence, "target": AGENT_ID}
    except (HTTPError, URLError, OSError, ValueError) as exc:
        return {"ok": False, "sequence": sequence, "target": AGENT_ID, "error": str(exc)[-300:]}


if __name__ == "__main__":
    host = os.getenv("CAM_BIND_HOST", os.getenv("AI_MEMORY_HOST", "localhost"))
    port = int(os.getenv("MEMORY_PORT", os.getenv("AI_MEMORY_PORT", "8765")))
    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
    )
