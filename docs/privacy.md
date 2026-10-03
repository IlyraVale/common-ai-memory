# Privacy and security model

## What stays where

- All data stays in `DATA_DIR` on your machine. There is no telemetry, no hosted service and no cloud embedding call. The optional embedding model is downloaded once and runs locally.
- Logs, receipts, ledgers and scheduler records contain metadata only (identities, ids, operation names, timestamps, error classes), never memory text, prompts, query bodies, claim tokens or credentials.
- `.env`, `owner-config.json`, API keys and everything under `DATA_DIR` are private and git-ignored. Do not commit or publish them. Reference API keys by environment-variable name only.

## Network exposure

- MCP server, UI and bridge bind to localhost by default (`CAM_BIND_HOST`). The UI refuses to bind to non-local addresses.
- The UI checks the `Host` header (DNS-rebinding protection), refuses cross-site requests, requires a custom header plus a per-process token for admin calls, sends a strict Content-Security-Policy, `X-Frame-Options`, `nosniff` and `Cache-Control: no-store`, and never puts its token in a URL, local storage, a log or the page source.
- The lounge only accepts posts from same-origin pages.
- The MCP server has no authentication of its own. To reach it from a remote client, place an authenticated HTTPS gateway in front of it that you operate and maintain. Nothing in this repository configures remote access.

## Identities and ownership

Each MCP process has one fixed identity. Every identity can read; only the owner can change or forget a memory. The UI's Manage page acts for an owner explicitly and applies the same rules.

## Publishing your own fork

The example identities (GPT, Claude, Alice) are synthetic. Before publishing a fork, run `python scripts/audit.py`, run a secret scanner over the exact files you stage, and check that no runtime data, `.env`, `owner-config.json` or scheduler export is included.
