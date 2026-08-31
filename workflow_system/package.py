"""Portable workflow packaging and import with zip-safety checks.

Split out of the former ``workflow_system/catalog.py`` god module.  All
packaging/import I/O is concentrated here: zip size/item limits, path
traversal checks and module-file extraction.
"""

from __future__ import annotations

import copy
import io
import json
import zipfile
from pathlib import Path
from typing import Any

from .runtime import MODULE_MANIFEST_FILE_NAME
from .schema import WORKFLOW_FILE_NAME, WORKFLOW_SCHEMA_VERSION, validate_workflow


WORKFLOW_PACKAGE_EXTENSION = ".ffnf-workflow"


def package_workflow(workflow: dict[str, Any]) -> bytes:
    source_dir_text = str(workflow.get("_source_dir", "") or "")
    workflow = validate_workflow(workflow)
    workflow = {
        key: value for key, value in workflow.items()
        if not str(key).startswith("_")
    }
    manifest = {
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "type": "ffnf-workflow",
        "id": workflow["id"],
        "name": workflow["name"],
        "version": workflow.get("version", "1.0.0"),
        "software": "OfflineFileNamer",
        "has_modules": bool(source_dir_text and (Path(source_dir_text) / MODULE_MANIFEST_FILE_NAME).is_file()),
    }
    vocabularies = {
        field["id"]: field.get("quick_tags", [])
        for field in workflow.get("fields", [])
        if field.get("quick_tags")
    }
    examples = workflow.get("examples", [])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr(WORKFLOW_FILE_NAME, json.dumps(workflow, ensure_ascii=False, indent=2))
        archive.writestr("vocabularies.json", json.dumps(vocabularies, ensure_ascii=False, indent=2))
        archive.writestr("examples.json", json.dumps(examples, ensure_ascii=False, indent=2))
        archive.writestr("README.md", f"# {workflow['name']}\n\n{workflow.get('description', '')}\n")
        if source_dir_text:
            source_dir = Path(source_dir_text)
            module_manifest = source_dir / MODULE_MANIFEST_FILE_NAME
            if module_manifest.is_file():
                archive.write(module_manifest, MODULE_MANIFEST_FILE_NAME)
                modules_dir = source_dir / "modules"
                if modules_dir.is_dir():
                    for path in sorted(modules_dir.rglob("*")):
                        if (not path.is_file() or "__pycache__" in path.parts
                                or path.suffix.casefold() == ".pyc"):
                            continue
                        archive.write(path, path.relative_to(source_dir).as_posix())
    return buffer.getvalue()


def load_workflow_bundle(data: bytes, filename: str = "workflow.json") -> tuple[dict[str, Any], dict[str, bytes]]:
    package_files: dict[str, bytes] = {}
    if filename.casefold().endswith(WORKFLOW_PACKAGE_EXTENSION):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                archive_items = archive.infolist()
                if len(archive_items) > 512 or sum(item.file_size for item in archive_items) > 64 * 1024 * 1024:
                    raise ValueError("工作流包文件数量或解压后大小超出限制")
                raw_names = archive.namelist()
                normalised_names = [name.replace("\\", "/") for name in raw_names]
                if len(normalised_names) != len(set(normalised_names)):
                    raise ValueError("工作流包包含重复路径")
                names = set(raw_names)
                if WORKFLOW_FILE_NAME not in names:
                    raise ValueError("工作流包缺少 workflow.json")
                for raw_name in raw_names:
                    name = raw_name.replace("\\", "/")
                    parts = name.split("/")
                    if ("\x00" in name or name.startswith("/") or ".." in parts or ":" in parts[0]
                            or any(not part for part in parts[:-1])):
                        raise ValueError("工作流包包含不安全路径")
                workflow = json.loads(archive.read(WORKFLOW_FILE_NAME).decode("utf-8"))
                vocabularies = json.loads(archive.read("vocabularies.json").decode("utf-8")) if "vocabularies.json" in names else {}
                examples = json.loads(archive.read("examples.json").decode("utf-8")) if "examples.json" in names else []
                for name in raw_names:
                    normalised_name = name.replace("\\", "/")
                    if (normalised_name == MODULE_MANIFEST_FILE_NAME
                            or normalised_name.startswith("modules/")) and not normalised_name.endswith("/"):
                        package_files[normalised_name] = archive.read(name)
        except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"工作流包无效: {exc}") from exc
        if isinstance(vocabularies, dict):
            for field in workflow.get("fields", []) if isinstance(workflow, dict) else []:
                if field.get("id") in vocabularies and not field.get("quick_tags"):
                    field["quick_tags"] = vocabularies[field["id"]]
        if isinstance(workflow, dict) and isinstance(examples, list):
            workflow["examples"] = examples
    else:
        try:
            workflow = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"workflow.json 无效: {exc}") from exc
    if isinstance(workflow, dict) and isinstance(workflow.get("workflow"), dict):
        workflow = workflow["workflow"]
    if not isinstance(workflow, dict):
        raise ValueError("工作流内容必须是 JSON 对象")
    workflow = {
        key: value for key, value in workflow.items()
        if not str(key).startswith("_")
    }
    workflow["builtin"] = False
    return validate_workflow(workflow, allow_builtin=False), package_files


def load_workflow_package(data: bytes, filename: str = "workflow.json") -> dict[str, Any]:
    workflow, _package_files = load_workflow_bundle(data, filename)
    return workflow


def copy_workflow(workflow: dict[str, Any], existing_ids: set[str]) -> dict[str, Any]:
    result = copy.deepcopy(workflow)
    base_id = result["id"]
    candidate = base_id
    suffix = 2
    while candidate in existing_ids:
        candidate = f"{base_id}_{suffix}"
        suffix += 1
    result["id"] = candidate
    result["name"] = f"{result['name']}（副本）" if candidate != base_id else result["name"]
    result["builtin"] = False
    return validate_workflow(result, allow_builtin=False)


def _public_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy({
        key: value for key, value in workflow.items()
        if not str(key).startswith("_")
    })
