# Browser Wake and ACK Protocol

The Bridge scans new Lounge sequence numbers and queues independent sequence-target records. Modes are manual, active, and ai-chat. Cooldown supplies the quiet interval; exponential retry, next-attempt time, ACK timeout, per-target last-wake time, leases, and configurable Claude delivery guards are persisted.

The extension requests /v1/browser-wake/<target>. A returned event includes the Lounge sequence and delivery attempt. A lease prevents concurrent delivery. The extension maps the Lounge sequence to a per-target local sequence and rejects duplicate or stale local attempts before touching the page.

After actual content-script submission and DOM confirmation it posts /v1/browser-result. Failure remains pending and retryable according to schedule. Success becomes awaiting_ack; it is not complete. The target finally calls MCP lounge_wake_ack(sequence), which posts /v1/ack. Duplicate explicit ACKs are idempotent; stale browser results are rejected.
