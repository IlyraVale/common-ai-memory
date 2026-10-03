# Release checklist

Run from a clean checkout. Every item must be checked before tagging. Commit, push, tag and GitHub Release each need explicit maintainer approval.

## Repository

- [ ] `git status` clean except the intended release changes, all reviewed
- [ ] No private files staged: `.env`, `owner-config.json`, `DATA_DIR` content, SQLite/WAL files, logs, scheduler exports, backups
- [ ] `.gitignore` covers runtime data, derived indexes and local configuration

## Version

- [ ] `config.__version__` set to the release version
- [ ] Package metadata (`pyproject.toml` dynamic version), UI `/health`, browser extension manifest and `CHANGELOG.md` agree (`tests/test_release_metadata.py`)
- [ ] Changelog entry dated

## Tests

- [ ] Full Python suite passes on each supported Python and MCP SDK version (source tree and installed wheel)
- [ ] Browser extension tests pass (`node --test browser-extension/tests/*.test.mjs`)
- [ ] Expected skips only (optional semantic model, platform-specific helpers)

## Packages

- [ ] sdist and wheel built from a clean tree
- [ ] Package contents audited: no runtime data, databases, logs, caches, private config
- [ ] Clean install in a new virtual environment: imports, entry points, MCP startup, UI startup
- [ ] Without FastEmbed: lexical recall and exact/lexical duplicate scan work; semantic reported unavailable
- [ ] With the `semantic` extra: vector rebuild, Hybrid recall and semantic duplicate grouping work

## Privacy

- [ ] Path, username, hostname and secret scan over the source tree, tracked files, wheel and sdist; every finding reviewed
- [ ] `python scripts/audit.py` reviewed

## Documentation

- [ ] README, quickstart, configuration, feature maturity, upgrading docs current
- [ ] License verified (PolyForm Noncommercial 1.0.0, Required Notice kept); third-party notices current

## Safety

- [ ] No production data was modified while preparing the release

## Publication (each step pending approval)

- [ ] Commit
- [ ] Push
- [ ] Tag `v<version>`
- [ ] GitHub Release with the changelog entry and the built sdist and wheel
