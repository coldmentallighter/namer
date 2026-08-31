"""Directory scanning, grouping and stem-association logic.

Split out of the former ``core/files.py`` god module.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Sequence

from .fsutil import (
    is_generated_structure,
    is_generated_workbook,
    is_hidden,
    is_system,
    natural_key,
    read_file_metadata,
)
from .models import FileRecord, NamingGroup, ScanResult


def directory_prefix_defaults(root: str | Path, directory: str | Path,
                              mapping: dict[str, int | None] | None = None) -> tuple[str, str, str]:
    """Return the scheme-one defaults for a file-containing directory.

    The three fields are the last three directory names, with the root name
    included when a shallow path does not yet contain three descendants.
    For example, ``Root/A/B/C`` becomes ``A, B, C`` while ``Root/A/B``
    becomes ``Root, A, B``.  A file directly in the root has an empty group
    and child prefix so the root name is not duplicated by default.
    """
    root_path = Path(root).expanduser().resolve()
    directory_path = Path(directory).expanduser().resolve()
    try:
        relative_parts = directory_path.relative_to(root_path).parts
    except ValueError:
        relative_parts = directory_path.parts
    names = [root_path.name or str(root_path)] + list(relative_parts)
    if mapping is not None:
        def mapped(field: str) -> str:
            value = mapping.get(field)
            if value is None:
                return ""
            try:
                index = int(value)
            except (TypeError, ValueError):
                return ""
            if index < 0:
                index = len(names) + index
            if 0 <= index < len(names):
                return names[index]
            return ""
        return mapped("meta"), mapped("group"), mapped("child")
    if len(names) >= 3:
        return tuple(names[-3:])  # type: ignore[return-value]
    if len(names) == 2:
        return names[0], names[1], ""
    return names[0] if names else "", "", ""


def scan_folder(root: str | Path, include_hidden: bool = False,
                include_system: bool = False,
                directory_mapping: dict[str, int | None] | None = None,
                metadata_reader: Any | None = None) -> ScanResult:
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise NotADirectoryError(str(root_path))
    records: list[FileRecord] = []
    skipped: list[str] = []
    counts: dict[str, int] = {}
    groups: dict[str, NamingGroup] = {}
    max_depth = 0
    for directory, dirs, files in os.walk(root_path):
        dirs[:] = sorted(dirs, key=natural_key)
        directory_path = Path(directory)
        if not include_hidden:
            dirs[:] = [d for d in dirs if not is_hidden(directory_path / d)]
        if not include_system:
            dirs[:] = [d for d in dirs if not is_system(directory_path / d)]
        for filename in sorted(files, key=natural_key):
            path = directory_path / filename
            if is_generated_workbook(path) or is_generated_structure(path):
                skipped.append(str(path))
                continue
            if not include_hidden and is_hidden(path):
                continue
            if not include_system and is_system(path):
                continue
            try:
                if not path.is_file():
                    continue
            except OSError as exc:
                skipped.append(f"{path}: {exc}")
                continue
            original_ext = path.suffix
            ext = original_ext.lower()
            relative_folder = os.path.relpath(directory_path, root_path)
            if relative_folder == ".":
                relative_folder = "(root)"
            folder_name = directory_path.name or str(directory_path)
            try:
                max_depth = max(max_depth, len(directory_path.relative_to(root_path).parts))
            except ValueError:
                pass
            default_meta, default_group, default_child = directory_prefix_defaults(root_path, directory_path, directory_mapping)
            rec = FileRecord(
                path=str(path), root=str(root_path), extension=ext,
                folder_name=folder_name, relative_folder=relative_folder,
                original_name=filename, stem=path.stem,
                extension_original=original_ext,
                child_prefix=default_child,
            )
            try:
                reader = metadata_reader or read_file_metadata
                rec.metadata = reader(path, root_path)
            except (OSError, ValueError, TypeError) as exc:
                rec.metadata = {"file": {"error": str(exc)}}
            records.append(rec)
            counts[ext] = counts.get(ext, 0) + 1
            group = groups.get(rec.group_key)
            if group is None:
                group = NamingGroup(
                    rec.group_key,
                    str(directory_path),
                    folder_name,
                    ext,
                    prefix=default_group,
                    relative_folder=relative_folder,
                    meta_prefix=default_meta,
                    extensions=[ext],
                )
                groups[rec.group_key] = group
            group.records.append(rec)
    for group in groups.values():
        group.records.sort(key=lambda item: natural_key(item.original_name))
    records.sort(key=lambda item: (natural_key(item.relative_folder), natural_key(item.original_name)))
    associations = refresh_stem_associations(records)
    return ScanResult(str(root_path), records, dict(sorted(counts.items(), key=lambda pair: natural_key(pair[0]))), groups, skipped, associations, max_depth)


def build_stem_associations(records: Sequence[FileRecord]) -> list[dict[str, Any]]:
    """Group files in one directory that share a stem across extensions.

    Each extension remains in its own naming group.  The WebUI may use the
    resulting identity to synchronize a shared target stem.
    """
    buckets: dict[tuple[str, str], list[FileRecord]] = {}
    for record in records:
        if record.removed:
            continue
        key = (record.relative_folder.casefold(), record.stem.casefold())
        buckets.setdefault(key, []).append(record)
    result: list[dict[str, Any]] = []
    for (relative_folder, stem), items in buckets.items():
        extensions = sorted({item.extension for item in items}, key=natural_key)
        if len(extensions) < 2:
            continue
        association_id = f"{relative_folder}\x1f{stem}"
        result.append({
            "id": association_id,
            "relative_folder": relative_folder,
            "stem": items[0].stem,
            "extensions": extensions,
            "paths": [item.path for item in sorted(items, key=lambda item: natural_key(item.extension))],
            "count": len(items),
        })
    return sorted(result, key=lambda item: (natural_key(item["relative_folder"]), natural_key(item["stem"])))


def refresh_stem_associations(records: Sequence[FileRecord]) -> list[dict[str, Any]]:
    """Rebuild association fields after records have been renamed in memory."""
    for record in records:
        record.association_id = ""
        record.associated_extensions = []
    associations = build_stem_associations(records)
    by_path = {record.path: record for record in records}
    for association in associations:
        for path in association["paths"]:
            record = by_path.get(path)
            if record:
                record.association_id = association["id"]
                record.associated_extensions = list(association["extensions"])
    return associations
