# Architecture

The extracted system keeps the production file boundaries. server.py exposes MCP tools over Streamable HTTP. MemoryStore persists one Markdown file per memory under DATA_DIR/memory and guards mutations with an exclusive cross-process lock file. MemoryAtrium reads the same store and combines durable records with the read audit trail.

GameHall routes game calls to LoungeRoom, the production MiniGames referee, or an injected ExternalGameAdapter. Built-in match state lives under DATA_DIR/.games; the spectator viewer applies game-specific redaction before returning state.

LoungeRoom owns message sequence and per-reader cursors under DATA_DIR/.lounge. LoungeBridge scans that stream and persists its reliable-delivery state separately under DATA_DIR/.lounge-bridge. The browser extension leases work from the Bridge, binds each target to an explicit tab, performs and confirms a real content-script submission, and reports success or failure. An MCP ACK is still required after the target reads Lounge state.
