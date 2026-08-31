"""Dispatch workflow-declared metadata capabilities outside the naming core."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.files import FileRecord, read_file_metadata
from .runtime import WorkflowModuleRegistry


_DEFAULT_REGISTRY: WorkflowModuleRegistry | None = None


def _default_registry() -> WorkflowModuleRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        from .catalog import RESOURCE_WORKFLOW_ROOT, discover_workflows

        workflows, _errors = discover_workflows(RESOURCE_WORKFLOW_ROOT)
        workflow_dirs = {
            workflow_id: workflow.get("_source_dir", "")
            for workflow_id, workflow in workflows.items()
            if workflow.get("_source_dir")
        }
        registry = WorkflowModuleRegistry()
        registry.refresh(workflow_dirs, workflows)
        _DEFAULT_REGISTRY = registry
    return _DEFAULT_REGISTRY


def _runtime(registry: WorkflowModuleRegistry | None) -> WorkflowModuleRegistry:
    return registry if registry is not None else _default_registry()


def _merge(left: dict[str, Any], right: dict[str, Any]) -> None:
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(left.get(key), dict):
            _merge(left[key], value)
        else:
            left[key] = value


def read_workflow_metadata(workflow: dict[str, Any], path: str | Path,
                           root: str | Path | None = None,
                           registry: WorkflowModuleRegistry | None = None) -> dict[str, Any]:
    metadata = read_file_metadata(path, root)
    runtime = _runtime(registry)
    workflow_id = str(workflow.get("id", ""))
    for declaration in workflow.get("metadata_providers", []):
        provider_id = str(declaration.get("provider", ""))
        reader = runtime.provider(workflow_id, provider_id)
        values = reader(path, root, declaration.get("options", {}))
        if not isinstance(values, dict):
            raise ValueError(f"metadata provider 必须返回对象: {workflow_id}.{provider_id}")
        _merge(metadata, values)
    return metadata


def normalise_workflow_value(workflow: dict[str, Any], field_id: str, value: Any,
                             registry: WorkflowModuleRegistry | None = None) -> str:
    definition = next((field for field in workflow.get("fields", [])
                       if field.get("id") == field_id), {})
    normalizer_id = str(definition.get("normalizer", "") or "")
    if normalizer_id:
        normalizer = _runtime(registry).normalizer(str(workflow.get("id", "")), normalizer_id)
        value = normalizer(value)
    return str(value or "")


def parse_workflow_filename(workflow: dict[str, Any], stem: str,
                            template: str = "auto",
                            registry: WorkflowModuleRegistry | None = None) -> dict[str, Any]:
    """Use the parser declared by a workflow, or the generic core parser."""
    parser_id = str(workflow.get("filename_parser", "") or "").strip()
    if not parser_id:
        from core.files import parse_filename
        return parse_filename(stem, template)
    parser = _runtime(registry).filename_parser(str(workflow.get("id", "")), parser_id)
    result = parser(stem, template, workflow)
    if not isinstance(result, dict):
        raise ValueError(f"filename parser 必须返回对象: {workflow.get('id')}.{parser_id}")
    return result


def apply_workflow_metadata(records: list[FileRecord], workflow: dict[str, Any],
                            registry: WorkflowModuleRegistry | None = None) -> None:
    for record in records:
        try:
            record.metadata = read_workflow_metadata(workflow, record.path, record.root, registry)
        except OSError as exc:
            record.metadata = {"file": {"error": str(exc)}}
