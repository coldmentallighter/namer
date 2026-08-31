"""Application-level workflow lifecycle orchestration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from server.state import StateManager
from workflow_system.catalog import validate_workflow, workflow_summary
from workflow_system.values import WorkflowValueStore


class WorkflowApplication:
    def __init__(self, state: StateManager,
                 value_store: Callable[[], WorkflowValueStore],
                 **services: Callable[..., Any]) -> None:
        self.state = state
        self._value_store = value_store
        self.s = services

    def apply_definition(self, workflow: dict[str, Any],
                         previous_workflow: dict[str, Any]) -> dict[str, Any]:
        self.state.workflow_id = workflow["id"]
        self.state.workflow_catalog.current_workflow = workflow["id"]
        self.state.workflow_snapshot = workflow
        allowed_modes = workflow.get("name_modes", ["original"])
        if self.state.mode not in allowed_modes:
            self.state.mode = allowed_modes[0]
        numbering = workflow.get("numbering", {})
        if numbering.get("enabled"):
            self.state.numeric_start = int(numbering.get("start", 1))
            self.state.numeric_width = max(1, int(numbering.get("width", 2)))
            self.state.numeric_step = int(numbering.get("step", 1)) or 1
        self.state.parse_template = "auto"
        self.state.parse_use_name = False
        regrouped = bool(
            self.state.groups and self.state.root
            and self.s["grouping_signature"](previous_workflow)
            != self.s["grouping_signature"](workflow)
        )
        if regrouped:
            mapping = None if self.state.directory_mapping_auto else self.state.directory_mapping
            result = self.s["scan_for_workflow"](
                self.state.root,
                self.state.include_hidden,
                self.state.include_system,
                mapping,
                workflow,
            )
            self.state.scan_result = result
            self.state.groups = result.groups
            self.state.current_group_key = next(iter(result.groups), None)
            self.state.group_enabled = {key: True for key in result.groups}
            self.state.extension_skipped.clear()
            self.s["apply_extension_defaults"](workflow, result)
            self.state.excel_mappings.clear()
            self.state.excel_skipped.clear()
            self.state.association_leaders.clear()
        if self.state.groups:
            self.s["apply_metadata"](self.s["all_records"](), workflow)
            if self.state.scan_result:
                self.s["apply_extension_defaults"](workflow, self.state.scan_result)
        self.s["initialise_values"](workflow)
        if self.state.groups:
            for group in self.state.groups.values():
                self.s["prepare_group"](group, workflow)
            self.s["expand_associated_records"]([
                record for record in self.s["all_records"]()
                if record.selected and not record.removed
            ])
        return workflow

    def sync_catalog(self) -> dict[str, Any]:
        refresh = self.state.workflow_catalog.refresh()
        if not refresh["changed"]:
            return refresh
        diagnostics = self.state.workflow_catalog.diagnostics()
        diagnostic_keys = {
            (issue.get("path", ""), issue.get("error", "")) for issue in diagnostics
        }
        for issue in diagnostics:
            key = (issue.get("path", ""), issue.get("error", ""))
            if key not in self.state.workflow_diagnostic_keys:
                self.state.log(
                    "WARN",
                    f"工作流插件未加载：{issue.get('folder') or issue.get('path')} · {issue.get('error')}",
                )
        self.state.workflow_diagnostic_keys = diagnostic_keys
        workflow = self.state.workflow_catalog.get(None)
        previous_workflow = self.state.workflow_snapshot
        active_changed = (
            self.state.workflow_id != workflow["id"] or previous_workflow != workflow
        )
        if active_changed:
            previous_id = self.state.workflow_id
            self.apply_definition(workflow, previous_workflow)
            if previous_id != workflow["id"]:
                self.state.log(
                    "WARN",
                    f"当前工作流 {previous_id} 已不可用，已切换到 {workflow['name']}。",
                )
            else:
                self.state.log("INFO", f"已重新加载工作流：{workflow['name']}。")
        return refresh

    def active_workflow(self) -> dict[str, Any]:
        self.sync_catalog()
        workflow = validate_workflow(
            self.state.workflow_catalog.get(self.state.workflow_id)
        )
        return self._apply_stored_tags(workflow)

    def workflow_state(self) -> dict[str, Any]:
        workflow = self.active_workflow()
        return {
            "active_id": self.state.workflow_id,
            "active": workflow,
            "available": [
                workflow_summary(item) for item in self.state.workflow_catalog.all()
            ],
            "values": dict(self.state.workflow_values),
            "candidates": {
                field_id: list(values)
                for field_id, values in self.state.workflow_candidates.items()
            },
            "revision": self.state.workflow_catalog.revision,
            "load_errors": self.state.workflow_catalog.diagnostics(),
        }

    def activate(self, workflow_id: str, *, persist: bool = True) -> dict[str, Any]:
        self.sync_catalog()
        previous_workflow = self.state.workflow_snapshot
        if persist:
            workflow = self.state.workflow_catalog.set_current(str(workflow_id))
        else:
            workflow = self.state.workflow_catalog.get(str(workflow_id))
            self.state.workflow_catalog.current_workflow = str(workflow_id)
        return self.apply_definition(workflow, previous_workflow)

    def _apply_stored_tags(self, workflow: dict[str, Any]) -> dict[str, Any]:
        try:
            stored_tags = self._value_store().read(workflow).get("tags", {})
        except (OSError, RuntimeError, ValueError):
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
                if bool(tag.get("enabled", True))
                and str(tag.get("value", tag.get("label", ""))).strip()
            ]
        return workflow
