from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
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
from memory_store import MemoryStore, PASSIVE_CONTEXT_BUDGET, PASSIVE_ITEM_BUDGET, PASSIVE_MAX_RESULTS
from dream_scraps import DreamScrapStore
from memory_witness import MemoryWitnessStore
from memory_provenance import query_provenance
from dreams import (
    DEFAULT_CONFIG as DREAM_CONFIG,
    attach_dream_to_wake,
    claim_on_wake_dream,
    dream_commit_result,
    dream_get_result,
    load_owner_config,
)


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
witness_store = MemoryWitnessStore(PROJECT_ROOT)
OWNER_CONFIG = load_owner_config(PROJECT_ROOT, AGENT_ID)
WITNESS_ENABLED = OWNER_CONFIG.independent_witness_enabled
PASSIVE_RECALL_ENABLED = OWNER_CONFIG.passive_recall_enabled
mcp = MCPServer("Common AI Memory")


def _passive_summaries(items: list[dict]) -> list[dict]:
    remaining = PASSIVE_CONTEXT_BUDGET
    output = []
    for item in items[:PASSIVE_MAX_RESULTS]:
        content = str(item.get("content") or "")
        size = min(PASSIVE_ITEM_BUDGET, remaining)
        snippet = content if len(content) <= size else content[:size] + "…"
        output.append({
            "id": item.get("id"), "memory_id": item.get("id"), "snippet": snippet,
            "category": item.get("category"), "status": item.get("status"),
            "source": item.get("source"), "lifecycle": item.get("lifecycle") or "active",
            "verification": item.get("verification") or "unknown",
            "evidence_refs": item.get("evidence_refs") or [],
            "truncated": len(snippet) < len(content),
        })
        remaining -= len(snippet)
        if remaining <= 0:
            break
    return output



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
def remember(
    content: str,
    category: str = "general",
    visibility: str = "agent",
    status: str | None = None,
    source: str | None = None,
) -> dict:
    """Save a memory for this AI identity.

    category must be an indexed value from memory/_house/CATEGORY_INDEX.md.
    Legacy category names are normalized to their current standard category.
    visibility='agent' writes to this AI's private-by-owner namespace.
    visibility='shared' appends a shared memory carrying this AI as owner.
    Other AIs may read it but cannot edit or delete it.
    """
    category = category_policy.normalize(category)
    return store.remember(
        content=content, category=category, visibility=visibility, status=status, source=source
    )


@mcp.tool()
def recall(query: str = "", owner: str = "all", limit: int = 10,
           include_inactive: bool = False, passive: bool = False) -> list[dict]:
    """Search explicitly, or conservatively surface context with passive=true.

    Passive mode is model-initiated. Use it for substantive references to a
    known project, preference, prior decision, unfinished item, or past event;
    do not call it for greetings, acknowledgements, filler, or every turn.
    It is owner-bound and may safely return zero results.
    """
    effective_owner = AGENT_ID if passive else owner
    items = [] if passive and not PASSIVE_RECALL_ENABLED else store.recall(
        query=query, owner=effective_owner,
        limit=min(int(limit), PASSIVE_MAX_RESULTS) if passive else limit,
        include_inactive=False if passive else include_inactive, passive=passive,
    )
    audit_log.log_read(tool="recall", query=query, owner=effective_owner, limit=limit, items=items)
    if WITNESS_ENABLED:
        witness_store.expose(
            AGENT_ID, [str(item["id"]) for item in items], f"recall:{secrets.token_urlsafe(18)}",
            source="passive_recall" if passive else "recall",
            context_kind="passive_recall" if passive else "recall",
        )
    return _passive_summaries(items) if passive else items


@mcp.tool()
def recent(limit: int = 10, owner: str = "all", include_inactive: bool = False) -> list[dict]:
    """Walk through the newest readable memories in the shared house. Each record includes its room location."""
    items = store.recent(limit=limit, owner=owner, include_inactive=include_inactive)
    audit_log.log_read(tool="recent", owner=owner, limit=limit, items=items)
    if WITNESS_ENABLED:
        witness_store.expose(
            AGENT_ID, [str(item["id"]) for item in items], f"recent:{secrets.token_urlsafe(18)}",
            source="recent", context_kind="active_search",
        )
    return items


@mcp.tool()
def dream_get(date: str | None = None) -> dict:
    """Read this server owner's current or dated derived Dream without changing state."""
    return dream_get_result(PROJECT_ROOT, AGENT_ID, date)


@mcp.tool()
def dream_commit(dream_date: str, claim_token: str, content: str) -> dict:
    """Commit one on-wake Dream using the owner-bound claim token returned by wake."""
    return dream_commit_result(PROJECT_ROOT, AGENT_ID, dream_date, claim_token, content)


@mcp.tool()
def wake(recent_limit: int = 5, include_dream: bool = True, dream_max_chars: int = DREAM_CONFIG.wake_chars) -> dict:
    """Return recent memory, unread Lounge inbox, Dream context, and at most one pending Dream packet."""
    packet = {"recent": store.recent(limit=max(0, min(int(recent_limit), 30)), owner=AGENT_ID)}
    packet["lounge_inbox"] = game_hall.lounge_inbox(limit=20, mark_read=True)
    exposure_episode_id = f"wake:{secrets.token_urlsafe(18)}"
    if WITNESS_ENABLED:
        witness_store.expose(
            AGENT_ID, [str(item["id"]) for item in packet["recent"]], exposure_episode_id,
            source="wake", context_kind="wake_context",
        )
    packet["exposure_episode_id"] = exposure_episode_id
    attach_dream_to_wake(
        packet, project_root=PROJECT_ROOT, owner=AGENT_ID,
        include_dream=include_dream, max_chars=dream_max_chars,
    )
    packet["pending_dream"] = claim_on_wake_dream(
        PROJECT_ROOT, AGENT_ID, now=datetime.now(timezone.utc)
    )
    return packet


@mcp.tool()
def update_memory(
    memory_id: str,
    content: str,
    category: str | None = None,
    status: str | None = None,
    source: str | None = None,
    lifecycle: str | None = None,
    superseded_by: str | None = None,
    verification: str | None = None,
    evidence_refs: list[str] | None = None,
) -> dict:
    """Update a memory only if it was written by this AI identity.

    If category is supplied, it must be indexed in CATEGORY_INDEX.md.
    """
    category = category_policy.normalize_optional(category)
    return store.update(
        memory_id=memory_id, content=content, category=category, status=status, source=source,
        lifecycle=lifecycle, superseded_by=superseded_by, verification=verification,
        evidence_refs=evidence_refs,
    )


@mcp.tool()
def forget(memory_id: str) -> dict:
    """Delete a memory only if it was written by this AI identity."""
    result = store.forget(memory_id=memory_id)
    DreamScrapStore(PROJECT_ROOT).delete_by_source_ref(AGENT_ID, memory_id)
    witness_store.delete_evidence_ref(AGENT_ID, memory_id)
    return result


@mcp.tool()
def memory_provenance(memory_id: str, limit: int = 20) -> dict:
    """Read-only metadata provenance for one memory id, scoped to this service identity.

    Reports evidence_refs status, linked execution receipts, this identity's
    exposure/retrieval/witness ledgers, dream source linkage, scrap and audit
    counts. Never returns memory, dream, scrap, or query text, and does not
    record an exposure. Sections whose store is missing say status=unavailable.
    limit bounds each list (1..50).
    """
    return query_provenance(PROJECT_ROOT, AGENT_ID, memory_id, limit=limit)


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
def lounge_send(target: str, text: str) -> dict:
    """Send a durable Lounge message to one configured identity, or use target='all' to broadcast."""
    return game_hall.lounge_send(target=target, text=text)


@mcp.tool()
def lounge_inbox(limit: int = 20, mark_read: bool = True) -> dict:
    """Read unread Lounge messages visible to this identity without any browser extension."""
    return game_hall.lounge_inbox(limit=limit, mark_read=mark_read)


@mcp.tool()
def lounge_ack(sequence: int | None = None) -> dict:
    """Advance this identity's Lounge inbox cursor after an explicit non-marking read."""
    return game_hall.lounge_ack(sequence=sequence)


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


def main() -> None:
    host = os.getenv("CAM_BIND_HOST", os.getenv("AI_MEMORY_HOST", "localhost"))
    port = int(os.getenv("MEMORY_PORT", os.getenv("AI_MEMORY_PORT", "8765")))
    settings = getattr(mcp, "settings", None)
    if settings is not None:
        # FastMCP exposes network options on settings and accepts only transport
        # in run(); newer MCPServer builds accept the options directly instead.
        settings.host = host
        settings.port = port
        settings.stateless_http = True
        settings.json_response = True
        mcp.run(transport="streamable-http")
    else:
        mcp.run(
            transport="streamable-http",
            host=host,
            port=port,
            stateless_http=True,
            json_response=True,
        )


if __name__ == "__main__":
    main()
