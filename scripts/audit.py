"""Conservative publication audit. Prints file and line only, never matched values."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".venv", "__pycache__", ".pytest_cache"}
TEXT_SUFFIXES = {".py", ".json", ".md", ".html", ".js", ".mjs", ".ts", ".ps1", ".toml", ".yaml", ".yml", ".example", ""}
PATTERNS = {
    "email-provider": r"@qq[.]com|gmail",
    "sensitive-field-name": r"(?i)(api[_-]?key|bearer|oauth|cookie|session|password|secret|token)",
    "private-network-product": r"(?i)(tailscale|tunnel)",
    "absolute-windows-path": r"(?i)\b[A-Z]:\\",
    "loopback-address": r"127[.]0[.]0[.]1",
    "machine-identifier": r"(?i)machine_id",
    "private-key-block": r"BEGIN [A-Z ]*PRIVATE KEY",
    "high-entropy-assignment": r"(?i)(key|secret|credential)\s*[:=]\s*['\"][A-Za-z0-9_./+=-]{24,}",
}

findings = []
for path in ROOT.rglob("*"):
    if not path.is_file() or any(part in SKIP for part in path.parts) or path.suffix.lower() not in TEXT_SUFFIXES:
        continue
    try: lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError: continue
    for number, line in enumerate(lines, 1):
        for name, expression in PATTERNS.items():
            if re.search(expression, line): findings.append((name, path.relative_to(ROOT), number))
for name, path, number in findings: print(f"{name}: {path}:{number}")
print(f"Audit findings: {len(findings)} (review required; names and documentation can be false positives)")
raise SystemExit(bool(findings))
