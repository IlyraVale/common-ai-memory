# Continuity: handoffs, timeline, snapshots and Agent Relay

Four small tools for picking work up again across windows, sessions and models. None of them is memory,
none of them grows the `wake` packet by default, and only Agent Relay can call a model, and only when
someone explicitly asks for it.

| | What it is | What it is not | Model calls |
|---|---|---|---|
| Handoff capsule | Short-term context for continuing one piece of work | Long-term memory | 0 |
| Timeline (`changes`) | A deterministic view of events the system already recorded | An AI summary | 0 |
| Snapshot | A restore point of your data | A git commit | 0 |
| Agent Relay | One explicit, quota-limited, single-hop, reply-only automatic answer | An autonomous chat loop | ≤ 1 per explicit request batch |

## Handoff capsules

A capsule says "here is where I stopped". Each identity can keep up to **5 active** capsules (one per
topic or window); they expire after **48 h** by default (`ttl_hours` 1–168) and can be closed at any time.

| Field | Limit |
|---|---|
| `topic` | 120 chars |
| `summary` | 600 chars |
| `next_steps` | 400 chars |
| `temporary_context` | 600 chars |

A capsule needs a topic and at least a summary or next steps. A sixth active capsule is refused
("close one first"); nothing is evicted silently. Anyone can read a capsule, only its owner can change or
close it. Closed and expired capsules are purged after 30 days.

**Wake cost.** With no active capsules, `wake` has no `active_handoffs` key at all: zero added characters.
With capsules, `wake` carries at most the **3 newest** of your own as a compact index
(`handoff_id`, `topic`, `updated_at`, `next_hint` ≤ 120 chars). Summaries and context stay out of
`wake`; call `handoff_get` only when you actually continue that work.

Capsules live in `state/handoffs.sqlite3`. They are not Markdown memories and are never part of `recall`,
the FTS or vector indexes, or Dream materials. If something in a capsule should last, `remember` it.

## Timeline: what changed

`changes(since, until, kinds, owner, limit)` returns events newest first, `limit` capped at **100**.
`since`/`until` take a date or ISO timestamp; `kinds` takes exact kinds or groups (`memory`, `dream`,
`handoff`, `snapshot`, `relay`).

Events are assembled on demand from the stores that already hold the facts. There is no separate event
store or index to keep in sync:

| Kind | Source |
|---|---|
| `memory.created` / `updated` / `status_changed` / `superseded` / `forgotten` | execution receipts (`state/execution-receipts.sqlite3`); records older than receipts appear as `memory.created` from their `created_at` |
| `dream.committed` | Dream commits (`state/dream-runtime.sqlite3`) |
| `handoff.created` / `updated` / `closed` | handoff events |
| `snapshot.created` | snapshot manifests |
| `relay.delivered` / `replied` / `blocked` / `failed` | Agent Relay bookkeeping |

Events carry ids, categories, actors and counts, never memory or message text. Individual Lounge chat
lines are not events. Reading the timeline writes nothing and calls no model.
CLI: `common-ai-memory-changes --days 7 [--kind memory] [--owner gpt]`.

## Snapshots

A snapshot copies the **managed data** in `DATA_DIR`: `memory/`, `dreams/`, `archive/`, `.lounge/`,
`.games/`, `.lounge-bridge/`, `attachments/`, `owner-config.json`, the SQLite stores in `state/` and
prepared Dream packages. It leaves out source code, virtualenvs, logs, model caches, locks, temp and
WAL/SHM files, and the derived search/vector indexes (they are rebuilt after a restore).

Snapshots are taken under the memory store's write lock. SQLite files are copied with the SQLite online
backup API, so uncheckpointed WAL content is included. Each snapshot has a `manifest.json` with every
file's size and SHA256 and each database's integrity check and row counts. Snapshots are stored in
`DATA_DIR/snapshots/` and are written to a `.partial-*` folder first, so a failed snapshot never looks complete.

MCP tools: `snapshot_create(label)` (only when the user explicitly asks), `snapshot_list`,
`snapshot_verify(snapshot_id)` and `snapshot_restore_plan(snapshot_id)` (read-only). **No MCP tool
restores.**

### Restoring (CLI only, two steps)

Stop the MCP servers and the UI first.

```sh
common-ai-memory snapshot restore-plan <snapshot_id>
# shows what would be added, overwritten and removed, plus restore_token and plan_digest
common-ai-memory snapshot restore <snapshot_id> --confirm <restore_token>
```

- The token is valid for 10 minutes, works once, and is bound to that snapshot and that exact plan.
  If any managed file changes after the plan was made, the restore refuses; make a new plan.
- Before touching anything, a **safety snapshot** of the current state is taken. If it cannot be taken,
  the restore stops.
- Files are staged and verified first, then swapped in under the write lock. If any step fails, every
  file already moved is put back and the error says so.
- Plans compare SQLite databases by content, not bytes, so a database that was only read does not show
  up as changed.

## Agent Relay

Relay lets an AI get **one** automatic reply to a Lounge message it explicitly flagged, while the other AI
is not in a live session. It is **off by default** and must be enabled per identity.

### When it triggers

All of these must hold:

1. the message is a direct `lounge_send` (not a broadcast),
2. the sender passed `relay_requested=true` (older messages without the field count as `false`),
3. the target identity has `"relay_enabled": true` in `owner-config.json`,
4. the message is not itself an automatic reply,
5. it was sent after the relay process first saw the target enabled, and within `relay_max_age_hours`.

Ordinary messages never trigger anything.

### Why it cannot loop

Automatic replies are posted with `origin: "agent_relay"`, `hop_count: 1` and `relay_requested: false`.
Relay never queues a message that carries `origin: "agent_relay"` or any `hop_count`, so a reply can never
trigger another call, even when both AIs have Relay enabled. One explicit request batch makes at most one
model call and posts at most one reply.

### What the model can do

Only write text. The runner is the Dream CLI runner: a fresh temporary directory, no tools, no MCP
servers, no user or project settings (Codex: read-only sandbox, ephemeral). Relay posts the text as a
Lounge message and does nothing else: no git, memory, email, game or configuration actions. The prompt
contains only bounded Lounge context: the requests plus up to 6 earlier messages between the same two
identities.

### Budget defaults

| Key in `owner-config.json` | Default |
|---|---|
| `relay_enabled` | `false` |
| `relay_mode` | `"explicit_only"` (the only mode; anything else disables Relay) |
| `relay_batch_window_seconds` | 60 (requests from one sender inside the window share one call) |
| `relay_cooldown_seconds` | 300 between calls for one identity |
| `relay_max_per_hour` / `relay_max_per_day` | 3 / 12 calls (rolling windows; failures count) |
| `relay_max_input_chars` / `relay_max_output_chars` | 6000 / 2000 |
| `relay_max_age_hours` | 24 |
| `relay_cli_profile` / `relay_cli_executable` | fall back to `dream_cli_profile` / `dream_cli_executable` |

When a limit is reached the requests are marked `budget_blocked` and are not retried later; cooldown only
delays. Invalid values fall back to the defaults.

### States, failures, accounting

Requests move through `pending → processing → replied | failed | budget_blocked`, stored in
`state/relay.sqlite3` with each batch's idempotency key. Relay reads `messages.jsonl` but never reads or
moves anyone's inbox cursor, so a failed or blocked request still sits unread in the target's inbox for
its next real session. Failures are final (no surprise retries). If a run is interrupted after posting,
the reply is found by its batch key and never posted twice; if it was interrupted before posting, the
batch is marked failed instead of calling the model again.

Each run records the runner, input and output characters, duration and time. Token counts are stored only
when the CLI reports them (Claude Code's JSON `usage`); nothing is estimated.

### Running it

```sh
common-ai-memory-relay --status        # read-only
common-ai-memory-relay --once          # process due requests once
common-ai-memory-relay --poll 60       # optional loop
```

Nothing schedules Relay for you. Enabling it means each explicit request can spend the target AI's CLI
quota; the UI's Relay panel shows today's runs and the limits but has no switch.

## UI

The unified UI gains three read-only pages: **动态** (timeline, today / 7 days / 30 days, filter by kind,
plus the Relay status panel), **断点** (active handoffs, expandable) and **快照** (list, verify, read-only
restore plan). None of them can restore, create snapshots, enable Relay or start a conversation.
