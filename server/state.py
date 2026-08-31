"""Mutable session state for the local naming application."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Iterable

from core.models import LogEntry, NamingGroup
from workflow_system.catalog import WorkflowCatalog


class StateManager:
    def __init__(self, app_root: str | Path, workflow_roots: Iterable[str | Path]) -> None:
        app_root = Path(app_root)
        self.root: str = ""
        self.scan_result = None
        self.groups: dict[str, NamingGroup] = {}
        self.current_group_key: str | None = None
        self.separator: str = "_"
        self.mode: str = "original"
        self.numeric_start: int = 1
        self.numeric_width: int = 2
        self.numeric_step: int = 1
        self.directory_mapping: dict[str, int | None] = {"meta": -3, "group": -2, "child": -1}
        self.directory_mapping_auto: bool = True
        self.parse_template: str = "auto"
        self.parse_use_name: bool = False
        self.workflow_catalog = WorkflowCatalog(app_root / "config.json", workflow_roots)
        self.workflow_id: str = self.workflow_catalog.current_workflow
        self.workflow_snapshot: dict[str, Any] = self.workflow_catalog.get(self.workflow_id)
        self.workflow_values: dict[str, str] = {}
        self.workflow_candidates: dict[str, list[str]] = {}
        self.workflow_suffix_mode: str = ""
        workflow_numbering = self.workflow_snapshot.get("numbering", {})
        if workflow_numbering.get("enabled"):
            self.numeric_start = int(workflow_numbering.get("start", self.numeric_start))
            self.numeric_width = max(1, int(workflow_numbering.get("width", self.numeric_width)))
            self.numeric_step = int(workflow_numbering.get("step", self.numeric_step)) or 1
        self.include_hidden: bool = False
        self.include_system: bool = False
        self.group_enabled: dict[str, bool] = {}
        self.extension_skipped: set[str] = set()
        self.extension_enabled: dict[str, bool] = {}
        self.excel_mappings: dict[str, dict[str, str]] = {}
        self.excel_skipped: dict[str, set[str]] = {}
        self.association_leaders: dict[str, str] = {}
        self.logs: list[LogEntry] = []
        self.workflow_diagnostic_keys: set[tuple[str, str]] = set()
        for issue in self.workflow_catalog.diagnostics():
            key = (issue.get("path", ""), issue.get("error", ""))
            self.workflow_diagnostic_keys.add(key)
            self.logs.append(LogEntry(
                "WARN",
                f"工作流插件未加载：{issue.get('folder') or issue.get('path')} · {issue.get('error')}",
            ))
        self.history_path = app_root / "history" / "history.json"
        self.lock = threading.RLock()

    def log(self, level: str, message: str) -> None:
        self.logs.append(LogEntry(level, message))
        self.logs = self.logs[-300:]

    def current_group(self) -> NamingGroup | None:
        return self.groups.get(self.current_group_key) if self.current_group_key else None


AppState = StateManager
