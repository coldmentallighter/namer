"""Declarative naming workflow definitions and portable workflow packages.

The naming engine consumes a small JSON document instead of hard-coding every
field in the WebUI.  Built-in workflows are shipped as editable JSON resources,
while user workflows are validated before they enter the application state.

The former god module was split: pure schema validation moved to
``workflow_system.schema`` and packaging/import to ``workflow_system.package``.
This module keeps the runtime catalog: discovery, hot reload and install
transactions.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .runtime import MODULE_MANIFEST_FILE_NAME, WorkflowModuleRegistry
from .package import _public_workflow, copy_workflow, package_workflow
from .schema import (
    CORE_FALLBACK_WORKFLOW,
    WORKFLOW_FILE_NAME,
    WORKFLOW_SCHEMA_VERSION,
    _FIELD_ID,
    validate_workflow,
)


RESOURCE_WORKFLOW_ROOT = Path(__file__).resolve().parent.parent / "workflows"


def workflow_root_signature(root: str | Path = RESOURCE_WORKFLOW_ROOT) -> tuple[tuple[str, int, int], ...]:
    """Return a cheap workflow and module fingerprint for plug-in monitoring."""
    root = Path(root)
    try:
        root_stat = root.stat()
        signature: list[tuple[str, int, int]] = [(".", root_stat.st_mtime_ns, root_stat.st_size)]
        folders = sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name.casefold())
    except OSError:
        return ((".", -1, -1),)
    for folder in folders:
        watched = [folder / WORKFLOW_FILE_NAME, folder / MODULE_MANIFEST_FILE_NAME]
        modules_dir = folder / "modules"
        if modules_dir.is_dir():
            watched.extend(
                path for path in modules_dir.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts and path.suffix.casefold() != ".pyc"
            )
        for path in watched:
            relative = path.relative_to(root).as_posix()
            try:
                stat = path.stat()
                signature.append((relative, stat.st_mtime_ns, stat.st_size))
            except OSError:
                signature.append((relative, -1, -1))
    return tuple(signature)


def discover_workflows(root: str | Path = RESOURCE_WORKFLOW_ROOT) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """Load every workflow directory independently and isolate broken plug-ins."""
    root = Path(root)
    workflows: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    try:
        folders = sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name.casefold())
    except OSError as exc:
        errors.append({
            "folder": "",
            "path": str(root),
            "error": f"工作流目录不可用: {exc}",
        })
        return workflows, errors
    for folder in folders:
        manifest = folder / WORKFLOW_FILE_NAME
        if not manifest.is_file():
            errors.append({
                "folder": folder.name,
                "path": str(manifest),
                "error": "缺少 workflow.json",
            })
            continue
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("工作流内容必须是 JSON 对象")
            raw["builtin"] = True
            workflow = validate_workflow(raw)
            workflow_id = workflow["id"]
            if workflow_id in workflows:
                raise ValueError(f"工作流 id 重复: {workflow_id}")
            workflow["_source_dir"] = str(folder.resolve())
            workflows[workflow_id] = workflow
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            errors.append({
                "folder": folder.name,
                "path": str(manifest),
                "error": str(exc),
            })
    return workflows, errors


def workflow_summary(workflow: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": workflow["id"],
        "name": workflow["name"],
        "version": workflow.get("version", "1.0.0"),
        "description": workflow.get("description", ""),
        "builtin": bool(workflow.get("builtin", False)),
        "kind": workflow.get("kind", "custom"),
        "field_count": len(workflow.get("fields", [])),
        "module_count": len(workflow.get("modules", [])),
    }


class WorkflowCatalog:
    """Monitor installed workflow plug-ins and persist user-owned workflows."""

    def __init__(self, config_path: str | Path,
                 workflow_root: str | Path | list[str | Path] | tuple[str | Path, ...] = RESOURCE_WORKFLOW_ROOT,
                 install_root: str | Path | None = None) -> None:
        self.path = Path(config_path)
        raw_roots = [workflow_root] if isinstance(workflow_root, (str, Path)) else list(workflow_root)
        self.install_root = Path(install_root) if install_root is not None else self.path.parent / "installed-workflows"
        self.install_root.mkdir(parents=True, exist_ok=True)
        raw_roots.append(self.install_root)
        roots: list[Path] = []
        seen_roots: set[str] = set()
        for item in raw_roots:
            root = Path(item)
            key = str(root.resolve()).casefold()
            if key not in seen_roots:
                roots.append(root)
                seen_roots.add(key)
        self.workflow_roots = tuple(roots or [RESOURCE_WORKFLOW_ROOT])
        self.workflow_root = self.workflow_roots[0]
        self.theme = "light"
        self.current_workflow = "default"
        self.builtin_workflows: dict[str, dict[str, Any]] = {}
        self.user_workflows: dict[str, dict[str, Any]] = {}
        self.load_errors: list[dict[str, str]] = []
        self.config_errors: list[dict[str, str]] = []
        self.module_registry = WorkflowModuleRegistry()
        self.revision = 0
        self._root_signature: tuple[tuple[str, tuple[tuple[str, int, int], ...]], ...] | None = None
        self.refresh(force=True)
        self.load()

    def _actual_ids(self) -> set[str]:
        return set(self.builtin_workflows) | set(self.user_workflows)

    def _resolve_current(self, requested: str | None) -> str:
        wanted = str(requested or "")
        actual_ids = self._actual_ids()
        if wanted in actual_ids:
            return wanted
        if "default" in actual_ids:
            return "default"
        if self.builtin_workflows:
            return next(iter(self.builtin_workflows))
        if self.user_workflows:
            return next(iter(self.user_workflows))
        return CORE_FALLBACK_WORKFLOW["id"]

    def refresh(self, *, force: bool = False) -> dict[str, Any]:
        signature = tuple(
            (str(root), workflow_root_signature(root)) for root in self.workflow_roots
        )
        if not force and signature == self._root_signature:
            return {
                "changed": False,
                "added": [],
                "removed": [],
                "updated": [],
                "current_changed": False,
                "revision": self.revision,
            }
        previous_signature = self._root_signature
        previous = self.builtin_workflows
        previous_errors = self.load_errors
        previous_current = self.current_workflow
        discovered: dict[str, dict[str, Any]] = {}
        errors: list[dict[str, str]] = []
        for root in self.workflow_roots:
            root_workflows, root_errors = discover_workflows(root)
            errors.extend(root_errors)
            for workflow_id, workflow in root_workflows.items():
                if workflow_id in discovered:
                    errors.append({
                        "folder": workflow_id,
                        "path": str(root),
                        "error": f"工作流 id 与其他安装目录冲突: {workflow_id}",
                    })
                    continue
                discovered[workflow_id] = workflow
        source_dirs = {
            workflow_id: workflow.get("_source_dir", "")
            for workflow_id, workflow in discovered.items()
            if workflow.get("_source_dir")
        }
        module_errors = self.module_registry.refresh(source_dirs, discovered)
        errors.extend(module_errors)
        invalid_module_workflows = {
            item.get("workflow_id", "") for item in module_errors
        }
        for workflow_id in invalid_module_workflows:
            discovered.pop(workflow_id, None)
        previous_ids = set(previous)
        discovered_ids = set(discovered)
        added = sorted(discovered_ids - previous_ids)
        removed = sorted(previous_ids - discovered_ids)
        roots_changed = previous_signature is not None and previous_signature != signature
        updated = sorted(
            workflow_id for workflow_id in previous_ids & discovered_ids
            if previous[workflow_id] != discovered[workflow_id] or roots_changed
        )
        changed = bool(added or removed or updated or previous_errors != errors)
        self.builtin_workflows = discovered
        self.load_errors = errors
        self._root_signature = signature
        self.current_workflow = self._resolve_current(self.current_workflow)
        current_changed = previous_current != self.current_workflow
        if changed or current_changed:
            self.revision += 1
        return {
            "changed": changed or current_changed,
            "added": added,
            "removed": removed,
            "updated": updated,
            "current_changed": current_changed,
            "revision": self.revision,
        }

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        self.theme = raw.get("theme", "light") if raw.get("theme") in {"light", "dark"} else "light"
        current = str(raw.get("current_workflow", "default"))
        self.user_workflows.clear()
        self.config_errors.clear()
        loaded = raw.get("workflows", {})
        if isinstance(loaded, list):
            loaded = {str(item.get("id")): item for item in loaded if isinstance(item, dict)}
        if isinstance(loaded, dict):
            for workflow_id, workflow in loaded.items():
                if not isinstance(workflow, dict):
                    continue
                try:
                    normalized = validate_workflow(workflow, allow_builtin=False)
                except (TypeError, ValueError) as exc:
                    self.config_errors.append({
                        "folder": "config.json",
                        "path": str(self.path),
                        "error": f"用户工作流 {workflow_id}: {exc}",
                    })
                    continue
                normalized["id"] = str(workflow_id) if _FIELD_ID.fullmatch(str(workflow_id)) else normalized["id"]
                self.user_workflows[normalized["id"]] = normalized
        self.current_workflow = self._resolve_current(current)

    def all_ids(self) -> set[str]:
        self.refresh()
        actual_ids = self._actual_ids()
        return actual_ids or {CORE_FALLBACK_WORKFLOW["id"]}

    def all(self) -> list[dict[str, Any]]:
        self.refresh()
        workflows = [_public_workflow(workflow) for workflow in self.builtin_workflows.values()] + [
            _public_workflow(workflow) for workflow in self.user_workflows.values()
        ]
        return workflows or [copy.deepcopy(CORE_FALLBACK_WORKFLOW)]

    def get(self, workflow_id: str | None) -> dict[str, Any]:
        self.refresh()
        wanted = str(workflow_id or self.current_workflow)
        workflow = self.builtin_workflows.get(wanted) or self.user_workflows.get(wanted)
        if workflow is None and not self._actual_ids() and wanted == CORE_FALLBACK_WORKFLOW["id"]:
            workflow = CORE_FALLBACK_WORKFLOW
        if workflow is None:
            raise KeyError(f"工作流不存在: {wanted}")
        return _public_workflow(workflow)

    def diagnostics(self) -> list[dict[str, str]]:
        self.refresh()
        return copy.deepcopy(self.load_errors + self.config_errors)

    def is_builtin(self, workflow_id: str) -> bool:
        self.refresh()
        return workflow_id in self.builtin_workflows or (
            workflow_id == CORE_FALLBACK_WORKFLOW["id"] and not self._actual_ids()
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": WORKFLOW_SCHEMA_VERSION,
            "theme": self.theme,
            "current_workflow": self.current_workflow,
            "workflows": self.user_workflows,
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def set_current(self, workflow_id: str) -> dict[str, Any]:
        if workflow_id not in self.all_ids():
            raise KeyError(f"工作流不存在: {workflow_id}")
        self.current_workflow = workflow_id
        self.save()
        return self.get(workflow_id)

    def upsert_import(self, workflow: dict[str, Any], strategy: str = "copy") -> tuple[dict[str, Any], bool]:
        self.refresh()
        workflow = copy.deepcopy(workflow)
        # Built-in definitions can be exported as examples, but an imported
        # copy is always user-owned and editable.
        workflow["builtin"] = False
        workflow = validate_workflow(workflow, allow_builtin=False)
        workflow = _public_workflow(workflow)
        existing = workflow["id"] in self.all_ids()
        if existing and strategy == "cancel":
            raise ValueError("已取消导入")
        if existing and strategy != "replace":
            workflow = copy_workflow(workflow, self.all_ids())
        self.user_workflows[workflow["id"]] = workflow
        self.current_workflow = workflow["id"]
        self.revision += 1
        self.save()
        return copy.deepcopy(workflow), existing

    def upsert_user_workflow(self, workflow: dict[str, Any]) -> dict[str, Any]:
        workflow = copy.deepcopy(workflow)
        workflow["builtin"] = False
        workflow = validate_workflow(workflow, allow_builtin=False)
        workflow = _public_workflow(workflow)
        if self.is_builtin(workflow["id"]):
            raise ValueError("内置工作流不可直接覆盖，请先导入为副本")
        self.user_workflows[workflow["id"]] = workflow
        self.current_workflow = workflow["id"]
        self.revision += 1
        self.save()
        return copy.deepcopy(workflow)

    def delete(self, workflow_id: str) -> None:
        self.refresh()
        if self.is_builtin(workflow_id):
            raise ValueError("内置工作流不可删除")
        if workflow_id not in self.user_workflows:
            raise KeyError(f"工作流不存在: {workflow_id}")
        del self.user_workflows[workflow_id]
        if self.current_workflow == workflow_id:
            self.current_workflow = self._resolve_current(None)
        self.revision += 1
        self.save()

    def package(self, workflow_id: str) -> bytes:
        self.refresh()
        workflow = self.builtin_workflows.get(workflow_id) or self.user_workflows.get(workflow_id)
        if workflow is None:
            raise KeyError(f"工作流不存在: {workflow_id}")
        return package_workflow(workflow)

    def install_package(self, workflow: dict[str, Any], package_files: dict[str, bytes],
                        strategy: str = "copy", *, trust_modules: bool = False) -> tuple[dict[str, Any], bool]:
        """Install a module-bearing package as one dynamically loaded directory."""
        if MODULE_MANIFEST_FILE_NAME not in package_files:
            return self.upsert_import(workflow, strategy)
        if not trust_modules:
            raise ValueError("工作流包包含 Python 模块；必须明确确认信任后才能安装")
        self.refresh()
        workflow = copy.deepcopy(workflow)
        workflow["builtin"] = False
        workflow = validate_workflow(workflow, allow_builtin=False)
        existing = workflow["id"] in self.all_ids()
        if existing and strategy == "cancel":
            raise ValueError("已取消导入")
        if existing and strategy != "replace":
            workflow = copy_workflow(workflow, self.all_ids())
        elif existing:
            existing_workflow = self.builtin_workflows.get(workflow["id"])
            existing_source = Path(str(existing_workflow.get("_source_dir", ""))).resolve() if existing_workflow else None
            if existing_source is not None and existing_source.parent != self.install_root.resolve():
                raise ValueError("内置工作流不可由外部模块包直接覆盖，请导入为副本")
        replace_user_workflow = bool(existing and strategy == "replace" and workflow["id"] in self.user_workflows)

        self.install_root.mkdir(parents=True, exist_ok=True)
        target = (self.install_root / workflow["id"]).resolve()
        if target.parent != self.install_root.resolve():
            raise ValueError("工作流安装路径无效")
        temporary = Path(tempfile.mkdtemp(prefix=".workflow-install-", dir=self.install_root))
        backup = temporary.with_name(temporary.name + ".backup")
        try:
            public_workflow = {
                key: value for key, value in workflow.items()
                if not str(key).startswith("_")
            }
            (temporary / WORKFLOW_FILE_NAME).write_text(
                json.dumps(public_workflow, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            for relative_name, data in package_files.items():
                parts = Path(relative_name.replace("\\", "/")).parts
                if (relative_name != MODULE_MANIFEST_FILE_NAME
                        and (not parts or parts[0] != "modules")):
                    continue
                destination = (temporary / Path(*parts)).resolve()
                if not destination.is_relative_to(temporary.resolve()):
                    raise ValueError("工作流包包含不安全模块路径")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            probe = WorkflowModuleRegistry()
            probe_errors = probe.refresh({workflow["id"]: temporary}, {workflow["id"]: workflow})
            if probe_errors:
                raise ValueError(probe_errors[0]["error"])
            if target.exists():
                os.replace(target, backup)
            try:
                os.replace(temporary, target)
            except Exception:
                if backup.exists() and not target.exists():
                    os.replace(backup, target)
                raise
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise
        self.current_workflow = workflow["id"]
        self.refresh(force=True)
        if workflow["id"] not in self.builtin_workflows:
            if target.exists():
                shutil.rmtree(target)
            if backup.exists():
                os.replace(backup, target)
            self.refresh(force=True)
            raise ValueError(f"工作流模块安装后未能加载: {workflow['id']}")
        if backup.exists():
            shutil.rmtree(backup)
        if replace_user_workflow:
            self.user_workflows.pop(workflow["id"], None)
        self.current_workflow = workflow["id"]
        self.save()
        return self.get(workflow["id"]), existing
