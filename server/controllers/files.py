"""Folder scanning and target preview request controller."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.files import FileRecord
from server.state import StateManager
from workflow_system.catalog import workflow_field_map


class FileController:
    def __init__(self, state: StateManager, **services: Callable[..., Any]) -> None:
        self.state = state
        self.s = services

    def scan(self, payload: dict[str, Any]) -> dict[str, Any]:
        root = str(payload.get("root", "")).strip()
        if not root:
            raise ValueError("请选择根目录")
        include_hidden = bool(payload.get("include_hidden", False))
        include_system = bool(payload.get("include_system", False))
        mapping, mapping_auto = self.s["normalise_mapping"](payload.get("directory_mapping"))
        workflow = self.s["active_workflow"]()
        result = self.s["scan_for_workflow"](
            root, include_hidden, include_system, None if mapping_auto else mapping, workflow
        )
        with self.state.lock:
            self.state.root = result.root
            self.state.scan_result = result
            self.state.groups = result.groups
            self.state.current_group_key = next(iter(result.groups), None)
            self.state.include_hidden = include_hidden
            self.state.include_system = include_system
            self.state.directory_mapping = mapping
            self.state.directory_mapping_auto = mapping_auto
            self.state.group_enabled = {key: True for key in result.groups}
            self.state.extension_skipped.clear()
            self.s["apply_extension_defaults"](workflow, result)
            self.state.excel_mappings.clear()
            self.state.excel_skipped.clear()
            self.state.association_leaders.clear()
            self.state.parse_template = "auto"
            self.state.parse_use_name = False
            if self.state.mode not in workflow.get("name_modes", ["original"]):
                self.state.mode = workflow.get("name_modes", ["original"])[0]
            self.s["initialise_values"]()
            for group in self.state.groups.values():
                self.s["prepare_group"](group)
            for module in workflow.get("modules", []):
                if module.get("trigger") != "after_scan":
                    continue
                try:
                    _result, added = self.s["run_module_candidates"](
                        workflow, module["id"], "after_scan",
                        [r for r in self.s["all_records"]() if r.selected and not r.removed],
                    )
                    self.state.log("INFO", f"扫描后模块 {module.get('label', module['id'])} 新增 {added} 个候选标签。")
                except Exception as exc:
                    self.state.log("ERROR", f"扫描后模块 {module.get('label', module['id'])} 执行失败：{exc}")
            self.state.log("INFO", f"扫描完成：{len(result.records)} 个文件，{len(result.groups)} 个命名组，{len(result.extension_counts)} 种扩展名。")
            if result.skipped:
                self.state.log("INFO", f"已忽略 {len(result.skipped)} 个生成的表格或不可读条目。")
        return {"ok": True, "state": self.s["state_json"]()}

    def preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.state.lock:
            if payload.get("group_key"):
                key = str(payload["group_key"])
                if key not in self.state.groups:
                    raise KeyError("命名组不存在")
                self.state.current_group_key = key
            self.state.separator = str(payload.get("separator", self.state.separator))
            previous_mode = self.state.mode
            requested_mode = str(payload.get("mode", self.state.mode))
            if previous_mode == "excel" and requested_mode != "excel":
                self.s["leave_excel_mode"]()
            workflow = self.s["active_workflow"]()
            if requested_mode not in workflow.get("name_modes", ["original"]):
                raise ValueError("当前工作流不支持该名称模式")
            self.state.mode = requested_mode
            if "suffix_mode" in payload:
                requested_suffix = str(payload.get("suffix_mode") or "")
                if requested_suffix and requested_suffix not in workflow.get("suffix_modes", {}):
                    raise ValueError("当前工作流不支持该后缀模式")
                self.state.workflow_suffix_mode = requested_suffix
                suffix_field = workflow.get("suffix_field", "")
                definition = workflow_field_map(workflow).get(suffix_field)
                if definition and definition.get("scope") == "workflow":
                    self.state.workflow_values[suffix_field] = requested_suffix
            if "directory_mapping" in payload:
                mapping, auto = self.s["normalise_mapping"](payload.get("directory_mapping"))
                self.s["apply_directory_mapping"](mapping, auto)
            if "parse_template" in payload:
                self.state.parse_template = str(payload.get("parse_template") or "auto").strip() or "auto"
            if "parse_use_name" in payload:
                self.state.parse_use_name = bool(payload.get("parse_use_name"))
            self._update_extensions(payload)
            numeric = payload.get("numeric") or {}
            self.state.numeric_start = self.s["parse_int"](numeric.get("start"), self.state.numeric_start)
            self.state.numeric_width = self.s["parse_int"](numeric.get("width"), self.state.numeric_width)
            self.state.numeric_step = self.s["parse_int"](numeric.get("step"), self.state.numeric_step)
            group = self.state.current_group()
            if group:
                for prepared_group in self.state.groups.values():
                    self.s["prepare_group"](prepared_group)
                if previous_mode != self.state.mode:
                    for record in group.records:
                        if record.selected and not record.removed:
                            self.s["mark_association_leader"](record)
                self.s["expand_associated_records"](
                    [record for record in group.records if record.selected and not record.removed]
                )
                self.state.log("INFO", f"已更新 {group.label} 的目标文件名预览。")
        return {"ok": True, "state": self.s["state_json"]()}

    def _update_extensions(self, payload: dict[str, Any]) -> None:
        if "extensions" not in payload:
            return
        selected = {str(ext).casefold() for ext in payload.get("extensions", [])}
        records: list[FileRecord] = self.state.scan_result.records if self.state.scan_result else []
        known = {record.extension.casefold() for record in records}
        for record in records:
            extension = record.extension.casefold()
            was_enabled = self.state.extension_enabled.get(extension, True)
            enabled = extension in selected
            if not enabled:
                was_selected = record.selected
                record.selected = False
                if not record.removed and was_selected:
                    self.state.extension_skipped.add(record.path)
            elif not was_enabled and record.path in self.state.extension_skipped:
                if not record.removed:
                    record.selected = True
                self.state.extension_skipped.discard(record.path)
        self.state.extension_enabled = {extension: extension in selected for extension in known}
