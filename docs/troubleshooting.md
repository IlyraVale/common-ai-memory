# Troubleshooting

- If MCP import fails, install the declared MCP 2.x dependency in the active virtual environment.
- `AI_MEMORY_AGENT is required`: set it in `.env` or the environment; every MCP process needs a fixed identity.
- A command-line tool shows no memories: it reads `DATA_DIR` (default `./runtime`, relative to the folder you run it in). Pass `--root` to point elsewhere.
- Semantic results missing: install the `semantic` extra and run `common-ai-memory-vectors rebuild`; until then recall is lexical by design.
- If automatic Bridge mode is rejected, configure both GPT and Claude adapters.
- If browser delivery remains pending, verify the popup Bridge URL, explicit tab binding, content-script readiness, lease timing, and Bridge status.
- A successful browser result still requires lounge_wake_ack after the AI reads Lounge state.
- If a memory or game operation reports busy, another process owns the file lock; retry after the bounded lock period.
- If image animation previews fail, install FFmpeg or avoid animated attachments.
- Change ports in .env; every documented port is consumed by its service.
- `python memory_services.py status` shows what is running; `GET /health` on the Memory UI port reports which pages are available.
