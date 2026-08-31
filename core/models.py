"""Core data models shared across the naming layers.

Split out of the former ``core/files.py`` god module.  Pure dataclasses, no
filesystem or spreadsheet behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class FileRecord:
    path: str
    root: str
    extension: str
    folder_name: str
    relative_folder: str
    original_name: str
    stem: str
    selected: bool = True
    child_prefix: str = ""
    name: str = ""
    base_name: str = ""
    status: str = "Ready"
    status_detail: str = ""
    target_name: str = ""
    # The unsuffixed workflow target is retained so conflict disambiguation
    # can be recomputed after a preview refresh without stacking `_01` again.
    workflow_base_target_name: str = ""
    excel_source: str = ""
    group_key: str = ""
    extension_original: str = ""
    removed: bool = False
    parsed_fields: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    parse_unmatched: str = ""
    parse_confidence: float = 0.0
    parse_error: str = ""
    association_id: str = ""
    associated_extensions: list[str] = field(default_factory=list)
    # Values entered through the active declarative workflow.  They are kept
    # separate from the legacy fields so the default workflow remains fully
    # compatible with existing imports and undo history.
    workflow_values: dict[str, str] = field(default_factory=dict)
    workflow_candidates: dict[str, list[str]] = field(default_factory=dict)
    workflow_candidate_details: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    workflow_derived: dict[str, Any] = field(default_factory=dict)
    workflow_actions: set[str] = field(default_factory=set)
    workflow_auto_fields: set[str] = field(default_factory=set)
    workflow_number_fields: set[str] = field(default_factory=set)
    workflow_manual_fields: set[str] = field(default_factory=set)

    def __post_init__(self):
        if not self.name:
            self.name = self.stem
        if not self.base_name:
            self.base_name = self.name
        if not self.extension_original:
            self.extension_original = self.extension
        if not self.group_key:
            self.group_key = f"{self.relative_folder}\x1f{self.extension.casefold()}"

    @property
    def source_path(self) -> Path:
        return Path(self.path)

    @property
    def group_label(self) -> str:
        return f"{self.folder_name} / {self.extension.lstrip('.').upper() or '无扩展名'}"


@dataclass
class NamingGroup:
    key: str
    folder: str
    folder_name: str
    extension: str
    records: list[FileRecord] = field(default_factory=list)
    selected: bool = True
    prefix: str = ""
    relative_folder: str = ""
    meta_prefix: str = ""
    workflow_values: dict[str, str] = field(default_factory=dict)
    workflow_candidates: dict[str, list[str]] = field(default_factory=dict)
    # A workflow may combine several extensions into one logical group. The
    # legacy ``extension`` value remains for compatibility with single-format
    # groups, while this list drives the mixed-format label and API payload.
    extensions: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        folder = self.relative_folder or self.folder_name
        extension_label = "图像" if self.extension == ".image" else self.extension.lstrip(".").upper() or "无扩展名"
        return f"{folder} / {extension_label} ({len(self.records)})"


@dataclass
class ScanResult:
    root: str
    records: list[FileRecord]
    extension_counts: dict[str, int]
    groups: dict[str, NamingGroup]
    skipped: list[str] = field(default_factory=list)
    associations: list[dict[str, Any]] = field(default_factory=list)
    max_depth: int = 0


@dataclass
class LogEntry:
    level: str
    message: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"))


@dataclass
class RenameItem:
    old_path: str
    new_path: str
    group_key: str
    success: bool
    error: str = ""
    old_fingerprint: dict[str, Any] = field(default_factory=dict)
    new_fingerprint: dict[str, Any] = field(default_factory=dict)


@dataclass
class RenameOperation:
    operation_time: str
    kind: str
    items: list[RenameItem]
    transaction_status: str = "committed"
    transaction_error: str = ""


@dataclass
class ValidationIssue:
    record: FileRecord
    code: str
    message: str


@dataclass
class ExcelMatchResult:
    mode: str
    mapping: dict[str, str]
    matched_count: int
    unmatched_files: list[FileRecord]
    unmatched_rows: list[tuple[int, str, str]]
    warnings: list[str]
    matched_without_name: list[FileRecord] = field(default_factory=list)
    sheet_name: str = ""
    detail_mode: bool = False
