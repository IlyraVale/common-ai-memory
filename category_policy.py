from __future__ import annotations

import re
from pathlib import Path

from memory_house import LEGACY_CATEGORY_MAP, normalize_category


FALLBACK_CATEGORIES = {
    "game/minigames",
    "project/general",
    "relationship/human-ai",
    "relationship/ai-shared",
    "life/daily",
    "life/milestone",
    "preference/food",
    "preference/style",
    "preference/communication",
    "learning/programming",
    "learning/english",
    "plan/development",
    "plan/career",
    "plan/general",
}

_CATEGORY_LINE = re.compile(
    r"^\s*-\s+([a-z][a-z0-9-]*/[a-z0-9][a-z0-9-]*)\s*[：:]"
)


class CategoryPolicy:
    """Validate AI-written memory categories against the human-maintained index."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.index_candidates = [
            self.project_root / "memory" / "_house" / "CATEGORY_INDEX.md",
            # One-version compatibility during/after migration.
            self.project_root / "memory" / "human" / "CATEGORY_INDEX.md",
        ]

    @property
    def index_path(self) -> Path:
        for path in self.index_candidates:
            if path.exists():
                return path
        return self.index_candidates[0]

    def allowed_categories(self) -> list[str]:
        categories = set(FALLBACK_CATEGORIES)
        path = self.index_path

        if path.exists():
            try:
                text = path.read_text(encoding="utf-8")
                parsed = {
                    match.group(1).lower()
                    for line in text.splitlines()
                    if (match := _CATEGORY_LINE.match(line))
                }
                if parsed:
                    categories = parsed
            except OSError:
                pass

        return sorted(categories)

    def normalize(self, category: str) -> str:
        raw = (category or "").strip()
        if not raw:
            raise ValueError(self._error_message(raw))

        normalized = normalize_category(raw)
        allowed = self.allowed_categories()

        if normalized not in allowed:
            raise ValueError(self._error_message(raw, allowed))
        return normalized

    def normalize_optional(self, category: str | None) -> str | None:
        if category is None:
            return None
        return self.normalize(category)

    def _error_message(self, category: str, allowed: list[str] | None = None) -> str:
        allowed = allowed or self.allowed_categories()
        shown = category if category else "<empty/general>"
        return (
            f"invalid memory category: {shown!r}. "
            "Choose one category from memory/_house/CATEGORY_INDEX.md. "
            "If a genuinely new subject is needed, add it to CATEGORY_INDEX.md first. "
            "Allowed categories: " + ", ".join(allowed)
        )
