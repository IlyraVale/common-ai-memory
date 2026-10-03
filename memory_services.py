"""User-side launcher for Common AI Memory: one command to bring up the local UI.

`start` makes sure the unified Memory UI is running (it also supervises the
lounge bridge worker) and never starts a second copy: a healthy instance is
detected through /health, not through a port collision. The MCP services stay
under their own start scripts; this launcher only reports their state.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from config import env_path, load_dotenv
from memory_ui import bridge_status, running_instance

load_dotenv()  # ports below may come from .env; never overrides the process environment
UI_TASK_NAME = "Common AI Memory - UI"
DEFAULT_UI_PORT = int(os.getenv("MEMORY_UI_PORT", "8877"))
MCP_SERVICES = tuple((name, int(port)) for name, port in (
    ("Memory MCP", os.getenv("MEMORY_PORT", "8765")),) if port)
HOST = "127.0.0.1"


def port_open(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=timeout):
            return True
    except OSError:
        return False


def gui_python() -> str:
    """pythonw on Windows (no console window), the current interpreter elsewhere."""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate) if os.name == "nt" and candidate.is_file() else sys.executable


def ui_command(root: Path, port: int) -> list[str]:
    entry = Path(__file__).resolve().with_name("memory_ui.py")
    return [gui_python(), str(entry), "--root", str(root), "--port", str(port), "--supervise-bridge"]


def task_definition(root: Path, port: int = DEFAULT_UI_PORT) -> dict:
    """What the logon task runs; used by the installer script and checked by tests."""
    command = ui_command(root, port)
    return {"name": UI_TASK_NAME, "trigger": "at logon (current user)", "execute": command[0],
            "arguments": command[1:], "working_directory": str(root), "multiple_instances": "IgnoreNew",
            "execution_time_limit": "none", "restart_on_failure": {"count": 3, "interval": "PT1M"}}


def task_installed() -> bool:
    if os.name != "nt" or not shutil.which("schtasks"):
        return False
    return subprocess.run(["schtasks", "/Query", "/TN", UI_TASK_NAME], capture_output=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode == 0


def status_rows(ui_port: int) -> list[tuple[str, str, str]]:
    rows = [(name, str(port), "running" if port_open(port) else "stopped") for name, port in MCP_SERVICES[:1]]
    ui = running_instance(HOST, ui_port)
    if ui:
        rows.append(("Memory UI", str(ui_port), f"running (pid {ui.get('pid')})"))
    else:
        rows.append(("Memory UI", str(ui_port), "port used by another program" if port_open(ui_port) else "stopped"))
    rows += [(name, str(port), "running" if port_open(port) else "stopped") for name, port in MCP_SERVICES[1:]]
    components = (ui or {}).get("components", {})
    bridge = (ui or {}).get("lounge_bridge") or bridge_status()
    lounge = "integrated" if components.get("lounge") else "unavailable"
    lounge += f" (bridge {'running, ' + str(bridge['mode']) + ' mode' if bridge['running'] else 'not running'})"
    rows.append(("Lounge", "-", lounge))
    rows.append(("Games", "-", "integrated" if components.get("games") else "unavailable"))
    return rows


def print_status(ui_port: int) -> None:
    for name, port, state in status_rows(ui_port):
        print(f"{name:<12} {port:<5} {state}")


def start(root: Path, ui_port: int, *, use_task: bool = True, wait_seconds: float = 30.0) -> int:
    existing = running_instance(HOST, ui_port)
    if existing:
        print(f"Memory UI already running (pid {existing.get('pid')}); not starting another.")
        return 0
    if port_open(ui_port):
        print(f"Port {ui_port} is used by another program; Memory UI not started.", file=sys.stderr)
        return 2
    if use_task and task_installed():
        subprocess.run(["schtasks", "/Run", "/TN", UI_TASK_NAME], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        logs = root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        with (logs / "memory-ui.out.log").open("ab") as out, (logs / "memory-ui.err.log").open("ab") as err:
            subprocess.Popen(ui_command(root, ui_port), cwd=str(root), stdout=out, stderr=err,
                             stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True,
                             start_new_session=os.name != "nt")
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if running_instance(HOST, ui_port, timeout=3.0):
            return 0
        time.sleep(0.5)
    print("Memory UI did not become healthy in time; see logs/memory-ui.err.log", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Common AI Memory local services")
    parser.add_argument("command", choices=["start", "status"])
    parser.add_argument("--root", default=str(env_path("DATA_DIR", "./runtime")))
    parser.add_argument("--port", type=int, default=DEFAULT_UI_PORT, help="Memory UI port")
    parser.add_argument("--no-task", action="store_true", help="start directly instead of through the logon task")
    parser.add_argument("--open", action="store_true", help="open the Memory UI in the browser")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    code = 0
    if args.command == "start":
        code = start(root, args.port, use_task=not args.no_task)
    print_status(args.port)
    if code == 0 and args.open and running_instance(HOST, args.port):
        webbrowser.open(f"http://{HOST}:{args.port}/")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
