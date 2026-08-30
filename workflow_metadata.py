"""Dispatch workflow-declared metadata providers outside the naming core."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from namer_core import FileRecord, read_file_metadata
from workflow_modules import image_assets, sample_pack, wallpaper


MetadataReader = Callable[[str | Path, str | Path | None, dict[str, Any] | None], dict[str, Any]]
ValueNormalizer = Callable[[Any], Any]
FilenameParser = Callable[..., dict[str, Any]]


def _read_image_metadata(path, root, _options=None):
    return image_assets.read_metadata(path, root)


def _read_sample_pack_metadata(path, root, _options=None):
    return sample_pack.read_metadata(path, root)


PROVIDERS: dict[str, MetadataReader] = {
    "image_dimensions": _read_image_metadata,
    "sample_pack": _read_sample_pack_metadata,
}

NORMALIZERS: dict[str, ValueNormalizer] = {
    "sample_pack_scale": sample_pack.normalise_scale,
    "sample_pack_bpm": sample_pack.normalise_bpm,
}

FILENAME_PARSERS: dict[str, FilenameParser] = {
    "sample_pack": sample_pack.parse_filename,
    "wallpaper": wallpaper.parse_filename,
}


def _merge(left: dict[str, Any], right: dict[str, Any]) -> None:
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(left.get(key), dict):
            _merge(left[key], value)
        else:
            left[key] = value


def read_workflow_metadata(workflow: dict[str, Any], path: str | Path,
                           root: str | Path | None = None) -> dict[str, Any]:
    metadata = read_file_metadata(path, root)
    for declaration in workflow.get("metadata_providers", []):
        provider_id = str(declaration.get("provider", ""))
        reader = PROVIDERS.get(provider_id)
        if reader is None:
            continue
        _merge(metadata, reader(path, root, declaration.get("options", {})))
    return metadata


def normalise_workflow_value(workflow: dict[str, Any], field_id: str, value: Any) -> str:
    definition = next((field for field in workflow.get("fields", [])
                       if field.get("id") == field_id), {})
    normalizer = NORMALIZERS.get(str(definition.get("normalizer", "")))
    result = normalizer(value) if normalizer else value
    return str(result or "")


def parse_workflow_filename(workflow: dict[str, Any], stem: str,
                            template: str = "auto") -> dict[str, Any]:
    """Use the parser declared by a workflow, or the generic core parser."""
    parser_id = str(workflow.get("filename_parser", "") or "").strip()
    parser = FILENAME_PARSERS.get(parser_id)
    if parser is None:
        from namer_core import parse_filename
        return parse_filename(stem, template)
    if parser_id == "sample_pack":
        return parser(stem, template, workflow)
    return parser(stem, template)


def apply_workflow_metadata(records: list[FileRecord], workflow: dict[str, Any]) -> None:
    for record in records:
        try:
            record.metadata = read_workflow_metadata(workflow, record.path, record.root)
        except OSError as exc:
            record.metadata = {"file": {"error": str(exc)}}
