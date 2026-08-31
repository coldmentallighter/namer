"""Composition root for workflow evaluation and target preparation steps.

Also owns the workflow-module execution protocol: request construction
(``module_items``) and candidate result mapping (``apply_module_result``),
formerly in ``engine/candidates.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from core.models import FileRecord, NamingGroup
from .composer import compose_target, resolve_target_conflicts, workflow_profile, workflow_value
from .rules import apply_rules, workflow_context


def module_items(records: Iterable[FileRecord]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    items: list[dict[str, Any]] = []
    paths_by_item_id: dict[str, str] = {}
    for index, record in enumerate(records, start=1):
        item_id = f"item-{index:06d}"
        paths_by_item_id[item_id] = record.path
        items.append({
            "id": item_id,
            "path": record.path,
            "name": record.original_name,
            "relative_folder": record.relative_folder,
            "extension": record.extension,
            "metadata": record.metadata,
        })
    return items, paths_by_item_id


def apply_module_result(workflow: dict[str, Any], module_id: str, result: dict[str, Any],
                        paths_by_item_id: dict[str, str], records: Iterable[FileRecord],
                        groups: dict[str, NamingGroup], workflow_candidates: dict[str, list[str]],
                        normalise: Callable[[dict[str, Any], str, Any], str]) -> int:
    current_records = {record.path: record for record in records}
    records_by_item_id = {
        item_id: current_records[path]
        for item_id, path in paths_by_item_id.items()
        if path in current_records
    }
    if any(item["id"] not in records_by_item_id for item in result["items"]):
        raise ValueError("模块执行期间文件任务已发生变化，请重新运行")
    declaration = next(item for item in workflow.get("modules", []) if item["id"] == module_id)
    outputs = {output["id"]: output for output in declaration["outputs"]}
    added = 0
    for item_result in result["items"]:
        record = records_by_item_id[item_result["id"]]
        group = groups.get(record.group_key)
        for slot_id, raw_value in item_result["values"].items():
            if not raw_value.strip():
                continue
            output = outputs[slot_id]
            field_id = output["field"]
            value = normalise(workflow, field_id, raw_value)
            if not value.strip():
                continue
            scope = output["scope"]
            if scope == "workflow":
                candidates = workflow_candidates.setdefault(field_id, [])
            elif scope == "group":
                if group is None:
                    continue
                candidates = group.workflow_candidates.setdefault(field_id, [])
            else:
                candidates = record.workflow_candidates.setdefault(field_id, [])
            if value in candidates:
                continue
            candidates.append(value)
            added += 1
            if scope == "record":
                record.workflow_candidate_details.setdefault(field_id, []).append({
                    "value": value,
                    "rule_id": f"module:{module_id}:{slot_id}",
                    "reason": declaration.get("description") or f"模块建议：{declaration.get('label', module_id)}",
                    "mode": "suggest",
                    "module_id": module_id,
                    "slot_id": slot_id,
                })
    return added


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
