"""Cross-format association coordination for the active naming session."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.files import FileRecord, refresh_stem_associations
from server.state import StateManager


class AssociationService:
    def __init__(self, state: StateManager,
                 resolve_conflicts: Callable[[], None]) -> None:
        self.state = state
        self.resolve_conflicts = resolve_conflicts

    def all_records(self) -> list[FileRecord]:
        return [record for group in self.state.groups.values() for record in group.records]

    def expand(self, records: list[FileRecord],
               allowed_group_keys: set[str] | None = None) -> list[FileRecord]:
        ordered = list(dict.fromkeys(id(record) for record in records))
        by_id = {id(record): record for record in records}
        result = [by_id[record_id] for record_id in ordered]
        all_records = self.all_records()
        current_key = self.state.current_group_key
        handled: set[str] = set()

        for initial in list(result):
            association_id = initial.association_id
            if not association_id or association_id in handled:
                continue
            handled.add(association_id)
            candidates = [
                record for record in result if record.association_id == association_id
            ]
            remembered_path = self.state.association_leaders.get(association_id, "")
            leader = next((
                record for record in all_records
                if record.association_id == association_id and record.path == remembered_path
            ), None)
            if leader is None:
                leader = next((
                    record for record in candidates if record.group_key == current_key
                ), candidates[0])
                self.state.association_leaders[association_id] = leader.path
            target_stem = Path(
                leader.workflow_base_target_name or leader.target_name
            ).stem
            for related in all_records:
                if related.association_id != association_id or related.removed:
                    continue
                if allowed_group_keys is not None and related.group_key not in allowed_group_keys:
                    continue
                if not self.state.extension_enabled.get(related.extension.casefold(), True):
                    continue
                already_requested = any(item is related for item in result)
                excel_inherited = self.state.mode == "excel" and related.status == "未匹配"
                if not already_requested and not related.selected and not excel_inherited:
                    continue
                extension = related.extension_original or related.extension
                related.workflow_base_target_name = f"{target_stem}{extension}"
                related.target_name = related.workflow_base_target_name
                if excel_inherited:
                    related.selected = True
                    related.status = "Ready"
                    related.status_detail = "继承关联文件的 Excel 名称"
                if not already_requested:
                    result.append(related)
        self.resolve_conflicts()
        return result

    def refresh(self) -> None:
        associations = refresh_stem_associations(self.all_records())
        if self.state.scan_result is not None:
            self.state.scan_result.associations = associations
        valid_ids = {association["id"] for association in associations}
        self.state.association_leaders = {
            association_id: path
            for association_id, path in self.state.association_leaders.items()
            if association_id in valid_ids
        }

    def mark_leader(self, record: FileRecord) -> None:
        if record.association_id:
            self.state.association_leaders[record.association_id] = record.path

    def leave_excel_mode(self) -> None:
        for record in self.all_records():
            if record.removed:
                continue
            if record.status == "未匹配" or record.status_detail.startswith("Excel "):
                if self.state.extension_enabled.get(record.extension.casefold(), True):
                    record.selected = True
                    record.status = "Ready"
                    record.status_detail = ""

    def reconcile_history(self, items: list[dict[str, Any]], direction: str) -> None:
        for item in items:
            destination = Path(
                item["old_path"] if direction == "undo" else item["new_path"]
            )
            hints = {
                str(item.get("old_path", "")),
                str(item.get("new_path", "")),
                str(item.get("undo_source_path", "")),
                str(item.get("redo_source_path", "")),
            }
            group_key = str(item.get("group_key", ""))
            candidates = [
                record for record in self.all_records()
                if record.path in hints
                and (not group_key or record.group_key == group_key)
            ]
            if not candidates and group_key in self.state.groups:
                hint_names = {Path(path).name for path in hints if path}
                candidates = [
                    record for record in self.state.groups[group_key].records
                    if Path(record.path).name in hint_names
                ]
            if not candidates:
                continue
            record = candidates[0]
            record.path = str(destination)
            record.original_name = destination.name
            record.stem = destination.stem
            record.base_name = destination.stem
            record.name = destination.stem
            record.target_name = destination.name
            record.selected = True
            record.status = "Ready" if direction == "undo" else "Renamed"
            record.status_detail = ""
        self.refresh()
