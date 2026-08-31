"""User-triggered workflow module controller."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.files import FileRecord
from engine import WorkflowEngine
from server.state import StateManager


class WorkflowModuleController:
    def __init__(self, state: StateManager, engine: WorkflowEngine,
                 active_workflow: Callable[[], dict[str, Any]],
                 all_records: Callable[[], list[FileRecord]],
                 state_json: Callable[[], dict[str, Any]]) -> None:
        self.state = state
        self.engine = engine
        self.active_workflow = active_workflow
        self.all_records = all_records
        self.state_json = state_json

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        module_id = str(payload.get("module_id", payload.get("id", ""))).strip()
        if not module_id:
            raise ValueError("请选择要调用的工作流模块")
        requested_paths = payload.get("paths")
        if requested_paths is not None and not isinstance(requested_paths, list):
            raise ValueError("paths 必须是数组")
        path_filter = {str(path) for path in (requested_paths or []) if str(path).strip()}
        selected_only = bool(payload.get("selected_only", True))
        with self.state.lock:
            workflow = self.active_workflow()
            declaration = next(
                (item for item in workflow.get("modules", []) if item.get("id") == module_id), None
            )
            if declaration is None:
                raise KeyError(f"当前工作流未声明模块: {module_id}")
            if declaration.get("trigger") != "on_user_request":
                raise ValueError(f"模块不能由用户请求触发: {module_id}")
            records = [
                record for record in self.all_records()
                if not record.removed
                and (not selected_only or record.selected)
                and (not path_filter or record.path in path_filter)
            ]
            if not records:
                raise ValueError("没有可交给模块处理的文件")
            items, paths_by_item_id = self.engine.module_items(records)
            workflow_id = self.state.workflow_id
        result, _request = self.state.workflow_catalog.module_registry.run(
            workflow, module_id, "on_user_request", items
        )
        with self.state.lock:
            if self.state.workflow_id != workflow_id:
                raise ValueError("模块执行期间工作流已切换，结果未应用")
            added = self.engine.apply_module_result(
                workflow, module_id, result, paths_by_item_id,
                self.all_records(), self.state.groups, self.state.workflow_candidates,
            )
            self.state.log(
                "INFO",
                f"模块 {declaration.get('label', module_id)} 已处理 {len(records)} 个文件，新增 {added} 个候选标签。",
            )
        return {
            "ok": True,
            "module_id": module_id,
            "processed": len(records),
            "candidates_added": added,
            "result": result,
            "state": self.state_json(),
        }
