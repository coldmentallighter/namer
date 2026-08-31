"""Naming group and file-record request operations."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.fsutil import open_in_explorer
from core.models import FileRecord
from server.state import StateManager
from workflow_system.schema import workflow_field_map


class RecordController:
    def __init__(self, state: StateManager, **services: Callable[..., Any]) -> None:
        self.state = state
        self.s = services

    def select_group(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.state.lock:
            key = str(payload.get("key", ""))
            if key not in self.state.groups:
                raise KeyError("命名组不存在")
            self.state.current_group_key = key
        return {"ok": True, "state": self.s["state_json"]()}

    def toggle_group(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.state.lock:
            key = str(payload.get("key", ""))
            if key not in self.state.groups:
                raise KeyError("命名组不存在")
            if self.state.mode == "excel" and not self.s["excel_group_ready"](key):
                raise ValueError("该命名组尚未导入 Excel 名称")
            self.state.group_enabled[key] = not self.state.group_enabled.get(key, True)
        return {"ok": True, "state": self.s["state_json"]()}

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._apply_update(payload)
        return {"ok": True, "state": self.s["state_json"]()}

    def update_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        updates = payload.get("updates", [])
        if not isinstance(updates, list):
            raise ValueError("批量记录更新格式无效")
        with self.state.lock:
            changed_records: list[FileRecord] = []
            for update in updates:
                if not isinstance(update, dict):
                    raise ValueError("批量记录更新项无效")
                record = self._apply_update(update)
                if update.get("removed"):
                    self._mark_removed(record)
                changed_records.append(record)
            if any(record.removed for record in changed_records):
                self.s["refresh_associations"]()
        return {"ok": True, "updated": len(changed_records), "state": self.s["state_json"]()}

    def remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.state.lock:
            record = self._apply_update({
                "path": payload.get("path"), "selected": False, "removed": True,
            })
            self._mark_removed(record)
            self.s["refresh_associations"]()
        return {"ok": True, "state": self.s["state_json"]()}

    def reorder(self, payload: dict[str, Any]) -> dict[str, Any]:
        order = [str(path) for path in payload.get("paths", [])]
        with self.state.lock:
            group = self.state.current_group()
            if not group:
                raise KeyError("没有当前命名组")
            by_path = {record.path: record for record in group.records}
            if set(order) != set(by_path):
                raise ValueError("排序数据与当前命名组不一致")
            group.records[:] = [by_path[path] for path in order]
            self.state.log("INFO", "已保存当前命名组的手动顺序。")
        return {"ok": True, "state": self.s["state_json"]()}

    def open_root(self, payload: dict[str, Any]) -> dict[str, Any]:
        root = str(payload.get("root", self.state.root)).strip()
        if not root or not Path(root).is_dir():
            raise ValueError("根目录不存在")
        open_in_explorer(root)
        return {"ok": True}

    def _apply_update(self, payload: dict[str, Any]) -> FileRecord:
        path = str(payload.get("path", ""))
        with self.state.lock:
            for group in self.state.groups.values():
                for record in group.records:
                    if record.path != path:
                        continue
                    workflow = self.s["active_workflow"]()
                    field_map = workflow_field_map(workflow)
                    workflow_values = payload.get("workflow_values")
                    workflow_changed = False
                    if isinstance(workflow_values, dict):
                        for field_id, value in workflow_values.items():
                            definition = field_map.get(str(field_id))
                            if not (definition
                                    and definition.get("scope") in {"record", "suffix"}
                                    and definition.get("editable", True)):
                                continue
                            normalised_value = self.s["normalise_value"](
                                workflow, str(field_id), value
                            )
                            record.workflow_values[str(field_id)] = normalised_value
                            record.workflow_manual_fields.add(str(field_id))
                            record.workflow_auto_fields.discard(str(field_id))
                            record.workflow_number_fields.discard(str(field_id))
                            self.s["remember_initial_value"](record, definition, normalised_value)
                            workflow_changed = True
                    if workflow_changed:
                        self.s["mark_association_leader"](record)
                    if "selected" in payload:
                        record.selected = bool(payload["selected"])
                    if "removed" in payload:
                        record.removed = bool(payload["removed"])
                    return record
        raise KeyError("文件记录不存在")

    @staticmethod
    def _mark_removed(record: FileRecord) -> None:
        record.selected = False
        record.status = "Skipped"
        record.status_detail = "Removed from this task"
