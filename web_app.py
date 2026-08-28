"""Local offline WebUI server for the file naming application.

The server binds to loopback only. It exposes the existing namer_core service
through a small JSON API and serves the bundled static WebUI without network
dependencies.
"""

from __future__ import annotations

import json
import mimetypes
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
from urllib.parse import parse_qs, urlparse

from namer_core import (
    ExcelMatchResult,
    FileRecord,
    LogEntry,
    NamingGroup,
    RenameOperation,
    append_history,
    apply_filename_parse,
    append_bpm_suffix,
    audio_content_type,
    assign_numeric,
    collect_directory_statistics,
    compose_filename,
    directory_prefix_defaults,
    execute_rename,
    export_filename_tables,
    import_xlsx,
    is_audio_extension,
    is_bpm_extension,
    natural_key,
    open_in_explorer,
    parse_filename,
    preview_group,
    refresh_stem_associations,
    redo_last,
    scan_folder,
    undo_last,
    validate_filename,
)


WEB_ROOT = Path(__file__).with_name("webui")
APP_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent

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
        self.meta_prefix: str = ""
        self.meta_prefix_overridden: bool = False
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
        "child_prefix": record.child_prefix,
        "name": record.name,
        "base_name": record.base_name,
        "target_name": record.target_name,
        "status": record.status,
        "status_detail": record.status_detail,
        "selected": record.selected,
        "removed": record.removed,
        "is_audio": is_audio_extension(record.extension),
        "audio_format": record.extension.lstrip(".").upper() or "AUDIO",
        "bpm": record.bpm,
        "bpm_source": record.bpm_source,
        "scale": record.scale,
        "bpm_suffix_enabled": record.bpm_suffix_enabled,
        "parsed_fields": record.parsed_fields,
        "parse_unmatched": record.parse_unmatched,
        "parse_confidence": record.parse_confidence,
        "parse_error": record.parse_error,
        "association_id": record.association_id,
        "associated_extensions": record.associated_extensions,
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
                    "prefix": group.prefix,
                    "meta_prefix": group.meta_prefix,
                    "enabled": _group_enabled_for_execution(key),
                    "count": sum(not record.removed for record in group.records),
                })
        return {
            "root": STATE.root,
            "meta_prefix": STATE.meta_prefix,
            "separator": STATE.separator,
            "mode": STATE.mode,
            "numeric": {"start": STATE.numeric_start, "width": STATE.numeric_width, "step": STATE.numeric_step},
            "directory_mapping": STATE.directory_mapping if not STATE.directory_mapping_auto else None,
            "directory_mapping_auto": STATE.directory_mapping_auto,
            "max_depth": result.max_depth if result else 0,
            "parse_template": STATE.parse_template,
            "parse_use_name": STATE.parse_use_name,
            "include_hidden": STATE.include_hidden,
            "include_system": STATE.include_system,
            "extensions": result.extension_counts if result else {},
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
            if candidate.is_relative_to(root) and is_audio_extension(candidate.suffix) and candidate.is_file():
                return candidate
        except (OSError, ValueError):
            return None
    return None


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


def _apply_directory_mapping(mapping: dict[str, int | None], auto: bool) -> None:
    STATE.directory_mapping = mapping
    STATE.directory_mapping_auto = auto
    for group in STATE.groups.values():
        if auto:
            meta, prefix, child = directory_prefix_defaults(STATE.root, group.folder)
        else:
            meta, prefix, child = directory_prefix_defaults(STATE.root, group.folder, mapping)
        group.meta_prefix = meta
        group.prefix = prefix
        for record in group.records:
            record.child_prefix = child
    current = STATE.current_group()
    STATE.meta_prefix = current.meta_prefix if current else Path(STATE.root).name
    STATE.meta_prefix_overridden = False


def _prepare_group(group: NamingGroup) -> None:
    separator = STATE.separator
    for record in group.records:
        record.name = record.base_name
    apply_filename_parse(group.records, STATE.parse_template, STATE.parse_use_name)
    if STATE.mode == "numeric":
        # A record that was previously skipped by an extension filter can be
        # selected again. Reset only the skip marker so it participates in the
        # next numbering pass; preserve real errors and conflicts.
        for record in group.records:
            if record.selected and record.status == "Skipped":
                record.status = "Ready"
                record.status_detail = ""
        assign_numeric(group, STATE.numeric_start, STATE.numeric_width, STATE.numeric_step,
                       group.meta_prefix, separator)
    elif STATE.mode == "excel":
        mapping = STATE.excel_mappings.get(group.key, {})
        skipped = STATE.excel_skipped.get(group.key, set())
        for record in group.records:
            if record.path in mapping:
                record.name = mapping[record.path]
                record.selected = True
            elif record.path in skipped:
                record.selected = False
                record.status = "Skipped"
                record.status_detail = "Excel B 列为空"
            else:
                record.selected = False
                record.status = "未匹配"
                record.status_detail = "Excel 未提供名称"
            record.target_name = compose_filename(group.meta_prefix, group.prefix, record.child_prefix,
                                                  record.name, record.extension_original or record.extension, separator)
    else:
        preview_group(group, group.meta_prefix, separator)
    for record in group.records:
        if record.bpm_suffix_enabled and record.bpm:
            suffixed_name = append_bpm_suffix(record.name, record.bpm, separator)
            record.target_name = compose_filename(
                group.meta_prefix, group.prefix, record.child_prefix, suffixed_name,
                record.extension_original or record.extension, separator,
            )
    for record in group.records:
        if not STATE.extension_enabled.get(record.extension.casefold(), True):
            record.selected = False
            record.status = "Skipped"
            record.status_detail = "Extension not selected"
    if STATE.mode != "excel":
        for record in group.records:
            if not record.selected and record.status != "Skipped":
                record.status = "Skipped"
                record.status_detail = "Not selected"
    for record in group.records:
        if record.selected:
            syntax_error = validate_filename(record.target_name)
            if syntax_error:
                record.status = "Conflict"
                record.status_detail = syntax_error
            elif record.status == "Conflict":
                record.status = "Ready"
                record.status_detail = ""


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
        target_stem = Path(leader.target_name).stem
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
            related.target_name = f"{target_stem}{extension}"
            if excel_inherited:
                related.selected = True
                related.status = "Ready"
                related.status_detail = "继承关联文件的 Excel 名称"
            if not already_requested:
                result.append(related)
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
                    if "child_prefix" in payload:
                        record.child_prefix = str(payload["child_prefix"])
                        _mark_association_leader(record)
                    if "name" in payload:
                        record.base_name = str(payload["name"])
                        record.name = record.base_name
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
        elif parsed.path.startswith("/assets/"):
            asset = parsed.path.removeprefix("/assets/")
            content_type = mimetypes.guess_type(asset)[0] or "text/plain"
            self._serve_asset(asset, f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        elif parsed.path == "/api/state":
            _send_json(self, {"ok": True, "state": _state_json()})
        elif parsed.path == "/audio":
            query = parse_qs(parsed.query)
            path = _safe_audio_path(query.get("path", [""])[0])
            if not path:
                _send_json(self, {"ok": False, "error": "音频路径无效"}, 404)
                return
            try:
                _send_file_range(self, path, audio_content_type(path.suffix))
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
            elif path == "/api/select-group":
                payload = _json_body(self)
                with STATE.lock:
                    key = str(payload.get("key", ""))
                    if key not in STATE.groups:
                        raise KeyError("命名组不存在")
                    STATE.current_group_key = key
                    STATE.meta_prefix = STATE.groups[key].meta_prefix
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
            elif path == "/api/add-bpm-suffix":
                self._add_bpm_suffix()
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

    def _scan(self) -> None:
        payload = _json_body(self)
        root = str(payload.get("root", "")).strip()
        if not root:
            raise ValueError("请选择根目录")
        include_hidden = bool(payload.get("include_hidden", False))
        include_system = bool(payload.get("include_system", False))
        mapping, mapping_auto = _normalise_directory_mapping(payload.get("directory_mapping"))
        result = scan_folder(root, include_hidden, include_system, None if mapping_auto else mapping)
        with STATE.lock:
            STATE.root = result.root
            STATE.scan_result = result
            STATE.groups = result.groups
            STATE.current_group_key = next(iter(result.groups), None)
            current = STATE.groups.get(STATE.current_group_key) if STATE.current_group_key else None
            STATE.meta_prefix = current.meta_prefix if current else Path(result.root).name
            STATE.meta_prefix_overridden = False
            STATE.include_hidden = include_hidden
            STATE.include_system = include_system
            STATE.directory_mapping = mapping
            STATE.directory_mapping_auto = mapping_auto
            STATE.group_enabled = {key: True for key in result.groups}
            STATE.extension_skipped.clear()
            STATE.extension_enabled = {ext.casefold(): True for ext in result.extension_counts}
            STATE.excel_mappings.clear()
            STATE.excel_skipped.clear()
            STATE.association_leaders.clear()
            STATE.parse_template = "auto"
            STATE.parse_use_name = False
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
            STATE.mode = requested_mode
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
                if "meta_prefix" in payload:
                    requested_meta = str(payload["meta_prefix"])
                    if requested_meta != group.meta_prefix:
                        association_input_changed = True
                        # The editable meta-prefix field is intentionally a
                        # shared override, preserving the original workflow;
                        # untouched groups keep their scheme-one defaults.
                        STATE.meta_prefix_overridden = True
                        for other_group in STATE.groups.values():
                            other_group.meta_prefix = requested_meta
                STATE.meta_prefix = group.meta_prefix
                if "group_prefix" in payload:
                    requested_prefix = str(payload["group_prefix"])
                    association_input_changed = association_input_changed or requested_prefix != group.prefix
                    group.prefix = requested_prefix
                if "child_prefix" in payload:
                    child = str(payload["child_prefix"])
                    for record in group.records:
                        if not record.removed:
                            association_input_changed = association_input_changed or child != record.child_prefix
                            record.child_prefix = child
                _prepare_group(group)
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
            STATE.parse_template = template
            STATE.parse_use_name = bool(payload.get("use_name", False))
            apply_filename_parse(group.records, template, STATE.parse_use_name)
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

    def _add_bpm_suffix(self) -> None:
        payload = _json_body(self)
        with STATE.lock:
            group_key = str(payload.get("group_key") or STATE.current_group_key or "")
            group = STATE.groups.get(group_key)
            if not group:
                raise KeyError("命名组不存在")
            eligible = 0
            added = 0
            missing = 0
            for record in group.records:
                if record.removed or not is_bpm_extension(record.extension):
                    continue
                eligible += 1
                if not record.bpm:
                    missing += 1
                    continue
                if not record.bpm_suffix_enabled:
                    record.bpm_suffix_enabled = True
                    added += 1
                _mark_association_leader(record)
            if not eligible:
                raise ValueError("当前命名组不是音频或 MIDI 文件")
            STATE.current_group_key = group.key
            _prepare_group(group)
            _expand_associated_records([record for record in group.records if record.selected and not record.removed])
            STATE.log("INFO", f"已为 {group.label} 添加 BPM 后缀：{added} 个，未识别 BPM：{missing} 个。")
        _send_json(self, {"ok": True, "added": added, "missing": missing, "state": _state_json()})

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
                match: ExcelMatchResult = import_xlsx(temp_path, group, requested_sheet)
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
        export_mode = str(payload.get("mode", "compat") or "compat")
        root_path = Path(root).expanduser().resolve()
        structure_existed = any((root_path / name).exists() for name in ("Structure.ffnf.txt", "filetree.txt"))
        outputs = export_filename_tables(root, selected, bool(payload.get("include_hidden", False)), bool(payload.get("include_system", False)), export_mode)
        export_stats = collect_directory_statistics(root, include_hidden=bool(payload.get("include_hidden", False)), include_system=bool(payload.get("include_system", False)))
        with STATE.lock:
            xlsx_outputs = [output for output in outputs if output.suffix.casefold() == ".xlsx"]
            structure_outputs = [output for output in outputs if output.name.casefold() in {"structure.ffnf.txt", "filetree.txt"}]
            STATE.log("INFO", f"导出完成：{len(xlsx_outputs)} 个 XLSX" + ("，已生成目录索引文件。" if structure_outputs else "。"))
            STATE.log("INFO", f"目录统计：{export_stats['directory_count']} 个目录，{export_stats['file_count']} 个文件，{export_stats['content_directory_count']} 个内容目录。")
            if structure_existed and not structure_outputs:
                STATE.log("WARN", "目录辅助文件已存在（Structure.ffnf.txt 或 filetree.txt），未覆盖原文件。")
            for output in outputs:
                STATE.log("INFO", str(output))
        _send_json(self, {
            "ok": True,
            "outputs": [str(output) for output in outputs],
            "xlsx_outputs": [str(output) for output in xlsx_outputs],
            "structure_output": str(structure_outputs[0]) if structure_outputs else "",
            "filetree_output": next((str(output) for output in structure_outputs if output.name.casefold() == "filetree.txt"), ""),
            "export_mode": export_mode,
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
