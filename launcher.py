"""Start the Common AI Memory services together.

The unified Memory UI serves the Atrium, manager, duplicate check, game hall and
lounge on one port. The lounge bridge worker and the MCP server run beside it.
The standalone game hall and lounge viewers are still runnable on their own but
are no longer started here.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from config import env_path, load_dotenv


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "snapshot":
        from snapshots import cli

        raise SystemExit(cli(sys.argv[2:]))
    load_dotenv()
    root = Path(__file__).resolve().parent
    from memory_ui import DEFAULT_PORT, running_instance

    ui_port = int(os.getenv("MEMORY_UI_PORT", str(DEFAULT_PORT)))
    programs = [["lounge_bridge.py"], ["server.py"]]
    if running_instance("127.0.0.1", ui_port) or running_instance("localhost", ui_port):
        print(f"Memory UI already running on port {ui_port}; not starting another.", flush=True)
    else:
        programs.insert(0, ["memory_ui.py", "--root", str(env_path("DATA_DIR", "./runtime")), "--port", str(ui_port)])
    children = [subprocess.Popen([sys.executable, str(root / program[0]), *program[1:]], cwd=root, env=os.environ.copy())
                for program in programs]
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
