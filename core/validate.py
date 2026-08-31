"""Filename validation and rename preflight checks.

Split out of the former ``core/files.py`` god module.
"""

from __future__ import annotations

from typing import Sequence

from .fsutil import ILLEGAL_CHARS, RESERVED_NAMES, _path_case_key
from .models import FileRecord, ValidationIssue
from .naming import compose_filename


def validate_filename(name: str) -> str | None:
    if not name or name in {".", ".."}:
        return "文件名不能为空"
    illegal = sorted({char for char in name if char in ILLEGAL_CHARS})
    if illegal:
        return f"包含 Windows 非法字符: {' '.join(illegal)}"
    if any(ord(char) < 32 for char in name):
        return "包含控制字符"
    if name.endswith((" ", ".")):
        return "文件名不能以空格或点结尾"
    # Windows reserves the device token before the first dot (CON.txt is
    # still invalid), regardless of the rest of the stem.
    base = name.split(".", 1)[0].upper()
    if base in RESERVED_NAMES:
        return f"保留设备名不可用: {base}"
    return None


def preflight(records: Sequence[FileRecord], separator: str = "_") -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    targets: dict[str, FileRecord] = {}
    selected_sources = {
        _path_case_key(record.source_path)
        for record in records
        if record.selected
    }
    for record in records:
        if not record.selected:
            continue
        if record.status == "Conflict" and record.status_detail.startswith("缺少工作流必填字段"):
            issues.append(ValidationIssue(record, "workflow_required", record.status_detail))
        source = record.source_path
        target_name = record.target_name or compose_filename("", "", record.child_prefix, record.name,
                                                            record.extension_original or record.extension, separator)
        target = source.with_name(target_name)
        record.target_name = target_name
        if not source.exists():
            issues.append(ValidationIssue(record, "missing", "源文件不存在"))
            continue
        error = validate_filename(target_name)
        if error:
            issues.append(ValidationIssue(record, "invalid", error))
        if len(str(target)) > 260:
            issues.append(ValidationIssue(record, "too_long", "路径或文件名超过 Windows 传统长度限制"))
        target_key = _path_case_key(target)
        source_key = _path_case_key(source)
        previous = targets.get(target_key)
        if previous is not None and _path_case_key(previous.source_path) != source_key:
            issues.append(ValidationIssue(record, "duplicate", f"目标名称与 {previous.original_name} 重复"))
        else:
            targets[target_key] = record
        if target_key != source_key and target.exists() and target_key not in selected_sources:
            issues.append(ValidationIssue(record, "exists", "目标文件已存在，不覆盖已有文件"))
        try:
            with source.open("rb"):
                pass
        except PermissionError:
            issues.append(ValidationIssue(record, "permission", "没有访问权限或文件被占用"))
        except OSError as exc:
            issues.append(ValidationIssue(record, "io", f"无法访问源文件: {exc}"))
    return issues
