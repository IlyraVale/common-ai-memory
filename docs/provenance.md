# Memory provenance

`memory_provenance(memory_id, limit=20)` answers "what do we know about where this memory came from and how it has been used?" without returning any memory, Dream, scrap or query text.

It is read-only, scoped to the calling identity, and does not itself count as an exposure.

## What it reports

- **evidence_refs**: each referenced receipt or archive entry and whether it resolves.
- **Execution receipts**: operations recorded for this memory (owner, operation, outcome, timestamps, changed field names). Receipts whose target was redacted by `forget` stay as evidence identities but no longer reveal the target.
- **Exposure, retrieval and witness ledgers** for the calling identity: when the memory was shown (recall, recent, wake, Dream material), and whether later evidence was independent of those exposures.
- **Dream linkage**: whether committed Dreams or Dream scraps used this memory as material.
- **Audit counts**: how often it was read, by tool.

Each section has a `status`. `unavailable` means that store does not exist yet or cannot be read. It is different from an empty list, which means the store exists and has nothing for this memory. `limit` bounds every list (1 to 50).

## From the command line

```sh
common-ai-memory-provenance <memory_id> --owner gpt
common-ai-memory-receipt <receipt_id> --owner gpt
```

Both are local, read-only administration commands and default to `DATA_DIR`.

## What it is not

Provenance shows that operations happened and how a memory was exposed. It does not prove a memory is true, and it never changes verification, lifecycle or ranking.
