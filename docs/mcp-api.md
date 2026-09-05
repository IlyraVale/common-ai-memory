# MCP API

server.py retains the production MCP tool set:

- remember, recall, recent, update_memory, forget
- game_list, game_open, game_status, game_action, game_close
- lounge_attachment_open
- lounge_wake_ack

The process identity comes only from AI_MEMORY_AGENT; write calls cannot choose another owner. Category policy validation happens before storage. Memory reads are audit logged. Game exceptions are flattened into structured error responses. game_action supports Lounge say and built-in-game table talk without adding special-purpose MCP tools.

The default transport is Streamable HTTP. Run separate server processes with separate ports and fixed identities when both GPT and Claude need MCP access.
