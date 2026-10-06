# Changelog

All notable changes to Common AI Memory. Versions follow [Semantic Versioning](https://semver.org/); before 1.0, minor versions may add features and change defaults, with upgrade notes in [docs/upgrading.md](docs/upgrading.md).

## [Unreleased]

### Continuity
- Handoff capsules (`handoff_set` / `list` / `get` / `close`): up to 5 active per identity, 48 h default TTL (max 168 h), bounded fields. Not memory: never in recall, search indexes or Dream materials. `wake` adds at most three compact hints and nothing when there are none.
- `changes`: a deterministic, read-only event timeline built from existing receipts, Dream commits, handoffs, snapshots and relay runs. No model calls, no new index.
- Snapshots (`snapshot_create` / `list` / `verify` / `restore_plan`): managed data only, SQLite via the online backup API under the write lock, SHA256 manifest. Restore is CLI-only and two-step, with a one-time plan-bound token, a mandatory safety snapshot and rollback on failure.
- Agent Relay (`common-ai-memory-relay`), off by default: `lounge_send(..., relay_requested=True)` may get one reply-only, single-hop automatic answer from a target that enabled Relay. Batched, cooldown and hourly/daily limits, bounded input/output, idempotent replies, failures never move inbox cursors.
- UI: read-only 动态 (timeline), 断点 (handoffs) and 快照 (snapshots) pages and a Relay status panel.

## [0.3.0] - 2026-10-04

### Lounge Inbox
- Added `lounge_send`, `lounge_inbox` and `lounge_ack` for durable direct or broadcast AI messaging without a browser extension.
- `wake` now returns `lounge_inbox` and marks only the messages actually returned to that identity as read.
- Direct-message visibility and Lounge Bridge wake routing are identity-specific; broadcast behavior remains available.
- Reader delivery proof is stored as bounded, compatible ranges. Pathological gaps may cause safe duplicate delivery, never cursor advancement across an undelivered message.

### Unified UI themes
- Added synchronized MONO and GLASS themes across Atrium, Manage, Duplicate check, Game hall and Lounge.
- GLASS uses a fixed pale-pink background with translucent blurred surfaces; functional game boards keep their necessary material colors and contrast.
- Theme choice persists in local storage and propagates to same-origin page frames.

### Compatibility
- MCP HTTP startup supports SDK variants that configure host, port and streamable-HTTP options through FastMCP settings.
- Python support remains `>=3.10,<3.11`.

## [0.2.0] - 2026-10-03

### Memory core
- Memory records gain independent `lifecycle` and `verification` fields; older records keep working with neutral defaults and are not rewritten.
- Concurrent writers are safer: bounded retries for Windows file-sharing conflicts during atomic replacement.
- `forget` redacts the target of linked receipts instead of deleting history.

### Retrieval
- SQLite FTS5/BM25 search index, rebuildable at any time, with automatic fallback to the Markdown lexical search.
- Optional local semantic retrieval (`semantic` extra, FastEmbed, multilingual MiniLM) combined with BM25 by reciprocal-rank fusion; exact lexical matches stay first.
- Passive Recall: `recall(passive=true)` returns at most three short, budgeted snippets and may return none.
- Retrieval Doctor (`common-ai-memory-doctor`) reports Markdown, FTS and vector index health separately; rebuilds are explicit.

### Lifecycle and verification
- `active`, `review_needed`, `stale`, `superseded` with explicit, same-owner, acyclic supersession. Inactive records are hidden from normal recall unless requested.
- Explicit `verification` states that never change automatically.

### Evidence, receipts and provenance
- Append-only, metadata-only execution receipts usable as `evidence_refs`.
- `memory_provenance` MCP tool and `common-ai-memory-provenance` command: a read-only metadata trail for one memory, distinguishing "unavailable" from "empty".
- Exposure and independent-witness ledgers so a recalled memory cannot confirm itself.

### Dream
- Dream modes per owner: `on_wake` (default, no setup), `cli`, `api` (adapter contract).
- Preferred local CLI for `on_wake` owners with automatic fallback; never writes a fake Dream.
- Nightly preparation (`common-ai-memory-dream-nightly`) with prepared packets, claim leases, a one-day grace rule, dry-run and clear exit codes; optional short-lived Dream scraps.
- Example scheduler setups for Windows, cron, systemd and macOS.

### Memory UI
- One localhost page (`common-ai-memory-ui`, default port 8877) with Atrium, Manage, Duplicate check, Game hall and Lounge.
- Manager edits go through the store API with owner checks and stale-edit protection.
- `/health` endpoint with non-sensitive status; a second copy on the same port is detected and does not start.
- Hardened defaults: Host and Origin checks, per-process admin token kept in page memory only, strict CSP, `no-store`, no access logs.
- `common-ai-memory-services start|status` and a unified launcher that starts the UI, the lounge bridge and the MCP server.

### Duplicate management
- Duplicate scanner (`common-ai-memory-duplicates scan` and the UI page): exact, lexical and optional semantic grouping with reasons. Suggestions only; nothing is merged or deleted automatically.

### Games and Lounge
- Game hall and lounge are served inside the unified UI (`/games`, `/lounge`); the standalone viewers remain available.
- Lounge bridge can be supervised by the UI and starts in manual mode.

### Windows integration (optional)
- `scripts/install-memory-ui-task.ps1`: per-user logon task that starts the UI hidden.
- `scripts/install-nightly-dream-task.ps1` and `run-nightly-dreams.ps1` for the nightly Dream run.
- `scripts/start-common-ai-memory.ps1`: start the UI if needed and print status.

### Reliability fixes
- Bounded retries for SQLite initialisation races (receipts, Dream runtime) instead of failing on first contention.
- Refused requests in the UI and the standalone manager drain their request body, so the reply is not lost to a connection reset.
- Fast `/health` even when the lounge bridge is offline.
- Command-line tools and nightly scripts default to `DATA_DIR`, matching the MCP server, instead of the install folder.
- New `common-ai-memory-mcp` entry point for installed packages.

### Supported Python
- This release supports Python 3.10 only (`requires-python = ">=3.10,<3.11"`).

### Known issues
- Python 3.12 and newer are not supported yet: on Windows some SQLite handles are released only by the garbage collector, which can delay index file replacement and makes the test suite report teardown errors. A fix is planned for 0.2.1.

## [0.1.0] - 2026-09-05

- First source-available release: Markdown memory store with owner isolation, MCP memory, game and lounge tools, read-only Atrium, built-in games, AI Lounge, lounge bridge and browser extension.
