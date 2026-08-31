"""Durable undo/redo history persistence and read/diff helpers.

Split out of the former ``core/files.py`` god module.  History *reads* and
diffing helpers (``read_snapshot``/``changed_items``/``change_description``)
that used to live in ``server/history.py`` are merged here by the
decomposition plan.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .fsutil import natural_key
from .models import RenameOperation
from .rename import _move_history_items


def append_history(history_path: str | Path, operation: RenameOperation) -> None:
    path = Path(history_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = []
    data.append({
        "operation_time": operation.operation_time,
        "kind": operation.kind,
        "items": [asdict(item) for item in operation.items],
    })
    # Replace the history atomically so an interrupted write cannot leave a
    # truncated JSON file and make the durable undo stack unusable.
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _history_action_indices(data: list[dict[str, Any]], target_index: int,
                            eligible) -> list[int]:
    target = data[target_index]
    if target.get("kind") != "batch":
        return [target_index]
    operation_time = target.get("operation_time")
    start = target_index
    while start > 0:
        candidate = data[start - 1]
        if candidate.get("kind") != "batch" or candidate.get("operation_time") != operation_time:
            break
        start -= 1
    end = target_index
    while end + 1 < len(data):
        candidate = data[end + 1]
        if candidate.get("kind") != "batch" or candidate.get("operation_time") != operation_time:
            break
        end += 1
    return [index for index in range(start, end + 1) if eligible(data[index])]


def _write_history(path: Path, data: list[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def undo_last(history_path: str | Path) -> tuple[bool, list[str]]:
    path = Path(history_path)
    if not path.exists():
        return False, ["没有可撤销的历史记录"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return False, [f"历史记录读取失败: {exc}"]
    if not isinstance(data, list) or not data:
        return False, ["没有可撤销的历史记录"]

    def eligible(operation: dict[str, Any]) -> bool:
        return (not operation.get("undone_at")
                and any(item.get("success") and not item.get("undone")
                        for item in operation.get("items", [])))

    target_index = next((index for index in range(len(data) - 1, -1, -1)
                         if eligible(data[index])), None)
    if target_index is None:
        return False, ["没有可撤销的历史记录"]
    operation_indices = _history_action_indices(data, target_index, eligible)
    items = [item for index in operation_indices for item in data[index].get("items", [])
             if item.get("success") and not item.get("undone")]
    errors = _move_history_items(items, "undo")
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    if errors:
        for index in operation_indices:
            data[index]["undo_attempted_at"] = now
        _write_history(path, data)
        return False, [f"撤销失败：{error}" for error in errors]

    next_sequence = max((int(operation.get("undo_sequence", 0) or 0)
                         for operation in data), default=0) + 1
    for index in operation_indices:
        operation = data[index]
        successful = [item for item in operation.get("items", []) if item.get("success")]
        operation["restored_count"] = sum(bool(item.get("undone")) for item in successful)
        if successful and all(item.get("undone") for item in successful):
            operation["undone_at"] = now
            operation["undo_sequence"] = next_sequence
            operation.pop("undo_attempted_at", None)
    _write_history(path, data)
    return True, []


def redo_last(history_path: str | Path) -> tuple[bool, list[str]]:
    path = Path(history_path)
    if not path.exists():
        return False, ["没有可还原的撤销记录"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return False, [f"历史记录读取失败: {exc}"]
    if not isinstance(data, list) or not data:
        return False, ["没有可还原的撤销记录"]

    def eligible(operation: dict[str, Any]) -> bool:
        return (bool(operation.get("undone_at"))
                and any(item.get("success") and item.get("undone")
                        for item in operation.get("items", [])))

    candidates = [index for index in range(len(data)) if eligible(data[index])]
    if not candidates:
        return False, ["没有可还原的撤销记录"]
    target_index = max(candidates, key=lambda index: (
        int(data[index].get("undo_sequence", 0) or 0), index,
    ))
    operation_indices = _history_action_indices(data, target_index, eligible)
    items = [item for index in operation_indices for item in data[index].get("items", [])
             if item.get("success") and item.get("undone")]
    if not items:
        return False, ["没有可还原的撤销文件"]
    errors = _move_history_items(items, "redo")
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    if errors:
        for index in operation_indices:
            data[index]["redo_attempted_at"] = now
        _write_history(path, data)
        return False, [f"还原失败：{error}" for error in errors]

    for index in operation_indices:
        operation = data[index]
        successful = [item for item in operation.get("items", []) if item.get("success")]
        operation["redone_count"] = sum(not item.get("undone") for item in successful)
        if successful and all(not item.get("undone") for item in successful):
            operation.pop("undone_at", None)
            operation.pop("undo_attempted_at", None)
            operation.pop("redo_attempted_at", None)
            operation["redone_at"] = now
    _write_history(path, data)
    return True, []


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
