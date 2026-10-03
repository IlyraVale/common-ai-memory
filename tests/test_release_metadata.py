"""The release version is defined once and reported the same everywhere."""
from __future__ import annotations

import json
import re
from pathlib import Path

import config
import memory_ui

ROOT = Path(__file__).resolve().parents[1]


def test_version_is_consistent() -> None:
    version = config.__version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert memory_ui.APP_VERSION == version
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in pyproject and 'attr = "config.__version__"' in pyproject
    manifest = json.loads((ROOT / "browser-extension" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == version
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## \[?{re.escape(version)}\]?", changelog, re.M)


def test_installed_metadata_matches_when_installed() -> None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        installed = version("common-ai-memory")
    except PackageNotFoundError:
        return
    assert installed == config.__version__
