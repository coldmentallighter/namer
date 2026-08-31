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
import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
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

# Uninstalled module workflows are isolated here before verification and
# cleanup; entries older than the retention window are rotated away.
TRASH_DIR_NAME = ".trash"
TRASH_RETENTION_DAYS = 7


def workflow_root_signature(root: str | Path = RESOURCE_WORKFLOW_ROOT) -> tuple[tuple[str, int, int], ...]:
    """Return a cheap workflow and module fingerprint for plug-in monitoring."""
    root = Path(root)
    try:
        root_stat = root.stat()
        signature: list[tuple[str, int, int]] = [(".", root_stat.st_mtime_ns, root_stat.st_size)]
        folders = sorted(
            (item for item in root.iterdir() if item.is_dir() and not item.name.startswith(".")),
            key=lambda item: item.name.casefold(),
        )
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
        # Dot-prefixed folders are housekeeping areas (.trash isolation,
        # .workflow-install-* staging) and are never workflow plug-ins.
        folders = sorted(
            (item for item in root.iterdir() if item.is_dir() and not item.name.startswith(".")),
            key=lambda item: item.name.casefold(),
        )
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
        self.disabled_workflows: dict[str, dict[str, Any]] = {}
        self.user_workflows: dict[str, dict[str, Any]] = {}
        self.load_errors: list[dict[str, str]] = []
        self.config_errors: list[dict[str, str]] = []
        self.module_registry = WorkflowModuleRegistry()
        self.installations: dict[str, dict[str, Any]] = {}
        self.disabled_ids: set[str] = set()
        self.revision = 0
        self._root_signature: tuple[tuple[str, tuple[tuple[str, int, int], ...]], ...] | None = None
        # Config state first (installations/disabled feed refresh), then the
        # initial load, which re-resolves the current workflow.
        self.load()
        self.refresh(force=True)

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
        # Disabled workflows stay discoverable for the management list but are
        # not loaded: no selector entry and no module code is registered.
        self.disabled_workflows = {}
        for workflow_id in list(discovered):
            if workflow_id in self.disabled_ids:
                self.disabled_workflows[workflow_id] = discovered.pop(workflow_id)
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
        self.installations.clear()
        loaded_installations = raw.get("installations", {})
        if isinstance(loaded_installations, dict):
            for installation_id, record in loaded_installations.items():
                if not isinstance(record, dict) or not record.get("workflow_id"):
                    continue
                record = dict(record)
                record["installation_id"] = str(installation_id)
                self.installations[str(installation_id)] = record
        loaded_disabled = raw.get("disabled_workflows", [])
        self.disabled_ids = {
            str(item) for item in loaded_disabled
        } if isinstance(loaded_disabled, list) else set()
        self._migrate_installations()
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
        return (actual_ids | set(self.disabled_workflows)) or {CORE_FALLBACK_WORKFLOW["id"]}

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
            "installations": self.installations,
            "disabled_workflows": sorted(self.disabled_ids),
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def set_current(self, workflow_id: str) -> dict[str, Any]:
        if workflow_id in self.disabled_ids:
            raise KeyError(f"工作流已停用: {workflow_id}")
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
        self.disabled_ids.discard(workflow_id)
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

    def _code_sha256(self, source_dir: str | Path) -> str:
        """Fingerprint the Python code of an installed module workflow."""
        root = Path(source_dir)
        manifest = root / MODULE_MANIFEST_FILE_NAME
        paths: list[Path] = []
        if manifest.is_file():
            paths.append(manifest)
        modules_dir = root / "modules"
        if modules_dir.is_dir():
            paths.extend(sorted(
                path for path in modules_dir.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts and path.suffix.casefold() != ".pyc"
            ))
        if not paths:
            return ""
        digest = hashlib.sha256()
        for path in paths:
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def _rotate_trash(self, trash_root: Path) -> None:
        cutoff = time.time() - TRASH_RETENTION_DAYS * 86400
        try:
            items = list(trash_root.iterdir())
        except OSError:
            return
        for item in items:
            if not item.is_dir():
                continue
            try:
                if item.stat().st_mtime < cutoff:
                    shutil.rmtree(item)
            except OSError:
                continue

    def _migrate_installations(self) -> None:
        """Synthesize installation records for pre-existing install directories.

        Legacy installs are considered trusted: the installed code is by
        definition the state that was accepted at the time.
        """
        if not self.install_root.is_dir():
            return
        recorded_dirs = {
            str(Path(str(record.get("source_dir", ""))).resolve()).casefold()
            for record in self.installations.values()
        }
        try:
            folders = [
                item for item in self.install_root.iterdir()
                if item.is_dir() and not item.name.startswith(".")
            ]
        except OSError:
            return
        for folder in folders:
            if str(folder.resolve()).casefold() in recorded_dirs:
                continue
            if not (folder / WORKFLOW_FILE_NAME).is_file():
                continue
            try:
                raw = json.loads((folder / WORKFLOW_FILE_NAME).read_text(encoding="utf-8"))
                workflow_id = str(raw.get("id", folder.name))
                name = str(raw.get("name", workflow_id))
                version = str(raw.get("version", "1.0.0"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                workflow_id = folder.name
                name = folder.name
                version = ""
            code_sha = self._code_sha256(folder)
            try:
                installed_at = datetime.fromtimestamp(folder.stat().st_mtime).astimezone().isoformat(timespec="seconds")
            except OSError:
                installed_at = ""
            installation_id = uuid.uuid4().hex
            self.installations[installation_id] = {
                "installation_id": installation_id,
                "workflow_id": workflow_id,
                "name": name,
                "version": version,
                "kind": "module",
                "source_dir": str(folder.resolve()),
                "installed_at": installed_at,
                "package_sha256": "",
                "code_sha256": code_sha,
                "trusted_sha256": code_sha,
            }

    def _manifest_capabilities(self, source_dir: str | Path) -> dict[str, list[str]]:
        try:
            manifest = json.loads(
                (Path(source_dir) / MODULE_MANIFEST_FILE_NAME).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"providers": [], "normalizers": [], "filename_parsers": [], "runners": []}
        providers: set[str] = set()
        normalizers: set[str] = set()
        parsers: set[str] = set()
        runners: set[str] = set()
        for module in manifest.get("modules", []) if isinstance(manifest, dict) else []:
            if not isinstance(module, dict):
                continue
            providers.update(str(key) for key in module.get("providers", {}))
            normalizers.update(str(key) for key in module.get("normalizers", {}))
            parsers.update(str(key) for key in module.get("filename_parsers", {}))
            if module.get("runner"):
                runners.add(str(module["runner"]))
        return {
            "providers": sorted(providers),
            "normalizers": sorted(normalizers),
            "filename_parsers": sorted(parsers),
            "runners": sorted(runners),
        }

    def _manage_entry(self, workflow_id: str, workflow: dict[str, Any],
                      record: dict[str, Any] | None) -> dict[str, Any]:
        code_sha = str(record.get("code_sha256", "") or "") if record else ""
        trusted_sha = str(record.get("trusted_sha256", "") or "") if record else ""
        kind = "module" if record else ("resource" if workflow_id in self.builtin_workflows else "config")
        source_dir = str(workflow.get("_source_dir", "") or "")
        return {
            "workflow_id": workflow_id,
            "name": str(workflow.get("name", workflow_id)),
            "version": str(workflow.get("version", "1.0.0")),
            "kind": kind,
            "enabled": workflow_id not in self.disabled_ids,
            "current": workflow_id == self.current_workflow,
            "source_dir": source_dir,
            "installation_id": str(record["installation_id"]) if record else "",
            "installed_at": str(record.get("installed_at", "")) if record else "",
            "package_sha256": str(record.get("package_sha256", "")) if record else "",
            "code_sha256": code_sha,
            "trusted_sha256": trusted_sha,
            "field_count": len(workflow.get("fields", [])),
            "module_count": len(workflow.get("modules", [])),
            "trust": "no-code" if not code_sha else ("trusted" if code_sha == trusted_sha else "changed"),
            "capabilities": self._manifest_capabilities(source_dir) if record else {
                "providers": [], "normalizers": [], "filename_parsers": [], "runners": []
            },
            "diagnostics": [
                item["error"] for item in self.load_errors if item.get("folder") == workflow_id
            ],
        }

    def manage_list(self) -> dict[str, Any]:
        """Return the management inventory across all workflow sources."""
        self.refresh()
        records_by_workflow: dict[str, dict[str, Any]] = {}
        for record in self.installations.values():
            records_by_workflow.setdefault(str(record.get("workflow_id", "")), record)
        seen: set[str] = set()
        entries: list[dict[str, Any]] = []
        for workflow_id, workflow in self.builtin_workflows.items():
            if workflow_id in seen:
                continue
            seen.add(workflow_id)
            entries.append(self._manage_entry(workflow_id, workflow, records_by_workflow.get(workflow_id)))
        for workflow_id, workflow in self.disabled_workflows.items():
            if workflow_id in seen:
                continue
            seen.add(workflow_id)
            entries.append(self._manage_entry(workflow_id, workflow, records_by_workflow.get(workflow_id)))
        for workflow_id, workflow in self.user_workflows.items():
            if workflow_id in seen:
                continue
            seen.add(workflow_id)
            entries.append(self._manage_entry(workflow_id, workflow, records_by_workflow.get(workflow_id)))
        for record in self.installations.values():
            workflow_id = str(record.get("workflow_id", ""))
            if workflow_id in seen:
                continue
            seen.add(workflow_id)
            entries.append(self._manage_entry(workflow_id, {
                "id": workflow_id,
                "name": record.get("name", workflow_id),
                "version": record.get("version", "1.0.0"),
                "fields": [],
                "modules": [],
                "_source_dir": record.get("source_dir", ""),
                "builtin": True,
            }, record))
        entries.sort(key=lambda item: (not item["current"], str(item["name"]).casefold()))
        return {
            "workflows": entries,
            "current_workflow": self.current_workflow,
            "revision": self.revision,
            "load_errors": copy.deepcopy(self.load_errors + self.config_errors),
        }

    def set_enabled(self, workflow_id: str, enabled: bool) -> dict[str, Any]:
        self.refresh()
        known = (
            workflow_id in self.builtin_workflows
            or workflow_id in self.disabled_workflows
            or workflow_id in self.user_workflows
            or any(str(record.get("workflow_id")) == workflow_id for record in self.installations.values())
        )
        if not known:
            raise KeyError(f"工作流不存在: {workflow_id}")
        if enabled:
            self.disabled_ids.discard(workflow_id)
        else:
            if workflow_id == self.current_workflow:
                raise ValueError("当前使用的工作流不能停用，请先切换到其他工作流")
            self.disabled_ids.add(workflow_id)
        self.save()
        self.refresh(force=True)
        workflow = (
            self.builtin_workflows.get(workflow_id)
            or self.disabled_workflows.get(workflow_id)
            or self.user_workflows.get(workflow_id)
        )
        record = next(
            (record for record in self.installations.values()
             if str(record.get("workflow_id")) == workflow_id),
            None,
        )
        return self._manage_entry(workflow_id, workflow or {"id": workflow_id, "name": workflow_id}, record)

    def uninstall(self, installation_id: str) -> dict[str, Any]:
        """Isolate an installed module workflow, verify, then clean up.

        The directory is first moved into the trash area (recoverable), a
        forced refresh must confirm the workflow is gone, and only then is the
        record dropped.  Any failure moves the directory back.
        """
        record = self.installations.get(installation_id)
        if record is None:
            raise KeyError(f"安装记录不存在: {installation_id}")
        workflow_id = str(record.get("workflow_id", ""))
        if workflow_id == self.current_workflow:
            raise ValueError("当前使用的工作流不能卸载，请先切换到其他工作流")
        self.refresh()
        source = Path(str(record.get("source_dir", ""))).resolve()
        if source.parent != self.install_root.resolve() or not source.is_dir():
            raise ValueError("安装目录无效或已不存在")
        trash_root = self.install_root / TRASH_DIR_NAME
        trash_root.mkdir(parents=True, exist_ok=True)
        self._rotate_trash(trash_root)
        target = trash_root / f"{workflow_id}-{uuid.uuid4().hex[:8]}"
        os.replace(source, target)
        try:
            self.refresh(force=True)
            if workflow_id in self.builtin_workflows or workflow_id in self.disabled_workflows:
                raise RuntimeError("刷新后工作流仍然存在")
        except Exception:
            if target.exists() and not source.exists():
                os.replace(target, source)
            self.refresh(force=True)
            raise
        del self.installations[installation_id]
        self.disabled_ids.discard(workflow_id)
        self.save()
        return {"ok": True, "workflow_id": workflow_id, "trash_path": str(target)}

    def install_package(self, workflow: dict[str, Any], package_files: dict[str, bytes],
                        strategy: str = "copy", *, trust_modules: bool = False,
                        package_sha256: str = "") -> tuple[dict[str, Any], bool]:
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
        existing_record = next(
            (record for record in self.installations.values()
             if str(record.get("workflow_id")) == workflow["id"]),
            None,
        )
        code_sha = self._code_sha256(target)
        if existing_record:
            installation_id = str(existing_record["installation_id"])
            installed_at = str(existing_record.get("installed_at", ""))
        else:
            installation_id = uuid.uuid4().hex
            installed_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        self.installations[installation_id] = {
            "installation_id": installation_id,
            "workflow_id": workflow["id"],
            "name": workflow["name"],
            "version": workflow.get("version", "1.0.0"),
            "kind": "module",
            "source_dir": str(target),
            "installed_at": installed_at,
            "package_sha256": package_sha256,
            "code_sha256": code_sha,
            # Installing a module package requires explicit trust, so the code
            # fingerprint at install time is the trusted baseline.
            "trusted_sha256": code_sha,
        }
        self.disabled_ids.discard(workflow["id"])
        self.current_workflow = workflow["id"]
        self.save()
        return self.get(workflow["id"]), existing
