# Quickstart

From nothing to a remembered and recalled memory in about ten minutes. You need Python 3.10 (this release supports 3.10 only). You do **not** need Claude Code, Codex, FastEmbed, a tunnel or any editor extension.

The steps: install → initialize → start MCP → connect a client → remember → recall → open the UI.

## 1. Install

Windows (PowerShell):

```powershell
git clone https://github.com/IlyraVale/common-ai-memory.git
cd common-ai-memory
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

macOS / Linux / any shell:

```sh
git clone https://github.com/IlyraVale/common-ai-memory.git
cd common-ai-memory
python3.10 -m venv .venv
. .venv/bin/activate
pip install -e .
```

## 2. Initialize

Copy the example settings. That is the only initialization step: the data directory (`DATA_DIR`, default `./runtime`) and its subfolders are created automatically on first use.

```powershell
Copy-Item .env.example .env      # Windows
```

```sh
cp .env.example .env             # macOS / Linux
```

`.env` is private and git-ignored. The defaults bind everything to localhost.

## 3. Start the MCP server

```sh
python server.py
```

It prints `Uvicorn running on http://localhost:8765`. The MCP endpoint is `http://localhost:8765/mcp` and the identity is `gpt` (from `AI_MEMORY_AGENT` in `.env`). Leave this terminal open.

## 4. Connect a client

Any MCP client that supports Streamable HTTP can use `http://localhost:8765/mcp`. To check everything without configuring a chat app, use the bundled test client. In a second terminal (activate the same virtual environment first):

```sh
python examples/quickstart_client.py
```

## 5. Remember and 6. recall

The test client calls `remember` with a sample sentence, then `recall("jasmine tea")`, and prints:

```text
tools: dream_commit, dream_get, forget, game_action, ..., recall, recent, remember, update_memory, wake
remembered: 60d46b1f…
recalled: ['60d46b1f…']
```

The memory is now a Markdown file under `runtime/memory/preference/`. In a chat app connected to the same URL you would simply ask the model to remember or recall something; it calls the same tools.

## 7. Open the UI

In another terminal:

```sh
python memory_ui.py
```

Open `http://127.0.0.1:8877/`. The Atrium shows the memory you just saved; Manage lets you edit it.

## Next steps

- Start everything at once instead: `python launcher.py` (MCP server, UI and lounge bridge).
- A second AI identity: `AI_MEMORY_AGENT=claude MEMORY_PORT=8766 python server.py` (PowerShell: `$env:AI_MEMORY_AGENT="claude"; $env:MEMORY_PORT="8766"; python server.py`).
- Better recall for paraphrases: `pip install -e ".[semantic]"`, then `common-ai-memory-vectors rebuild` (downloads a small local model once).
- Settings: [configuration.md](configuration.md). What is stable and what is optional: [features.md](features.md).
- Windows only, optional: `scripts/install-memory-ui-task.ps1` starts the UI at logon.

## If something goes wrong

- `AI_MEMORY_AGENT is required`: you skipped step 2, or started the server outside the project folder. Set it in `.env` or the environment.
- The client cannot connect: is the server terminal still running, and does the port match `MEMORY_PORT`?
- Port already in use: change `MEMORY_PORT` or `MEMORY_UI_PORT` in `.env`.
