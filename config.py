"""Environment configuration shared by the production-derived entry points."""
from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path | None = None) -> None:
    """Load a small, dependency-free .env file without overriding the process."""
    candidate = Path(path) if path else Path(__file__).resolve().parent / ".env"
    if not candidate.is_file():
        return
    for raw in candidate.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] == value[-1:] and value[:1] in {'"', "'"}:
            value = value[1:-1]
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


def env_path(name: str, default: str) -> Path:
    return Path(os.getenv(name, default)).expanduser().resolve()


def lounge_identities() -> tuple[str, ...]:
    values = tuple(x.strip().lower() for x in os.getenv("LOUNGE_IDENTITIES", "gpt,claude,alice").split(",") if x.strip())
    if len(values) < 2 or len(values) != len(set(values)):
        raise ValueError("LOUNGE_IDENTITIES must contain distinct identities")
    return values


def human_identity() -> str:
    value = os.getenv("LOUNGE_HUMAN_IDENTITY", "alice").strip().lower()
    if value not in lounge_identities():
        raise ValueError("LOUNGE_HUMAN_IDENTITY must be listed in LOUNGE_IDENTITIES")
    return value


def human_display_name() -> str:
    return os.getenv("LOUNGE_HUMAN_DISPLAY_NAME", "Alice").strip() or "Alice"


load_dotenv()
