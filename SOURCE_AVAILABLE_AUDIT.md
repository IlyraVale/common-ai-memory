# Source-Available Publication Audit

Audit date: 2026-09-05

## Scope and method

The scope is only this new common-ai-memory directory. The private production directory was read-only. Its Git history, environment, runtime state, and data were not copied. No commit, push, repository publication, or upload was performed during the extraction audit.

The repository was built from an explicit production-code allowlist, then scanned across Python, JavaScript, MJS tests, JSON, configuration examples, PowerShell, HTML, and Markdown. scripts/audit.py reports only category, file, and line so a scan cannot echo a value.

## Production extraction

The detailed source-to-destination map and per-file transformations are in docs/provenance.md.

The following production files are normalized-text identical to their sources (line-ending and BOM differences only):

- memory_store.py
- memory_audit.py
- minigames.py
- minigames_gomoku_core.py
- lounge_attachments.py

The production logic of memory_house.py, category_policy.py, memory_atrium.py, game_hall.py, minigames_viewer.py, lounge_room.py, lounge_viewer.py, lounge_bridge.py, server.py, and the Lounge Bridge browser extension was retained with targeted privacy, configuration, or integration-boundary edits documented in the provenance map.

## Removed or excluded private material

Excluded in full:

- real memory Markdown and private category/manual content
- AI Lounge messages and read state
- game matches and bridge delivery state
- logs, activity streams, attachments, screenshots, and image fixtures
- environment files, authentication material, mail logins, browser state, browser profiles, machine identifiers, and private network configuration
- backups, renovation work, patch installers, process files, binaries, caches, virtual environments, and deployment copies
- the source .git directory and all old history
- all copied third-party project source, assets, databases, and resources

No source runtime directory was copied. New runtime state is created only under DATA_DIR and is ignored.

## Desensitization and generalization

- The private human identity and display nicknames were replaced by configured identities; public defaults are Alice, GPT, and Claude.
- Private project, relationship, and game categories were replaced with generic categories.
- Hard-coded bind addresses, data roots, service links, and Bridge ports were converted to used environment settings.
- The extension Bridge URL moved from a fixed value to extension-local configuration restricted to localhost.
- Private external game transport, endpoint, and credential loading were removed. The Common AI Memory adapter protocol remains.
- Private-game MCP documentation was removed while generic adapter routing remains.

## Public modules

Suitable for public source distribution under the repository license:

- production Markdown Shared Memory and cross-process locking
- Memory Atrium UI and APIs
- Game Hall and four built-in production games
- spectator redaction and match viewer
- AI Lounge room, shared state, read/unread cursors, table talk, Viewer, and attachments
- Lounge Bridge delivery state machine and HTTP protocol
- working GPT/Claude browser delivery extension
- production MCP tool surface
- launch, health checks, schemas, synthetic examples, and tests

## Production functions not migrated

- server-claude-public.py: excluded because it combines the common MCP surface with instance authentication persistence, platform-protected credential handling, and private deployment defaults. The common tool implementation is already retained in server.py.
- Private external-game transport in game_hall.py: excluded because it depends on a private service, private endpoint, private tool vocabulary, and protected credentials.
- Tunnel and private-network launch scripts: excluded because they are deployment-instance material.
- Existing memory-house manuals and category index: excluded because they contain private taxonomy and memory context.
- Production attachment fixtures: excluded because they are real media; the attachment code and generated test fixtures remain.
- Browser login state and profiles: excluded by design; the extension binds an already open tab without exporting that state.

## Functional verification

- Python: 22 tests passed.
- Browser extension: 18 tests passed.
- Concurrent MemoryStore acceptance: two independent instances wrote 100 records each; 200 remained.
- Coverage includes owner isolation, scopes, game turns, Lounge say/read/unread, wake sequence, browser success plus ACK, failed delivery retry, lease expiry, duplicate result rejection, independent GPT/Claude state, .env loading, Atrium HTTP, MCP tool surface, attachment formats, production Lounge behavior, and production Bridge modes.
- All production-derived Python modules compile.

## Secret scan classification

No private key block, common provider key signature, high-entropy credential assignment, real email address, numeric loopback address, Windows absolute path, machine identifier value, private endpoint, or real authentication value was found.

The final conservative lexical scan produced 28 review findings: ten safe standard-library random-ID references in production-derived code, five security/exclusion references in this report, eight ordinary source or documentation warnings, and five self-matches in the scanner definitions. None contained a secret value.

Lexical findings remain expected in three classes:

- safe Python uses of the standard-library secrets module for random IDs and temporary filenames;
- protocol, exclusion, and security documentation that necessarily names sensitive concepts;
- the scanner's own pattern definitions.

Third-party/private project names occur only in exclusion documentation and this audit report. Every such occurrence describes code that was not migrated.

## Publication notes

- Copyright holder and public license notice are now set in LICENSE.
- The public repository uses PolyForm Noncommercial License 1.0.0 with a Required Notice identifying the original project as Glassbuckle / Common AI Memory.
- Review third-party Python and browser-platform dependency licenses separately.
- Review browser store policies and live-site selectors immediately before distribution.
- Verify whether optional external systems have public official documentation before adding links.
- Exercise the browser extension manually against the then-current GPT and Claude web UIs; selectors can change without notice.

## Potential secrets found

None. Automated lexical findings are intentionally treated as review items rather than silently allowlisted; rerun python scripts/audit.py and inspect every reported location before publication.
