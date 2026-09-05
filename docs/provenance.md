# Production Extraction Map

All source paths below are relative to the private production root. No source Git history was copied.

| Production file | Public source file | Transformation |
|---|---|---|
| memory_store.py | memory_store.py | Verbatim functional implementation; no runtime records copied. |
| memory_house.py | memory_house.py | Core room/category functions retained; private legacy/category metadata replaced with generic entries. |
| category_policy.py | category_policy.py | Validator retained; private category allowlist replaced. |
| memory_audit.py | memory_audit.py | Production audit implementation retained; no audit events copied. |
| memory_atrium.py | memory_atrium.py | Full UI/API retained; private category descriptions removed and links, host, port, and data root configured. |
| game_hall.py | game_hall.py | Built-in/Lounge routing and complete production minigame command dispatcher retained; private external transport moved behind ExternalGameAdapter. |
| minigames.py | minigames.py | Production Battleship, Blackjack, and Holdem implementation retained. |
| minigames_gomoku_core.py | minigames_gomoku_core.py | Production Gomoku implementation retained. |
| minigames_viewer.py | minigames_viewer.py | Production spectator UI and redaction retained; host, port, and data root configured. |
| lounge_room.py | lounge_room.py | Production sequence, read, unread, and message implementation retained; private human identity replaced by configured identities. |
| lounge_attachments.py | lounge_attachments.py | Production validation, conversion, and MCP image implementation retained; no attachments copied. |
| lounge_viewer.py | lounge_viewer.py | Full production UI/API retained; private nicknames removed and identity, URLs, ports configured. |
| lounge_bridge.py | lounge_bridge.py | Production delivery state machine retained; human identity, root, host, port configured. |
| server.py | server.py | Production MCP tools retained; paths, ports, Bridge URL configured and private-game help removed. |
| .lounge-bridge/edge-extension files | browser-extension files | Working extension and unit tests retained; private provenance and fixed Bridge address removed; popup configuration added. |
| generic production test files | tests/*_production.py | Production tests retained; private identity fixtures replaced and current MCP field names corrected. |

This repository is publicly readable but distributed under the PolyForm Noncommercial License 1.0.0 rather than an OSI open-source license. See ../LICENSE for the controlling license notice.
