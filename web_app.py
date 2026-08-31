"""Local offline WebUI server for the file naming application.

The server binds to loopback only. It exposes the filesystem core and workflow engine
through a small JSON API and serves the bundled static WebUI without network
dependencies.
"""

from __future__ import annotations

import mimetypes
import sys
from pathlib import Path
from typing import Any

from core.models import FileRecord, NamingGroup
from core.validate import validate_filename
from workflow_system.catalog import RESOURCE_WORKFLOW_ROOT
from workflow_system.values import WorkflowValueStore
from engine.executor import WorkflowEngine
from engine.session import WorkflowSession
from server.launcher import Server, create_server
from server.controllers.workflow import WorkflowController
from server.controllers.modules import WorkflowModuleController
from server.controllers.files import FileController
from server.controllers.operations import OperationController
from server.controllers.assets import AssetController
from server.controllers.excel import ExcelController
from server.controllers.records import RecordController
from server.controllers.system import SystemController
from server.controllers.workflow_fields import WorkflowFieldController
from server.application import WorkflowApplication
from server.associations import AssociationService
from core.history import (
    change_description as _history_change_description,
    changed_items as _changed_history_items,
    read_snapshot as _read_history_snapshot,
)
from server.presenter import StatePresenter
from server.routes import create_handler
from server.scanning import WorkflowScanService
from server.state import StateManager
from workflow_system.runtime import (
    apply_workflow_metadata as _apply_workflow_metadata,
    normalise_workflow_value as _normalise_workflow_value,
    parse_workflow_filename as _parse_workflow_filename,
    read_workflow_metadata as _read_workflow_metadata,
)


WEB_ROOT = Path(__file__).with_name("webui")
APP_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
WORKFLOW_VALUE_STORE = WorkflowValueStore(APP_ROOT / "workflow-values")

CLIENT_HEARTBEAT_INTERVAL_SECONDS = 2.0
AppState = StateManager
STATE = StateManager(APP_ROOT, (RESOURCE_WORKFLOW_ROOT, APP_ROOT / "workflows"))


def read_workflow_metadata(workflow: dict[str, Any], path: str | Path,
                           root: str | Path | None = None) -> dict[str, Any]:
    return _read_workflow_metadata(
        workflow, path, root, STATE.workflow_catalog.module_registry
    )


def normalise_workflow_value(workflow: dict[str, Any], field_id: str, value: Any) -> str:
    return _normalise_workflow_value(
        workflow, field_id, value, STATE.workflow_catalog.module_registry
    )


def parse_workflow_filename(workflow: dict[str, Any], stem: str,
                            template: str = "auto") -> dict[str, Any]:
    return _parse_workflow_filename(
        workflow, stem, template, STATE.workflow_catalog.module_registry
    )


def apply_workflow_metadata(records: list[FileRecord], workflow: dict[str, Any]) -> None:
    _apply_workflow_metadata(records, workflow, STATE.workflow_catalog.module_registry)


ENGINE = WorkflowEngine(normalise_workflow_value, validate_filename)


def _workflow_context(record: FileRecord, workflow: dict[str, Any]) -> dict[str, Any]:
    return ENGINE.context(record, workflow)


def _apply_workflow_rules(workflow: dict[str, Any], record: FileRecord) -> None:
    ENGINE.apply_rules(record, workflow)


def _sync_workflow_catalog() -> dict[str, Any]:
    return APPLICATION.sync_catalog()


def _active_workflow() -> dict:
    return APPLICATION.active_workflow()


def _workflow_state() -> dict:
    return APPLICATION.workflow_state()


def _activate_workflow(workflow_id: str, *, persist: bool = True) -> dict:
    return APPLICATION.activate(workflow_id, persist=persist)


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


def _state_json() -> dict:
    return PRESENTER.state_json()


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


def _directory_source_values(folder: str | Path) -> dict[str, str]:
    return SESSION.directory_source_values(folder)


def _initial_field_value(definition: dict[str, Any], group: NamingGroup | None = None,
                         record: FileRecord | None = None) -> str:
    return SESSION.initial_field_value(definition, group, record)


def _remember_initial_value(record: FileRecord, definition: dict[str, Any], value: str) -> None:
    return SESSION.remember_initial_value(record, definition, value)


def _apply_directory_mapping(mapping: dict[str, int | None], auto: bool) -> None:
    return SESSION.apply_directory_mapping(mapping, auto)


def _initialise_workflow_values(workflow: dict | None = None) -> None:
    """Initialise canonical values from declarative workflow field sources."""
    return SESSION.initialise_values(workflow)


def _workflow_value(workflow: dict, group: NamingGroup, record: FileRecord,
                    field_id: str) -> str:
    return SESSION.value(workflow, group, record, field_id)


def _workflow_profile(workflow: dict, group: NamingGroup, record: FileRecord) -> dict[str, Any] | None:
    return SESSION.profile(workflow, group, record)


def _compose_workflow_target(workflow: dict, group: NamingGroup, record: FileRecord) -> str:
    return SESSION.compose_target(workflow, group, record)


def _resolve_target_conflicts() -> None:
    return SESSION.resolve_target_conflicts()


def _assign_workflow_numbers(workflow: dict, group: NamingGroup,
                             overrides: dict[str, int] | None = None) -> None:
    return SESSION.assign_numbers(workflow, group, overrides)


def _excel_field(workflow: dict, fields: dict[str, dict]) -> str:
    return SESSION.excel_field(workflow, fields)


def _expand_workflow_excel_name(value: str, record: FileRecord,
                                row_values: dict[str, str]) -> str:
    return SESSION.expand_excel_name(value, record, row_values)


def _prepare_group(group: NamingGroup, workflow: dict | None = None) -> None:
    return SESSION.prepare_group(group, workflow)


def _all_records() -> list[FileRecord]:
    return ASSOCIATIONS.all_records()


def _run_workflow_module_candidates(workflow: dict[str, Any], module_id: str,
                                    trigger: str, records: list[FileRecord]) -> tuple[dict[str, Any], int]:
    """Run one module and adapt validated string outputs into candidates."""
    items, paths_by_item_id = ENGINE.module_items(records)
    result, _request = STATE.workflow_catalog.module_registry.run(
        workflow, module_id, trigger, items
    )
    added = ENGINE.apply_module_result(
        workflow, module_id, result, paths_by_item_id,
        _all_records(), STATE.groups, STATE.workflow_candidates,
    )
    return result, added


def _expand_associated_records(records: list[FileRecord],
                               allowed_group_keys: set[str] | None = None) -> list[FileRecord]:
    return ASSOCIATIONS.expand(records, allowed_group_keys)


def _refresh_associations() -> None:
    ASSOCIATIONS.refresh()


def _mark_association_leader(record: FileRecord) -> None:
    ASSOCIATIONS.mark_leader(record)


def _leave_excel_mode() -> None:
    ASSOCIATIONS.leave_excel_mode()


def _reconcile_history_records(items: list[dict], direction: str) -> None:
    ASSOCIATIONS.reconcile_history(items, direction)


SCANNER = WorkflowScanService(STATE, read_workflow_metadata)
ASSOCIATIONS = AssociationService(STATE, _resolve_target_conflicts)
SESSION = WorkflowSession(
    STATE,
    ENGINE,
    normalise_workflow_value,
    _active_workflow,
    parse_workflow_filename,
    _all_records,
)
APPLICATION = WorkflowApplication(
    STATE,
    lambda: WORKFLOW_VALUE_STORE,
    grouping_signature=SCANNER.grouping_signature,
    scan_for_workflow=SCANNER.scan,
    apply_extension_defaults=SCANNER.apply_extension_defaults,
    all_records=_all_records,
    apply_metadata=apply_workflow_metadata,
    initialise_values=_initialise_workflow_values,
    prepare_group=_prepare_group,
    expand_associated_records=_expand_associated_records,
)
PRESENTER = StatePresenter(STATE, _workflow_state, _group_enabled_for_execution)
WORKFLOW_CONTROLLER = WorkflowController(
    STATE, lambda: WORKFLOW_VALUE_STORE, _activate_workflow, _workflow_state, _state_json
)
WORKFLOW_MODULE_CONTROLLER = WorkflowModuleController(
    STATE, ENGINE, _active_workflow, _all_records, _state_json
)
FILE_CONTROLLER = FileController(
    STATE,
    normalise_mapping=_normalise_directory_mapping,
    active_workflow=_active_workflow,
    scan_for_workflow=SCANNER.scan,
    apply_extension_defaults=SCANNER.apply_extension_defaults,
    initialise_values=_initialise_workflow_values,
    prepare_group=_prepare_group,
    run_module_candidates=_run_workflow_module_candidates,
    all_records=_all_records,
    state_json=_state_json,
    leave_excel_mode=_leave_excel_mode,
    apply_directory_mapping=_apply_directory_mapping,
    parse_int=_parse_int,
    mark_association_leader=_mark_association_leader,
    expand_associated_records=_expand_associated_records,
)
OPERATION_CONTROLLER = OperationController(
    STATE,
    prepare_group=_prepare_group,
    expand_associated=_expand_associated_records,
    group_enabled=_group_enabled_for_execution,
    refresh_associations=_refresh_associations,
    read_history=_read_history_snapshot,
    changed_history=_changed_history_items,
    reconcile_history=_reconcile_history_records,
    history_description=_history_change_description,
    active_workflow=_active_workflow,
    read_metadata=read_workflow_metadata,
    state_json=_state_json,
)
WORKFLOW_FIELD_CONTROLLER = WorkflowFieldController(
    STATE,
    ENGINE,
    active_workflow=_active_workflow,
    normalise_value=normalise_workflow_value,
    prepare_group=_prepare_group,
    expand_associated_records=_expand_associated_records,
    all_records=_all_records,
    remember_initial_value=_remember_initial_value,
    mark_association_leader=_mark_association_leader,
    state_json=_state_json,
    normalise_mapping=_normalise_directory_mapping,
    apply_directory_mapping=_apply_directory_mapping,
    parse_filename=parse_workflow_filename,
)
RECORD_CONTROLLER = RecordController(
    STATE,
    active_workflow=_active_workflow,
    normalise_value=normalise_workflow_value,
    remember_initial_value=_remember_initial_value,
    mark_association_leader=_mark_association_leader,
    refresh_associations=_refresh_associations,
    excel_group_ready=_excel_group_ready,
    state_json=_state_json,
)
EXCEL_CONTROLLER = ExcelController(
    STATE,
    expand_excel_name=_expand_workflow_excel_name,
    prepare_group=_prepare_group,
    mark_association_leader=_mark_association_leader,
    expand_associated_records=_expand_associated_records,
    state_json=_state_json,
)
ASSET_CONTROLLER = AssetController(
    STATE,
    WEB_ROOT,
    lambda: WORKFLOW_VALUE_STORE,
    state_json=_state_json,
    workflow_state=_workflow_state,
    safe_audio_path=_safe_audio_path,
    audio_content_type=_audio_content_type,
)
SYSTEM_CONTROLLER = SystemController(
    STATE, _sync_workflow_catalog, lambda: _pick_windows_folder()
)


Handler = create_handler(
    state=STATE,
    state_json=_state_json,
    asset_controller=ASSET_CONTROLLER,
    workflow_controller=WORKFLOW_CONTROLLER,
    workflow_field_controller=WORKFLOW_FIELD_CONTROLLER,
    workflow_module_controller=WORKFLOW_MODULE_CONTROLLER,
    file_controller=FILE_CONTROLLER,
    record_controller=RECORD_CONTROLLER,
    excel_controller=EXCEL_CONTROLLER,
    operation_controller=OPERATION_CONTROLLER,
    system_controller=SYSTEM_CONTROLLER,
)


def run_server(port: int = 0, open_browser: bool = True) -> tuple[Server, str]:
    return create_server(Handler, port, open_browser)


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
