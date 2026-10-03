#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
# Runs from the checkout so .env and DATA_DIR (default ./runtime) resolve like the MCP server.
cd "$ROOT"
exec "$ROOT/.venv/bin/python" "$ROOT/dream_nightly.py" "$@"
