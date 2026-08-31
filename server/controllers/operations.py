"""Rename, history, and export request controller."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from core.history import append_history, redo_last, undo_last
from core.models import RenameOperation
from core.rename import execute_rename
from core.scan import scan_folder
from core.xlsx import collect_directory_statistics, export_filename_tables
from server.state import StateManager


class OperationController:
    def __init__(self, state: StateManager, **services: Callable[..., Any]) -> None:
        self.state = state
        self.s = services

    def rename(self, payload: dict[str, Any]) -> dict[str, Any]:
        scope = str(payload.get("scope", "group"))
        with self.state.lock:
            if not self.state.groups:
                raise ValueError("请先扫描根目录")
            requested_root = str(payload.get("root", self.state.root)).strip()
            if requested_root and Path(requested_root).expanduser().resolve() != Path(self.state.root).expanduser().resolve():
                raise ValueError("根目录已修改，请先重新扫描再执行重命名")
            operations: list[RenameOperation] = []
            if scope == "single":
                path = str(payload.get("path", ""))
                record = next((r for g in self.state.groups.values() for r in g.records if r.path == path), None)
                if not record:
                    raise KeyError("文件记录不存在")
                self.s["prepare_group"](self.state.groups[record.group_key])
                record.selected = True
                linked = self.s["expand_associated"]([record])
                operations.append(execute_rename(linked, self.state.history_path, kind="single", separator=self.state.separator))
            elif scope == "group":
                group = self.state.current_group()
                if group is not None:
                    self.s["prepare_group"](group)
                    selected = [r for r in group.records if r.selected and not r.removed]
                    operations.append(execute_rename(
                        self.s["expand_associated"](selected), self.state.history_path,
                        kind="batch", separator=self.state.separator,
                    ))
            else:
                operations = self._rename_all()
            self.s["refresh_associations"]()
            success = sum(item.success for operation in operations for item in operation.items)
            failed = sum(not item.success for operation in operations for item in operation.items)
            self.state.log("ERROR" if failed else "INFO", f"重命名完成：成功 {success}" + (f"，失败/冲突 {failed}。" if failed else " 个文件。"))
            statuses = [operation.transaction_status for operation in operations]
            if any(status == "rolled_back" for status in statuses):
                self.state.log("WARN", "事务式重命名：部分命名组执行失败，已尝试自动回滚。")
            elif statuses and all(status == "committed" for status in statuses):
                self.state.log("INFO", "事务式重命名：已提交所有无冲突命名组。")
        return {"ok": True, "success": success, "failed": failed, "state": self.s["state_json"]()}

    def _rename_all(self) -> list[RenameOperation]:
        enabled = {key for key in self.state.groups if self.s["group_enabled"](key)}
        groups = [group for key, group in self.state.groups.items() if key in enabled]
        current = self.state.current_group()
        if current in groups:
            groups.remove(current)
            groups.insert(0, current)
        for group in groups:
            self.s["prepare_group"](group)
        operations: list[RenameOperation] = []
        processed: set[int] = set()
        for group in groups:
            selected = [r for r in group.records if r.selected and not r.removed and id(r) not in processed]
            if not selected:
                continue
            linked = [r for r in self.s["expand_associated"](selected, enabled) if id(r) not in processed]
            processed.update(id(record) for record in linked)
            operations.append(execute_rename(
                linked, self.state.history_path, kind="batch", separator=self.state.separator,
                write_history=False,
            ))
        items = [item for operation in operations for item in operation.items]
        if items and any(item.success for item in items):
            append_history(self.state.history_path, RenameOperation(
                datetime.now().astimezone().isoformat(timespec="seconds"), "batch", items
            ))
        return operations

    def history(self, direction: str) -> dict[str, Any]:
        with self.state.lock:
            before = self.s["read_history"](self.state.history_path)
            ok, errors = (undo_last if direction == "undo" else redo_last)(self.state.history_path)
            changed = self.s["changed_history"](
                before, self.s["read_history"](self.state.history_path), direction
            )
            if changed:
                self.s["reconcile_history"](changed, direction)
                suffix = "" if ok else "（部分成功）"
                action = "最近一次重命名已撤销" if direction == "undo" else "最近一次撤销的重命名已还原"
                self.state.log("INFO", f"{action}{suffix}：{self.s['history_description'](changed)}。")
            elif ok:
                self.state.log("INFO", "最近一次重命名已撤销。" if direction == "undo" else "最近一次撤销的重命名已还原。")
            for error in errors:
                self.state.log("ERROR", error)
        return {"ok": ok, "errors": errors, "state": self.s["state_json"]()}

    def export(self, payload: dict[str, Any]) -> dict[str, Any]:
        root = str(payload.get("root", self.state.root)).strip()
        hidden, system = bool(payload.get("include_hidden", False)), bool(payload.get("include_system", False))
        filetree_existed = (Path(root).expanduser().resolve() / "filetree.txt").exists()
        workflow = self.s["active_workflow"]()
        outputs = export_filename_tables(
            root, payload.get("extensions", []), hidden, system,
            metadata_reader=lambda path, root_path: self.s["read_metadata"](workflow, path, root_path),
        )
        stats = collect_directory_statistics(root, include_hidden=hidden, include_system=system)
        with self.state.lock:
            xlsx = [output for output in outputs if output.suffix.casefold() == ".xlsx"]
            filetree = next((output for output in outputs if output.name.casefold() == "filetree.txt"), None)
            self.state.log("INFO", f"导出完成：{len(xlsx)} 个 XLSX" + ("，已生成目录索引文件。" if filetree else "。"))
            self.state.log("INFO", f"目录统计：{stats['directory_count']} 个目录，{stats['file_count']} 个文件，{stats['content_directory_count']} 个内容目录。")
            if filetree_existed and not filetree:
                self.state.log("WARN", "目录索引 filetree.txt 已存在，未覆盖原文件。")
            for output in outputs:
                self.state.log("INFO", str(output))
        return {"ok": True, "outputs": [str(x) for x in outputs], "xlsx_outputs": [str(x) for x in xlsx],
                "filetree_output": str(filetree) if filetree else "", "export_stats": stats,
                "state": self.s["state_json"]()}

    def export_scan(self, payload: dict[str, Any]) -> dict[str, Any]:
        root = str(payload.get("root", "")).strip()
        if not root:
            raise ValueError("请选择根目录")
        result = scan_folder(root, include_hidden=bool(payload.get("include_hidden", False)),
                             include_system=bool(payload.get("include_system", False)))
        return {"ok": True, "root": result.root, "extensions": result.extension_counts,
                "total_file_count": len(result.records)}
