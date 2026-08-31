"""Workflow-aware file scanning and logical grouping."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.files import (
    FileRecord,
    NamingGroup,
    directory_prefix_defaults,
    natural_key,
    refresh_stem_associations,
    scan_folder,
)
from server.state import StateManager


class WorkflowScanService:
    def __init__(self, state: StateManager,
                 metadata_reader: Callable[[dict[str, Any], str, str], dict[str, Any]]) -> None:
        self.state = state
        self.metadata_reader = metadata_reader

    @staticmethod
    def grouping_signature(workflow: dict[str, Any]) -> tuple[str, str]:
        grouping = workflow.get("grouping", {})
        if not isinstance(grouping, dict):
            grouping = {}
        return (
            str(grouping.get("mode", "extension") or "extension").strip().casefold(),
            str(grouping.get("filter", "all") or "all").strip().casefold(),
        )

    @staticmethod
    def _resource_kind(record: FileRecord) -> str:
        extension = record.extension.casefold()
        mime_type = str(record.metadata.get("file", {}).get("mime_type", "")).casefold()
        if extension in {".mid", ".midi"}:
            return "midi"
        if mime_type.startswith("audio/"):
            return "audio"
        if mime_type.startswith("image/"):
            return "artwork"
        if extension in {".fxp", ".fxb", ".fst", ".vstpreset", ".aupreset", ".nksf"}:
            return "preset"
        if mime_type.startswith("text/") or extension in {
            ".pdf", ".doc", ".docx", ".rtf", ".md", ".xlsx", ".xls",
        }:
            return "document"
        return "other"

    def initial_extension_enabled(self, workflow: dict[str, Any],
                                  result: Any) -> dict[str, bool]:
        resource_filter = workflow.get("resource_filter", {})
        included = {
            str(item).strip().casefold()
            for item in resource_filter.get("include", [])
        } if isinstance(resource_filter, dict) else set()
        skip_mismatch = (
            isinstance(resource_filter, dict)
            and str(resource_filter.get("on_mismatch", "include")).casefold() == "skip"
            and bool(included)
        )
        defaults: dict[str, bool] = {}
        for extension in result.extension_counts:
            matching_records = [
                record for record in result.records
                if record.extension.casefold() == extension.casefold()
            ]
            defaults[extension.casefold()] = (
                not skip_mismatch
                or any(self._resource_kind(record) in included for record in matching_records)
            )
        return defaults

    def apply_extension_defaults(self, workflow: dict[str, Any], result: Any) -> None:
        defaults = self.initial_extension_enabled(workflow, result)
        for record in result.records:
            extension = record.extension.casefold()
            was_enabled = self.state.extension_enabled.get(extension, True)
            is_enabled = defaults.get(extension, True)
            if not is_enabled:
                if record.selected and not record.removed:
                    self.state.extension_skipped.add(record.path)
                record.selected = False
            elif not was_enabled and record.path in self.state.extension_skipped:
                if not record.removed:
                    record.selected = True
                self.state.extension_skipped.discard(record.path)
        self.state.extension_enabled = defaults

    def apply_grouping(self, result: Any, workflow: dict[str, Any]) -> Any:
        """Project a physical scan into the logical groups requested by a workflow."""
        mode, grouping_filter = self.grouping_signature(workflow)
        if mode != "directory":
            return result

        records = result.records
        if grouping_filter == "image":
            records = [
                record for record in records
                if isinstance(record.metadata.get("image"), dict)
                and record.metadata["image"].get("available") is True
            ]

        groups: dict[str, NamingGroup] = {}
        for record in records:
            key = f"{record.relative_folder}\x1f{grouping_filter}"
            record.group_key = key
            group = groups.get(key)
            if group is None:
                default_meta, default_group, _default_child = directory_prefix_defaults(
                    result.root, record.source_path.parent
                )
                group = NamingGroup(
                    key,
                    str(record.source_path.parent),
                    record.folder_name,
                    f".{grouping_filter}",
                    prefix=default_group,
                    relative_folder=record.relative_folder,
                    meta_prefix=default_meta,
                    extensions=[],
                )
                groups[key] = group
            group.records.append(record)
            if record.extension not in group.extensions:
                group.extensions.append(record.extension)

        for group in groups.values():
            group.records.sort(key=lambda item: natural_key(item.original_name))
            group.extensions.sort(key=natural_key)
        result.records = sorted(
            records,
            key=lambda item: (natural_key(item.relative_folder), natural_key(item.original_name)),
        )
        result.groups = groups
        result.extension_counts = {
            extension: sum(1 for record in records if record.extension == extension)
            for extension in sorted({record.extension for record in records}, key=natural_key)
        }
        result.associations = refresh_stem_associations(records)
        return result

    def scan(self, root: str, include_hidden: bool, include_system: bool,
             mapping: dict[str, int | None] | None,
             workflow: dict[str, Any]) -> Any:
        result = scan_folder(
            root,
            include_hidden,
            include_system,
            mapping,
            metadata_reader=lambda path, root_path: self.metadata_reader(
                workflow, str(path), str(root_path)
            ),
        )
        return self.apply_grouping(result, workflow)
