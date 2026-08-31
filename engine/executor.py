"""Composition root for workflow evaluation and target preparation steps."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from core.files import FileRecord, NamingGroup
from .composer import compose_target, resolve_target_conflicts, workflow_profile, workflow_value
from .candidates import apply_module_result, module_items
from .rules import apply_rules, workflow_context


class WorkflowEngine:
    def __init__(self, normalise: Callable[[dict[str, Any], str, Any], str],
                 validate_filename: Callable[[str], str | None]) -> None:
        self._normalise = normalise
        self._validate_filename = validate_filename

    def context(self, record: FileRecord, workflow: dict[str, Any]) -> dict[str, Any]:
        return workflow_context(record, workflow)

    def apply_rules(self, record: FileRecord, workflow: dict[str, Any]) -> None:
        apply_rules(workflow, record, self._normalise)

    def value(self, workflow: dict[str, Any], group: NamingGroup, record: FileRecord,
              field_id: str, workflow_values: dict[str, str]) -> str:
        return workflow_value(workflow, group, record, field_id, workflow_values)

    def profile(self, workflow: dict[str, Any], group: NamingGroup, record: FileRecord,
                workflow_values: dict[str, str]) -> dict[str, Any] | None:
        return workflow_profile(workflow, group, record, workflow_values)

    def compose_target(self, workflow: dict[str, Any], group: NamingGroup, record: FileRecord,
                       workflow_values: dict[str, str], suffix_mode: str, separator: str) -> str:
        return compose_target(
            workflow, group, record, workflow_values, suffix_mode, separator, self._normalise
        )

    def resolve_conflicts(self, records: Iterable[FileRecord], workflow: dict[str, Any]) -> None:
        resolve_target_conflicts(records, workflow, self._validate_filename)

    def validate_filename(self, filename: str) -> str | None:
        return self._validate_filename(filename)

    def module_items(self, records: Iterable[FileRecord]) -> tuple[list[dict[str, Any]], dict[str, str]]:
        return module_items(records)

    def apply_module_result(self, workflow: dict[str, Any], module_id: str,
                            result: dict[str, Any], paths_by_item_id: dict[str, str],
                            records: Iterable[FileRecord], groups: dict[str, NamingGroup],
                            workflow_candidates: dict[str, list[str]]) -> int:
        return apply_module_result(
            workflow, module_id, result, paths_by_item_id, records, groups,
            workflow_candidates, self._normalise,
        )
