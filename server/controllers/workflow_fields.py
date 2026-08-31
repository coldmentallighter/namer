"""Workflow field values, candidate filling, parsing, and actions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.models import FileRecord
from core.naming import apply_filename_parse
from engine import WorkflowEngine
from engine.rules import action_map, action_value
from server.state import StateManager
from workflow_system.catalog import workflow_field_map


class WorkflowFieldController:
    def __init__(self, state: StateManager, engine: WorkflowEngine,
                 **services: Callable[..., Any]) -> None:
        self.state = state
        self.engine = engine
        self.s = services

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        field_id = str(payload.get("field", payload.get("field_id", ""))).strip()
        value = str(payload.get("value", "") or "")
        workflow = self.s["active_workflow"]()
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
            with self.state.lock:
                self.state.workflow_values[field_id] = value
                if field_id == workflow.get("suffix_field"):
                    self.state.workflow_suffix_mode = value
                for group in self.state.groups.values():
                    self.s["prepare_group"](group)
                self.s["expand_associated_records"]([
                    record for record in self.s["all_records"]()
                    if record.selected and not record.removed
                ])
                self.state.log("INFO", f"已更新工作流字段：{definition.get('label', field_id)}。")
            return {"ok": True, "field": field_id, "state": self.s["state_json"]()}

        group_key = str(payload.get("group_key") or self.state.current_group_key or "")
        path = str(payload.get("path", ""))
        with self.state.lock:
            group = self.state.groups.get(group_key)
            if not group:
                raise KeyError("命名组不存在")
            if definition["scope"] == "group":
                group.workflow_values[field_id] = value
            else:
                record = next((item for item in group.records if item.path == path), None)
                if not record:
                    raise KeyError("文件记录不存在")
                normalised_value = self.s["normalise_value"](workflow, field_id, value)
                record.workflow_values[field_id] = normalised_value
                record.workflow_manual_fields.add(field_id)
                record.workflow_auto_fields.discard(field_id)
                record.workflow_number_fields.discard(field_id)
                self.s["remember_initial_value"](record, definition, normalised_value)
                self.s["mark_association_leader"](record)
            self.s["prepare_group"](group)
            self.s["expand_associated_records"]([
                record for record in group.records if record.selected and not record.removed
            ])
            self.state.log("INFO", f"已更新工作流字段：{definition.get('label', field_id)}。")
        return {"ok": True, "field": field_id, "state": self.s["state_json"]()}

    def fill_candidates(self, payload: dict[str, Any]) -> dict[str, Any]:
        requested_fields = payload.get("fields", [])
        if requested_fields is None:
            requested_fields = []
        if not isinstance(requested_fields, list):
            raise ValueError("fields 必须是数组")
        requested = {str(field_id).strip() for field_id in requested_fields if str(field_id).strip()}
        workflow = self.s["active_workflow"]()
        fields = workflow_field_map(workflow)
        unknown = requested - set(fields)
        if unknown:
            raise KeyError(f"工作流字段不存在: {', '.join(sorted(unknown))}")
        filled = 0
        filled_fields: set[str] = set()
        with self.state.lock:
            for field_id, candidates in self.state.workflow_candidates.items():
                if requested and field_id not in requested:
                    continue
                definition = fields.get(field_id, {})
                current = str(self.state.workflow_values.get(field_id, "") or "")
                if len(candidates) != 1 or (current and current != str(definition.get("default", "") or "")):
                    continue
                self.state.workflow_values[field_id] = candidates[0]
                filled += 1
                filled_fields.add(field_id)
            for group in self.state.groups.values():
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
                        value = self.s["normalise_value"](workflow, field_id, candidates[0])
                        if not value or value == current:
                            continue
                        record.workflow_values[field_id] = value
                        record.workflow_auto_fields.add(field_id)
                        filled += 1
                        filled_fields.add(field_id)
                self.s["prepare_group"](group)
            self.s["expand_associated_records"]([
                record for record in self.s["all_records"]()
                if record.selected and not record.removed
            ])
            self.state.log("INFO", f"已填充工作流自动值：{filled} 个字段。")
        return {
            "ok": True,
            "filled": filled,
            "fields": sorted(filled_fields),
            "state": self.s["state_json"](),
        }

    def apply_directory_mapping(self, payload: dict[str, Any]) -> dict[str, Any]:
        mapping, auto = self.s["normalise_mapping"](payload.get("mapping"))
        with self.state.lock:
            if not self.state.root or not self.state.groups:
                raise ValueError("请先扫描根目录")
            self.s["apply_directory_mapping"](mapping, auto)
            self.state.association_leaders.clear()
            for group in self.state.groups.values():
                self.s["prepare_group"](group)
            self.s["expand_associated_records"]([
                record for record in self.s["all_records"]()
                if record.selected and not record.removed
            ])
            self.state.log("INFO", "已应用目录层级映射：自动末端三级" if auto else f"已应用目录层级映射：{mapping}")
        return {"ok": True, "state": self.s["state_json"]()}

    def parse_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        template = str(payload.get("template") or "auto").strip() or "auto"
        with self.state.lock:
            group_key = str(payload.get("group_key") or self.state.current_group_key or "")
            group = self.state.groups.get(group_key)
            if not group:
                raise KeyError("命名组不存在")
            workflow = self.s["active_workflow"]()
            self.state.parse_template = template
            self.state.parse_use_name = bool(payload.get("use_name", False))
            apply_filename_parse(
                group.records,
                template,
                self.state.parse_use_name,
                parser=lambda stem, parse_template: self.s["parse_filename"](
                    workflow, stem, parse_template
                ),
            )
            self.s["prepare_group"](group)
            if self.state.parse_use_name:
                for record in group.records:
                    if record.selected and not record.removed:
                        self.s["mark_association_leader"](record)
            self.s["expand_associated_records"]([
                record for record in group.records if record.selected and not record.removed
            ])
            values = [{
                "path": record.path,
                "original_name": record.original_name,
                "fields": record.parsed_fields,
                "unmatched": record.parse_unmatched,
                "confidence": record.parse_confidence,
                "error": record.parse_error,
            } for record in group.records]
            self.state.log("INFO", f"已完成 {group.label} 的文件名解析预览（模板：{template}）。")
            error_count = sum(
                1 for record in group.records
                if record.parse_error or record.parse_confidence <= 0
            )
            if error_count:
                self.state.log("WARN", f"文件名解析有 {error_count} 个文件未能完整匹配模板。")
        return {"ok": True, "parsed": values, "state": self.s["state_json"]()}

    def run_action(self, payload: dict[str, Any], *, use_first_action: bool = False) -> dict[str, Any]:
        with self.state.lock:
            group_key = str(payload.get("group_key") or self.state.current_group_key or "")
            group = self.state.groups.get(group_key)
            if not group:
                raise KeyError("命名组不存在")
            workflow = self.s["active_workflow"]()
            actions = action_map(workflow)
            action_id = str(payload.get("action_id", "")).strip()
            if not action_id and use_first_action:
                action_id = next(iter(actions), "")
            action = actions.get(action_id)
            if not action:
                raise KeyError(f"工作流 action 不存在: {action_id}")
            added = 0
            missing = 0
            for record in group.records:
                if record.removed:
                    continue
                context = self.engine.context(record, workflow)
                value = action_value(action, context)
                if value is None or str(value).strip() == "":
                    value = record.workflow_values.get(action["field"], "")
                value = self.s["normalise_value"](workflow, action["field"], value)
                if not value.strip():
                    missing += 1
                    continue
                record.workflow_values[action["field"]] = value
                record.workflow_manual_fields.add(action["field"])
                if action_id not in record.workflow_actions:
                    record.workflow_actions.add(action_id)
                    added += 1
                self.s["mark_association_leader"](record)
            self.state.current_group_key = group.key
            self.s["prepare_group"](group)
            self.s["expand_associated_records"]([
                record for record in group.records if record.selected and not record.removed
            ])
            self.state.log("INFO", f"已执行工作流动作：{action['label']}，应用 {added} 个，缺少值 {missing} 个。")
        return {
            "ok": True,
            "action": action,
            "added": added,
            "missing": missing,
            "state": self.s["state_json"](),
        }
