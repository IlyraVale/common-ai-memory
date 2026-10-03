# Retrieval Doctor and source-aware ranking

Run the read-only lexical integrity check from the project root:

```powershell
python memory_doctor.py
python memory_doctor.py --json
```

The doctor checks ordinary Markdown memory cards, frontmatter integrity, duplicate IDs, category/status/source validity, owner filters, duplicate recall results, and bounded lexical searchability. It reports both disposable indexes separately: FTS schema/parity/hash/integrity/BM25 smoke, and optional vector model fingerprint/dimension/parity/hash/vector validity/integrity/semantic smoke. House manuals are checked only as manuals and are not counted as ordinary memory cards.

Default doctor runs are read-only. A stale or missing search index does not mean the Markdown memory is damaged; recall automatically falls back to the original lexical engine. Rebuild the derived index explicitly with either:

```powershell
python memory_search.py rebuild
python memory_doctor.py --rebuild-search-index
```

Markdown remains the sole source of truth. `state/memory-search.sqlite3` is derived, rebuildable, disposable, and gitignored. Recall uses FTS5 `trigram` tokenization and SQLite BM25 for safe queries of at least three characters. Empty, one-character, emoji-only, syntactically unsafe, missing-index, dirty-index, or corrupt-index cases use the original whole-phrase/category/Chinese-overlap/word-token engine.

Base recall is lexical. Optional local semantic retrieval uses FastEmbed with `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` and stores rebuildable vectors in `state/memory-vectors.sqlite3`. Install it with `pip install -e ".[semantic]"`, then run `python memory_vectors.py rebuild`. Hybrid ranking uses reciprocal-rank fusion rather than adding incomparable BM25 and cosine scores. Exact lexical content matches are protected. If the optional model/index is unavailable, dirty, mismatched, or corrupt, recall automatically uses BM25/lexical fallback.

`source` records provenance, not truth. `user_statement`, `observed`, and legacy records without source are neutral peers. `inferred` receives only an exact lexical tie-break penalty. A more relevant inferred memory still ranks above a less relevant non-inferred memory.

## Passive Recall

Normal Recall is explicit broad retrieval. Passive Recall is conservative contextual surfacing through `recall(query=..., passive=true)`. It reuses lifecycle filtering and the existing BM25/semantic Hybrid order, then applies a stricter confidence gate, a maximum of three results, 360 characters per snippet, and a 1000-character total budget. Zero results is expected for weak input.

The standard MCP deployment is model-initiated because it has no reliable every-turn message hook or conversation ID. Automatic invocation therefore depends on the host/model integration. No owner-wide cooldown is used. A returned memory is recorded as `passive_recall` exposure, but passive recall never creates a witness, receipt, verification change, lifecycle change, or memory.
