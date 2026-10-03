import os
import urllib.request

from config import load_dotenv

load_dotenv()

ui_port = int(os.getenv("MEMORY_UI_PORT", os.getenv("MEMORY_ATRIUM_PORT", "8877")))
checks = {
    "memory-ui": (ui_port, "/health"),
    "game-hall": (ui_port, "/api/games/matches"),
    "ai-lounge": (ui_port, "/api/lounge/state"),
    "lounge-bridge": (int(os.getenv("LOUNGE_BRIDGE_PORT", "8879")), "/v1/status"),
}
for name, (port, path) in checks.items():
    with urllib.request.urlopen(f"http://localhost:{port}{path}", timeout=3) as response:
        if response.status != 200: raise SystemExit(f"{name} is unhealthy")
print("Common AI Memory is healthy")
