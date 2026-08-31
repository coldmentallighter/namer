"""HTTP route table kept separate from request transport and controllers."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse

from server.transport import json_body, multipart_body, send_json


POST_METHOD_ROUTES = {
    "/api/client-heartbeat": "_client_heartbeat",
    "/api/client-closed": "_client_closed",
    "/api/select-group": "_select_group",
    "/api/toggle-group": "_toggle_group",
    "/api/record": "_record_update",
    "/api/records-batch": "_records_batch_update",
    "/api/remove": "_remove_record",
    "/api/open-root": "_open_root",
    "/api/scan": "_scan",
    "/api/workflow/select": "_select_or_save_workflow",
    "/api/workflow": "_select_or_save_workflow",
    "/api/workflow/import": "_import_workflow",
    "/api/workflow-import": "_import_workflow",
    "/api/workflow/save": "_save_workflow",
    "/api/workflow-update": "_save_workflow",
    "/api/config": "_config",
    "/api/workflow-value": "_workflow_value_update",
    "/api/workflow/field": "_workflow_value_update",
    "/api/workflow-values/tag": "_workflow_tag_update",
    "/api/preview": "_preview",
    "/api/directory-mapping": "_directory_mapping",
    "/api/parse-preview": "_parse_preview",
    "/api/workflow-action": "_run_workflow_action",
    "/api/add-bpm-suffix": "_run_workflow_action",
    "/api/workflow-fill": "_workflow_fill_candidates",
    "/api/workflow/auto-fill": "_workflow_fill_candidates",
    "/api/workflow-module/run": "_run_workflow_module",
    "/api/workflow/module/run": "_run_workflow_module",
    "/api/reorder": "_reorder",
    "/api/import-excel": "_import_excel",
    "/api/rename": "_rename",
    "/api/undo": "_undo",
    "/api/redo": "_redo",
    "/api/restore": "_redo",
    "/api/export": "_export",
    "/api/export-scan": "_export_scan",
    "/api/pick-folder": "_pick_folder",
}


def dispatch_post(handler: BaseHTTPRequestHandler, path: str) -> bool:
    method_name = POST_METHOD_ROUTES.get(path)
    if not method_name:
        return False
    getattr(handler, method_name)()
    return True


def create_handler(*, state: Any, state_json: Any, asset_controller: Any,
                   workflow_controller: Any, workflow_field_controller: Any,
                   workflow_module_controller: Any, file_controller: Any,
                   record_controller: Any, excel_controller: Any,
                   operation_controller: Any, system_controller: Any,
                   ) -> type[BaseHTTPRequestHandler]:
    """Bind application controllers to the loopback HTTP adapter."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "OfflineFileNamerWeb/1.0"

        def log_message(self, format: str, *args) -> None:
            if (self.path.startswith("/api/")
                    and not self.path.startswith((
                        "/api/client-heartbeat", "/api/client-closed",
                    ))):
                super().log_message(format, *args)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            asset_controller.serve(self, parsed.path, parse_qs(parsed.query))

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                if not dispatch_post(self, path):
                    send_json(self, {"ok": False, "error": "Not found"}, 404)
            except Exception as exc:
                with state.lock:
                    state.log("ERROR", str(exc))
                send_json(
                    self,
                    {"ok": False, "error": str(exc), "state": state_json()},
                    400,
                )

        def _select_or_save_workflow(self) -> None:
            payload = json_body(self)
            workflow_value = payload.get("workflow")
            if workflow_value is not None or "fields" in payload:
                self._save_workflow_payload(
                    workflow_value if isinstance(workflow_value, dict) else payload
                )
                return
            send_json(self, workflow_controller.select(payload))

        def _workflow_tag_update(self) -> None:
            send_json(self, workflow_controller.update_tag(json_body(self)))

        def _save_workflow_payload(self, payload: dict) -> None:
            send_json(self, workflow_controller.save(payload))

        def _save_workflow(self) -> None:
            payload = json_body(self)
            workflow = payload.get("workflow", payload)
            if not isinstance(workflow, dict):
                raise ValueError("工作流保存格式无效")
            self._save_workflow_payload(workflow)

        def _import_workflow(self) -> None:
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" in content_type:
                form = multipart_body(self)
                upload = form.get("file")
                if not isinstance(upload, tuple) or not upload[1]:
                    raise ValueError("未选择工作流文件")
                filename, data = upload
                strategy = str(form.get("strategy", "copy") or "copy")
                trust_modules = (
                    str(form.get("trust_modules", "false")).strip().casefold() == "true"
                )
            else:
                payload = json_body(self)
                filename = str(payload.get("filename", "workflow.json"))
                source = payload.get("workflow", payload)
                data = json.dumps(source, ensure_ascii=False).encode("utf-8")
                strategy = str(payload.get("strategy", "copy") or "copy")
                trust_modules = bool(payload.get("trust_modules", False))
            send_json(
                self,
                workflow_controller.install(
                    data, filename, strategy, trust_modules
                ),
            )

        def _config(self) -> None:
            send_json(self, workflow_controller.update_config(json_body(self)))

        def _workflow_value_update(self) -> None:
            send_json(self, workflow_field_controller.update(json_body(self)))

        def _workflow_fill_candidates(self) -> None:
            send_json(self, workflow_field_controller.fill_candidates(json_body(self)))

        def _run_workflow_module(self) -> None:
            send_json(self, workflow_module_controller.run(json_body(self)))

        def _scan(self) -> None:
            send_json(self, file_controller.scan(json_body(self)))

        def _preview(self) -> None:
            send_json(self, file_controller.preview(json_body(self)))

        def _directory_mapping(self) -> None:
            send_json(
                self,
                workflow_field_controller.apply_directory_mapping(json_body(self)),
            )

        def _parse_preview(self) -> None:
            send_json(self, workflow_field_controller.parse_preview(json_body(self)))

        def _run_workflow_action(self) -> None:
            use_first = urlparse(self.path).path == "/api/add-bpm-suffix"
            send_json(
                self,
                workflow_field_controller.run_action(
                    json_body(self), use_first_action=use_first
                ),
            )

        def _reorder(self) -> None:
            send_json(self, record_controller.reorder(json_body(self)))

        def _import_excel(self) -> None:
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                raise ValueError("需要上传 XLSX 文件")
            send_json(self, excel_controller.import_table(multipart_body(self)))

        def _select_group(self) -> None:
            send_json(self, record_controller.select_group(json_body(self)))

        def _toggle_group(self) -> None:
            send_json(self, record_controller.toggle_group(json_body(self)))

        def _record_update(self) -> None:
            send_json(self, record_controller.update(json_body(self)))

        def _records_batch_update(self) -> None:
            send_json(self, record_controller.update_batch(json_body(self)))

        def _remove_record(self) -> None:
            send_json(self, record_controller.remove(json_body(self)))

        def _open_root(self) -> None:
            send_json(self, record_controller.open_root(json_body(self)))

        def _rename(self) -> None:
            send_json(self, operation_controller.rename(json_body(self)))

        def _undo(self) -> None:
            send_json(self, operation_controller.history("undo"))

        def _redo(self) -> None:
            send_json(self, operation_controller.history("redo"))

        def _export(self) -> None:
            send_json(self, operation_controller.export(json_body(self)))

        def _export_scan(self) -> None:
            send_json(self, operation_controller.export_scan(json_body(self)))

        def _pick_folder(self) -> None:
            send_json(self, system_controller.pick_folder())

        def _client_heartbeat(self) -> None:
            send_json(self, system_controller.heartbeat(self.server))

        def _client_closed(self) -> None:
            send_json(self, system_controller.client_closed(self.server))

    return Handler
