"""Transactional file rename, fingerprinting and history-move logic.

Split out of the former ``core/files.py`` god module.  ``append_history`` is
imported lazily inside ``execute_rename`` so the module graph stays acyclic:
``history.py`` imports ``_move_history_items`` from here at module level.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .fsutil import _path_case_key
from .models import FileRecord, RenameItem, RenameOperation, ValidationIssue
from .validate import preflight


def file_fingerprint(path: str | Path, include_hash: bool = True) -> dict[str, Any]:
    """Return a durable identity snapshot used to guard undo/redo.

    The SHA-256 is intentionally included by default so a replacement file at
    the same path cannot be mistaken for the renamed source.  Stat metadata is
    retained as a quick diagnostic and for older/large-file callers that opt
    out of hashing.
    """
    source = Path(path)
    stat = source.stat()
    result: dict[str, Any] = {
        "size": int(stat.st_size),
        "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
        "file_id": int(getattr(stat, "st_ino", 0) or 0),
    }
    if include_hash:
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        result["sha256"] = digest.hexdigest()
    return result


def _fingerprint_matches(path: Path, expected: dict[str, Any]) -> bool:
    if not expected:
        return True  # legacy history written before fingerprints existed
    try:
        actual = file_fingerprint(path, include_hash=bool(expected.get("sha256")))
    except OSError:
        return False
    if expected.get("sha256"):
        if (actual.get("sha256") != expected.get("sha256")
                or (expected.get("size") is not None and actual.get("size") != expected.get("size"))):
            return False
        expected_file_id = int(expected.get("file_id", 0) or 0)
        actual_file_id = int(actual.get("file_id", 0) or 0)
        return not expected_file_id or not actual_file_id or expected_file_id == actual_file_id
    return all(actual.get(key) == value for key, value in expected.items() if key in actual)


def _resolve_history_source(expected_path: Path, destination_path: Path,
                            expected_fingerprint: dict[str, Any]) -> tuple[Path | None, str]:
    """Resolve an undo/redo source by identity, using paths only as hints."""
    hints: list[Path] = []
    for hint in (expected_path, destination_path):
        if _path_case_key(hint) not in {_path_case_key(item) for item in hints}:
            hints.append(hint)
    for hint in hints:
        if hint.is_file() and _fingerprint_matches(hint, expected_fingerprint):
            return hint, ""
    if not expected_fingerprint:
        return None, f"历史记录没有文件指纹，且预期路径不存在: {expected_path}"

    matches: list[tuple[Path, dict[str, Any]]] = []
    expected_size = expected_fingerprint.get("size")
    searched: set[str] = set()
    for directory in (expected_path.parent, destination_path.parent):
        directory_key = _path_case_key(directory)
        if directory_key in searched or not directory.is_dir():
            continue
        searched.add(directory_key)
        try:
            children = list(directory.iterdir())
        except OSError:
            continue
        for candidate in children:
            try:
                if not candidate.is_file() or ".ffnf-txn-" in candidate.name:
                    continue
                if expected_size is not None and candidate.stat().st_size != int(expected_size):
                    continue
                actual = file_fingerprint(candidate, include_hash=bool(expected_fingerprint.get("sha256")))
            except (OSError, TypeError, ValueError):
                continue
            if expected_fingerprint.get("sha256"):
                matched = actual.get("sha256") == expected_fingerprint.get("sha256")
            else:
                matched = all(actual.get(key) == value for key, value in expected_fingerprint.items()
                              if key in actual)
            if matched:
                matches.append((candidate, actual))

    expected_file_id = int(expected_fingerprint.get("file_id", 0) or 0)
    if expected_file_id:
        same_id = [candidate for candidate, actual in matches
                   if int(actual.get("file_id", 0) or 0) == expected_file_id]
        if len(same_id) == 1:
            return same_id[0], ""
        if len(same_id) > 1:
            names = "、".join(str(path) for path in same_id[:3])
            return None, f"文件指纹匹配到多个候选，无法安全识别: {names}"
    if len(matches) == 1:
        return matches[0][0], ""
    if len(matches) > 1:
        names = "、".join(str(path) for path, _actual in matches[:3])
        return None, f"文件内容指纹匹配到多个候选，无法安全识别: {names}"
    return None, f"未在原目录找到匹配文件指纹的文件: {expected_path.parent}"


def _move_history_items(items: Sequence[dict[str, Any]], direction: str) -> list[str]:
    """Move one history action transactionally after resolving every file."""
    plans: list[tuple[dict[str, Any], Path, Path]] = []
    already_at_destination: list[tuple[dict[str, Any], Path, Path]] = []
    errors: list[str] = []
    for item in items:
        try:
            old_path = Path(item["old_path"])
            new_path = Path(item["new_path"])
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"历史路径无效: {exc}")
            continue
        if direction == "undo":
            expected_path, destination = new_path, old_path
            fingerprint = item.get("new_fingerprint", {})
        else:
            expected_path, destination = old_path, new_path
            fingerprint = item.get("undo_fingerprint") or item.get("old_fingerprint", {})
        source, error = _resolve_history_source(expected_path, destination, fingerprint)
        if source is None:
            errors.append(error)
            continue
        if str(source) == str(destination):
            already_at_destination.append((item, source, destination))
        else:
            plans.append((item, source, destination))
    if errors:
        return errors

    source_keys: dict[str, Path] = {}
    destination_keys: dict[str, Path] = {}
    for _item, source, destination in plans:
        source_key = _path_case_key(source)
        destination_key = _path_case_key(destination)
        if source_key in source_keys:
            errors.append(f"多个历史项目解析到同一文件指纹: {source}")
        else:
            source_keys[source_key] = source
        if destination_key in destination_keys:
            errors.append(f"多个历史项目要求同一目标路径: {destination}")
        else:
            destination_keys[destination_key] = destination
    for _item, _source, destination in plans:
        destination_key = _path_case_key(destination)
        if destination.exists() and destination_key not in source_keys:
            errors.append(f"目标路径已存在，不覆盖: {destination}")
    if errors:
        return errors

    staged: list[tuple[dict[str, Any], Path, Path, Path]] = []
    committed: list[tuple[dict[str, Any], Path, Path]] = []
    try:
        for item, source, destination in plans:
            temporary = source.with_name(f".{source.name}.ffnf-txn-{uuid.uuid4().hex}.tmp")
            os.rename(source, temporary)
            staged.append((item, source, destination, temporary))
        for item, source, destination, temporary in staged:
            os.rename(temporary, destination)
            committed.append((item, source, destination))
    except Exception as exc:
        rollback_errors: list[str] = []
        for _item, source, destination in reversed(committed):
            try:
                same_path_different_case = (_path_case_key(destination) == _path_case_key(source)
                                            and str(destination) != str(source))
                if destination.exists() and (same_path_different_case or not source.exists()):
                    if same_path_different_case:
                        rollback_temporary = destination.with_name(
                            f".{destination.name}.ffnf-txn-{uuid.uuid4().hex}.tmp"
                        )
                        os.rename(destination, rollback_temporary)
                        os.rename(rollback_temporary, source)
                    else:
                        os.rename(destination, source)
            except Exception as rollback_exc:
                rollback_errors.append(f"{destination} -> {source}: {rollback_exc}")
        committed_ids = {id(item) for item, _source, _destination in committed}
        for item, source, _destination, temporary in reversed(staged):
            if id(item) in committed_ids:
                continue
            try:
                if temporary.exists() and not source.exists():
                    os.rename(temporary, source)
            except Exception as rollback_exc:
                rollback_errors.append(f"{temporary} -> {source}: {rollback_exc}")
        detail = f"事务执行失败，已回滚: {exc}"
        if rollback_errors:
            detail += "；部分回滚失败: " + "；".join(rollback_errors)
        return [detail]

    for item, source, destination in already_at_destination + committed:
        try:
            fingerprint = file_fingerprint(destination)
        except OSError:
            fingerprint = {}
        if direction == "undo":
            item["undone"] = True
            item["undo_source_path"] = str(source)
            item["undo_fingerprint"] = fingerprint
        else:
            item["undone"] = False
            item["redo_source_path"] = str(source)
            item["new_fingerprint"] = fingerprint
            item.pop("redo_error", None)
    return []


def execute_rename(records: Sequence[FileRecord], history_path: str | Path,
                   kind: str = "batch", separator: str = "_",
                   write_history: bool = True) -> RenameOperation:
    selected = [record for record in records if record.selected]
    issues = preflight(selected, separator)
    issue_by_path: dict[str, list[ValidationIssue]] = {}
    for issue in issues:
        issue_by_path.setdefault(issue.record.path, []).append(issue)
        issue.record.status = "Conflict"
        issue.record.status_detail = issue.message
    if issues:
        operation = RenameOperation(datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"), kind, [], "blocked", "预检查发现冲突，事务未执行")
        for record in selected:
            if record.path in issue_by_path:
                operation.items.append(RenameItem(record.path, str(record.source_path.with_name(record.target_name)), record.group_key, False,
                                                  "; ".join(issue.message for issue in issue_by_path[record.path])))
        # A blocked preflight has no filesystem mutation, so it is not an
        # undoable operation and must not hide the previous successful entry.
        return operation
    operation_time = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    items: list[RenameItem] = []
    moving: list[tuple[FileRecord, Path, Path, dict[str, Any]]] = []
    for record in selected:
        source = record.source_path
        target = source.with_name(record.target_name)
        try:
            old_fingerprint = file_fingerprint(source)
        except OSError as exc:
            record.status = "Error"
            record.status_detail = str(exc)
            items.append(RenameItem(str(source), str(target), record.group_key, False, str(exc)))
            continue
        if str(source) == str(target):
            record.status = "Unchanged"
            items.append(RenameItem(str(source), str(target), record.group_key, True,
                                    old_fingerprint=old_fingerprint, new_fingerprint=old_fingerprint))
        else:
            moving.append((record, source, target, old_fingerprint))

    staged: list[tuple[FileRecord, Path, Path, Path, dict[str, Any]]] = []
    committed: list[tuple[FileRecord, Path, Path, dict[str, Any]]] = []
    transaction_error = ""
    rollback_failed = False
    stage_failed_record: tuple[FileRecord, Path, Path] | None = None
    # Stage every source under a unique sibling name first. This makes swaps
    # and chains safe and gives us a known point from which to compensate.
    try:
        for record, source, target, old_fingerprint in moving:
            temporary = source.with_name(f".{source.name}.ffnf-txn-{uuid.uuid4().hex}.tmp")
            stage_failed_record = (record, source, target)
            os.rename(source, temporary)
            staged.append((record, source, target, temporary, old_fingerprint))
        for record, source, target, temporary, old_fingerprint in staged:
            os.rename(temporary, target)
            new_fingerprint = file_fingerprint(target)
            committed.append((record, source, target, new_fingerprint))
            record.path = str(target)
            record.original_name = target.name
            record.stem = target.stem
            record.status = "Renamed"
            record.status_detail = ""
            items.append(RenameItem(str(source), str(target), record.group_key, True,
                                    old_fingerprint=old_fingerprint, new_fingerprint=new_fingerprint))
    except Exception as exc:
        transaction_error = str(exc)
        # Compensate final names and any still-staged names. Never overwrite.
        for record, source, target, new_fingerprint in reversed(committed):
            try:
                same_path_different_case = (_path_case_key(target) == _path_case_key(source)
                                            and str(target) != str(source))
                if target.exists() and (same_path_different_case or not source.exists()):
                    if same_path_different_case:
                        rollback_temporary = target.with_name(
                            f".{target.name}.ffnf-txn-{uuid.uuid4().hex}.tmp"
                        )
                        os.rename(target, rollback_temporary)
                        os.rename(rollback_temporary, source)
                    else:
                        os.rename(target, source)
                record.path = str(source)
                record.original_name = source.name
                record.stem = source.stem
            except Exception as rollback_exc:
                rollback_failed = True
                transaction_error += f"；回滚失败 {target} -> {source}: {rollback_exc}"
        for record, source, target, temporary, old_fingerprint in reversed(staged):
            if any(item[0] is record for item in committed):
                continue
            try:
                if temporary.exists() and not source.exists():
                    os.rename(temporary, source)
            except Exception as rollback_exc:
                rollback_failed = True
                transaction_error += f"；回滚失败 {temporary} -> {source}: {rollback_exc}"
        committed_paths = {str(source): str(target) for _record, source, target, _fp in committed}
        for item in items:
            if item.success and item.old_path in committed_paths:
                # A successfully compensated item is no longer an executed
                # rename and must not become an undo candidate.
                source = Path(item.old_path)
                target = Path(item.new_path)
                if source.exists() and not target.exists():
                    item.success = False
                    item.error = f"事务失败，已回滚：{transaction_error}"
        for record, source, target, _new_fingerprint in committed:
            record.status = "Error"
            record.status_detail = f"事务失败，已回滚：{transaction_error}"
        for record, source, target, temporary, old_fingerprint in staged:
            if not any(item[0] is record for item in committed):
                record.status = "Error"
                record.status_detail = f"事务失败，已回滚：{transaction_error}"
        if stage_failed_record is not None and not any(item[0] is stage_failed_record[0] for item in staged):
            record, source, target = stage_failed_record
            record.status = "Error"
            record.status_detail = f"事务失败：{transaction_error}"
            items.append(RenameItem(str(source), str(target), record.group_key, False, transaction_error))
        # Add failed items for moving records that never reached the final path.
        committed_records = {id(item[0]) for item in committed}
        for record, source, target, _temporary, _old_fingerprint in staged:
            if id(record) not in committed_records:
                items.append(RenameItem(str(source), str(target), record.group_key, False, transaction_error))
        operation = RenameOperation(operation_time, kind, items,
                                    "partial" if rollback_failed else "rolled_back",
                                    transaction_error)
    else:
        operation = RenameOperation(operation_time, kind, items, "committed", "")
    if write_history and items and any(item.success for item in items):
        # Imported lazily to avoid a module-level cycle with core.history,
        # which imports _move_history_items from this module.
        from .history import append_history
        append_history(history_path, operation)
    return operation
