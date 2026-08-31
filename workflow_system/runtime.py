"""Dynamic runtime for Python modules owned by installed workflows.

Only modules from installed workflow directories are loaded here. The module
manifest is an allowlist: code can expose only the providers, normalizers,
filename parsers, and runner declared beside that workflow.

Also hosts the workflow metadata capability dispatch formerly in
``workflow_system/metadata.py``: the provider/normalizer/parser getter
facade for the registry below.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.fsutil import read_file_metadata
from core.models import FileRecord


MODULE_MANIFEST_FILE_NAME = "module-manifest.json"
MODULE_SCHEMA_VERSION = 1
_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_CALLABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CAPABILITY_KEYS = ("providers", "normalizers", "filename_parsers")


class WorkflowModuleError(ValueError):
    """Raised when a workflow module contract is missing or invalid."""


@dataclass
class LoadedWorkflowRuntime:
    providers: dict[str, Callable[..., Any]] = field(default_factory=dict)
    normalizers: dict[str, Callable[..., Any]] = field(default_factory=dict)
    filename_parsers: dict[str, Callable[..., Any]] = field(default_factory=dict)
    runners: dict[str, Callable[..., Any]] = field(default_factory=dict)
    modules: dict[str, types.ModuleType] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)


def _normalise_capabilities(value: Any, location: str) -> dict[str, str]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise WorkflowModuleError(f"{location} 必须是 capability id 到函数名的对象")
    result: dict[str, str] = {}
    for raw_id, raw_callable in value.items():
        capability_id = str(raw_id).strip()
        callable_name = str(raw_callable).strip()
        if not _ID.fullmatch(capability_id):
            raise WorkflowModuleError(f"{location} capability id 无效: {capability_id}")
        if not _CALLABLE_NAME.fullmatch(callable_name):
            raise WorkflowModuleError(f"{location} 函数名无效: {callable_name}")
        result[capability_id] = callable_name
    return result


def validate_module_manifest(value: Any, workflow_dir: str | Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowModuleError("module-manifest.json 必须是 JSON 对象")
    schema_version = int(value.get("schema_version", MODULE_SCHEMA_VERSION))
    if schema_version != MODULE_SCHEMA_VERSION:
        raise WorkflowModuleError(f"不支持的 module schema_version: {schema_version}")
    modules = value.get("modules", [])
    if not isinstance(modules, list) or not modules:
        raise WorkflowModuleError("module-manifest.json 至少需要声明一个模块")

    root = Path(workflow_dir).resolve()
    result: list[dict[str, Any]] = []
    module_ids: set[str] = set()
    capability_ids: dict[str, set[str]] = {key: set() for key in _CAPABILITY_KEYS}
    for index, raw_module in enumerate(modules):
        if not isinstance(raw_module, dict):
            raise WorkflowModuleError(f"modules[{index}] 必须是对象")
        module_id = str(raw_module.get("id", "")).strip()
        if not _ID.fullmatch(module_id) or module_id in module_ids:
            raise WorkflowModuleError(f"模块 id 无效或重复: {module_id}")
        entrypoint_text = str(raw_module.get("entrypoint", "")).replace("\\", "/").strip()
        entrypoint_parts = Path(entrypoint_text).parts
        if (not entrypoint_text or Path(entrypoint_text).is_absolute()
                or ".." in entrypoint_parts or not entrypoint_text.casefold().endswith(".py")
                or not entrypoint_parts or entrypoint_parts[0] != "modules"):
            raise WorkflowModuleError(f"模块 entrypoint 必须是 modules 下的 Python 文件: {module_id}")
        entrypoint = (root / Path(*entrypoint_parts)).resolve()
        if not entrypoint.is_relative_to(root) or not entrypoint.is_file():
            raise WorkflowModuleError(f"模块 entrypoint 不存在: {entrypoint_text}")

        normalised: dict[str, Any] = {
            "id": module_id,
            "entrypoint": entrypoint_text,
        }
        for key in _CAPABILITY_KEYS:
            capabilities = _normalise_capabilities(raw_module.get(key, {}), f"modules.{module_id}.{key}")
            duplicate = capability_ids[key].intersection(capabilities)
            if duplicate:
                raise WorkflowModuleError(f"{key} capability 重复: {', '.join(sorted(duplicate))}")
            capability_ids[key].update(capabilities)
            normalised[key] = capabilities
        runner = str(raw_module.get("runner", "") or "").strip()
        if runner and not _CALLABLE_NAME.fullmatch(runner):
            raise WorkflowModuleError(f"模块 runner 函数名无效: {module_id}")
        normalised["runner"] = runner
        result.append(normalised)
        module_ids.add(module_id)
    return {"schema_version": schema_version, "modules": result}


def _safe_module_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", value)


def _ensure_package(name: str, path: Path | None = None) -> None:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        module.__package__ = name
        module.__path__ = [] if path is None else [str(path)]
        sys.modules[name] = module
    elif path is not None:
        module.__path__ = [str(path)]


def _workflow_package_name(workflow_id: str, workflow_dir: Path) -> str:
    source_token = hashlib.sha256(str(workflow_dir).casefold().encode("utf-8")).hexdigest()[:12]
    return f"_ffnf_workflows.{_safe_module_token(workflow_id)}_{source_token}"


def _load_python_module(workflow_id: str, module_id: str, entrypoint: Path) -> types.ModuleType:
    _ensure_package("_ffnf_workflows")
    workflow_package = _workflow_package_name(workflow_id, entrypoint.parent.parent)
    _ensure_package(workflow_package, entrypoint.parent)
    module_name = f"{workflow_package}.{_safe_module_token(module_id)}"
    for loaded_name in list(sys.modules):
        if loaded_name == module_name or loaded_name.startswith(module_name + "."):
            del sys.modules[loaded_name]
    spec = importlib.util.spec_from_file_location(module_name, entrypoint)
    if spec is None or spec.loader is None:
        raise WorkflowModuleError(f"无法创建模块加载器: {entrypoint}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _declared_callable(module: types.ModuleType, name: str, location: str) -> Callable[..., Any]:
    value = getattr(module, name, None)
    if not callable(value):
        raise WorkflowModuleError(f"模块未提供声明的函数: {location}.{name}")
    return value


class WorkflowModuleRegistry:
    """Load and dispatch capabilities without sharing them across workflows."""

    def __init__(self) -> None:
        self._runtimes: dict[str, LoadedWorkflowRuntime] = {}
        self._workflow_dirs: dict[str, Path] = {}
        self.errors: list[dict[str, str]] = []

    def refresh(self, workflow_dirs: dict[str, str | Path],
                workflows: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
        runtimes: dict[str, LoadedWorkflowRuntime] = {}
        errors: list[dict[str, str]] = []
        resolved_dirs = {workflow_id: Path(path).resolve() for workflow_id, path in workflow_dirs.items()}
        for workflow_id, workflow in workflows.items():
            folder = resolved_dirs.get(workflow_id)
            if folder is None:
                continue
            try:
                runtime = self._load_workflow(workflow_id, folder)
                self._validate_requirements(workflow_id, workflow, runtime)
                runtimes[workflow_id] = runtime
            except Exception as exc:
                errors.append({
                    "workflow_id": workflow_id,
                    "folder": folder.name,
                    "path": str(folder / MODULE_MANIFEST_FILE_NAME),
                    "error": f"工作流模块未加载: {exc}",
                })
        self._runtimes = runtimes
        self._workflow_dirs = resolved_dirs
        self.errors = errors
        return copy.deepcopy(errors)

    def _load_workflow(self, workflow_id: str, folder: Path) -> LoadedWorkflowRuntime:
        manifest_path = folder / MODULE_MANIFEST_FILE_NAME
        if not manifest_path.is_file():
            return LoadedWorkflowRuntime()
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowModuleError(f"module-manifest.json 无效: {exc}") from exc
        manifest = validate_module_manifest(raw, folder)
        runtime = LoadedWorkflowRuntime(manifest=manifest)
        workflow_package = _workflow_package_name(workflow_id, folder)
        for loaded_name in list(sys.modules):
            if loaded_name == workflow_package or loaded_name.startswith(workflow_package + "."):
                del sys.modules[loaded_name]
        for declaration in manifest["modules"]:
            entrypoint = folder / Path(*Path(declaration["entrypoint"]).parts)
            module = _load_python_module(workflow_id, declaration["id"], entrypoint)
            runtime.modules[declaration["id"]] = module
            for capability_key in _CAPABILITY_KEYS:
                target = getattr(runtime, capability_key)
                for capability_id, callable_name in declaration[capability_key].items():
                    target[capability_id] = _declared_callable(
                        module, callable_name, f"{declaration['id']}.{capability_key}.{capability_id}"
                    )
            if declaration["runner"]:
                runtime.runners[declaration["id"]] = _declared_callable(
                    module, declaration["runner"], f"{declaration['id']}.runner"
                )
        return runtime

    @staticmethod
    def _validate_requirements(workflow_id: str, workflow: dict[str, Any],
                               runtime: LoadedWorkflowRuntime) -> None:
        for declaration in workflow.get("metadata_providers", []):
            provider_id = str(declaration.get("provider", ""))
            if provider_id not in runtime.providers:
                raise WorkflowModuleError(f"metadata provider 未注册: {workflow_id}.{provider_id}")
        for field in workflow.get("fields", []):
            normalizer_id = str(field.get("normalizer", "") or "")
            if normalizer_id and normalizer_id not in runtime.normalizers:
                raise WorkflowModuleError(f"normalizer 未注册: {workflow_id}.{normalizer_id}")
        parser_id = str(workflow.get("filename_parser", "") or "")
        if parser_id and parser_id not in runtime.filename_parsers:
            raise WorkflowModuleError(f"filename parser 未注册: {workflow_id}.{parser_id}")
        for declaration in workflow.get("modules", []):
            module_id = str(declaration.get("id", ""))
            if module_id not in runtime.runners:
                raise WorkflowModuleError(f"runner 未注册: {workflow_id}.{module_id}")

    def _runtime(self, workflow_id: str) -> LoadedWorkflowRuntime:
        runtime = self._runtimes.get(workflow_id)
        if runtime is None:
            raise WorkflowModuleError(f"工作流没有可用模块运行时: {workflow_id}")
        return runtime

    def provider(self, workflow_id: str, provider_id: str) -> Callable[..., Any]:
        reader = self._runtime(workflow_id).providers.get(provider_id)
        if reader is None:
            raise WorkflowModuleError(f"metadata provider 未注册: {workflow_id}.{provider_id}")
        return reader

    def normalizer(self, workflow_id: str, normalizer_id: str) -> Callable[..., Any]:
        normalizer = self._runtime(workflow_id).normalizers.get(normalizer_id)
        if normalizer is None:
            raise WorkflowModuleError(f"normalizer 未注册: {workflow_id}.{normalizer_id}")
        return normalizer

    def filename_parser(self, workflow_id: str, parser_id: str) -> Callable[..., Any]:
        parser = self._runtime(workflow_id).filename_parsers.get(parser_id)
        if parser is None:
            raise WorkflowModuleError(f"filename parser 未注册: {workflow_id}.{parser_id}")
        return parser

    def module(self, workflow_id: str, module_id: str) -> types.ModuleType:
        module = self._runtime(workflow_id).modules.get(module_id)
        if module is None:
            raise WorkflowModuleError(f"模块未加载: {workflow_id}.{module_id}")
        return module

    def run(self, workflow: dict[str, Any], module_id: str, trigger: str,
            items: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
        workflow_id = str(workflow.get("id", ""))
        declaration = next(
            (item for item in workflow.get("modules", []) if item.get("id") == module_id),
            None,
        )
        if declaration is None:
            raise WorkflowModuleError(f"工作流未声明模块调用: {module_id}")
        if declaration.get("trigger") != trigger:
            raise WorkflowModuleError(
                f"模块 {module_id} 不能由 {trigger} 触发，应为 {declaration.get('trigger')}"
            )
        item_ids = [str(item.get("id", "")) for item in items]
        if any(not item_id for item_id in item_ids) or len(item_ids) != len(set(item_ids)):
            raise WorkflowModuleError("模块输入 item_id 不能为空或重复")
        runner = self._runtime(workflow_id).runners.get(module_id)
        if runner is None:
            raise WorkflowModuleError(f"runner 未注册: {workflow_id}.{module_id}")
        request = {
            "schema_version": MODULE_SCHEMA_VERSION,
            "workflow": {
                "id": workflow_id,
                "version": str(workflow.get("version", "1.0.0")),
            },
            "module": {
                "id": module_id,
                "trigger": trigger,
                "options": copy.deepcopy(declaration.get("options", {})),
            },
            "items": copy.deepcopy(items),
        }
        result = runner(request)
        return self._validate_result(declaration, item_ids, result), request

    @staticmethod
    def _validate_result(declaration: dict[str, Any], item_ids: list[str], result: Any) -> dict[str, Any]:
        if not isinstance(result, dict) or set(result) - {"items"}:
            raise WorkflowModuleError("模块返回值只能包含 items")
        result_items = result.get("items")
        if not isinstance(result_items, list):
            raise WorkflowModuleError("模块返回 items 必须是数组")
        known_items = set(item_ids)
        known_slots = {str(output.get("id", "")) for output in declaration.get("outputs", [])}
        seen_items: set[str] = set()
        normalised: list[dict[str, Any]] = []
        for raw_item in result_items:
            if not isinstance(raw_item, dict) or set(raw_item) - {"id", "values"}:
                raise WorkflowModuleError("模块返回 item 只能包含 id 和 values")
            if not isinstance(raw_item.get("id"), str):
                raise WorkflowModuleError("模块返回 item_id 必须是字符串")
            item_id = raw_item["id"]
            if item_id not in known_items or item_id in seen_items:
                raise WorkflowModuleError(f"模块返回了未知或重复的 item_id: {item_id}")
            values = raw_item.get("values")
            if not isinstance(values, dict):
                raise WorkflowModuleError(f"模块返回 values 必须是对象: {item_id}")
            if any(not isinstance(slot_id, str) for slot_id in values):
                raise WorkflowModuleError(f"模块输出槽 id 必须是字符串: {item_id}")
            unknown_slots = set(values) - known_slots
            if unknown_slots:
                raise WorkflowModuleError(f"模块返回了未声明的输出槽: {', '.join(sorted(unknown_slots))}")
            normalised_values: dict[str, str] = {}
            for raw_slot, value in values.items():
                slot_id = raw_slot
                if not isinstance(value, str):
                    raise WorkflowModuleError(f"模块输出必须是字符串: {item_id}.{slot_id}")
                if len(value) > 4096:
                    raise WorkflowModuleError(f"模块输出字符串过长: {item_id}.{slot_id}")
                normalised_values[slot_id] = value
            normalised.append({"id": item_id, "values": normalised_values})
            seen_items.add(item_id)
        return {"items": normalised}


# --- Workflow metadata capability dispatch (merged from metadata.py) ---

_DEFAULT_REGISTRY: WorkflowModuleRegistry | None = None


def _default_registry() -> WorkflowModuleRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        # Imported lazily: catalog imports this module at module level, so a
        # top-level import here would form a runtime <-> catalog cycle.
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
        from core.naming import parse_filename
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
