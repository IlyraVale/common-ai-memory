# Troubleshooting

- If MCP import fails, install the declared MCP 2.x dependency in the active virtual environment.
- If automatic Bridge mode is rejected, configure both GPT and Claude adapters.
- If browser delivery remains pending, verify the popup Bridge URL, explicit tab binding, content-script readiness, lease timing, and Bridge status.
- A successful browser result still requires lounge_wake_ack after the AI reads Lounge state.
- If a memory or game operation reports busy, another process owns the file lock; retry after the bounded lock period.
- If image animation previews fail, install FFmpeg or avoid animated attachments.
- Change ports in .env; every documented port is consumed by its service.
