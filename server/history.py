"""History diff and log-description helpers."""

from __future__ import annotations

import json
from pathlib import Path

from core.fsutil import natural_key


def read_snapshot(history_path: str | Path) -> list[dict]:
    try:
        value = json.loads(Path(history_path).read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def changed_items(before: list[dict], after: list[dict], direction: str) -> list[dict]:
    changed: list[dict] = []
    for index, operation in enumerate(after):
        previous = before[index] if index < len(before) else {}
        old_items = previous.get("items", []) if isinstance(previous, dict) else []
        for item_index, item in enumerate(operation.get("items", [])):
            if not item.get("success"):
                continue
            previous_item = old_items[item_index] if item_index < len(old_items) else {}
            was_undone = bool(previous_item.get("undone"))
            is_undone = bool(item.get("undone"))
            if ((direction == "undo" and not was_undone and is_undone)
                    or (direction == "redo" and was_undone and not is_undone)):
                changed.append(item)
    return changed


def change_description(items: list[dict]) -> str:
    groups: set[str] = set()
    files: set[str] = set()
    for item in items:
        old_path = Path(str(item.get("old_path", "")))
        new_path = Path(str(item.get("new_path", "")))
        path = old_path if old_path.name else new_path
        extension = path.suffix.lstrip(".").upper() or "无扩展名"
        folder = path.parent.name or str(path.parent)
        groups.add(f"{folder} / {extension}")
        if old_path.name:
            files.add(old_path.name)
        elif new_path.name:
            files.add(new_path.name)
    group_text = "、".join(sorted(groups, key=natural_key)) or "未知组"
    ordered_files = sorted(files, key=natural_key)
    if len(ordered_files) > 8:
        file_text = "、".join(ordered_files[:8]) + f" 等 {len(ordered_files)} 个文件"
    else:
        file_text = "、".join(ordered_files) or "未知文件"
    return f"组：{group_text}；文件：{file_text}"
