# Configuration

Common AI Memory reads settings from the process environment and from an optional `.env` file in the project folder (copy `.env.example`). Values already set in the environment win over `.env`. Per-owner Dream and recall options live in an optional `owner-config.json` inside `DATA_DIR`.

**Private files: never commit them.** `.env`, `owner-config.json`, any API key, and everything under `DATA_DIR` are git-ignored on purpose. Reference credentials by environment-variable name; never write a key into a JSON file.

## Identity (owner)

| Variable | Default | Meaning |
|---|---|---|
| `AI_MEMORY_AGENT` | none (required by the MCP server) | The fixed identity of one MCP server process, for example `gpt` or `claude`. Writes always belong to this owner. Run one process per AI. |

Owners are plain lowercase names. Reading is shared; updating and forgetting are limited to the owner.

## Data directory (roots)

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `./runtime` | Root for all runtime data: `memory/`, `dreams/`, `archive/`, `state/`, `.lounge/`, `.games/`, `.lounge-bridge/`. Relative paths resolve from the folder you start the process in. |
| `AI_MEMORY_ROOT` | none | Older name, used only when `DATA_DIR` is unset. |

Every command-line tool uses the same default and accepts `--root` (`dream_nightly` uses `--project-root`) to point elsewhere.

## Ports and host

| Variable | Default | Service |
|---|---|---|
| `MEMORY_PORT` | `8765` | MCP server (`/mcp`) |
| `MEMORY_UI_PORT` | `8877` | Unified Memory UI |
| `MEMORY_ATRIUM_PORT` | `8877` | Same UI when started through `memory_atrium.py` |
| `LOUNGE_BRIDGE_PORT` | `8879` | Lounge bridge status/ACK endpoint (optional) |
| `LOUNGE_BRIDGE_URL` | `http://localhost:8879` | Where the MCP server and UI reach the bridge |
| `GAME_HALL_PORT`, `LOUNGE_PORT` | `8876`, `8878` | Only for the standalone game/lounge viewers; the UI serves `/games` and `/lounge` itself |
| `CAM_BIND_HOST` | `localhost` | Bind address. The UI refuses non-local values. Exposing the MCP server beyond localhost requires your own authenticated HTTPS gateway. |

## Lounge (optional)

| Variable | Default | Meaning |
|---|---|---|
| `LOUNGE_IDENTITIES` | `gpt,claude,alice` | Who may post in the lounge |
| `LOUNGE_HUMAN_IDENTITY` | `alice` | The identity the UI posts as |
| `LOUNGE_HUMAN_DISPLAY_NAME` | `Alice` | Display name in the lounge page |

Bridge behaviour (mode `manual` / `active` / `ai-chat`, cooldowns, retries) is configured in `DATA_DIR/.lounge-bridge/config.json`; start from `examples/lounge-bridge-config.json`. Manual mode, the default, never wakes anyone automatically.

## Embeddings (optional)

Install `pip install -e ".[semantic]"` to enable local semantic retrieval with FastEmbed and `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`. Build the index with `common-ai-memory-vectors rebuild`; the model is downloaded once into `DATA_DIR/state/models/`. Without it, recall and the duplicate check use lexical matching only and say so.

## owner-config.json

Optional. Without it, every owner uses the defaults below. Copy `owner-config.example.json` to `DATA_DIR/owner-config.json` and keep only the owners and keys you need.

| Key | Default | Meaning |
|---|---|---|
| `dream_mode` | `on_wake` | `on_wake`, `cli` or `api` (see below) |
| `dream_preferred_runner` | absent | `"cli"`: with `dream_mode: on_wake`, a nightly run may generate the Dream with a local CLI and falls back to `on_wake` on any problem |
| `dream_cli_profile` | — | `claude_code` or `codex` (fixed, tool-less invocations) for the preferred runner |
| `dream_cli_executable` | profile name | Absolute path, name on `PATH`, or glob |
| `dream_cli` | — | For `dream_mode: cli`: the command as a JSON array, run without a shell |
| `dream_api_adapter` | — | For `dream_mode: api`: name of an adapter you register (none is bundled) |
| `dream_api_key_env` | — | Name of the environment variable holding the API key |
| `dream_model` | `null` | Model name passed to the CLI/API runner |
| `dream_timeout_seconds` | 300 (preferred CLI), 180 | Upper bound per run (preferred CLI at most 480) |
| `dream_scraps_enabled`, `dream_scrap_ttl_hours`, `dream_scrap_max_per_dream` | `true`, `72`, `1` | Short-lived Dream scraps |
| `independent_witness_enabled` | `true` | Exposure ledger that keeps recalled memories from confirming themselves |
| `passive_recall_enabled` | `true` | Allow `recall(passive=true)` for this owner |

### Dream modes

- **`on_wake` (default, safe):** nothing to install. The chat model calls `wake`, writes the Dream from the provided materials, and submits it with `dream_commit`.
- **CLI adapter (optional):** `dream_mode: cli`, or `on_wake` with `dream_preferred_runner: cli`. Needs a local CLI that you have installed and logged into, plus a scheduled nightly run (`dream_nightly.py`, see [dreams.md](dreams.md)). If the CLI is missing or logged out, nothing fake is written; `on_wake` takes over.
- **API adapter (optional):** `dream_mode: api` with an adapter you register in code and a key referenced by `dream_api_key_env`. This release ships the adapter contract only, no network provider.

## Windows helpers (optional)

`scripts/install-memory-ui-task.ps1` registers a per-user logon task that starts the UI hidden; `scripts/install-nightly-dream-task.ps1` registers a daily Dream preparation task; `scripts/start-common-ai-memory.ps1` starts the UI if needed and prints status. None of them needs administrator rights, and the core package does not depend on them.
