"""Excel naming-table import controller."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import tempfile
from typing import Any

from core.models import ExcelMatchResult
from core.xlsx import import_xlsx
from server.state import StateManager


class ExcelController:
    def __init__(self, state: StateManager, **services: Callable[..., Any]) -> None:
        self.state = state
        self.s = services

    def import_table(self, form: dict[str, Any]) -> dict[str, Any]:
        upload = form.get("file")
        if not isinstance(upload, tuple) or not upload[0]:
            raise ValueError("未选择 XLSX 文件")
        with self.state.lock:
            group_key = str(form.get("group_key", self.state.current_group_key or ""))
            group = self.state.groups.get(group_key)
            if not group:
                raise KeyError("命名组不存在")
            filename, upload_data = upload
            suffix = Path(filename).suffix or ".xlsx"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp:
                temp.write(upload_data)
                temp_path = Path(temp.name)
            try:
                requested_sheet = str(form.get("sheet_name", "")).strip() or None
                match: ExcelMatchResult = import_xlsx(
                    temp_path, group, requested_sheet, self.s["expand_excel_name"]
                )
            finally:
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            self.state.excel_mappings[group.key] = match.mapping
            self.state.excel_skipped[group.key] = {
                record.path for record in match.matched_without_name
            }
            self.state.mode = "excel"
            self.state.group_enabled[group.key] = True
            self.s["prepare_group"](group)
            for record in group.records:
                if record.path in match.mapping:
                    self.s["mark_association_leader"](record)
            self.s["expand_associated_records"]([
                record for record in group.records if record.selected and not record.removed
            ])
            detail_label = f"，工作表 {match.sheet_name}" if match.sheet_name else ""
            self.state.log(
                "INFO",
                f"Excel 匹配完成：成功 {match.matched_count}，未匹配文件 {len(match.unmatched_files)}，"
                f"未匹配行 {len(match.unmatched_rows)}{detail_label}。",
            )
            for warning in match.warnings:
                self.state.log("WARN", warning.removeprefix("WARN "))
            preview = {
                "matched": match.matched_count,
                "unmatched_files": len(match.unmatched_files),
                "unmatched_rows": len(match.unmatched_rows),
                "warnings": match.warnings,
                "sheet": match.sheet_name,
                "detail": match.detail_mode,
            }
        return {"ok": True, "match": preview, "state": self.s["state_json"]()}
