# Upgrading, backup and recovery

Your data lives in `DATA_DIR` (default `./runtime`), never inside the installed package. Upgrading the code does not touch it, and nothing in an upgrade deletes data by default.

## What is where

| Path under `DATA_DIR` | What it is | If lost |
|---|---|---|
| `memory/` | One Markdown file per memory: **the source of truth** | Memories are gone; restore from backup |
| `dreams/` | Committed Dreams (Markdown) | Those Dreams are gone |
| `archive/` | Optional imported conversation archive | Archive is gone |
| `owner-config.json` | Your per-owner Dream/recall options | Defaults apply until you restore it |
| `state/memory-search.sqlite3` | FTS search index | Rebuild: `common-ai-memory-search rebuild` (recall falls back to lexical meanwhile) |
| `state/memory-vectors.sqlite3`, `state/models/` | Optional vector index and model cache | Rebuild: `common-ai-memory-vectors rebuild` |
| `state/execution-receipts.sqlite3` | Receipts referenced by `evidence_refs` | Those references show as missing |
| `state/memory-witness.sqlite3`, `state/memory-feedback.sqlite3` | Exposure, witness and retrieval ledgers | History restarts from empty |
| `state/dream-runtime.sqlite3`, `state/dream-prepared/` | Dream claims, commit records, prepared packets | A pending Dream is prepared again on the next `wake` |
| `.lounge/`, `.games/`, `.lounge-bridge/` | Optional lounge messages, game state, bridge delivery state | Optional features start fresh |

Logs go to `DATA_DIR/logs/` and `state/dream-scheduler/` and contain metadata only.

## Backup

1. Stop the MCP server(s) and the UI (or at least make sure nobody is writing).
2. Copy the whole `DATA_DIR`, plus your `.env`.
3. Keep at least one copy off the machine. The memory folder is plain Markdown, so you can also keep it in a private Git repository: if `DATA_DIR` is a Git worktree, each change is committed locally (never pushed).

SQLite files are safe to copy while the services are stopped. Copy the `-wal`/`-shm` files with them if present.

## Recovery

- Restore `memory/` (and `dreams/`, `archive/` if you use them). That is enough to get every memory back.
- Delete any derived index you do not trust and rebuild it; `common-ai-memory-doctor` shows which ones are stale.
- Restoring `state/` keeps receipts, ledgers and Dream records consistent with the restored memories. A `state/` from a different point in time is harmless for memories, but some evidence references may then show as missing.

## Upgrading

1. **Back up first** (above).
2. Update the code: `git pull` in your checkout, or install the new wheel into the same virtual environment (`pip install --upgrade <wheel or path>`).
3. Keep your `.env`, `owner-config.json` and `DATA_DIR`. Upgrades never replace them; compare with the new `.env.example` / `owner-config.example.json` for new optional keys.
4. Start the services. SQLite stores migrate themselves when opened: changes are additive (new tables or columns, existing rows kept). A store written by a **newer** version refuses to open instead of guessing.
5. Run `common-ai-memory-doctor`. If it reports an index as stale or from another model, rebuild it.

Markdown memory files are read as they are. Older records without newer fields (`lifecycle`, `verification`, …) keep working with neutral defaults and are not rewritten. Dream history, commit records and prepared packets carry over; a Dream pending at upgrade time is claimed normally on the next `wake`.

### From 0.1.0 to 0.2.0

- No memory migration is needed.
- New derived files appear under `state/` on first use. They are ignored by Git.
- The UI is now `memory_ui.py` on `MEMORY_UI_PORT` (default 8877) and includes the game hall and lounge. `launcher.py` no longer starts the standalone game and lounge viewers; they still run on their own if you want them.
- Command-line tools now default to `DATA_DIR` (like the MCP server) instead of the folder they are installed in. Pass `--root` if you kept data somewhere else.
- The nightly Dream scripts now default to `DATA_DIR` as well; pass `-ProjectRoot` / `--project-root` to keep an explicit location.

## Downgrading

Not supported automatically. Restore the backup you made before upgrading.
