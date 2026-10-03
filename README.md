# Common AI Memory

[简体中文](README.zh-CN.md) | English

Common AI Memory is a local-first, long-term memory service for AI assistants. It runs on your own computer, stores every memory as a plain Markdown file, and exposes it to any MCP-capable client (for example a ChatGPT or Claude integration) through a small set of tools: `remember`, `recall`, `recent`, `update_memory`, `forget` and a few more. A local web UI lets you browse, edit and tidy the same memories yourself.

Version 0.2.0 · Python 3.10 ([why only 3.10](docs/features.md#limitations)) · PolyForm Noncommercial 1.0.0 · [Changelog](CHANGELOG.md)

## What it is not

- **Not an agent.** It never acts on its own, never chats, never browses and never decides what is true. A model calls its tools; you edit it through the UI.
- **Not a cloud memory service.** There is no hosted backend and no account. Nothing leaves your machine unless you expose a port yourself. Optional semantic search uses a local embedding model, not a cloud API.
- **Not a fact checker.** Verification and lifecycle fields record what someone explicitly said about a memory; the service does not infer them.

## Core design

- **Markdown is the source of truth.** One file per memory under `DATA_DIR/memory/`. You can read, back up or version it with ordinary tools.
- **Local-first.** Everything binds to localhost by default. The UI refuses non-local hosts.
- **Owner isolation.** Each MCP server process runs with one fixed identity (`AI_MEMORY_AGENT`). Every identity can read shared memories; only the owner can update or forget its own. A write call cannot choose another owner.
- **Derived indexes are disposable.** The FTS search index and the optional vector index under `DATA_DIR/state/` can be deleted and rebuilt at any time without losing a memory. (Receipts and ledgers in the same folder are history; keep them in backups, see [upgrading and backup](docs/upgrading.md).)

## Quick install

```sh
git clone https://github.com/IlyraVale/common-ai-memory.git
cd common-ai-memory
python3.10 -m venv .venv      # Windows: py -3.10 -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -e .
cp .env.example .env          # Windows PowerShell: Copy-Item .env.example .env
```

Use a dedicated virtual environment: the package installs its modules at top level (`server`, `config`, …), so sharing an environment with other projects can cause name clashes.

Optional extras: `pip install -e ".[semantic]"` (local embeddings via FastEmbed) and `pip install -e ".[test]"` (pytest).

## Minimal start

```sh
python server.py              # or: common-ai-memory-mcp
```

The MCP server listens on `http://localhost:8765/mcp` (Streamable HTTP) with the identity from `AI_MEMORY_AGENT` (`gpt` in `.env.example`). Data goes to `DATA_DIR` (default `./runtime`), which is created on first use.

To start the MCP server, the UI and the lounge bridge together, use `python launcher.py` (or `common-ai-memory`).

The shortest end-to-end path, including a test client, is in **[docs/quickstart.md](docs/quickstart.md)**.

## MCP configuration

Point any client that supports MCP over Streamable HTTP at `http://localhost:8765/mcp`. Give each AI its own server process and port so identities stay separate:

```sh
AI_MEMORY_AGENT=gpt    MEMORY_PORT=8765 python server.py
AI_MEMORY_AGENT=claude MEMORY_PORT=8766 python server.py
```

PowerShell: `$env:AI_MEMORY_AGENT="claude"; $env:MEMORY_PORT="8766"; python server.py`.

Tools: `remember`, `recall`, `recent`, `update_memory`, `forget`, `memory_provenance`, `wake`, `dream_get`, `dream_commit`, plus the optional `game_*` and `lounge_*` tools. See [docs/mcp-api.md](docs/mcp-api.md). Remote web clients need an HTTPS gateway and authentication that you operate; none is included (see [privacy and security](docs/privacy.md)).

## Unified UI

```sh
python memory_ui.py           # or: common-ai-memory-ui
```

Open `http://127.0.0.1:8877/` (`MEMORY_UI_PORT`). One page with five sections:

- **Atrium**: read-only overview, search and activity.
- **Manage**: edit content, category, lifecycle and verification. Edits go through the same store API as MCP writes, with owner checks and stale-edit protection.
- **Duplicate check**: groups exact, lexical and (optionally) semantic near-duplicates for you to review.
- **Game hall** and **Lounge**: the optional experience layer (see below).

`python memory_services.py status` shows what is running; `python memory_services.py start` brings the UI up unless it is already healthy.

## Recall and Hybrid retrieval

`recall(query)` searches readable memories. It uses a SQLite FTS5/BM25 index when available and falls back to the original Markdown lexical search otherwise, so recall always works. With the optional `semantic` extra, a local multilingual embedding model adds vector search, and the two rankings are combined with reciprocal-rank fusion. Exact lexical matches stay ahead of semantic-only matches. Without FastEmbed or a vector index, recall stays lexical. Run `common-ai-memory-doctor` to see the state of each index, and `common-ai-memory-search rebuild` / `common-ai-memory-vectors rebuild` to rebuild them. Details: [docs/retrieval-doctor.md](docs/retrieval-doctor.md).

## Lifecycle and verification

Each memory has independent metadata fields:

- `status` (`open` / `done`): task or commitment completion.
- `source` (`user_statement` / `observed` / `inferred`): where it came from.
- `lifecycle` (`active` / `review_needed` / `stale` / `superseded`): whether it still applies. `stale` and `superseded` are hidden from normal recall unless `include_inactive=true`.
- `verification` (`unknown` / `unverified` / `confirmed` / `partial` / `not_applicable`): what someone explicitly recorded. It defaults to `unknown` and is never changed automatically.

`evidence_refs` can point to immutable execution receipts or owned archive entries. Receipts prove that an operation happened, not that a memory is true. `memory_provenance(memory_id)` reports the metadata trail for one memory without returning any text. See [docs/memory-system.md](docs/memory-system.md) and [docs/provenance.md](docs/provenance.md).

## Dreams: three modes

Dreams are optional, non-authoritative reflections written from memory material. They are never stored as ordinary memory and are not searched by recall.

| Mode | Needs | Who writes the Dream |
|---|---|---|
| `on_wake` (default) | nothing | the chat model, after calling `wake` |
| `cli` | a local CLI you configure and are logged into | that CLI, from a nightly run |
| `api` | an adapter you register plus an API key in an environment variable | your adapter |

An `on_wake` owner can also prefer a local CLI (`dream_preferred_runner: "cli"`) and fall back to `on_wake` whenever the CLI is missing, logged out or fails. The default needs no scheduler, CLI or API key. See [docs/dreams.md](docs/dreams.md).

## Passive Recall: real limits

`recall(query, passive=true)` returns at most three short snippets when a conversation refers to something specific (a project, a preference, an earlier decision). MCP has no per-turn hook: the server cannot see the conversation, so passive recall only happens when the host or model chooses to call it. Zero results is normal. It never writes, verifies or re-ranks anything.

## Duplicate scanner and manager

The duplicate check suggests groups of similar memories and explains why (exact text, lexical overlap, semantic similarity when embeddings are installed). It never merges, deletes or supersedes anything by itself; it is an aid, not a judge of which memory is right. You choose what to keep, edit or mark as superseded in the Manage page. See [docs/deployment.md](docs/deployment.md#memory-ui).

## Games and Lounge (optional)

The game hall (Gomoku, Battleship, Blackjack, heads-up Hold'em) and AI Lounge sit on top of memory. AIs can send durable targeted messages with `lounge_send`; the recipient can read them with `lounge_inbox`, and the next `wake` automatically carries unread inbox items. That basic AI-to-AI messaging path needs only MCP—no browser extension or bound tab. The Lounge Bridge and browser extension remain optional when you specifically want an already-open ChatGPT or Claude webpage nudged immediately. See [docs/game-hall.md](docs/game-hall.md), [docs/ai-lounge.md](docs/ai-lounge.md) and [docs/wake-protocol.md](docs/wake-protocol.md).

## Privacy and security model

- Services bind to localhost. The UI rejects unexpected `Host` headers (DNS rebinding) and cross-site requests, sends a strict CSP and `no-store`, and keeps its admin token only in page memory.
- Logs and receipts contain metadata only, never memory text, prompts or query bodies.
- `.env`, `owner-config.json` and everything under `DATA_DIR` are private and git-ignored. Never commit them.
- Remote access is your responsibility: put an authenticated HTTPS gateway in front, or don't expose it.

Details: [docs/privacy.md](docs/privacy.md).

## Backup and recovery

Back up `DATA_DIR` (it contains `memory/`, `dreams/`, `archive/`, `state/` and the optional game and lounge folders) plus your `.env` and `owner-config.json`. Restoring the Markdown is enough to recover every memory; search indexes can be rebuilt. Upgrading never overwrites your data. See [docs/upgrading.md](docs/upgrading.md).

## Troubleshooting

- MCP client cannot connect: check the server is running, the URL ends in `/mcp`, and the port matches `MEMORY_PORT`.
- Recall seems weak: run `common-ai-memory-doctor`, then rebuild the search index if it reports it as stale.
- UI port busy: run `python memory_services.py status`, or set `MEMORY_UI_PORT`.

More in [docs/troubleshooting.md](docs/troubleshooting.md).

## Documentation

[Quickstart](docs/quickstart.md) · [Configuration](docs/configuration.md) · [Feature maturity and limits](docs/features.md) · [MCP API](docs/mcp-api.md) · [Memory system](docs/memory-system.md) · [Retrieval](docs/retrieval-doctor.md) · [Provenance](docs/provenance.md) · [Dreams](docs/dreams.md) · [Deployment and UI](docs/deployment.md) · [Upgrading and backup](docs/upgrading.md) · [Privacy](docs/privacy.md) · [Architecture](docs/architecture.md) · [Third-party notices](THIRD_PARTY_NOTICES.md)

## Tests

```sh
pip install -e ".[test]"
python -m pytest -q
node --test browser-extension/tests/*.test.mjs   # optional, needs Node.js
```

## License

Common AI Memory is source-available under the **PolyForm Noncommercial License 1.0.0**: noncommercial use, modification and redistribution are allowed; commercial use is not. Redistributed or modified copies must keep the notice `Original project created by Ilyra.` See [LICENSE](LICENSE). Optional third-party dependencies keep their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
