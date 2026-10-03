# Dreams

Dreams are derived, non-authoritative (`factual_authority=false`) reflections assembled from memory material. They are never written back into ordinary memory, do not create open items, and normal `recall` does not search them.

## Modes

| Mode | Requirements | Night scheduler | Generation | Best for |
|---|---|---|---|---|
| `on_wake` | None | Optional | Current chat model after `wake` | Ordinary MCP users |
| `cli` | A configured non-interactive local CLI | Yes | Local CLI | Desktop automation |
| `api` | A separately installed/registered adapter and environment credential | Yes | Provider API | Custom integrations |

The default is `on_wake`, including when `owner-config.json` is absent or an owner is not listed. A scheduler is never required for this mode.

### Preferred local CLI with on_wake fallback

An `on_wake` owner may additionally set `dream_preferred_runner: "cli"`. The nightly runner then acts as a wake on that owner's behalf: it claims through the normal on_wake protocol (same grace rule, packet reuse, lease and `dream_commit` idempotency), gives a fixed local CLI only the prepared materials and the standard Dream instruction, and commits the body with the claim token. Each owner maps to exactly one CLI profile; content never selects the runner.

| Key | Values |
|---|---|
| `dream_preferred_runner` | `"cli"` or absent. Requires `dream_mode: "on_wake"`. |
| `dream_cli_profile` | `claude_code` (`claude -p --output-format json`, no tools, no MCP servers, no setting sources, no session persistence) or `codex` (`codex exec --ephemeral --ignore-user-config --ignore-rules --sandbox read-only`, final message read from `-o <file>`). |
| `dream_cli_executable` | An absolute path, a name on `PATH`, or a glob (`~` and environment variables expanded; the newest match wins), so versioned install folders keep working. |
| `dream_timeout_seconds` | Defaults to 300; at most 480 so a generation can never outlive its 10-minute claim. |

Any CLI problem (executable missing, launch failure, auth expired, quota exhausted, nonzero exit, timeout, empty/malformed/oversized output) is a fallback, never a fake dream: nothing is committed, the prepared packet is kept, the child process tree is terminated on timeout, and the claim lease expires so a later `wake` can claim the same date. The CLI is never replaced by an API. Logs record only owner, date, runner, outcome, error class and duration. Neither CLI is required unless an owner opts in.

The `api` mode currently provides the configuration and adapter contract only. This distribution does not include a built-in network provider. Configuring `api` without registering an adapter fails with an explicit error; credentials are referenced by environment-variable name and are never stored in Dream packets or logs.

## Ephemeral scraps

Dream preparation can include at most one short-lived scrap. Scraps are derived, ephemeral, non-authoritative fragments kept outside ordinary memory, with a default TTL of 72 hours (configurable from 24 to 168 hours). They never appear in normal recall, wake memory, archive, or evolve, and they do not count as independent evidence. Explicitly deleted, forgotten, corrected, stale, superseded, or secret-like material is rejected rather than retained as a scrap.

A selected scrap is labelled `material_source=ephemeral_scrap` and `factual_authority=false`. It may supply a small association, tone, or image, but the Dream instruction forbids treating it as a fact. Selection is deterministic for an owner and Dream date, and a prepared packet is reused without re-sampling.

## Independent witness

The runtime exposure ledger prevents a recalled memory from strengthening itself through repetition. It records only owner, memory ID, episode ID, context kind, and timestamps; it does not duplicate memory text.

- Episode A: memory M is returned by recall and is then repeated. The witness is not independent because M was exposed in that episode.
- Episode B: M was not injected into the episode and the same fact is naturally confirmed again. That evidence may be recorded as an independent witness.

Wake memory, `memory_get`, active searches, recall results, and Dream materials all count as exposure. Dream generation and `dream_commit` never create witnesses. The helper records provenance for later evolve/supersession work; it does not automatically change memory content or importance.

## Prepared packets

Night preparation is not dream generation. It writes one atomic packet to `state/dream-prepared/<owner>/<date>.json`. The packet contains selected materials and a stable `generation_id`, but no claim token or credential. The first `wake` leases that packet; if there is no packet, `wake` prepares one immediately. A successful commit deletes the packet while retaining the dated dream and runtime commit authority.

Only the latest complete day is normally selected. The one-day grace rule can recover exactly the preceding day when an expired historical lease or a valid prepared packet proves it was pending. Older packets are never replayed as a backlog.

## Quickstart

With no configuration, call `wake`, write a body using only `pending_dream.materials`, and submit it with `dream_commit`. To prepare in advance:

```sh
python dream_nightly.py --owner my-agent --timezone Asia/Shanghai
```

Copy `owner-config.example.json` to `owner-config.json` only when explicit per-owner modes are useful. CLI commands must be JSON arrays and run with `shell=False` in a neutral temporary directory. API credentials must be referenced by environment-variable name, never stored in JSON.

## Scheduling

Scheduling is optional. Run after the configured timezone rolls over to the next date.

- Windows: use `scripts/install-nightly-dream-task.ps1`; it installs a daily, non-overlapping, StartWhenAvailable task for the current interactive user.
- Linux cron: `30 2 * * * /path/to/project/scripts/nightly-dreams.sh --owner my-agent --timezone Asia/Shanghai`
- systemd: create a oneshot service invoking the same shell wrapper and a daily timer with `Persistent=true`.
- macOS: use cron with the wrapper above, or a LaunchAgent whose `ProgramArguments` call `.venv/bin/python`, `dream_nightly.py`, and explicit owner arguments.

The Python runner computes dates in the configured Dream timezone; scheduler host timezone does not change Dream date semantics.

Use one daily task for all owners. Runs are serialized by a lock, so a repeated or overlapping trigger is a no-op. `--dry-run` reports owners, dates, packets, commits, leases, the selected runner and CLI availability without preparing, claiming, committing or logging. Exit codes: `0` all owners done or already done, `4` some preferred-CLI owners fell back to on_wake, `5` every preferred-CLI owner fell back, `2` infrastructure failure, `3` another run holds the lock. A fallback is not data damage.

## Privacy and troubleshooting

Prepared packets contain memory excerpts and therefore belong under private runtime state. `state/dream-prepared/`, scheduler logs, SQLite/WAL files, dreams, OAuth state, and local owner configuration must not be published. Scheduler logs contain metadata only, never materials, prompts, claim tokens, or credentials.

If one CLI or API owner fails, other owners continue. The prepared packet remains available for a later retry. Missing CLI executables affect only owners configured for `cli`. Without any scheduler, `on_wake` continues to work through its fallback preparation path.

Migration from Dream v1 requires no memory migration. Existing dated/current dreams and runtime commit records remain authoritative.
