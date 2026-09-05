"""Start the production-derived Common AI Memory services together."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from config import load_dotenv


def main() -> None:
    load_dotenv()
    root = Path(__file__).resolve().parent
    programs = ["memory_atrium.py", "minigames_viewer.py", "lounge_viewer.py", "lounge_bridge.py", "server.py"]
    children = [subprocess.Popen([sys.executable, str(root / program)], cwd=root, env=os.environ.copy()) for program in programs]
    try:
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
        failed = next((child for child in children if child.poll() is not None), None)
        raise SystemExit(failed.returncode if failed else 0)
    except KeyboardInterrupt:
        pass
    finally:
        for child in children:
            if child.poll() is None: child.terminate()
        for child in children:
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill()


if __name__ == "__main__":
    main()

