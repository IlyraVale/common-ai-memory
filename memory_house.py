from __future__ import annotations

import re
from pathlib import Path
from typing import Any


LEGACY_CATEGORY_MAP = {
    "integration": "project/general",
    "mcp-test": "project/general",
    "test": "project/general",
}

ROOM_META: dict[str, tuple[str, str, str, str]] = {
    "relationship/human-ai": ("R", "Relationships", "Human and AI", "Shared human and AI memories"),
    "project/general": ("P", "Projects", "General Project", "General project records"),
    "relationship/ai-shared": ("♥", "关系档案", "AI Shared", "AI 之间共同留下的关系记忆"),

    "project/general": ("◇", "项目档案", "Common AI Memory", "共享记忆库本身的建设、部署与维护"),

    "game/minigames": ("♟", "游戏大厅", "Minigames", "Common AI Memory 内置小游戏"),

    "life/daily": ("☾", "生活碎片", "Daily", "值得长期留下的日常"),
    "life/milestone": ("☾", "生活碎片", "Milestone", "阶段性里程碑"),

    "preference/food": ("✦", "偏好收藏", "Food", "饮食与口味"),
    "preference/style": ("✦", "偏好收藏", "Style", "审美与风格"),
    "preference/communication": ("✦", "偏好收藏", "Communication", "交流方式与互动偏好"),

    "learning/programming": ("⌁", "学习地图", "Programming", "编程学习与能力积累"),
    "learning/english": ("⌁", "学习地图", "English", "英语学习"),

    "plan/development": ("→", "未来计划", "Development", "开发方向与待完成建设"),
    "plan/career": ("→", "未来计划", "Career", "职业方向"),
    "plan/general": ("→", "未来计划", "General", "其他长期计划"),
}

SECTION_META: dict[str, tuple[str, str, str]] = {
    "relationship": ("♥", "关系档案", "人与 AI、AI 与 AI 留下的重要关系"),
    "project": ("◇", "项目档案", "正在建造、部署与维护的东西"),
    "game": ("G", "Game Hall", "Built-in and adapter-provided games"),
    "life": ("☾", "生活碎片", "值得长期留下的日常与阶段事件"),
    "preference": ("✦", "偏好收藏", "饮食、审美、交流方式与长期习惯"),
    "learning": ("⌁", "学习地图", "编程、英语与持续积累的知识"),
    "plan": ("→", "未来计划", "开发、职业与尚未完成的长期方向"),
}

_CATEGORY_RE = re.compile(r"^[a-z][a-z0-9-]*/[a-z0-9][a-z0-9-]*$")


def normalize_category(category: str) -> str:
    raw = (category or "").strip()
    return LEGACY_CATEGORY_MAP.get(raw, raw).strip().lower()


def validate_category_shape(category: str) -> str:
    category = normalize_category(category)
    if not _CATEGORY_RE.fullmatch(category):
        raise ValueError(f"invalid category path: {category!r}")
    return category


def category_parts(category: str) -> tuple[str, str]:
    category = validate_category_shape(category)
    return tuple(category.split("/", 1))  # type: ignore[return-value]


def room_meta(category: str) -> dict[str, str]:
    category = normalize_category(category)
    section, subject = category_parts(category)
    icon, section_name, subject_name, description = ROOM_META.get(
        category,
        (
            SECTION_META.get(section, ("·", section.title(), ""))[0],
            SECTION_META.get(section, ("·", section.title(), ""))[1],
            subject.replace("-", " ").title(),
            "",
        ),
    )
    return {
        "category": category,
        "section": section,
        "subject": subject,
        "icon": icon,
        "section_name": section_name,
        "subject_name": subject_name,
        "description": description,
        "location": f"{icon} {section_name} / {subject_name}",
        "room_path": category,
    }


def room_dir(memory_root: str | Path, category: str) -> Path:
    section, subject = category_parts(category)
    return Path(memory_root) / section / subject


def decorate_record(record: dict[str, Any]) -> dict[str, Any]:
    out = dict(record)
    category = normalize_category(str(out.get("category") or ""))
    if category and _CATEGORY_RE.fullmatch(category):
        meta = room_meta(category)
        out["category"] = category
        out["section"] = meta["section"]
        out["room"] = meta["subject"]
        out["location"] = meta["location"]
        out["room_path"] = meta["room_path"]
    elif out.get("owner") == "human" or out.get("scope") == "human":
        out["section"] = "_house"
        out["room"] = "manual"
        out["location"] = "⌘ 中庭档案 / House Manual"
        out["room_path"] = "_house"
    return out


def house_readme() -> str:
    return """# COMMON AI MEMORY · 记忆中庭

这里保存那些不该被下一次刷新冲走的东西。

- ♥ 关系档案
- ◇ 项目档案
- ♟ 游戏大厅
- ☾ 生活碎片
- ✦ 偏好收藏
- ⌁ 学习地图
- → 未来计划

## House Rule

大类固定，事项固定，一条记忆只说一件事。

## Owner Rule

GPT、Claude 与人类可以彼此读取可读记忆；AI 只能修改和删除自己写下的记忆。

## Physical Layout

`memory/<section>/<subject>/...md`

栏目不再只是标签：每条长期记忆实际住在自己的房间目录里。
"""


def room_readme(category: str) -> str:
    meta = room_meta(category)
    return (
        f"# {meta['icon']} {meta['section_name']} / {meta['subject_name']}\n\n"
        f"`{meta['category']}`\n\n"
        f"{meta['description'] or 'Common AI Memory room.'}\n\n"
        "本目录中的长期记忆均属于这一固定事项。记忆的 owner / scope "
        "写在每张档案卡的 metadata 中；目录本身按主题组织，而不是按 AI 分仓。\n"
    )


def ensure_house_files(memory_root: str | Path, categories: list[str] | set[str]) -> None:
    root = Path(memory_root)
    house = root / "_house"
    house.mkdir(parents=True, exist_ok=True)

    house_file = house / "HOUSE.md"
    if not house_file.exists():
        house_file.write_text(house_readme(), encoding="utf-8")

    for category in sorted({normalize_category(c) for c in categories if c}):
        if not _CATEGORY_RE.fullmatch(category):
            continue
        room = room_dir(root, category)
        room.mkdir(parents=True, exist_ok=True)
        guide = room / "_ROOM.md"
        if not guide.exists():
            guide.write_text(room_readme(category), encoding="utf-8")


