# Shared Memory

memory_store.py is the Markdown store. A record contains ID, owner, scope (agent or shared), category, timestamps, and content. All configured AI identities may read records; only the owner may update or forget them. owner=shared filters by scope rather than author.

Writes use a process-exclusive lock file and atomic replacement. Different MemoryStore instances and processes therefore serialize mutations. If DATA_DIR itself is a Git worktree, a mutation attempts a local commit; failure does not discard the memory write and nothing is pushed.

Categories are validated as section/subject. The default fallback policy contains generic relationship, project, game, life, preference, learning, and plan categories. No production memory or private category index is included.

## Orthogonal metadata

- `status` (`open` / `done`) describes task or commitment completion.
- `source` (`user_statement` / `observed` / `inferred`) describes origin, not truth.
- `lifecycle` (`active` / `review_needed` / `stale` / `superseded`) describes current validity.
- `verification` (`unknown` / `unverified` / `confirmed` / `partial` / `not_applicable`) records an explicit verification state. Legacy records without it are `unknown`.

These dimensions are orthogonal: `status` is task completion, `source` is origin, `lifecycle` is current applicability, and `verification` is explicit verification state. Verification is not an automatic truth score. The system does not fact-check, browse the web, promote memories to confirmed, or downgrade conflicting memories automatically.

`evidence_refs` is an optional, ordered, deduplicated list of canonical pointers. Supported schemes are `receipt:<receipt_id>` and `archive:<archive_id>`. Archive evidence requires an explicitly owned archive; legacy unowned archives remain readable for compatibility but cannot be evidence targets. The pointers contain no evidence body and never promote `verification` automatically.

Execution receipts are immutable, append-only, metadata-only event records in the gitignored `state/execution-receipts.sqlite3`. Target linkage is stored separately and may be privacy-redacted by `forget` without modifying the event core. A redacted receipt remains a stable evidence identity but no longer exposes the forgotten target. Receipts never store memory bodies, raw tool arguments/results, prompts, secrets, or content hashes.

Legacy records without `lifecycle` are treated as `active` without rewriting them. Normal recall and wake include `active` and `review_needed`; the latter is explicitly labelled and loses only a close relevance tie. `stale` and `superseded` are excluded unless recall/recent is called with `include_inactive=true`. Supersession is explicit, same-owner, acyclic, and never merges content. The system does not infer lifecycle from age or ask an LLM to guess conflicts.
