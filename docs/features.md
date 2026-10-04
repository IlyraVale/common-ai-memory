# Feature maturity and limitations

This page says plainly what is dependable, what is optional, and where the edges are.

## Stable

Covered by the main test suite and safe to rely on with the default configuration.

- **Core memory CRUD**: `remember`, `recall`, `recent`, `update_memory`, `forget`; Markdown files as the source of truth; atomic writes and a cross-process lock; owner isolation.
- **Full-text search**: SQLite FTS5/BM25 index with automatic fallback to the Markdown lexical engine when the index is missing, dirty or corrupt.
- **Lifecycle**: `active`, `review_needed`, `stale`, `superseded`, with explicit same-owner supersession.
- **Verification**: an explicit field that defaults to `unknown` and changes only when someone sets it.
- **Execution receipts**: append-only, metadata-only records of operations; usable as `evidence_refs`.
- **Provenance**: `memory_provenance` and `common-ai-memory-provenance` report the metadata trail of one memory.
- **Memory UI manager**: Atrium, Manage and Duplicate check on one localhost page, with owner checks, stale-edit protection and same-origin protection.

## Optional / advanced

Work well, but need extra installation, configuration or an integration on the host side.

- **Embeddings / Hybrid retrieval**: `pip install -e ".[semantic]"` and a vector rebuild. Adds paraphrase matching; combined with BM25 by reciprocal-rank fusion.
- **Passive Recall**: `recall(passive=true)` for short contextual snippets, only when the host or model calls it.
- **Dream CLI / API modes**: nightly generation with a local CLI you are logged into, or an API adapter you provide.
- **Games and Lounge**: built-in games, a shared chat room, the lounge bridge and the browser extension.
- **Windows helper scripts**: logon task for the UI, nightly Dream task, start script.

## Limitations

- **Passive Recall needs the host or model to call it.** MCP has no per-turn hook and the server never sees the conversation, so it cannot recall "by itself".
- **CLI Dreams depend on the local CLI's login.** If the CLI is missing, logged out or out of quota, no Dream is invented; the owner falls back to `on_wake`.
- **The API Dream mode is a contract only.** No network provider is bundled.
- **The Windows autostart helper is optional.** On other systems, use your own service manager to run `memory_ui.py --supervise-bridge` and `server.py`.
- **Semantic features degrade without embeddings.** Recall and the duplicate check fall back to lexical matching and report semantic analysis as unavailable.
- **The duplicate scanner assists; it does not decide.** It suggests groups and reasons. It never merges, deletes or marks anything superseded, and it cannot tell which of two conflicting memories is true.
- **Verification is not fact checking.** Nothing browses the web or compares memories to the world.
- **Browser wake delivery is best-effort.** The Claude page selectors need live verification after site changes. A wake is only complete once the model sends `lounge_wake_ack`.
- **Remote access is not built in.** Exposing the MCP server or UI beyond localhost needs your own authenticated HTTPS gateway.
- **Top-level module names.** The package installs modules such as `server` and `config` at top level. Use a dedicated virtual environment.
- **Python 3.10 only.** This release supports and was tested on Python 3.10 (Windows, MCP SDK 2.1.1, 2.2.0 and 2.3.0); the package refuses to install on other Python versions. The core has no Windows-only dependency, but other operating systems were not part of this release's test run.
- **Why not Python 3.12 or newer yet.** From Python 3.12, SQLite connections that are not closed explicitly are released only by the garbage collector. Several stores rely on implicit release, so on Windows a database file can stay locked a little longer than expected. Everyday use keeps working, but rebuilding an index while another handle is still open can fail and must be retried, and the test suite reports teardown errors. Support for newer Python versions remains future work after that fix.
