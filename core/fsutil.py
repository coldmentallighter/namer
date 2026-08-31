"""Filesystem and Windows-specific small utilities.

Split out of the former ``core/files.py`` god module so the Windows-specific
code (hidden/system attribute probing, explorer launching) is concentrated in
one place.
"""

from __future__ import annotations

import ctypes
import mimetypes
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


ILLEGAL_CHARS = set('\\/:*?"<>|')
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def natural_key(value: str):
    """Sort text like a human: file2 comes before file10."""
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", value)]


def normalise_ext(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    return value if value.startswith(".") else f".{value}"


def read_file_metadata(path: str | Path, root: str | Path | None = None) -> dict[str, Any]:
    """Return metadata that is meaningful for every file type.

    Format-specific namespaces are deliberately supplied by workflow providers
    through ``scan_folder(..., metadata_reader=...)``.
    """
    source = Path(path)
    stat = source.stat()
    mime_type, _encoding = mimetypes.guess_type(source.name)
    metadata: dict[str, Any] = {
        "file": {
            "name": source.name,
            "stem": source.stem,
            "extension": source.suffix.lstrip(".").casefold(),
            "size_bytes": stat.st_size,
            "created_ns": stat.st_ctime_ns,
            "created_date": datetime.fromtimestamp(stat.st_ctime).strftime("%Y%m%d"),
            "modified_ns": stat.st_mtime_ns,
            "mime_type": mime_type or "application/octet-stream",
        }
    }
    if root is not None:
        try:
            metadata["file"]["relative_path"] = str(source.relative_to(Path(root)))
        except ValueError:
            pass
    return metadata


def is_generated_workbook(path: Path) -> bool:
    """Generated exports, including .oriNN, are never scanned again."""
    return bool(re.fullmatch(r".+(?:\.ori\d+)?\.ffnf\.xlsx", path.name, re.I))


def is_generated_structure(path: Path) -> bool:
    """The structure companion is an export artifact, not source content."""
    return path.name.casefold() == "filetree.txt"


def _windows_file_attributes(path: Path) -> int:
    if os.name != "nt":
        return 0
    try:
        return ctypes.windll.kernel32.GetFileAttributesW(str(path))
    except Exception:
        return 0


def is_hidden(path: Path) -> bool:
    attrs = _windows_file_attributes(path)
    return bool(attrs != 0xFFFFFFFF and attrs & 0x2) or path.name.startswith(".")


def is_system(path: Path) -> bool:
    attrs = _windows_file_attributes(path)
    return bool(attrs != 0xFFFFFFFF and attrs & 0x4)


def _path_case_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def open_in_explorer(path: str | Path) -> None:
    path = str(Path(path).resolve())
    if os.name == "nt":
        subprocess.Popen(["explorer", path])
    elif os.name == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])
