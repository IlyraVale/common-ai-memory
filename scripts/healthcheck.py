import os
import urllib.request

from config import load_dotenv

load_dotenv()

checks = {
    "memory-atrium": (int(os.getenv("MEMORY_ATRIUM_PORT", "8877")), "/api/home"),
    "game-hall": (int(os.getenv("GAME_HALL_PORT", "8876")), "/api/matches"),
    "ai-lounge": (int(os.getenv("LOUNGE_PORT", "8878")), "/api/state"),
    "lounge-bridge": (int(os.getenv("LOUNGE_BRIDGE_PORT", "8879")), "/v1/status"),
}
for name, (port, path) in checks.items():
    with urllib.request.urlopen(f"http://localhost:{port}{path}", timeout=3) as response:
        if response.status != 200: raise SystemExit(f"{name} is unhealthy")
print("Common AI Memory is healthy")
