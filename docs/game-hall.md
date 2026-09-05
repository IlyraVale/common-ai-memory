# Game Hall

The MCP surface is game_list, game_open, game_status, game_action, and game_close. game_action keeps its machine command separate from optional table_talk before routing to the production parser.

The retained built-ins are Gomoku, Battleship, Blackjack, and heads-up Holdem. Their production mechanisms include persistent match IDs, per-match locks, player validation, turn enforcement, hidden-information projections, actions, resigning, chat, waits, active-match discovery, finish archival, and spectator-safe rendering.

Private games and copied external projects are absent. external_games.py keeps only the Common AI Memory adapter boundary; operators inject separately installed adapters without placing external source or credentials here.
