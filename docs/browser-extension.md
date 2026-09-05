# Browser Extension

browser-extension/ is extracted from the working production Lounge Bridge extension. It retains explicit GPT/Claude target binding, origin validation, persisted per-target local sequences, delivery planning, duplicate rejection, content-script recovery, busy detection, composer discovery, real text entry, send-button activation, submission confirmation, assistant-turn-start confirmation, result reporting, and lease-based recovery if reporting fails.

The Bridge URL is stored in extension-local settings and edited from the popup. The public build permits only an HTTP service on localhost; it does not inspect browser authentication state or export conversations. A claimed delivery is never silently treated as successful: every attempted outcome is reported, and a lost result expires back through the Bridge lease.
