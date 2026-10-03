# MCP API

`server.py` (`common-ai-memory-mcp`) serves MCP over Streamable HTTP at `http://localhost:<MEMORY_PORT>/mcp`. Each process has one fixed identity from `AI_MEMORY_AGENT`; no tool lets a caller pick another owner.

## Memory

| Tool | Purpose |
|---|---|
| `remember(content, category="general", visibility="agent", status=None, source=None)` | Save a memory for this identity. `category` must be a known `section/subject` value; `visibility="shared"` makes it a shared memory still owned by this identity. |
| `recall(query="", owner="all", limit=10, include_inactive=False, passive=False)` | Search readable memories (BM25/lexical, plus semantic when installed). `passive=True` returns at most three short snippets for this identity only and may return none. |
| `recent(limit=10, owner="all", include_inactive=False)` | Newest readable memories. |
| `update_memory(memory_id, content, category=None, status=None, source=None, lifecycle=None, superseded_by=None, verification=None, evidence_refs=None)` | Change a memory this identity owns. |
| `forget(memory_id)` | Delete a memory this identity owns; linked receipts are redacted, not deleted. |
| `memory_provenance(memory_id, limit=20)` | Read-only metadata trail for one memory (see [provenance.md](provenance.md)). |

## Dreams

| Tool | Purpose |
|---|---|
| `wake(recent_limit=5, include_dream=True, dream_max_chars=…)` | Recent memories for this identity, the current Dream, and at most one pending Dream to write. |
| `dream_commit(dream_date, claim_token, content)` | Submit the Dream written from `wake`'s pending materials. |
| `dream_get(date=None)` | Read the current or a dated Dream. |

## Games and Lounge (optional)

| Tool | Purpose |
|---|---|
| `game_list()`, `game_open(game)`, `game_status(game)`, `game_action(game, area, command, table_talk="")`, `game_close(game)` | Built-in games and the lounge (`game="lounge"`, `command="say"`). |
| `lounge_attachment_open(attachment_id)` | Open an image attached in the lounge. |
| `lounge_wake_ack(sequence)` | Confirm a browser-delivered lounge wake after reading the lounge. |

Reads are audit-logged with metadata only. Category policy is checked before storage. Game errors are returned as structured results.

Run one server process per AI identity, on separate ports.
