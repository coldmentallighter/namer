"""Local offline WebUI server for the file naming application.

The server binds to loopback only. It exposes the existing namer_core service
through a small JSON API and serves the bundled static WebUI without network
dependencies.
"""

from __future__ import annotations

import itertools
import json
import mimetypes
import re
import sys
import tempfile
import threading
import time
import webbrowser
from dataclasses import asdict
from datetime import datetime
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from namer_core import (
    ExcelMatchResult,
    FileRecord,
    LogEntry,
    NamingGroup,
    RenameOperation,
    append_history,
    apply_filename_parse,
    collect_directory_statistics,
    directory_prefix_defaults,
    execute_rename,
    export_filename_tables,
    import_xlsx,
    natural_key,
    normalise_ext,
    open_in_explorer,
    refresh_stem_associations,
    redo_last,
    scan_folder,
    undo_last,
    validate_filename,
)
from workflow_config import (
    BUILTIN_WORKFLOWS,
    WorkflowCatalog,
    load_workflow_package,
    package_workflow,
    validate_workflow,
    workflow_field_map,
    workflow_summary,
)
from workflow_values import WorkflowValueStore
from workflow_metadata import (
    apply_workflow_metadata,
    normalise_workflow_value,
    parse_workflow_filename,
    read_workflow_metadata,
)


WEB_ROOT = Path(__file__).with_name("webui")
APP_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
WORKFLOW_VALUE_STORE = WorkflowValueStore(APP_ROOT / "workflow-values")

# The browser cannot expose a direct "tab closed" event to the local server.
# A short heartbeat plus a pagehide beacon gives the server a reliable signal
# while allowing a normal page reload to reconnect before shutdown.
CLIENT_HEARTBEAT_INTERVAL_SECONDS = 2.0
CLIENT_HEARTBEAT_TIMEOUT_SECONDS = 8.0
CLIENT_CLOSE_GRACE_SECONDS = 3.0
CLIENT_STARTUP_TIMEOUT_SECONDS = 30.0


class AppState:
    def __init__(self) -> None:
        self.root: str = ""
        self.scan_result = None
        self.groups: dict[str, NamingGroup] = {}
        self.current_group_key: str | None = None
        self.separator: str = "_"
        self.mode: str = "original"
        self.numeric_start: int = 1
        self.numeric_width: int = 2
        self.numeric_step: int = 1
        # Relative directory indexes: -1=current folder, -2=parent, ...;
        # non-negative values address root-relative levels (0=root).
        self.directory_mapping: dict[str, int | None] = {"meta": -3, "group": -2, "child": -1}
        self.directory_mapping_auto: bool = True
        self.parse_template: str = "auto"
        self.parse_use_name: bool = False
        self.workflow_catalog = WorkflowCatalog(APP_ROOT / "config.json")
        self.workflow_id: str = self.workflow_catalog.current_workflow
        self.workflow_values: dict[str, str] = {}
        self.workflow_candidates: dict[str, list[str]] = {}
        self.workflow_suffix_mode: str = ""
        workflow_numbering = self.workflow_catalog.get(self.workflow_id).get("numbering", {})
        if workflow_numbering.get("enabled"):
            self.numeric_start = int(workflow_numbering.get("start", self.numeric_start))
            self.numeric_width = max(1, int(workflow_numbering.get("width", self.numeric_width)))
            self.numeric_step = int(workflow_numbering.get("step", self.numeric_step)) or 1
        self.include_hidden: bool = False
        self.include_system: bool = False
        self.group_enabled: dict[str, bool] = {}
        # Paths skipped solely because their extension filter was disabled.
        # This lets re-enabling an extension restore only those checkboxes and
        # preserves files the user deliberately deselected.
        self.extension_skipped: set[str] = set()
        self.extension_enabled: dict[str, bool] = {}
        self.excel_mappings: dict[str, dict[str, str]] = {}
        self.excel_skipped: dict[str, set[str]] = {}
        self.association_leaders: dict[str, str] = {}
        self.logs: list[LogEntry] = []
        # Keep the durable undo log beside the source, or beside the packaged
        # executable when running as a frozen distribution.
        self.history_path = APP_ROOT / "history" / "history.json"
        self.lock = threading.RLock()

    def log(self, level: str, message: str) -> None:
        self.logs.append(LogEntry(level, message))
        self.logs = self.logs[-300:]

    def current_group(self) -> NamingGroup | None:
        return self.groups.get(self.current_group_key) if self.current_group_key else None


STATE = AppState()


def _active_workflow() -> dict:
    workflow = validate_workflow(STATE.workflow_catalog.get(STATE.workflow_id))
    try:
        stored_tags = WORKFLOW_VALUE_STORE.read(workflow).get("tags", {})
    except (OSError, RuntimeError, ValueError):
        # A malformed or unavailable vocabulary workbook must not make the
        # naming workbench unusable; the declarative workflow remains valid.
        return workflow
    for field in workflow.get("fields", []):
        field_id = str(field.get("id", ""))
        tags = stored_tags.get(field_id)
        if tags is None:
            continue
        field["quick_tags"] = [
            {
                "label": str(tag.get("label", tag.get("value", ""))),
                "value": str(tag.get("value", tag.get("label", ""))),
            }
            for tag in tags
            if bool(tag.get("enabled", True)) and str(tag.get("value", tag.get("label", ""))).strip()
        ]
    return workflow


def _workflow_is_default() -> bool:
    return _active_workflow().get("kind") == "default"


def _workflow_state() -> dict:
    workflow = _active_workflow()
    return {
        "active_id": STATE.workflow_id,
        "active": workflow,
        "available": [workflow_summary(item) for item in STATE.workflow_catalog.all()],
        "values": dict(STATE.workflow_values),
        "candidates": {field_id: list(values) for field_id, values in STATE.workflow_candidates.items()},
    }


def _activate_workflow(workflow_id: str, *, persist: bool = True) -> dict:
    previous_workflow = _active_workflow()
    if persist:
        workflow = STATE.workflow_catalog.set_current(str(workflow_id))
    else:
        workflow = STATE.workflow_catalog.get(str(workflow_id))
        STATE.workflow_catalog.current_workflow = str(workflow_id)
    STATE.workflow_id = workflow["id"]
    allowed_modes = workflow.get("name_modes", ["original"])
    if STATE.mode not in allowed_modes:
        STATE.mode = allowed_modes[0]
    numbering = workflow.get("numbering", {})
    if numbering.get("enabled"):
        STATE.numeric_start = int(numbering.get("start", 1))
        STATE.numeric_width = max(1, int(numbering.get("width", 2)))
        STATE.numeric_step = int(numbering.get("step", 1)) or 1
    STATE.parse_template = "auto"
    STATE.parse_use_name = False
    regrouped = bool(
        STATE.groups and STATE.root
        and _workflow_grouping_signature(previous_workflow)
        != _workflow_grouping_signature(workflow)
    )
    if regrouped:
        mapping = None if STATE.directory_mapping_auto else STATE.directory_mapping
        result = _scan_for_workflow(
            STATE.root, STATE.include_hidden, STATE.include_system, mapping, workflow
        )
        STATE.scan_result = result
        STATE.groups = result.groups
        STATE.current_group_key = next(iter(result.groups), None)
        STATE.group_enabled = {key: True for key in result.groups}
        STATE.extension_skipped.clear()
        _apply_initial_extension_defaults(workflow, result)
        STATE.excel_mappings.clear()
        STATE.excel_skipped.clear()
        STATE.association_leaders.clear()
    if STATE.groups:
        apply_workflow_metadata(_all_records(), workflow)
        if STATE.scan_result:
            _apply_initial_extension_defaults(workflow, STATE.scan_result)
    _initialise_workflow_values()
    if STATE.groups:
        for group in STATE.groups.values():
            _prepare_group(group)
        _expand_associated_records([
            record for record in _all_records() if record.selected and not record.removed
        ])
    return workflow


def _excel_group_ready(key: str) -> bool:
    if STATE.excel_mappings.get(key):
        return True
    mapped_paths = {path for mapping in STATE.excel_mappings.values() for path in mapping}
    mapped_associations = {
        record.association_id for record in _all_records()
        if record.path in mapped_paths and record.association_id
    }
    group = STATE.groups.get(key)
    return bool(group and any(record.association_id in mapped_associations for record in group.records))


def _group_enabled_for_execution(key: str) -> bool:
    if not STATE.group_enabled.get(key, True):
        return False
    return STATE.mode != "excel" or _excel_group_ready(key)


def _record_json(record: FileRecord) -> dict:
    return {
        "path": record.path,
        "original_name": record.original_name,
        "extension": record.extension,
        "extension_original": record.extension_original,
        "folder_name": record.folder_name,
        "relative_folder": record.relative_folder,
        "target_name": record.target_name,
        "status": record.status,
        "status_detail": record.status_detail,
        "selected": record.selected,
        "removed": record.removed,
        "is_audio": str(record.metadata.get("file", {}).get("mime_type", "")).startswith("audio/"),
        "audio_format": str(record.metadata.get("file", {}).get("mime_type", "")).removeprefix("audio/").upper() or "AUDIO",
        "parsed_fields": record.parsed_fields,
        "parse_unmatched": record.parse_unmatched,
        "parse_confidence": record.parse_confidence,
        "parse_error": record.parse_error,
        "metadata": record.metadata,
        "association_id": record.association_id,
        "associated_extensions": record.associated_extensions,
        "workflow_values": record.workflow_values,
        "workflow_candidates": record.workflow_candidates,
        "workflow_candidate_details": record.workflow_candidate_details,
        "workflow_derived": record.workflow_derived,
        "workflow_actions": sorted(record.workflow_actions),
    }


def _state_json() -> dict:
    with STATE.lock:
        result = STATE.scan_result
        groups = []
        if result:
            for key, group in STATE.groups.items():
                groups.append({
                    "key": key,
                    "label": group.label,
                    "folder": group.folder,
                    "folder_name": group.folder_name,
                    "relative_folder": group.relative_folder,
                    "extension": group.extension,
                    "extensions": group.extensions or [group.extension],
                    "workflow_values": group.workflow_values,
                    "workflow_candidates": group.workflow_candidates,
                    "enabled": _group_enabled_for_execution(key),
                    "count": sum(not record.removed for record in group.records),
                })
        return {
            "root": STATE.root,
            "separator": STATE.separator,
            "mode": STATE.mode,
            "numeric": {"start": STATE.numeric_start, "width": STATE.numeric_width, "step": STATE.numeric_step},
            "directory_mapping": STATE.directory_mapping if not STATE.directory_mapping_auto else None,
            "directory_mapping_auto": STATE.directory_mapping_auto,
            "max_depth": result.max_depth if result else 0,
            "parse_template": STATE.parse_template,
            "parse_use_name": STATE.parse_use_name,
            "workflow": _workflow_state(),
            "config": {
                "theme": STATE.workflow_catalog.theme,
                "current_workflow": STATE.workflow_id,
            },
            "include_hidden": STATE.include_hidden,
            "include_system": STATE.include_system,
            "extensions": result.extension_counts if result else {},
            "extension_enabled": dict(STATE.extension_enabled),
            "groups": groups,
            "current_group_key": STATE.current_group_key,
            "associations": result.associations if result else [],
            "records": [_record_json(record) for record in STATE.current_group().records] if STATE.current_group() else [],
            "total_file_count": sum(not record.removed for record in result.records) if result else 0,
            "logs": [asdict(entry) for entry in STATE.logs[-120:]],
        }


def _read_history_snapshot(history_path: str | Path) -> list[dict]:
    """Read history for log diffing without making logging fail the action."""
    try:
        value = json.loads(Path(history_path).read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def _changed_history_items(before: list[dict], after: list[dict], direction: str) -> list[dict]:
    """Return successful items whose undo marker changed in one direction."""
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
            if (direction == "undo" and not was_undone and is_undone) or (direction == "redo" and was_undone and not is_undone):
                changed.append(item)
    return changed


def _history_change_description(items: list[dict]) -> str:
    """Format a compact, readable group/file description for INFO logs."""
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


def _json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length) if length else b"{}"
    return json.loads(raw.decode("utf-8"))


def _multipart_body(handler: BaseHTTPRequestHandler) -> dict[str, tuple[str, bytes] | str]:
    """Parse browser FormData without the removed Python 3.13 cgi module."""
    content_type = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        raise ValueError("需要 multipart/form-data 上传")
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length)
    envelope = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n"
        "\r\n"
    ).encode("utf-8") + raw
    message = BytesParser(policy=email_policy).parsebytes(envelope)
    fields: dict[str, tuple[str, bytes] | str] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        value = part.get_payload(decode=True) or b""
        if filename is not None:
            fields[name] = (filename, value)
        else:
            charset = part.get_content_charset() or "utf-8"
            fields[name] = value.decode(charset, errors="replace")
    return fields


def _send_json(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _send_bytes(handler: BaseHTTPRequestHandler, data: bytes, content_type: str) -> None:
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def _send_attachment(handler: BaseHTTPRequestHandler, data: bytes, filename: str,
                     content_type: str = "application/octet-stream") -> None:
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def _send_file_range(handler: BaseHTTPRequestHandler, path: Path, content_type: str) -> None:
    """Serve an audio file with byte-range support so media elements can seek."""
    size = path.stat().st_size
    range_header = handler.headers.get("Range", "").strip()
    start = 0
    end = size - 1
    partial = False

    if range_header:
        # Audio elements issue a single `bytes=start-end` range. Multi-range
        # requests are deliberately rejected because they are unnecessary here.
        if not range_header.lower().startswith("bytes=") or "," in range_header:
            _send_range_error(handler, size)
            return
        spec = range_header[6:].strip()
        first, separator, last = spec.partition("-")
        try:
            if not separator:
                raise ValueError
            if first:
                start = int(first)
                if start < 0 or start >= size:
                    raise ValueError
                end = int(last) if last else size - 1
                if end < start:
                    raise ValueError
                end = min(end, size - 1)
            else:
                suffix_length = int(last)
                if suffix_length <= 0 or size == 0:
                    raise ValueError
                start = max(size - suffix_length, 0)
                end = size - 1
        except (TypeError, ValueError):
            _send_range_error(handler, size)
            return
        partial = True

    length = max(0, end - start + 1)
    handler.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Length", str(length))
    handler.send_header("Cache-Control", "no-store")
    if partial:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.end_headers()

    if length == 0:
        return
    with path.open("rb") as stream:
        stream.seek(start)
        remaining = length
        while remaining:
            chunk = stream.read(min(64 * 1024, remaining))
            if not chunk:
                break
            handler.wfile.write(chunk)
            remaining -= len(chunk)


def _send_range_error(handler: BaseHTTPRequestHandler, size: int) -> None:
    handler.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
    handler.send_header("Content-Range", f"bytes */{size}")
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _pick_windows_folder() -> str:
    """Open the native Windows folder picker without requiring Tcl/Tk."""
    import ctypes
    from ctypes import wintypes

    class BrowseInfo(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", ctypes.c_void_p),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    owner = user32.GetForegroundWindow()

    browse = shell32.SHBrowseForFolderW
    browse.argtypes = [ctypes.POINTER(BrowseInfo)]
    browse.restype = ctypes.c_void_p
    get_path = shell32.SHGetPathFromIDListW
    get_path.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    get_path.restype = wintypes.BOOL
    free_pidl = ole32.CoTaskMemFree
    free_pidl.argtypes = [ctypes.c_void_p]
    co_initialize = ole32.CoInitialize
    co_initialize.argtypes = [ctypes.c_void_p]
    co_initialize.restype = ctypes.c_long
    co_uninitialize = ole32.CoUninitialize

    co_status = co_initialize(None)
    try:
        display_name = ctypes.create_unicode_buffer(260)
        flags = 0x0001 | 0x0010 | 0x0040  # RETURNONLYFSDIRS | EDITBOX | NEWDIALOGSTYLE
        display_name_ptr = ctypes.cast(display_name, wintypes.LPWSTR)
        info = BrowseInfo(owner, None, display_name_ptr, "选择根目录", flags, None, 0, 0)
        pidl = browse(ctypes.byref(info))
        if not pidl:
            return ""
        try:
            selected = ctypes.create_unicode_buffer(32768)
            return selected.value if get_path(pidl, selected) else ""
        finally:
            free_pidl(pidl)
    finally:
        if co_status in (0, 1):  # S_OK or S_FALSE both require uninitialization.
            co_uninitialize()


def _safe_audio_path(path_text: str) -> Path | None:
    with STATE.lock:
        if not STATE.root:
            return None
        try:
            candidate = Path(path_text).resolve()
            root = Path(STATE.root).resolve()
            if not candidate.is_relative_to(root) or not candidate.is_file():
                return None
            record = next((item for item in _all_records()
                           if Path(item.path).resolve() == candidate and not item.removed), None)
            mime_type = str(record.metadata.get("file", {}).get("mime_type", "")) if record else ""
            if mime_type.startswith("audio/"):
                return candidate
        except (OSError, ValueError):
            return None
    return None


def _audio_content_type(path: Path) -> str:
    with STATE.lock:
        record = next((item for item in _all_records()
                       if Path(item.path).resolve() == path and not item.removed), None)
        if record:
            mime_type = str(record.metadata.get("file", {}).get("mime_type", ""))
            if mime_type.startswith("audio/"):
                return mime_type
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _parse_int(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _normalise_directory_mapping(value) -> tuple[dict[str, int | None], bool]:
    """Validate mapping indexes sent by the WebUI.

    ``None``/``auto`` preserves the scheme-one last-three-directory defaults;
    numeric indexes use root-relative levels or negative end-relative levels.
    """
    if value is None or value == "" or value == "auto":
        return {"meta": -3, "group": -2, "child": -1}, True
    if not isinstance(value, dict):
        raise ValueError("目录层级映射格式无效")
    result: dict[str, int | None] = {}
    for field in ("meta", "group", "child"):
        raw = value.get(field)
        if raw in (None, "", "none"):
            result[field] = None
            continue
        try:
            index = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"目录映射 {field} 必须是整数或空值") from exc
        if index < -32 or index > 128:
            raise ValueError(f"目录映射 {field} 超出允许范围")
        result[field] = index
    return result, False


def _workflow_grouping_signature(workflow: dict) -> tuple[str, str]:
    grouping = workflow.get("grouping", {})
    if not isinstance(grouping, dict):
        grouping = {}
    return (
        str(grouping.get("mode", "extension") or "extension").strip().casefold(),
        str(grouping.get("filter", "all") or "all").strip().casefold(),
    )


def _record_resource_kind(record: FileRecord) -> str:
    extension = record.extension.casefold()
    mime_type = str(record.metadata.get("file", {}).get("mime_type", "")).casefold()
    if extension in {".mid", ".midi"}:
        return "midi"
    if mime_type.startswith("audio/"):
        return "audio"
    if mime_type.startswith("image/"):
        return "artwork"
    if extension in {".fxp", ".fxb", ".fst", ".vstpreset", ".aupreset", ".nksf"}:
        return "preset"
    if mime_type.startswith("text/") or extension in {
        ".pdf", ".doc", ".docx", ".rtf", ".md", ".xlsx", ".xls",
    }:
        return "document"
    return "other"


def _initial_extension_enabled(workflow: dict, result: Any) -> dict[str, bool]:
    resource_filter = workflow.get("resource_filter", {})
    included = {
        str(item).strip().casefold()
        for item in resource_filter.get("include", [])
    } if isinstance(resource_filter, dict) else set()
    skip_mismatch = (
        isinstance(resource_filter, dict)
        and str(resource_filter.get("on_mismatch", "include")).casefold() == "skip"
        and bool(included)
    )
    defaults: dict[str, bool] = {}
    for extension in result.extension_counts:
        matching_records = [record for record in result.records if record.extension.casefold() == extension.casefold()]
        defaults[extension.casefold()] = (
            not skip_mismatch
            or any(_record_resource_kind(record) in included for record in matching_records)
        )
    return defaults


def _apply_initial_extension_defaults(workflow: dict, result: Any) -> None:
    defaults = _initial_extension_enabled(workflow, result)
    for record in result.records:
        extension = record.extension.casefold()
        was_enabled = STATE.extension_enabled.get(extension, True)
        is_enabled = defaults.get(extension, True)
        if not is_enabled:
            if record.selected and not record.removed:
                STATE.extension_skipped.add(record.path)
            record.selected = False
        elif not was_enabled and record.path in STATE.extension_skipped:
            if not record.removed:
                record.selected = True
            STATE.extension_skipped.discard(record.path)
    STATE.extension_enabled = defaults


def _apply_workflow_grouping(result: Any, workflow: dict) -> Any:
    """Project a scan into the logical groups requested by a workflow."""
    mode, grouping_filter = _workflow_grouping_signature(workflow)
    if mode != "directory":
        return result

    records = result.records
    if grouping_filter == "image":
        records = [
            record for record in records
            if isinstance(record.metadata.get("image"), dict)
            and record.metadata["image"].get("available") is True
        ]

    groups: dict[str, NamingGroup] = {}
    for record in records:
        key = f"{record.relative_folder}\x1f{grouping_filter}"
        record.group_key = key
        group = groups.get(key)
        if group is None:
            default_meta, default_group, _default_child = directory_prefix_defaults(
                result.root, record.source_path.parent
            )
            group = NamingGroup(
                key,
                str(record.source_path.parent),
                record.folder_name,
                f".{grouping_filter}",
                prefix=default_group,
                relative_folder=record.relative_folder,
                meta_prefix=default_meta,
                extensions=[],
            )
            groups[key] = group
        group.records.append(record)
        if record.extension not in group.extensions:
            group.extensions.append(record.extension)

    for group in groups.values():
        group.records.sort(key=lambda item: natural_key(item.original_name))
        group.extensions.sort(key=natural_key)
    result.records = sorted(records, key=lambda item: (natural_key(item.relative_folder), natural_key(item.original_name)))
    result.groups = groups
    result.extension_counts = {
        extension: sum(1 for record in records if record.extension == extension)
        for extension in sorted({record.extension for record in records}, key=natural_key)
    }
    result.associations = refresh_stem_associations(records)
    return result


def _scan_for_workflow(root: str, include_hidden: bool, include_system: bool,
                       mapping: dict[str, int | None] | None,
                       workflow: dict) -> Any:
    result = scan_folder(
        root, include_hidden, include_system, mapping,
        metadata_reader=lambda path, root_path: read_workflow_metadata(workflow, path, root_path),
    )
    return _apply_workflow_grouping(result, workflow)


def _directory_source_values(folder: str | Path) -> dict[str, str]:
    mapping = None if STATE.directory_mapping_auto else STATE.directory_mapping
    meta, group, child = directory_prefix_defaults(STATE.root, folder, mapping)
    return {
        "directory.meta": meta,
        "directory.group": group,
        "directory.child": child,
    }


def _initial_field_value(definition: dict[str, Any], group: NamingGroup | None = None,
                         record: FileRecord | None = None) -> str:
    source = str(definition.get("initial_source", "") or "")
    if source == "stem" and record is not None:
        return str(record.base_name or record.stem)
    if source.startswith("directory."):
        folder = record.source_path.parent if record is not None else group.folder if group is not None else ""
        if folder:
            return str(_directory_source_values(folder).get(source, ""))
    return str(definition.get("default", "") or "")


def _remember_initial_value(record: FileRecord, definition: dict[str, Any], value: str) -> None:
    if definition.get("initial_source") == "stem":
        record.base_name = value
        record.name = value


def _apply_directory_mapping(mapping: dict[str, int | None], auto: bool) -> None:
    fields = workflow_field_map(_active_workflow())
    STATE.directory_mapping = mapping
    STATE.directory_mapping_auto = auto
    for group in STATE.groups.values():
        for field_id, definition in fields.items():
            if definition["scope"] == "group" and str(definition.get("initial_source", "")).startswith("directory."):
                group.workflow_values[field_id] = _initial_field_value(definition, group)
        for record in group.records:
            for field_id, definition in fields.items():
                if (definition["scope"] in {"record", "suffix"}
                        and str(definition.get("initial_source", "")).startswith("directory.")):
                    record.workflow_values[field_id] = _initial_field_value(definition, group, record)
                    record.workflow_manual_fields.discard(field_id)
                    record.workflow_auto_fields.add(field_id)
    current = STATE.current_group()
    current_record = current.records[0] if current and current.records else None
    for field_id, definition in fields.items():
        if definition["scope"] == "workflow" and str(definition.get("initial_source", "")).startswith("directory."):
            STATE.workflow_values[field_id] = _initial_field_value(definition, current, current_record)


def _path_value(value: Any, path: str) -> Any:
    current = value
    for part in str(path or "").split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _expression_value(expression: dict[str, Any], context: dict[str, Any]) -> Any:
    if "path" in expression:
        return _path_value(context, expression["path"])
    if "value" in expression:
        return expression["value"]
    operator = expression.get("op")
    values = [_expression_value(item, context) for item in expression.get("args", [])]
    if operator == "coalesce":
        return next((value for value in values if value is not None and str(value).strip()), None)
    if operator == "concat":
        return "".join(str(value) for value in values if value is not None)
    if operator in {"lower", "upper"}:
        if not values or values[0] is None:
            return None
        return str(values[0]).casefold() if operator == "lower" else str(values[0]).upper()
    if operator == "abs":
        return abs(_number(values[0])) if values and _number(values[0]) is not None else None
    if operator == "round":
        number = _number(values[0]) if values else None
        return round(number, int(expression.get("digits", 0))) if number is not None else None
    numbers = [_number(value) for value in values]
    if any(value is None for value in numbers) or not numbers:
        return None
    if operator == "add":
        return sum(numbers)
    if operator == "subtract":
        return numbers[0] - sum(numbers[1:])
    if operator == "multiply":
        result = 1.0
        for value in numbers:
            result *= value
        return result
    if operator == "divide":
        result = numbers[0]
        for value in numbers[1:]:
            if value == 0:
                return None
            result /= value
        return result
    if operator == "mod":
        if len(numbers) != 2 or numbers[1] == 0:
            return None
        return numbers[0] % numbers[1]
    if operator == "min":
        return min(numbers)
    if operator == "max":
        return max(numbers)
    return None


def _workflow_context(record: FileRecord, workflow: dict) -> dict[str, Any]:
    context = {
        "metadata": record.metadata,
        "record": {
            "name": record.name,
            "base_name": record.base_name,
            "stem": record.stem,
            "filename": record.original_name,
            "extension": record.extension.lstrip("."),
            "folder_name": record.folder_name,
            "relative_folder": record.relative_folder,
            "parsed_fields": record.parsed_fields,
        },
    }
    context["derived"] = {}
    for item in workflow.get("derived", []):
        context["derived"][item["id"]] = _expression_value(item["expression"], context)
    record.workflow_derived = context["derived"]
    return context


def _number(value: Any) -> float | None:
    try:
        if isinstance(value, bool) or value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _same_value(left: Any, right: Any) -> bool:
    left_number, right_number = _number(left), _number(right)
    if left_number is not None and right_number is not None:
        return left_number == right_number
    return str(left).casefold() == str(right).casefold()


def _condition_matches(condition: dict[str, Any], context: dict[str, Any]) -> bool:
    if "all" in condition:
        return all(_condition_matches(item, context) for item in condition["all"])
    if "any" in condition:
        return any(_condition_matches(item, context) for item in condition["any"])
    if "not" in condition:
        return not _condition_matches(condition["not"], context)
    actual = _path_value(context, condition.get("path", ""))
    operator = condition.get("op")
    if operator == "exists":
        return actual is not None and actual != ""
    expected = (_path_value(context, condition["value_from"])
                if condition.get("value_from") else condition.get("value"))
    if actual is None or expected is None:
        return operator == "not_equals" and actual is not None
    if operator == "equals":
        return _same_value(actual, expected)
    if operator == "not_equals":
        return not _same_value(actual, expected)
    if operator in {"contains", "starts_with", "ends_with"}:
        actual_text = str(actual or "").casefold()
        expected_text = str(expected or "").casefold()
        return {
            "contains": expected_text in actual_text,
            "starts_with": actual_text.startswith(expected_text),
            "ends_with": actual_text.endswith(expected_text),
        }[operator]
    if operator in {"in", "not_in"}:
        if not isinstance(expected, (list, tuple, set)):
            return operator == "not_in"
        matched = any(_same_value(actual, item) for item in expected)
        return matched if operator == "in" else not matched
    actual_number, expected_number = _number(actual), _number(expected)
    if actual_number is None or expected_number is None:
        return False
    return {
        "gt": actual_number > expected_number,
        "gte": actual_number >= expected_number,
        "lt": actual_number < expected_number,
        "lte": actual_number <= expected_number,
    }[operator]


def _workflow_action_value(action: dict[str, Any], context: dict[str, Any]) -> Any:
    if "value" in action:
        return action["value"]
    return _path_value(context, action.get("value_from", ""))


def _workflow_action_map(workflow: dict) -> dict[str, dict[str, Any]]:
    return {action["id"]: action for action in workflow.get("actions", [])}


def _append_workflow_suffix(name: str, value: Any, action: dict[str, Any], separator: str) -> str:
    text = str(name or "")
    value_text = str(value or "").strip()
    suffix = str(action.get("suffix", ""))
    if not value_text or not suffix:
        return text
    token = f"{value_text}{suffix}"
    if text.casefold().endswith(token.casefold()):
        return text
    joiner = str(action.get("separator") or separator)
    if text.casefold().endswith(value_text.casefold()):
        return f"{text[:-len(value_text)]}{token}"
    return f"{text}{joiner}{token}" if text else token


def _apply_workflow_rules(workflow: dict, record: FileRecord) -> None:
    context = _workflow_context(record, workflow)
    rules = sorted(workflow.get("rules", []), key=lambda rule: int(rule.get("priority", 0)), reverse=True)
    assigned: set[str] = set()
    fields = workflow_field_map(workflow)
    for rule in rules:
        if not _condition_matches(rule["when"], context):
            continue
        for action in rule["then"]:
            field_id = action["field"]
            value = _workflow_action_value(action, context)
            if value is None or str(value).strip() == "":
                continue
            value = str(value)
            value = normalise_workflow_value(workflow, field_id, value)
            candidates = record.workflow_candidates.setdefault(field_id, [])
            if value not in candidates:
                candidates.append(value)
            details = record.workflow_candidate_details.setdefault(field_id, [])
            detail = {
                "value": value,
                "rule_id": rule["id"],
                "reason": action.get("reason") or f"命中规则：{rule['id']}",
                "mode": action["mode"],
            }
            if not any(item.get("value") == value and item.get("rule_id") == rule["id"] for item in details):
                details.append(detail)
            if (action["mode"] == "assign" and field_id not in assigned
                    and field_id not in record.workflow_manual_fields
                    and (not record.workflow_values.get(field_id) or field_id in record.workflow_auto_fields)):
                record.workflow_values[field_id] = value
                record.workflow_auto_fields.add(field_id)
                assigned.add(field_id)


def _initialise_workflow_values() -> None:
    """Initialise canonical values from declarative workflow field sources."""
    workflow = _active_workflow()
    fields = workflow_field_map(workflow)
    current = STATE.current_group()
    current_record = current.records[0] if current and current.records else None
    STATE.workflow_values = {
        field_id: _initial_field_value(definition, current, current_record)
        for field_id, definition in fields.items()
        if definition["scope"] == "workflow"
    }
    STATE.workflow_candidates = {}
    suffix_modes = workflow.get("suffix_modes", {})
    suffix_field = str(workflow.get("suffix_field", "") or "")
    configured_suffix = STATE.workflow_values.get(suffix_field, "") if suffix_field else ""
    if configured_suffix not in suffix_modes:
        configured_suffix = "scale_bpm" if "scale_bpm" in suffix_modes else next(iter(suffix_modes), "")
    STATE.workflow_suffix_mode = configured_suffix
    for group in STATE.groups.values():
        group.workflow_values = {}
        group.workflow_candidates = {}
        for field_id, definition in fields.items():
            if definition["scope"] != "group":
                continue
            group.workflow_values[field_id] = _initial_field_value(definition, group)
        for record in group.records:
            record.workflow_values = {}
            record.workflow_candidates = {}
            record.workflow_candidate_details = {}
            record.workflow_derived = {}
            record.workflow_actions = set()
            record.workflow_auto_fields = set()
            record.workflow_number_fields = set()
            record.workflow_manual_fields = set()
            parsed_candidates = parse_workflow_filename(workflow, record.stem)
            parsed_fields = parsed_candidates.get("fields", {})
            if isinstance(parsed_fields, dict) and parsed_fields:
                # Expose workflow-owned parser results to declarative field
                # extractors such as ``record.parsed_fields.source``.
                for field_id in fields:
                    record.parsed_fields.pop(field_id, None)
                record.parsed_fields.update({
                    str(field_id): str(value)
                    for field_id, value in parsed_fields.items()
                    if str(value).strip()
                })
            sample_metadata = record.metadata.get("sample_pack", {})
            parsed_bpm = str(record.parsed_fields.get("bpm", "") or "")
            embedded_bpm = str(sample_metadata.get("bpm_metadata", "") or "") if isinstance(sample_metadata, dict) else ""
            if parsed_bpm and embedded_bpm and parsed_bpm != embedded_bpm:
                sample_metadata["bpm_warning"] = f"文件名 BPM {parsed_bpm} 与内部 metadata BPM {embedded_bpm} 冲突，优先采用文件名 token"
            for field_id, definition in fields.items():
                if definition["scope"] not in {"record", "suffix"}:
                    continue
                value = _initial_field_value(definition, group, record)
                # Extracted values are intentionally candidates only.  They do
                # not enter the target name until the user confirms them.
                extractors = list(definition.get("extractors", []))
                if definition.get("extractor"):
                    extractors.append(str(definition["extractor"]))
                candidates: list[str] = []
                for extractor in extractors:
                    candidate_value: Any = None
                    if extractor in {"stem", "filename"}:
                        candidate_value = record.stem
                    elif extractor == "extension" and record.extension:
                        candidate_value = record.extension.lstrip(".")
                    elif extractor == "parent" and record.folder_name:
                        candidate_value = record.folder_name
                    elif extractor == "relative_folder" and record.relative_folder:
                        candidate_value = record.relative_folder
                    elif extractor == "number":
                        candidate_value = parsed_candidates.get("fields", {}).get("number")
                    elif extractor and extractor.split(".", 1)[0] in {"metadata", "record", "derived"}:
                        candidate_value = _path_value(_workflow_context(record, workflow), extractor)
                    candidate = normalise_workflow_value(workflow, field_id, candidate_value)
                    if candidate.strip() and candidate not in candidates:
                        candidates.append(candidate)
                if candidates:
                    record.workflow_candidates[field_id] = candidates
                record.workflow_values[field_id] = str(value or "")
                parsed_value = normalise_workflow_value(workflow, field_id, record.parsed_fields.get(field_id, ""))
                if parsed_value and definition.get("autofill"):
                    record.workflow_values[field_id] = parsed_value
                    record.workflow_auto_fields.add(field_id)
                if record.workflow_values[field_id] and record.workflow_values[field_id] == str(definition.get("default", "") or ""):
                    record.workflow_auto_fields.add(field_id)
            _apply_workflow_rules(workflow, record)
        for field_id, definition in fields.items():
            if definition["scope"] not in {"workflow", "group"}:
                continue
            candidates: list[str] = []
            for record in group.records:
                value = normalise_workflow_value(workflow, field_id, record.parsed_fields.get(field_id, ""))
                if value.strip() and value not in candidates:
                    candidates.append(value)
            if not candidates:
                continue
            if definition["scope"] == "group":
                group.workflow_candidates[field_id] = candidates
            else:
                shared = STATE.workflow_candidates.setdefault(field_id, [])
                for value in candidates:
                    if value not in shared:
                        shared.append(value)


def _workflow_value(workflow: dict, group: NamingGroup, record: FileRecord,
                    field_id: str) -> str:
    definition = workflow_field_map(workflow).get(field_id, {})
    if definition.get("scope") == "workflow":
        return str(STATE.workflow_values.get(field_id, definition.get("default", "")) or "")
    if definition.get("scope") == "group":
        return str(group.workflow_values.get(field_id, definition.get("default", "")) or "")
    return str(record.workflow_values.get(field_id, definition.get("default", "")) or "")


def _workflow_profile(workflow: dict, group: NamingGroup, record: FileRecord) -> dict[str, Any] | None:
    profiles = {profile["id"]: profile for profile in workflow.get("profiles", [])}
    if not profiles:
        return None
    profile_field = str(workflow.get("profile_field", "") or "")
    profile_id = _workflow_value(workflow, group, record, profile_field) if profile_field else ""
    return profiles.get(profile_id) or profiles.get(str(workflow.get("default_profile", "") or ""))


def _compose_workflow_target(workflow: dict, group: NamingGroup, record: FileRecord) -> str:
    fields = workflow_field_map(workflow)
    actions = _workflow_action_map(workflow)
    profile = _workflow_profile(workflow, group, record)
    template = (
        [{"field": field_id} for field_id in profile.get("ordered_segments", [])]
        if profile else workflow.get("template", [])
    )
    has_literal = any(part.get("literal") is not None for part in template)
    values: list[str] = list(profile.get("fixed_prefix_tokens", [])) if profile else []
    for part in template:
        if part.get("field"):
            value = _workflow_value(workflow, group, record, part["field"])
            if not value.strip() and profile:
                value = str(profile.get("defaults", {}).get(part["field"], "") or "")
            if value.strip():
                values.append(value)
        elif has_literal:
            values.append(str(part.get("literal", "")))
    suffix_fields = list(workflow.get("suffix_modes", {}).get(STATE.workflow_suffix_mode, []))
    configured_actions = set(suffix_fields) & set(actions)
    suffix_fields.extend(action_id for action_id in actions if action_id not in configured_actions)
    template_field_ids = {part.get("field") for part in template}
    context = _workflow_context(record, workflow)
    for suffix_id in suffix_fields:
        if suffix_id in actions:
            if suffix_id not in record.workflow_actions:
                continue
            action = actions[suffix_id]
            value = _workflow_action_value(action, context)
            if value is None or str(value).strip() == "":
                value = record.workflow_values.get(action["field"], "")
            value = normalise_workflow_value(workflow, action["field"], value)
            if action["kind"] == "append_field_suffix" and value.strip():
                if values:
                    values[-1] = _append_workflow_suffix(values[-1], value, action, STATE.separator)
                else:
                    values.append(_append_workflow_suffix("", value, action, STATE.separator))
            continue
        if suffix_id not in fields:
            continue
        value = _workflow_value(workflow, group, record, suffix_id)
        if value.strip() and suffix_id not in template_field_ids:
            values.append(value)
    if profile:
        values.extend(str(token) for token in profile.get("fixed_suffix_tokens", []) if str(token).strip())
    if has_literal:
        stem = "".join(values).strip(STATE.separator)
    else:
        stem = STATE.separator.join(values)
        if STATE.separator:
            stem = re.sub(rf"(?:{re.escape(STATE.separator)}){{2,}}", STATE.separator, stem)
    return f"{stem or 'unnamed'}{normalise_ext(record.extension_original or record.extension)}"


def _append_conflict_suffix(target_name: str, number: int, width: int = 2) -> str:
    """Append a two-digit collision suffix immediately before the extension."""
    extension = Path(target_name).suffix
    stem = target_name[:-len(extension)] if extension else target_name
    return f"{stem}_{number:0{max(1, width)}d}{extension}"


def _target_path_key(path: Path) -> str:
    try:
        return str(path.resolve()).casefold()
    except OSError:
        return str(path.absolute()).casefold()


def _resolve_target_conflicts() -> None:
    """Make selected duplicate/existing targets unique in stable record order.

    The suffix is a preview-only disambiguator. Workflow fields remain unchanged
    so the generated `_01`/`_02` follows the final field (the wallpaper date)
    without becoming part of the user's metadata.
    """
    records = [record for record in _all_records() if record.selected and not record.removed]
    if not records:
        return
    selected_sources = {_target_path_key(Path(record.path)) for record in records}
    units: list[list[FileRecord]] = []
    unit_by_key: dict[tuple[str, str], list[FileRecord]] = {}
    for record in records:
        key = ("association", record.association_id) if record.association_id else ("record", str(id(record)))
        unit = unit_by_key.get(key)
        if unit is None:
            unit = []
            unit_by_key[key] = unit
            units.append(unit)
        unit.append(record)

    workflow = _active_workflow()
    collision = workflow.get("collision_suffix", {})
    collision_enabled = bool(collision.get("enabled", True))
    collision_width = max(1, int(collision.get("width", 2)))
    collision_start = max(1, int(collision.get("start", 1)))
    used_targets: set[str] = set()
    for unit in units:
        base_names = {
            id(record): (record.workflow_base_target_name or record.target_name)
            for record in unit
        }
        if not all(base_names.values()):
            continue
        sequence = (0,) if not collision_enabled else itertools.chain((0,), range(collision_start, 1000000))
        for number in sequence:
            names = {
                id(record): (base_names[id(record)] if number == 0
                             else _append_conflict_suffix(base_names[id(record)], number, collision_width))
                for record in unit
            }
            keys = [_target_path_key(Path(record.path).with_name(names[id(record)])) for record in unit]
            if len(keys) != len(set(keys)):
                continue
            if any(key in used_targets for key in keys):
                continue
            if any(Path(record.path).with_name(names[id(record)]).exists()
                   and key not in selected_sources
                   for record, key in zip(unit, keys)):
                continue
            for record in unit:
                record.target_name = names[id(record)]
            used_targets.update(keys)
            break
        else:
            for record in unit:
                record.status = "Conflict"
                record.status_detail = "目标名称重复且 collision_suffix 已禁用"

    for record in records:
        if record.status == "Conflict" and record.status_detail.startswith((
            "缺少工作流必填字段", "目标名称重复",
        )):
            continue
        syntax_error = validate_filename(record.target_name)
        record.status = "Conflict" if syntax_error else "Ready"
        record.status_detail = syntax_error or ""


def _assign_workflow_numbers(workflow: dict, group: NamingGroup,
                             overrides: dict[str, int] | None = None) -> None:
    definitions: list[tuple[str | None, dict[str, Any]]] = []
    numbering = workflow.get("numbering", {})
    if numbering.get("enabled"):
        definitions.append((None, numbering))
    for profile in workflow.get("profiles", []):
        profile_numbering = profile.get("numbering", {})
        if profile_numbering.get("enabled"):
            definitions.append((profile["id"], profile_numbering))
    for profile_id, definition in definitions:
        _assign_numbering_definition(workflow, group, definition, overrides, profile_id)


def _assign_numbering_definition(workflow: dict, group: NamingGroup, numbering: dict[str, Any],
                                 overrides: dict[str, int] | None = None,
                                 profile_id: str | None = None) -> None:
    field_id = str(numbering.get("field", ""))
    overrides = overrides or {}
    width = max(1, int(overrides.get("width", numbering.get("width", 2))))
    start = int(overrides.get("start", numbering.get("start", 1)))
    step = max(1, int(overrides.get("step", numbering.get("step", 1))))
    group_by = [str(item) for item in numbering.get("group_by", [])]
    eligible = [
        record for record in group.records
        if not record.removed and record.selected
        and (profile_id is None or (_workflow_profile(workflow, group, record) or {}).get("id") == profile_id)
    ]
    reserved: dict[tuple[str, ...], set[int]] = {}
    for record in eligible:
        key = tuple(_workflow_value(workflow, group, record, field) for field in group_by)
        value = record.workflow_values.get(field_id, "")
        if field_id in record.workflow_number_fields:
            record.workflow_values[field_id] = ""
            value = ""
        try:
            if value.strip():
                reserved.setdefault(key, set()).add(int(value))
        except ValueError:
            pass
    counters: dict[tuple[str, ...], int] = {}
    for record in eligible:
        key = tuple(_workflow_value(workflow, group, record, field) for field in group_by)
        value = record.workflow_values.get(field_id, "")
        if value.strip() and field_id not in record.workflow_number_fields:
            continue
        candidate = counters.get(key, start)
        used = reserved.setdefault(key, set())
        while candidate in used:
            candidate += step
        record.workflow_values[field_id] = str(candidate).zfill(width)
        record.workflow_auto_fields.add(field_id)
        record.workflow_number_fields.add(field_id)
        used.add(candidate)
        counters[key] = candidate + step


def _excel_field(workflow: dict, fields: dict[str, dict]) -> str:
    configured = str(workflow.get("excel_field", "") or "")
    if configured in fields and fields[configured].get("scope") in {"record", "suffix"}:
        return configured
    for candidate in ("name", "detail"):
        if candidate in fields and fields[candidate].get("scope") in {"record", "suffix"}:
            return candidate
    return ""


def _expand_workflow_excel_name(value: str, record: FileRecord,
                                row_values: dict[str, str]) -> str:
    workflow = _active_workflow()
    context = _workflow_context(record, workflow)
    placeholders = workflow.get("excel_placeholders", {})

    def replace(match: re.Match[str]) -> str:
        placeholder = match.group(1).casefold()
        direct = row_values.get(placeholder, "")
        if direct:
            return normalise_workflow_value(workflow, placeholder, direct)
        path = placeholders.get(placeholder, "")
        resolved = _path_value(context, path) if path else None
        return normalise_workflow_value(workflow, placeholder, resolved) if resolved is not None else ""

    expanded = re.sub(r"\{([a-zA-Z][a-zA-Z0-9_.-]*)\}", replace, value)
    expanded = re.sub(r"([ _.-])\1+", r"\1", expanded)
    return expanded.strip(" _-.")


def _prepare_group(group: NamingGroup) -> None:
    workflow = _active_workflow()
    fields = workflow_field_map(workflow)
    is_default = _workflow_is_default()
    numbering = workflow.get("numbering", {})
    numbering_mode = str(workflow.get("numbering_mode", "numeric" if is_default else "always"))
    profile_numbering_enabled = any(
        profile.get("numbering", {}).get("enabled") for profile in workflow.get("profiles", [])
    )
    numbering_active = (
        (numbering.get("enabled") or profile_numbering_enabled)
        and (numbering_mode == "always" or STATE.mode == "numeric")
    )

    if not numbering_active:
        for record in group.records:
            for field_id in list(record.workflow_number_fields):
                definition = fields.get(field_id)
                if definition:
                    record.workflow_values[field_id] = _initial_field_value(definition, group, record)
                record.workflow_number_fields.discard(field_id)
                record.workflow_auto_fields.discard(field_id)

    # The built-in default parser remains available, while the workflow value
    # stays authoritative and manual edits survive preview refreshes.
    if is_default:
        for record in group.records:
            record.name = record.base_name
        apply_filename_parse(
            group.records,
            STATE.parse_template,
            STATE.parse_use_name,
            parser=lambda stem, template: parse_workflow_filename(workflow, stem, template),
        )
    if ("name" in fields and fields["name"].get("scope") in {"record", "suffix"}
            and STATE.mode != "numeric" and STATE.parse_use_name):
        for record in group.records:
            if "name" not in record.workflow_manual_fields:
                record.workflow_values["name"] = record.name
                record.workflow_auto_fields.add("name")

    excel_field = _excel_field(workflow, fields)
    for record in group.records:
        if (STATE.mode != "excel" and record.excel_source and excel_field
                and excel_field not in record.workflow_manual_fields):
            record.workflow_values[excel_field] = _initial_field_value(fields[excel_field], group, record)

    if numbering_active and numbering_mode == "numeric" and numbering.get("enabled"):
        number_field = str(numbering.get("field", ""))
        if number_field in fields:
            for record in group.records:
                record.workflow_values[number_field] = ""
                record.workflow_number_fields.add(number_field)

    if numbering_active:
        overrides = {"start": STATE.numeric_start, "width": STATE.numeric_width, "step": STATE.numeric_step}
        _assign_workflow_numbers(workflow, group, overrides)

    excel_field = _excel_field(workflow, fields)
    if STATE.mode == "excel":
        mapping = STATE.excel_mappings.get(group.key, {})
        skipped = STATE.excel_skipped.get(group.key, set())
        for record in group.records:
            if record.path in mapping:
                if excel_field and excel_field not in record.workflow_manual_fields:
                    record.workflow_values[excel_field] = mapping[record.path]
                    record.workflow_auto_fields.discard(excel_field)
                record.selected = True
                record.status = "Ready"
                record.status_detail = ""
            elif record.path in skipped:
                record.selected = False
                record.status = "Skipped"
                record.status_detail = "Excel B 列为空"
            else:
                record.selected = False
                record.status = "未匹配"
                record.status_detail = "Excel 未提供名称"

    for record in group.records:
        record.workflow_base_target_name = _compose_workflow_target(workflow, group, record)
        record.target_name = record.workflow_base_target_name
        missing = [
            definition["label"] for field_id, definition in fields.items()
            if definition.get("required") and not _workflow_value(workflow, group, record, field_id).strip()
        ]
        if not STATE.extension_enabled.get(record.extension.casefold(), True):
            record.selected = False
            record.status = "Skipped"
            record.status_detail = "Extension not selected"
        elif not record.selected:
            record.status = "Skipped" if record.status != "未匹配" else record.status
            if not record.status_detail:
                record.status_detail = "Not selected"
        elif missing:
            record.status = "Conflict"
            record.status_detail = "缺少工作流必填字段: " + ", ".join(missing)
        else:
            syntax_error = validate_filename(record.target_name)
            record.status = "Conflict" if syntax_error else "Ready"
            record.status_detail = syntax_error or ""
    _resolve_target_conflicts()


def _all_records() -> list[FileRecord]:
    return [record for group in STATE.groups.values() for record in group.records]


def _expand_associated_records(records: list[FileRecord],
                               allowed_group_keys: set[str] | None = None) -> list[FileRecord]:
    """Synchronize target stems and include eligible cross-format siblings."""
    ordered = list(dict.fromkeys(id(record) for record in records))
    by_id = {id(record): record for record in records}
    result = [by_id[record_id] for record_id in ordered]
    all_records = _all_records()
    current_key = STATE.current_group_key
    handled: set[str] = set()

    for initial in list(result):
        association_id = initial.association_id
        if not association_id or association_id in handled:
            continue
        handled.add(association_id)
        candidates = [record for record in result if record.association_id == association_id]
        remembered_path = STATE.association_leaders.get(association_id, "")
        leader = next((record for record in all_records
                       if record.association_id == association_id and record.path == remembered_path), None)
        if leader is None:
            leader = next((record for record in candidates if record.group_key == current_key), candidates[0])
            STATE.association_leaders[association_id] = leader.path
        target_stem = Path(leader.workflow_base_target_name or leader.target_name).stem
        for related in all_records:
            if related.association_id != association_id or related.removed:
                continue
            if allowed_group_keys is not None and related.group_key not in allowed_group_keys:
                continue
            if not STATE.extension_enabled.get(related.extension.casefold(), True):
                continue
            already_requested = any(item is related for item in result)
            excel_inherited = STATE.mode == "excel" and related.status == "未匹配"
            if not already_requested and not related.selected and not excel_inherited:
                continue
            extension = related.extension_original or related.extension
            related.workflow_base_target_name = f"{target_stem}{extension}"
            related.target_name = related.workflow_base_target_name
            if excel_inherited:
                related.selected = True
                related.status = "Ready"
                related.status_detail = "继承关联文件的 Excel 名称"
            if not already_requested:
                result.append(related)
    _resolve_target_conflicts()
    return result


def _refresh_associations() -> None:
    records = _all_records()
    associations = refresh_stem_associations(records)
    if STATE.scan_result is not None:
        STATE.scan_result.associations = associations
    valid_ids = {association["id"] for association in associations}
    STATE.association_leaders = {
        association_id: path for association_id, path in STATE.association_leaders.items()
        if association_id in valid_ids
    }


def _mark_association_leader(record: FileRecord) -> None:
    if record.association_id:
        STATE.association_leaders[record.association_id] = record.path


def _leave_excel_mode() -> None:
    for record in _all_records():
        if record.removed:
            continue
        if record.status == "未匹配" or record.status_detail.startswith("Excel "):
            if STATE.extension_enabled.get(record.extension.casefold(), True):
                record.selected = True
                record.status = "Ready"
                record.status_detail = ""


def _reconcile_history_records(items: list[dict], direction: str) -> None:
    for item in items:
        destination = Path(item["old_path"] if direction == "undo" else item["new_path"])
        hints = {
            str(item.get("old_path", "")), str(item.get("new_path", "")),
            str(item.get("undo_source_path", "")), str(item.get("redo_source_path", "")),
        }
        group_key = str(item.get("group_key", ""))
        candidates = [record for record in _all_records()
                      if record.path in hints and (not group_key or record.group_key == group_key)]
        if not candidates and group_key in STATE.groups:
            candidates = [record for record in STATE.groups[group_key].records
                          if Path(record.path).name in {Path(path).name for path in hints if path}]
        if not candidates:
            continue
        record = candidates[0]
        record.path = str(destination)
        record.original_name = destination.name
        record.stem = destination.stem
        record.base_name = destination.stem
        record.name = destination.stem
        record.target_name = destination.name
        record.selected = True
        record.status = "Ready" if direction == "undo" else "Renamed"
        record.status_detail = ""
    _refresh_associations()


def _apply_record_update(payload: dict) -> FileRecord:
    path = str(payload.get("path", ""))
    with STATE.lock:
        for group in STATE.groups.values():
            for record in group.records:
                if record.path == path:
                    workflow = _active_workflow()
                    field_map = workflow_field_map(workflow)
                    workflow_values = payload.get("workflow_values")
                    workflow_changed = False
                    if isinstance(workflow_values, dict):
                        for field_id, value in workflow_values.items():
                            definition = field_map.get(str(field_id))
                            if definition and definition.get("scope") in {"record", "suffix"} and definition.get("editable", True):
                                normalised_value = normalise_workflow_value(workflow, str(field_id), value)
                                record.workflow_values[str(field_id)] = normalised_value
                                record.workflow_manual_fields.add(str(field_id))
                                record.workflow_auto_fields.discard(str(field_id))
                                record.workflow_number_fields.discard(str(field_id))
                                _remember_initial_value(record, definition, normalised_value)
                                workflow_changed = True
                    if workflow_changed:
                        _mark_association_leader(record)
                    if "selected" in payload:
                        record.selected = bool(payload["selected"])
                    if "removed" in payload:
                        record.removed = bool(payload["removed"])
                    return record
    raise KeyError("文件记录不存在")


class Handler(BaseHTTPRequestHandler):
    server_version = "OfflineFileNamerWeb/1.0"

    def log_message(self, format: str, *args) -> None:
        # Keep the console useful without duplicating every browser asset call.
        if self.path.startswith("/api/") and not self.path.startswith(("/api/client-heartbeat", "/api/client-closed")):
            super().log_message(format, *args)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._serve_asset("index.html", "text/html; charset=utf-8")
        elif parsed.path == "/tag-manager":
            self._serve_asset("tag-manager.html", "text/html; charset=utf-8")
        elif parsed.path.startswith("/assets/"):
            asset = parsed.path.removeprefix("/assets/")
            content_type = mimetypes.guess_type(asset)[0] or "text/plain"
            self._serve_asset(asset, f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        elif parsed.path == "/api/state":
            _send_json(self, {"ok": True, "state": _state_json()})
        elif parsed.path in {"/api/workflows", "/api/workflow"}:
            query = parse_qs(parsed.query)
            requested_id = query.get("workflow_id", query.get("id", [STATE.workflow_id]))[0]
            if requested_id == STATE.workflow_id:
                info = _workflow_state()
            else:
                selected = STATE.workflow_catalog.get(requested_id)
                info = {
                    "active_id": selected["id"],
                    "active": selected,
                    "available": [workflow_summary(item) for item in STATE.workflow_catalog.all()],
                    "values": {},
                }
            _send_json(self, {"ok": True, "workflows": info["available"], "active": info["active"], "active_id": info["active_id"], "workflow": info})
        elif parsed.path == "/api/workflow-values":
            query = parse_qs(parsed.query)
            workflow_id = query.get("workflow_id", query.get("id", [STATE.workflow_id]))[0]
            workflow = STATE.workflow_catalog.get(workflow_id)
            _send_json(self, {"ok": True, "data": WORKFLOW_VALUE_STORE.read(workflow)})
        elif parsed.path == "/api/config":
            _send_json(self, {"ok": True, "config": {
                "theme": STATE.workflow_catalog.theme,
                "current_workflow": STATE.workflow_id,
            }})
        elif parsed.path in {"/api/workflow-export", "/api/workflow/export"}:
            query = parse_qs(parsed.query)
            workflow_id = query.get("id", query.get("workflow_id", [STATE.workflow_id]))[0]
            workflow = STATE.workflow_catalog.get(workflow_id)
            filename = f"{workflow['id']}.ffnf-workflow"
            _send_attachment(self, package_workflow(workflow), filename, "application/zip")
        elif parsed.path == "/audio":
            query = parse_qs(parsed.query)
            path = _safe_audio_path(query.get("path", [""])[0])
            if not path:
                _send_json(self, {"ok": False, "error": "音频路径无效"}, 404)
                return
            try:
                _send_file_range(self, path, _audio_content_type(path))
            except OSError as exc:
                _send_json(self, {"ok": False, "error": str(exc)}, 404)
        else:
            _send_json(self, {"ok": False, "error": "Not found"}, 404)

    def _serve_asset(self, name: str, content_type: str) -> None:
        try:
            asset = (WEB_ROOT / name).resolve()
            if not asset.is_relative_to(WEB_ROOT.resolve()) or not asset.is_file():
                raise FileNotFoundError(name)
            _send_bytes(self, asset.read_bytes(), content_type)
        except OSError:
            _send_json(self, {"ok": False, "error": "资源不存在"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/client-heartbeat":
                self.server.client_heartbeat()
                _send_json(self, {"ok": True})
            elif path == "/api/client-closed":
                self.server.client_closed()
                _send_json(self, {"ok": True})
            elif path == "/api/scan":
                self._scan()
            elif path in {"/api/workflow/select", "/api/workflow"}:
                self._select_or_save_workflow()
            elif path in {"/api/workflow/import", "/api/workflow-import"}:
                self._import_workflow()
            elif path in {"/api/workflow/save", "/api/workflow-update"}:
                self._save_workflow()
            elif path == "/api/config":
                self._config()
            elif path in {"/api/workflow-value", "/api/workflow/field"}:
                self._workflow_value_update()
            elif path == "/api/workflow-values/tag":
                self._workflow_tag_update()
            elif path == "/api/select-group":
                payload = _json_body(self)
                with STATE.lock:
                    key = str(payload.get("key", ""))
                    if key not in STATE.groups:
                        raise KeyError("命名组不存在")
                    STATE.current_group_key = key
                _send_json(self, {"ok": True, "state": _state_json()})
            elif path == "/api/toggle-group":
                payload = _json_body(self)
                with STATE.lock:
                    key = str(payload.get("key", ""))
                    if key not in STATE.groups:
                        raise KeyError("命名组不存在")
                    if STATE.mode == "excel" and not _excel_group_ready(key):
                        raise ValueError("该命名组尚未导入 Excel 名称")
                    STATE.group_enabled[key] = not STATE.group_enabled.get(key, True)
                _send_json(self, {"ok": True, "state": _state_json()})
            elif path == "/api/preview":
                self._preview()
            elif path == "/api/directory-mapping":
                self._directory_mapping()
            elif path == "/api/parse-preview":
                self._parse_preview()
            elif path in {"/api/workflow-action", "/api/add-bpm-suffix"}:
                self._run_workflow_action()
            elif path in {"/api/workflow-fill", "/api/workflow/auto-fill"}:
                self._workflow_fill_candidates()
            elif path == "/api/record":
                _apply_record_update(_json_body(self))
                _send_json(self, {"ok": True, "state": _state_json()})
            elif path == "/api/records-batch":
                payload = _json_body(self)
                updates = payload.get("updates", [])
                if not isinstance(updates, list):
                    raise ValueError("批量记录更新格式无效")
                with STATE.lock:
                    changed_records: list[FileRecord] = []
                    for update in updates:
                        if not isinstance(update, dict):
                            raise ValueError("批量记录更新项无效")
                        record = _apply_record_update(update)
                        if update.get("removed"):
                            record.selected = False
                            record.status = "Skipped"
                            record.status_detail = "Removed from this task"
                        changed_records.append(record)
                    if any(record.removed for record in changed_records):
                        _refresh_associations()
                _send_json(self, {"ok": True, "updated": len(changed_records), "state": _state_json()})
            elif path == "/api/reorder":
                self._reorder()
            elif path == "/api/remove":
                payload = _json_body(self)
                with STATE.lock:
                    record = _apply_record_update({"path": payload.get("path"), "selected": False, "removed": True})
                    record.status = "Skipped"
                    record.status_detail = "Removed from this task"
                    _refresh_associations()
                _send_json(self, {"ok": True, "state": _state_json()})
            elif path == "/api/import-excel":
                self._import_excel()
            elif path == "/api/rename":
                self._rename()
            elif path == "/api/undo":
                self._undo()
            elif path in {"/api/redo", "/api/restore"}:
                self._redo()
            elif path == "/api/export":
                self._export()
            elif path == "/api/export-scan":
                self._export_scan()
            elif path == "/api/open-root":
                payload = _json_body(self)
                root = str(payload.get("root", STATE.root)).strip()
                if not root or not Path(root).is_dir():
                    raise ValueError("根目录不存在")
                open_in_explorer(root)
                _send_json(self, {"ok": True})
            elif path == "/api/pick-folder":
                self._pick_folder()
            else:
                _send_json(self, {"ok": False, "error": "Not found"}, 404)
        except Exception as exc:
            with STATE.lock:
                STATE.log("ERROR", str(exc))
            _send_json(self, {"ok": False, "error": str(exc), "state": _state_json()}, 400)

    def _select_or_save_workflow(self) -> None:
        payload = _json_body(self)
        workflow_value = payload.get("workflow")
        if workflow_value is not None or "fields" in payload:
            self._save_workflow_payload(workflow_value if isinstance(workflow_value, dict) else payload)
            return
        workflow_id = str(payload.get("id", payload.get("workflow_id", ""))).strip()
        if not workflow_id:
            raise ValueError("请选择工作流")
        with STATE.lock:
            workflow = _activate_workflow(workflow_id)
            STATE.log("INFO", f"已切换工作流：{workflow['name']}。当前任务预览已更新，已执行的重命名不会改变。")
        _send_json(self, {"ok": True, "workflow": _workflow_state(), "state": _state_json()})

    def _workflow_tag_update(self) -> None:
        payload = _json_body(self)
        workflow_id = str(payload.get("workflow_id", STATE.workflow_id)).strip()
        field_id = str(payload.get("field_id", "")).strip()
        workflow = STATE.workflow_catalog.get(workflow_id)
        action = str(payload.get("action", "upsert")).strip().casefold()
        if action == "toggle":
            data = WORKFLOW_VALUE_STORE.toggle(workflow, field_id, str(payload.get("tag_id", "")).strip())
        elif action == "delete":
            data = WORKFLOW_VALUE_STORE.delete(workflow, field_id, str(payload.get("tag_id", "")).strip())
        else:
            value = payload.get("tag", {})
            if not isinstance(value, dict):
                raise ValueError("标签数据格式无效")
            data = WORKFLOW_VALUE_STORE.upsert(workflow, field_id, value)
        _send_json(self, {"ok": True, "data": data})

    def _save_workflow_payload(self, payload: dict) -> None:
        workflow = validate_workflow(payload, allow_builtin=False)
        if workflow["id"] in BUILTIN_WORKFLOWS:
            raise ValueError("内置工作流不可直接覆盖，请先导入为副本")
        with STATE.lock:
            STATE.workflow_catalog.user_workflows[workflow["id"]] = workflow
            STATE.workflow_catalog.current_workflow = workflow["id"]
            STATE.workflow_catalog.save()
            _activate_workflow(workflow["id"], persist=False)
            STATE.log("INFO", f"已保存工作流：{workflow['name']}。")
        _send_json(self, {"ok": True, "workflow": _workflow_state(), "state": _state_json()})

    def _save_workflow(self) -> None:
        payload = _json_body(self)
        workflow = payload.get("workflow", payload)
        if not isinstance(workflow, dict):
            raise ValueError("工作流保存格式无效")
        self._save_workflow_payload(workflow)

    def _import_workflow(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        strategy = "copy"
        if "multipart/form-data" in content_type:
            form = _multipart_body(self)
            upload = form.get("file")
            if not isinstance(upload, tuple) or not upload[1]:
                raise ValueError("未选择工作流文件")
            filename, data = upload
            strategy = str(form.get("strategy", "copy") or "copy")
        else:
            payload = _json_body(self)
            filename = str(payload.get("filename", "workflow.json"))
            source = payload.get("workflow", payload)
            data = json.dumps(source, ensure_ascii=False).encode("utf-8")
            strategy = str(payload.get("strategy", "copy") or "copy")
        if strategy not in {"copy", "replace", "cancel"}:
            raise ValueError("导入策略必须是 copy、replace 或 cancel")
        workflow = load_workflow_package(data, filename)
        with STATE.lock:
            imported, existed = STATE.workflow_catalog.upsert_import(workflow, strategy)
            _activate_workflow(imported["id"], persist=False)
            STATE.log("INFO", f"已导入工作流：{imported['name']}。")
        _send_json(self, {
            "ok": True,
            "imported": workflow_summary(imported),
            "replaced": bool(existed and strategy == "replace"),
            "copied": bool(existed and strategy != "replace"),
            "workflow": _workflow_state(),
            "state": _state_json(),
        })

    def _config(self) -> None:
        payload = _json_body(self)
        with STATE.lock:
            if "theme" in payload:
                theme = str(payload["theme"])
                if theme not in {"light", "dark"}:
                    raise ValueError("主题必须是 light 或 dark")
                STATE.workflow_catalog.theme = theme
            if payload.get("workflow_id"):
                _activate_workflow(str(payload["workflow_id"]))
            STATE.workflow_catalog.save()
        _send_json(self, {"ok": True, "config": {
            "theme": STATE.workflow_catalog.theme,
            "current_workflow": STATE.workflow_id,
        }, "state": _state_json()})

    def _workflow_value_update(self) -> None:
        payload = _json_body(self)
        field_id = str(payload.get("field", payload.get("field_id", ""))).strip()
        value = str(payload.get("value", "") or "")
        workflow = _active_workflow()
        definition = workflow_field_map(workflow).get(field_id)
        if not definition:
            raise KeyError(f"工作流字段不存在: {field_id}")
        if not definition.get("editable", True):
            raise ValueError(f"工作流字段不可编辑: {field_id}")
        scope = definition.get("scope")
        if scope == "workflow":
            suffix_modes = workflow.get("suffix_modes", {})
            if field_id == workflow.get("suffix_field") and value not in suffix_modes:
                raise ValueError("当前工作流不支持该后缀模式")
            with STATE.lock:
                STATE.workflow_values[field_id] = value
                if field_id == workflow.get("suffix_field"):
                    STATE.workflow_suffix_mode = value
                for group in STATE.groups.values():
                    _prepare_group(group)
                _expand_associated_records([
                    record for record in _all_records() if record.selected and not record.removed
                ])
                STATE.log("INFO", f"已更新工作流字段：{definition.get('label', field_id)}。")
            _send_json(self, {"ok": True, "field": field_id, "state": _state_json()})
            return

        group_key = str(payload.get("group_key") or STATE.current_group_key or "")
        path = str(payload.get("path", ""))
        with STATE.lock:
            group = STATE.groups.get(group_key)
            if not group:
                raise KeyError("命名组不存在")
            if definition["scope"] == "group":
                group.workflow_values[field_id] = value
            else:
                record = next((item for item in group.records if item.path == path), None)
                if not record:
                    raise KeyError("文件记录不存在")
                normalised_value = normalise_workflow_value(workflow, field_id, value)
                record.workflow_values[field_id] = normalised_value
                record.workflow_manual_fields.add(field_id)
                record.workflow_auto_fields.discard(field_id)
                record.workflow_number_fields.discard(field_id)
                _remember_initial_value(record, definition, normalised_value)
                _mark_association_leader(record)
            _prepare_group(group)
            _expand_associated_records([record for record in group.records if record.selected and not record.removed])
            STATE.log("INFO", f"已更新工作流字段：{definition.get('label', field_id)}。")
        _send_json(self, {"ok": True, "field": field_id, "state": _state_json()})

    def _workflow_fill_candidates(self) -> None:
        """Apply the first automatic candidate for every untouched record field."""
        payload = _json_body(self)
        requested_fields = payload.get("fields", [])
        if requested_fields is None:
            requested_fields = []
        if not isinstance(requested_fields, list):
            raise ValueError("fields 必须是数组")
        requested = {str(field_id).strip() for field_id in requested_fields if str(field_id).strip()}
        workflow = _active_workflow()
        fields = workflow_field_map(workflow)
        unknown = requested - set(fields)
        if unknown:
            raise KeyError(f"工作流字段不存在: {', '.join(sorted(unknown))}")
        filled = 0
        filled_fields: set[str] = set()
        with STATE.lock:
            for field_id, candidates in STATE.workflow_candidates.items():
                if requested and field_id not in requested:
                    continue
                definition = fields.get(field_id, {})
                current = str(STATE.workflow_values.get(field_id, "") or "")
                if len(candidates) != 1 or (current and current != str(definition.get("default", "") or "")):
                    continue
                STATE.workflow_values[field_id] = candidates[0]
                filled += 1
                filled_fields.add(field_id)
            for group in STATE.groups.values():
                for field_id, candidates in group.workflow_candidates.items():
                    if requested and field_id not in requested:
                        continue
                    definition = fields.get(field_id, {})
                    current = str(group.workflow_values.get(field_id, "") or "")
                    if len(candidates) != 1 or (current and current != str(definition.get("default", "") or "")):
                        continue
                    group.workflow_values[field_id] = candidates[0]
                    filled += 1
                    filled_fields.add(field_id)
                for record in group.records:
                    if record.removed:
                        continue
                    for field_id, candidates in record.workflow_candidates.items():
                        if requested and field_id not in requested:
                            continue
                        if field_id not in fields or fields[field_id].get("scope") not in {"record", "suffix"}:
                            continue
                        if field_id in record.workflow_manual_fields or not candidates:
                            continue
                        current = str(record.workflow_values.get(field_id, "") or "").strip()
                        if current and field_id not in record.workflow_auto_fields:
                            continue
                        value = normalise_workflow_value(workflow, field_id, candidates[0])
                        if not value or value == current:
                            continue
                        record.workflow_values[field_id] = value
                        record.workflow_auto_fields.add(field_id)
                        filled += 1
                        filled_fields.add(field_id)
                _prepare_group(group)
            _expand_associated_records([
                record for record in _all_records() if record.selected and not record.removed
            ])
            STATE.log("INFO", f"已填充工作流自动值：{filled} 个字段。")
        _send_json(self, {"ok": True, "filled": filled, "fields": sorted(filled_fields), "state": _state_json()})

    def _scan(self) -> None:
        payload = _json_body(self)
        root = str(payload.get("root", "")).strip()
        if not root:
            raise ValueError("请选择根目录")
        include_hidden = bool(payload.get("include_hidden", False))
        include_system = bool(payload.get("include_system", False))
        mapping, mapping_auto = _normalise_directory_mapping(payload.get("directory_mapping"))
        active_workflow = _active_workflow()
        result = _scan_for_workflow(
            root, include_hidden, include_system, None if mapping_auto else mapping,
            active_workflow,
        )
        with STATE.lock:
            STATE.root = result.root
            STATE.scan_result = result
            STATE.groups = result.groups
            STATE.current_group_key = next(iter(result.groups), None)
            STATE.include_hidden = include_hidden
            STATE.include_system = include_system
            STATE.directory_mapping = mapping
            STATE.directory_mapping_auto = mapping_auto
            STATE.group_enabled = {key: True for key in result.groups}
            STATE.extension_skipped.clear()
            _apply_initial_extension_defaults(active_workflow, result)
            STATE.excel_mappings.clear()
            STATE.excel_skipped.clear()
            STATE.association_leaders.clear()
            STATE.parse_template = "auto"
            STATE.parse_use_name = False
            if STATE.mode not in active_workflow.get("name_modes", ["original"]):
                STATE.mode = active_workflow.get("name_modes", ["original"])[0]
            _initialise_workflow_values()
            for group in STATE.groups.values():
                _prepare_group(group)
            STATE.log("INFO", f"扫描完成：{len(result.records)} 个文件，{len(result.groups)} 个命名组，{len(result.extension_counts)} 种扩展名。")
            if result.skipped:
                STATE.log("INFO", f"已忽略 {len(result.skipped)} 个生成的表格或不可读条目。")
        _send_json(self, {"ok": True, "state": _state_json()})

    def _preview(self) -> None:
        payload = _json_body(self)
        with STATE.lock:
            if payload.get("group_key"):
                key = str(payload["group_key"])
                if key not in STATE.groups:
                    raise KeyError("命名组不存在")
                STATE.current_group_key = key
            STATE.separator = str(payload.get("separator", STATE.separator))
            previous_mode = STATE.mode
            requested_mode = str(payload.get("mode", STATE.mode))
            if previous_mode == "excel" and requested_mode != "excel":
                _leave_excel_mode()
            active_workflow = _active_workflow()
            if requested_mode not in active_workflow.get("name_modes", ["original"]):
                raise ValueError("当前工作流不支持该名称模式")
            STATE.mode = requested_mode
            if "suffix_mode" in payload:
                requested_suffix = str(payload.get("suffix_mode") or "")
                if requested_suffix and requested_suffix not in active_workflow.get("suffix_modes", {}):
                    raise ValueError("当前工作流不支持该后缀模式")
                STATE.workflow_suffix_mode = requested_suffix
                suffix_definition = workflow_field_map(active_workflow).get(active_workflow.get("suffix_field", ""))
                if suffix_definition and suffix_definition.get("scope") == "workflow":
                    STATE.workflow_values[active_workflow.get("suffix_field", "")] = requested_suffix
            if "directory_mapping" in payload:
                mapping, auto = _normalise_directory_mapping(payload.get("directory_mapping"))
                _apply_directory_mapping(mapping, auto)
            if "parse_template" in payload:
                STATE.parse_template = str(payload.get("parse_template") or "auto").strip() or "auto"
            if "parse_use_name" in payload:
                STATE.parse_use_name = bool(payload.get("parse_use_name"))
            if "extensions" in payload:
                selected_extensions = {str(ext).casefold() for ext in payload.get("extensions", [])}
                known_extensions = {
                    record.extension.casefold()
                    for record in (STATE.scan_result.records if STATE.scan_result else [])
                }
                for record in (STATE.scan_result.records if STATE.scan_result else []):
                    extension = record.extension.casefold()
                    was_enabled = STATE.extension_enabled.get(extension, True)
                    is_enabled = extension in selected_extensions
                    if not is_enabled:
                        was_selected = record.selected
                        record.selected = False
                        if not record.removed and was_selected:
                            STATE.extension_skipped.add(record.path)
                    elif not was_enabled and record.path in STATE.extension_skipped:
                        if not record.removed:
                            record.selected = True
                        STATE.extension_skipped.discard(record.path)
                STATE.extension_enabled = {
                    extension: extension in selected_extensions
                    for extension in known_extensions
                }
            numeric = payload.get("numeric") or {}
            STATE.numeric_start = _parse_int(numeric.get("start"), STATE.numeric_start)
            STATE.numeric_width = _parse_int(numeric.get("width"), STATE.numeric_width)
            STATE.numeric_step = _parse_int(numeric.get("step"), STATE.numeric_step)
            group = STATE.current_group()
            if group:
                association_input_changed = previous_mode != STATE.mode
                for prepared_group in STATE.groups.values():
                    _prepare_group(prepared_group)
                if association_input_changed:
                    for record in group.records:
                        if record.selected and not record.removed:
                            _mark_association_leader(record)
                _expand_associated_records([record for record in group.records if record.selected and not record.removed])
                STATE.log("INFO", f"已更新 {group.label} 的目标文件名预览。")
        _send_json(self, {"ok": True, "state": _state_json()})

    def _directory_mapping(self) -> None:
        payload = _json_body(self)
        mapping, auto = _normalise_directory_mapping(payload.get("mapping"))
        with STATE.lock:
            if not STATE.root or not STATE.groups:
                raise ValueError("请先扫描根目录")
            _apply_directory_mapping(mapping, auto)
            STATE.association_leaders.clear()
            for group in STATE.groups.values():
                _prepare_group(group)
            _expand_associated_records([record for record in _all_records()
                                        if record.selected and not record.removed])
            STATE.log("INFO", "已应用目录层级映射：自动末端三级" if auto else f"已应用目录层级映射：{mapping}")
        _send_json(self, {"ok": True, "state": _state_json()})

    def _parse_preview(self) -> None:
        payload = _json_body(self)
        template = str(payload.get("template") or "auto").strip() or "auto"
        with STATE.lock:
            group_key = str(payload.get("group_key") or STATE.current_group_key or "")
            group = STATE.groups.get(group_key)
            if not group:
                raise KeyError("命名组不存在")
            workflow = _active_workflow()
            STATE.parse_template = template
            STATE.parse_use_name = bool(payload.get("use_name", False))
            apply_filename_parse(
                group.records,
                template,
                STATE.parse_use_name,
                parser=lambda stem, parse_template: parse_workflow_filename(
                    workflow, stem, parse_template
                ),
            )
            _prepare_group(group)
            if STATE.parse_use_name:
                for record in group.records:
                    if record.selected and not record.removed:
                        _mark_association_leader(record)
            _expand_associated_records([record for record in group.records if record.selected and not record.removed])
            values = [{
                "path": record.path,
                "original_name": record.original_name,
                "fields": record.parsed_fields,
                "unmatched": record.parse_unmatched,
                "confidence": record.parse_confidence,
                "error": record.parse_error,
            } for record in group.records]
            STATE.log("INFO", f"已完成 {group.label} 的文件名解析预览（模板：{template}）。")
            error_count = sum(1 for record in group.records if record.parse_error or record.parse_confidence <= 0)
            if error_count:
                STATE.log("WARN", f"文件名解析有 {error_count} 个文件未能完整匹配模板。")
        _send_json(self, {"ok": True, "parsed": values, "state": _state_json()})

    def _run_workflow_action(self) -> None:
        payload = _json_body(self)
        with STATE.lock:
            group_key = str(payload.get("group_key") or STATE.current_group_key or "")
            group = STATE.groups.get(group_key)
            if not group:
                raise KeyError("命名组不存在")
            workflow = _active_workflow()
            actions = _workflow_action_map(workflow)
            action_id = str(payload.get("action_id", "")).strip()
            if not action_id and urlparse(self.path).path == "/api/add-bpm-suffix":
                action_id = next(iter(actions), "")
            action = actions.get(action_id)
            if not action:
                raise KeyError(f"工作流 action 不存在: {action_id}")
            added = 0
            missing = 0
            for record in group.records:
                if record.removed:
                    continue
                context = _workflow_context(record, workflow)
                value = _workflow_action_value(action, context)
                if value is None or str(value).strip() == "":
                    value = record.workflow_values.get(action["field"], "")
                value = normalise_workflow_value(workflow, action["field"], value)
                if not value.strip():
                    missing += 1
                    continue
                record.workflow_values[action["field"]] = value
                record.workflow_manual_fields.add(action["field"])
                if action_id not in record.workflow_actions:
                    record.workflow_actions.add(action_id)
                    added += 1
                _mark_association_leader(record)
            STATE.current_group_key = group.key
            _prepare_group(group)
            _expand_associated_records([record for record in group.records if record.selected and not record.removed])
            STATE.log("INFO", f"已执行工作流动作：{action['label']}，应用 {added} 个，缺少值 {missing} 个。")
        _send_json(self, {"ok": True, "action": action, "added": added, "missing": missing, "state": _state_json()})

    def _reorder(self) -> None:
        payload = _json_body(self)
        order = [str(path) for path in payload.get("paths", [])]
        with STATE.lock:
            group = STATE.current_group()
            if not group:
                raise KeyError("没有当前命名组")
            by_path = {record.path: record for record in group.records}
            if set(order) != set(by_path):
                raise ValueError("排序数据与当前命名组不一致")
            group.records[:] = [by_path[path] for path in order]
            STATE.log("INFO", "已保存当前命名组的手动顺序。")
        _send_json(self, {"ok": True, "state": _state_json()})

    def _import_excel(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise ValueError("需要上传 XLSX 文件")
        form = _multipart_body(self)
        upload = form.get("file")
        if not isinstance(upload, tuple) or not upload[0]:
            raise ValueError("未选择 XLSX 文件")
        with STATE.lock:
            group_key = str(form.get("group_key", STATE.current_group_key or ""))
            group = STATE.groups.get(group_key)
            if not group:
                raise KeyError("命名组不存在")
            filename, upload_data = upload
            suffix = Path(filename).suffix or ".xlsx"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp:
                temp.write(upload_data)
                temp_path = Path(temp.name)
            try:
                requested_sheet = str(form.get("sheet_name", "")).strip() or None
                match: ExcelMatchResult = import_xlsx(
                    temp_path, group, requested_sheet, _expand_workflow_excel_name
                )
            finally:
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            STATE.excel_mappings[group.key] = match.mapping
            STATE.excel_skipped[group.key] = {record.path for record in match.matched_without_name}
            STATE.mode = "excel"
            STATE.group_enabled[group.key] = True
            _prepare_group(group)
            for record in group.records:
                if record.path in match.mapping:
                    _mark_association_leader(record)
            _expand_associated_records([record for record in group.records if record.selected and not record.removed])
            detail_label = f"，工作表 {match.sheet_name}" if match.sheet_name else ""
            STATE.log("INFO", f"Excel 匹配完成：成功 {match.matched_count}，未匹配文件 {len(match.unmatched_files)}，未匹配行 {len(match.unmatched_rows)}{detail_label}。")
            for warning in match.warnings:
                STATE.log("WARN", warning.removeprefix("WARN "))
            preview = {"matched": match.matched_count, "unmatched_files": len(match.unmatched_files),
                       "unmatched_rows": len(match.unmatched_rows), "warnings": match.warnings,
                       "sheet": match.sheet_name, "detail": match.detail_mode}
        _send_json(self, {"ok": True, "match": preview, "state": _state_json()})

    def _rename(self) -> None:
        payload = _json_body(self)
        scope = str(payload.get("scope", "group"))
        with STATE.lock:
            if not STATE.groups:
                raise ValueError("请先扫描根目录")
            requested_root = str(payload.get("root", STATE.root)).strip()
            if requested_root and Path(requested_root).expanduser().resolve() != Path(STATE.root).expanduser().resolve():
                raise ValueError("根目录已修改，请先重新扫描再执行重命名")
            operations: list[RenameOperation] = []
            if scope == "single":
                path = str(payload.get("path", ""))
                record = next((record for group in STATE.groups.values() for record in group.records if record.path == path), None)
                if not record:
                    raise KeyError("文件记录不存在")
                _prepare_group(STATE.groups[record.group_key])
                record.selected = True
                linked = _expand_associated_records([record])
                operations.append(execute_rename(linked, STATE.history_path, kind="single", separator=STATE.separator))
            elif scope == "group":
                group = STATE.current_group()
                if group is not None:
                    _prepare_group(group)
                    selected = [record for record in group.records if record.selected and not record.removed]
                    linked = _expand_associated_records(selected)
                    operations.append(execute_rename(linked, STATE.history_path, kind="batch", separator=STATE.separator))
            else:
                enabled = {key for key in STATE.groups if _group_enabled_for_execution(key)}
                groups = [group for key, group in STATE.groups.items() if key in enabled]
                current = STATE.current_group()
                if current in groups:
                    groups.remove(current)
                    groups.insert(0, current)
                for group in groups:
                    _prepare_group(group)
                processed: set[int] = set()
                for group in groups:
                    selected = [record for record in group.records
                                if record.selected and not record.removed and id(record) not in processed]
                    if not selected:
                        continue
                    linked = [record for record in _expand_associated_records(selected, enabled)
                              if id(record) not in processed]
                    processed.update(id(record) for record in linked)
                    operations.append(execute_rename(
                        linked, STATE.history_path, kind="batch", separator=STATE.separator,
                        write_history=False,
                    ))
                combined_items = [item for operation in operations for item in operation.items]
                if combined_items and any(item.success for item in combined_items):
                    combined = RenameOperation(
                        datetime.now().astimezone().isoformat(timespec="seconds"),
                        "batch",
                        combined_items,
                    )
                    append_history(STATE.history_path, combined)
            _refresh_associations()
            success = sum(item.success for operation in operations for item in operation.items)
            failed = sum(not item.success for operation in operations for item in operation.items)
            if failed:
                STATE.log("ERROR", f"重命名完成：成功 {success}，失败/冲突 {failed}。")
            else:
                STATE.log("INFO", f"重命名完成：成功 {success} 个文件。")
            statuses = [operation.transaction_status for operation in operations]
            if any(status == "rolled_back" for status in statuses):
                STATE.log("WARN", "事务式重命名：部分命名组执行失败，已尝试自动回滚。")
            elif statuses and all(status == "committed" for status in statuses):
                STATE.log("INFO", "事务式重命名：已提交所有无冲突命名组。")
        _send_json(self, {"ok": True, "success": success, "failed": failed, "state": _state_json()})

    def _undo(self) -> None:
        with STATE.lock:
            before = _read_history_snapshot(STATE.history_path)
            ok, errors = undo_last(STATE.history_path)
            changed = _changed_history_items(
                before, _read_history_snapshot(STATE.history_path), "undo"
            )
            if changed:
                _reconcile_history_records(changed, "undo")
            if changed:
                suffix = "" if ok else "（部分成功）"
                STATE.log("INFO", f"最近一次重命名已撤销{suffix}：{_history_change_description(changed)}。")
            elif ok:
                STATE.log("INFO", "最近一次重命名已撤销。")
            for error in errors:
                STATE.log("ERROR", error)
        _send_json(self, {"ok": ok, "errors": errors, "state": _state_json()})

    def _redo(self) -> None:
        with STATE.lock:
            before = _read_history_snapshot(STATE.history_path)
            ok, errors = redo_last(STATE.history_path)
            changed = _changed_history_items(
                before, _read_history_snapshot(STATE.history_path), "redo"
            )
            if changed:
                _reconcile_history_records(changed, "redo")
            if changed:
                suffix = "" if ok else "（部分成功）"
                STATE.log("INFO", f"最近一次撤销的重命名已还原{suffix}：{_history_change_description(changed)}。")
            elif ok:
                STATE.log("INFO", "最近一次撤销的重命名已还原。")
            for error in errors:
                STATE.log("ERROR", error)
        _send_json(self, {"ok": ok, "errors": errors, "state": _state_json()})

    def _export(self) -> None:
        payload = _json_body(self)
        root = str(payload.get("root", STATE.root)).strip()
        selected = payload.get("extensions", [])
        root_path = Path(root).expanduser().resolve()
        filetree_existed = (root_path / "filetree.txt").exists()
        active_workflow = _active_workflow()
        outputs = export_filename_tables(
            root, selected, bool(payload.get("include_hidden", False)),
            bool(payload.get("include_system", False)),
            metadata_reader=lambda path, root_path: read_workflow_metadata(active_workflow, path, root_path),
        )
        export_stats = collect_directory_statistics(root, include_hidden=bool(payload.get("include_hidden", False)), include_system=bool(payload.get("include_system", False)))
        with STATE.lock:
            xlsx_outputs = [output for output in outputs if output.suffix.casefold() == ".xlsx"]
            filetree_output = next((output for output in outputs if output.name.casefold() == "filetree.txt"), None)
            STATE.log("INFO", f"导出完成：{len(xlsx_outputs)} 个 XLSX" + ("，已生成目录索引文件。" if filetree_output else "。"))
            STATE.log("INFO", f"目录统计：{export_stats['directory_count']} 个目录，{export_stats['file_count']} 个文件，{export_stats['content_directory_count']} 个内容目录。")
            if filetree_existed and not filetree_output:
                STATE.log("WARN", "目录索引 filetree.txt 已存在，未覆盖原文件。")
            for output in outputs:
                STATE.log("INFO", str(output))
        _send_json(self, {
            "ok": True,
            "outputs": [str(output) for output in outputs],
            "xlsx_outputs": [str(output) for output in xlsx_outputs],
            "filetree_output": str(filetree_output) if filetree_output else "",
            "export_stats": export_stats,
            "state": _state_json(),
        })

    def _export_scan(self) -> None:
        payload = _json_body(self)
        root = str(payload.get("root", "")).strip()
        if not root:
            raise ValueError("请选择根目录")
        result = scan_folder(
            root,
            include_hidden=bool(payload.get("include_hidden", False)),
            include_system=bool(payload.get("include_system", False)),
        )
        _send_json(self, {
            "ok": True,
            "root": result.root,
            "extensions": result.extension_counts,
            "total_file_count": len(result.records),
        })

    def _pick_folder(self) -> None:
        try:
            if sys.platform == "win32":
                selected = _pick_windows_folder()
            else:
                # Keep a small fallback for non-Windows development runs.
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                selected = filedialog.askdirectory(title="选择根目录")
                root.destroy()
        except Exception as exc:
            raise RuntimeError(f"无法打开本机文件夹选择器: {exc}") from exc
        _send_json(self, {"ok": True, "path": selected or ""})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client_lifecycle_enabled = False
        self._client_lifecycle_started = time.monotonic()
        self._client_last_seen: float | None = None
        self._client_closed_at: float | None = None
        self._client_lifecycle_lock = threading.Lock()
        self._client_shutdown_started = False
        self._client_monitor_thread: threading.Thread | None = None

    def enable_client_lifecycle(self) -> None:
        """Start monitoring the browser tab used by the interactive app."""
        with self._client_lifecycle_lock:
            if self._client_lifecycle_enabled:
                return
            self._client_lifecycle_enabled = True
            self._client_lifecycle_started = time.monotonic()
            self._client_monitor_thread = threading.Thread(
                target=self._monitor_client,
                name="webui-client-monitor",
                daemon=True,
            )
            self._client_monitor_thread.start()

    def client_heartbeat(self) -> None:
        """Record a live page and cancel a pending close from a reload."""
        with self._client_lifecycle_lock:
            self._client_last_seen = time.monotonic()
            self._client_closed_at = None

    def client_closed(self) -> None:
        """Record pagehide; shutdown is delayed to permit a page reload."""
        with self._client_lifecycle_lock:
            if not self._client_lifecycle_enabled or self._client_shutdown_started:
                return
            self._client_closed_at = time.monotonic()

    def _monitor_client(self) -> None:
        while True:
            time.sleep(0.5)
            with self._client_lifecycle_lock:
                if self._client_shutdown_started:
                    return
                now = time.monotonic()
                last_seen = self._client_last_seen
                closed_at = self._client_closed_at
                startup_expired = (
                    last_seen is None
                    and now - self._client_lifecycle_started >= CLIENT_STARTUP_TIMEOUT_SECONDS
                )
                close_beacon_expired = (
                    closed_at is not None
                    and now - closed_at >= CLIENT_CLOSE_GRACE_SECONDS
                    and (last_seen is None or last_seen < closed_at)
                )
                heartbeat_expired = (
                    last_seen is not None
                    and now - last_seen >= CLIENT_HEARTBEAT_TIMEOUT_SECONDS
                )
                if not (startup_expired or close_beacon_expired or heartbeat_expired):
                    continue
                self._client_shutdown_started = True
            # shutdown() must run outside the lock and on a thread other than
            # serve_forever(); the main thread will then leave its loop.
            self.shutdown()
            return


def run_server(port: int = 0, open_browser: bool = True) -> tuple[Server, str]:
    server = Server(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    if open_browser:
        server.enable_client_lifecycle()
        webbrowser.open(url)
    return server, url


def main() -> None:
    server, url = run_server()
    print(f"离线 WebUI 已启动: {url}")
    print("关闭此窗口即可停止本地服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
