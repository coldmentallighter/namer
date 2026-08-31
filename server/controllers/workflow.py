"""Workflow lifecycle and vocabulary controller."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from server.state import StateManager
from workflow_system.catalog import load_workflow_bundle, validate_workflow, workflow_summary
from workflow_system.values import WorkflowValueStore


class WorkflowController:
    def __init__(self, state: StateManager, value_store: Callable[[], WorkflowValueStore],
                 activate: Callable[..., dict[str, Any]],
                 workflow_state: Callable[[], dict[str, Any]],
                 state_json: Callable[[], dict[str, Any]]) -> None:
        self.state = state
        self._value_store = value_store
        self.activate = activate
        self.workflow_state = workflow_state
        self.state_json = state_json

    def select(self, payload: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(payload.get("id", payload.get("workflow_id", ""))).strip()
        if not workflow_id:
            raise ValueError("请选择工作流")
        with self.state.lock:
            workflow = self.activate(workflow_id)
            self.state.log("INFO", f"已切换工作流：{workflow['name']}。当前任务预览已更新，已执行的重命名不会改变。")
        return {"ok": True, "workflow": self.workflow_state(), "state": self.state_json()}

    def update_tag(self, payload: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(payload.get("workflow_id", self.state.workflow_id)).strip()
        field_id = str(payload.get("field_id", "")).strip()
        workflow = self.state.workflow_catalog.get(workflow_id)
        value_store = self._value_store()
        action = str(payload.get("action", "upsert")).strip().casefold()
        if action == "toggle":
            data = value_store.toggle(workflow, field_id, str(payload.get("tag_id", "")).strip())
        elif action == "delete":
            data = value_store.delete(workflow, field_id, str(payload.get("tag_id", "")).strip())
        else:
            value = payload.get("tag", {})
            if not isinstance(value, dict):
                raise ValueError("标签数据格式无效")
            data = value_store.upsert(workflow, field_id, value)
        return {"ok": True, "data": data}

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        workflow = validate_workflow(payload, allow_builtin=False)
        with self.state.lock:
            workflow = self.state.workflow_catalog.upsert_user_workflow(workflow)
            self.activate(workflow["id"], persist=False)
            self.state.log("INFO", f"已保存工作流：{workflow['name']}。")
        return {"ok": True, "workflow": self.workflow_state(), "state": self.state_json()}

    def install(self, data: bytes, filename: str, strategy: str,
                trust_modules: bool) -> dict[str, Any]:
        if strategy not in {"copy", "replace", "cancel"}:
            raise ValueError("导入策略必须是 copy、replace 或 cancel")
        workflow, package_files = load_workflow_bundle(data, filename)
        with self.state.lock:
            imported, existed = self.state.workflow_catalog.install_package(
                workflow, package_files, strategy, trust_modules=trust_modules
            )
            self.activate(imported["id"], persist=False)
            self.state.log("INFO", f"已导入工作流：{imported['name']}。")
        return {
            "ok": True,
            "imported": workflow_summary(imported),
            "replaced": bool(existed and strategy == "replace"),
            "copied": bool(existed and strategy != "replace"),
            "workflow": self.workflow_state(),
            "state": self.state_json(),
        }

    def update_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.state.lock:
            if "theme" in payload:
                theme = str(payload["theme"])
                if theme not in {"light", "dark"}:
                    raise ValueError("主题必须是 light 或 dark")
                self.state.workflow_catalog.theme = theme
            if payload.get("workflow_id"):
                self.activate(str(payload["workflow_id"]))
            self.state.workflow_catalog.save()
        return {
            "ok": True,
            "config": {
                "theme": self.state.workflow_catalog.theme,
                "current_workflow": self.state.workflow_id,
            },
            "state": self.state_json(),
        }
