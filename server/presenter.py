"""JSON presentation of mutable application state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from core.models import FileRecord
from server.state import StateManager


class StatePresenter:
    def __init__(self, state: StateManager,
                 workflow_state: Callable[[], dict[str, Any]],
                 group_enabled: Callable[[str], bool]) -> None:
        self.state = state
        self.workflow_state = workflow_state
        self.group_enabled = group_enabled

    @staticmethod
    def record(record: FileRecord) -> dict[str, Any]:
        mime_type = str(record.metadata.get("file", {}).get("mime_type", ""))
        return {
            "path": record.path,
            "original_name": record.original_name,
            "extension": record.extension,
            "extension_original": record.extension_original,
            "folder_name": record.folder_name,
            "relative_folder": record.relative_folder,
            "target_name": record.target_name,
            "status": record.status,
            "status_detail": record.status_detail,
            "selected": record.selected,
            "removed": record.removed,
            "is_audio": mime_type.startswith("audio/"),
            "audio_format": mime_type.removeprefix("audio/").upper() or "AUDIO",
            "parsed_fields": record.parsed_fields,
            "parse_unmatched": record.parse_unmatched,
            "parse_confidence": record.parse_confidence,
            "parse_error": record.parse_error,
            "metadata": record.metadata,
            "association_id": record.association_id,
            "associated_extensions": record.associated_extensions,
            "workflow_values": record.workflow_values,
            "workflow_candidates": record.workflow_candidates,
            "workflow_candidate_details": record.workflow_candidate_details,
            "workflow_derived": record.workflow_derived,
            "workflow_actions": sorted(record.workflow_actions),
        }

    def state_json(self) -> dict[str, Any]:
        with self.state.lock:
            result = self.state.scan_result
            groups = []
            if result:
                for key, group in self.state.groups.items():
                    groups.append({
                        "key": key,
                        "label": group.label,
                        "folder": group.folder,
                        "folder_name": group.folder_name,
                        "relative_folder": group.relative_folder,
                        "extension": group.extension,
                        "extensions": group.extensions or [group.extension],
                        "workflow_values": group.workflow_values,
                        "workflow_candidates": group.workflow_candidates,
                        "enabled": self.group_enabled(key),
                        "count": sum(not record.removed for record in group.records),
                    })
            current = self.state.current_group()
            return {
                "root": self.state.root,
                "separator": self.state.separator,
                "mode": self.state.mode,
                "numeric": {
                    "start": self.state.numeric_start,
                    "width": self.state.numeric_width,
                    "step": self.state.numeric_step,
                },
                "directory_mapping": (
                    self.state.directory_mapping
                    if not self.state.directory_mapping_auto else None
                ),
                "directory_mapping_auto": self.state.directory_mapping_auto,
                "max_depth": result.max_depth if result else 0,
                "parse_template": self.state.parse_template,
                "parse_use_name": self.state.parse_use_name,
                "workflow": self.workflow_state(),
                "config": {
                    "theme": self.state.workflow_catalog.theme,
                    "current_workflow": self.state.workflow_id,
                },
                "include_hidden": self.state.include_hidden,
                "include_system": self.state.include_system,
                "extensions": result.extension_counts if result else {},
                "extension_enabled": dict(self.state.extension_enabled),
                "groups": groups,
                "current_group_key": self.state.current_group_key,
                "associations": result.associations if result else [],
                "records": [self.record(record) for record in current.records] if current else [],
                "total_file_count": (
                    sum(not record.removed for record in result.records) if result else 0
                ),
                "logs": [asdict(entry) for entry in self.state.logs[-120:]],
            }
