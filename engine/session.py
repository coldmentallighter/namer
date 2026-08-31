"""Stateful workflow naming session orchestration."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import re
from typing import Any

from core.files import FileRecord, NamingGroup, apply_filename_parse, directory_prefix_defaults
from workflow_system.catalog import workflow_field_map
from .executor import WorkflowEngine
from .rules import path_value


class WorkflowSession:
    def __init__(self, state: Any, engine: WorkflowEngine,
                 normalise: Callable[[dict[str, Any], str, Any], str],
                 active_workflow: Callable[[], dict[str, Any]],
                 parse_filename: Callable[[dict[str, Any], str, str], dict[str, Any]],
                 all_records: Callable[[], list[FileRecord]]) -> None:
        self.state = state
        self.engine = engine
        self.normalise = normalise
        self.active_workflow = active_workflow
        self.parse_filename = parse_filename
        self.all_records = all_records

    def directory_source_values(self, folder: str | Path) -> dict[str, str]:
        mapping = None if self.state.directory_mapping_auto else self.state.directory_mapping
        meta, group, child = directory_prefix_defaults(self.state.root, folder, mapping)
        return {
            "directory.meta": meta,
            "directory.group": group,
            "directory.child": child,
        }

    def initial_field_value(self, definition: dict[str, Any],
                            group: NamingGroup | None = None,
                            record: FileRecord | None = None) -> str:
        source = str(definition.get("initial_source", "") or "")
        if source == "stem" and record is not None:
            return str(record.base_name or record.stem)
        if source.startswith("directory."):
            folder = (
                record.source_path.parent if record is not None
                else group.folder if group is not None else ""
            )
            if folder:
                return str(self.directory_source_values(folder).get(source, ""))
        return str(definition.get("default", "") or "")

    @staticmethod
    def remember_initial_value(record: FileRecord, definition: dict[str, Any],
                               value: str) -> None:
        if definition.get("initial_source") == "stem":
            record.base_name = value
            record.name = value

    def apply_directory_mapping(self, mapping: dict[str, int | None], auto: bool) -> None:
        fields = workflow_field_map(self.active_workflow())
        self.state.directory_mapping = mapping
        self.state.directory_mapping_auto = auto
        for group in self.state.groups.values():
            for field_id, definition in fields.items():
                if (definition["scope"] == "group"
                        and str(definition.get("initial_source", "")).startswith("directory.")):
                    group.workflow_values[field_id] = self.initial_field_value(definition, group)
            for record in group.records:
                for field_id, definition in fields.items():
                    if (definition["scope"] in {"record", "suffix"}
                            and str(definition.get("initial_source", "")).startswith("directory.")):
                        record.workflow_values[field_id] = self.initial_field_value(
                            definition, group, record
                        )
                        record.workflow_manual_fields.discard(field_id)
                        record.workflow_auto_fields.add(field_id)
        current = self.state.current_group()
        current_record = current.records[0] if current and current.records else None
        for field_id, definition in fields.items():
            if (definition["scope"] == "workflow"
                    and str(definition.get("initial_source", "")).startswith("directory.")):
                self.state.workflow_values[field_id] = self.initial_field_value(
                    definition, current, current_record
                )

    def initialise_values(self, workflow: dict[str, Any] | None = None) -> None:
        workflow = workflow or self.active_workflow()
        fields = workflow_field_map(workflow)
        current = self.state.current_group()
        current_record = current.records[0] if current and current.records else None
        self.state.workflow_values = {
            field_id: self.initial_field_value(definition, current, current_record)
            for field_id, definition in fields.items()
            if definition["scope"] == "workflow"
        }
        self.state.workflow_candidates = {}
        suffix_modes = workflow.get("suffix_modes", {})
        suffix_field = str(workflow.get("suffix_field", "") or "")
        configured_suffix = (
            self.state.workflow_values.get(suffix_field, "") if suffix_field else ""
        )
        if configured_suffix not in suffix_modes:
            configured_suffix = (
                "scale_bpm" if "scale_bpm" in suffix_modes else next(iter(suffix_modes), "")
            )
        self.state.workflow_suffix_mode = configured_suffix
        for group in self.state.groups.values():
            self._initialise_group_values(group, workflow, fields)

    def _initialise_group_values(self, group: NamingGroup, workflow: dict[str, Any],
                                 fields: dict[str, dict[str, Any]]) -> None:
        group.workflow_values = {}
        group.workflow_candidates = {}
        for field_id, definition in fields.items():
            if definition["scope"] == "group":
                group.workflow_values[field_id] = self.initial_field_value(definition, group)
        for record in group.records:
            self._initialise_record_values(record, group, workflow, fields)
        for field_id, definition in fields.items():
            if definition["scope"] not in {"workflow", "group"}:
                continue
            candidates: list[str] = []
            for record in group.records:
                value = self.normalise(
                    workflow, field_id, record.parsed_fields.get(field_id, "")
                )
                if value.strip() and value not in candidates:
                    candidates.append(value)
            if not candidates:
                continue
            if definition["scope"] == "group":
                group.workflow_candidates[field_id] = candidates
            else:
                shared = self.state.workflow_candidates.setdefault(field_id, [])
                for value in candidates:
                    if value not in shared:
                        shared.append(value)

    def _initialise_record_values(self, record: FileRecord, group: NamingGroup,
                                  workflow: dict[str, Any],
                                  fields: dict[str, dict[str, Any]]) -> None:
        record.workflow_values = {}
        record.workflow_candidates = {}
        record.workflow_candidate_details = {}
        record.workflow_derived = {}
        record.workflow_actions = set()
        record.workflow_auto_fields = set()
        record.workflow_number_fields = set()
        record.workflow_manual_fields = set()
        parsed_candidates = self.parse_filename(workflow, record.stem, "auto")
        parsed_fields = parsed_candidates.get("fields", {})
        if isinstance(parsed_fields, dict) and parsed_fields:
            for field_id in fields:
                record.parsed_fields.pop(field_id, None)
            record.parsed_fields.update({
                str(field_id): str(value)
                for field_id, value in parsed_fields.items()
                if str(value).strip()
            })
        sample_metadata = record.metadata.get("sample_pack", {})
        parsed_bpm = str(record.parsed_fields.get("bpm", "") or "")
        embedded_bpm = (
            str(sample_metadata.get("bpm_metadata", "") or "")
            if isinstance(sample_metadata, dict) else ""
        )
        if parsed_bpm and embedded_bpm and parsed_bpm != embedded_bpm:
            sample_metadata["bpm_warning"] = (
                f"文件名 BPM {parsed_bpm} 与内部 metadata BPM {embedded_bpm} 冲突，"
                "优先采用文件名 token"
            )
        for field_id, definition in fields.items():
            if definition["scope"] not in {"record", "suffix"}:
                continue
            value = self.initial_field_value(definition, group, record)
            candidates = self._extract_candidates(
                workflow, field_id, definition, record, parsed_candidates
            )
            if candidates:
                record.workflow_candidates[field_id] = candidates
            record.workflow_values[field_id] = str(value or "")
            parsed_value = self.normalise(
                workflow, field_id, record.parsed_fields.get(field_id, "")
            )
            if parsed_value and definition.get("autofill"):
                record.workflow_values[field_id] = parsed_value
                record.workflow_auto_fields.add(field_id)
            if (record.workflow_values[field_id]
                    and record.workflow_values[field_id]
                    == str(definition.get("default", "") or "")):
                record.workflow_auto_fields.add(field_id)
        self.engine.apply_rules(record, workflow)

    def _extract_candidates(self, workflow: dict[str, Any], field_id: str,
                            definition: dict[str, Any], record: FileRecord,
                            parsed_candidates: dict[str, Any]) -> list[str]:
        extractors = list(definition.get("extractors", []))
        if definition.get("extractor"):
            extractors.append(str(definition["extractor"]))
        candidates: list[str] = []
        for extractor in extractors:
            candidate_value: Any = None
            if extractor in {"stem", "filename"}:
                candidate_value = record.stem
            elif extractor == "extension" and record.extension:
                candidate_value = record.extension.lstrip(".")
            elif extractor == "parent" and record.folder_name:
                candidate_value = record.folder_name
            elif extractor == "relative_folder" and record.relative_folder:
                candidate_value = record.relative_folder
            elif extractor == "number":
                candidate_value = parsed_candidates.get("fields", {}).get("number")
            elif (extractor
                  and extractor.split(".", 1)[0] in {"metadata", "record", "derived"}):
                candidate_value = path_value(self.engine.context(record, workflow), extractor)
            candidate = self.normalise(workflow, field_id, candidate_value)
            if candidate.strip() and candidate not in candidates:
                candidates.append(candidate)
        return candidates

    def value(self, workflow: dict[str, Any], group: NamingGroup,
              record: FileRecord, field_id: str) -> str:
        return self.engine.value(
            workflow, group, record, field_id, self.state.workflow_values
        )

    def profile(self, workflow: dict[str, Any], group: NamingGroup,
                record: FileRecord) -> dict[str, Any] | None:
        return self.engine.profile(workflow, group, record, self.state.workflow_values)

    def compose_target(self, workflow: dict[str, Any], group: NamingGroup,
                       record: FileRecord) -> str:
        return self.engine.compose_target(
            workflow, group, record, self.state.workflow_values,
            self.state.workflow_suffix_mode, self.state.separator,
        )

    def resolve_target_conflicts(self) -> None:
        self.engine.resolve_conflicts(self.all_records(), self.active_workflow())

    def assign_numbers(self, workflow: dict[str, Any], group: NamingGroup,
                       overrides: dict[str, int] | None = None) -> None:
        definitions: list[tuple[str | None, dict[str, Any]]] = []
        numbering = workflow.get("numbering", {})
        if numbering.get("enabled"):
            definitions.append((None, numbering))
        for profile in workflow.get("profiles", []):
            profile_numbering = profile.get("numbering", {})
            if profile_numbering.get("enabled"):
                definitions.append((profile["id"], profile_numbering))
        for profile_id, definition in definitions:
            self._assign_numbering_definition(
                workflow, group, definition, overrides, profile_id
            )

    def _assign_numbering_definition(self, workflow: dict[str, Any],
                                     group: NamingGroup, numbering: dict[str, Any],
                                     overrides: dict[str, int] | None,
                                     profile_id: str | None) -> None:
        field_id = str(numbering.get("field", ""))
        overrides = overrides or {}
        width = max(1, int(overrides.get("width", numbering.get("width", 2))))
        start = int(overrides.get("start", numbering.get("start", 1)))
        step = max(1, int(overrides.get("step", numbering.get("step", 1))))
        group_by = [str(item) for item in numbering.get("group_by", [])]
        eligible = [
            record for record in group.records
            if not record.removed and record.selected
            and (profile_id is None
                 or (self.profile(workflow, group, record) or {}).get("id") == profile_id)
        ]
        reserved: dict[tuple[str, ...], set[int]] = {}
        for record in eligible:
            key = tuple(self.value(workflow, group, record, field) for field in group_by)
            value = record.workflow_values.get(field_id, "")
            if field_id in record.workflow_number_fields:
                record.workflow_values[field_id] = ""
                value = ""
            try:
                if value.strip():
                    reserved.setdefault(key, set()).add(int(value))
            except ValueError:
                pass
        counters: dict[tuple[str, ...], int] = {}
        for record in eligible:
            key = tuple(self.value(workflow, group, record, field) for field in group_by)
            value = record.workflow_values.get(field_id, "")
            if value.strip() and field_id not in record.workflow_number_fields:
                continue
            candidate = counters.get(key, start)
            used = reserved.setdefault(key, set())
            while candidate in used:
                candidate += step
            record.workflow_values[field_id] = str(candidate).zfill(width)
            record.workflow_auto_fields.add(field_id)
            record.workflow_number_fields.add(field_id)
            used.add(candidate)
            counters[key] = candidate + step

    @staticmethod
    def excel_field(workflow: dict[str, Any], fields: dict[str, dict]) -> str:
        configured = str(workflow.get("excel_field", "") or "")
        if configured in fields and fields[configured].get("scope") in {"record", "suffix"}:
            return configured
        for candidate in ("name", "detail"):
            if candidate in fields and fields[candidate].get("scope") in {"record", "suffix"}:
                return candidate
        return ""

    def expand_excel_name(self, value: str, record: FileRecord,
                          row_values: dict[str, str]) -> str:
        workflow = self.active_workflow()
        context = self.engine.context(record, workflow)
        placeholders = workflow.get("excel_placeholders", {})

        def replace(match: re.Match[str]) -> str:
            placeholder = match.group(1).casefold()
            direct = row_values.get(placeholder, "")
            if direct:
                return self.normalise(workflow, placeholder, direct)
            source_path = placeholders.get(placeholder, "")
            resolved = path_value(context, source_path) if source_path else None
            return (
                self.normalise(workflow, placeholder, resolved)
                if resolved is not None else ""
            )

        expanded = re.sub(r"\{([a-zA-Z][a-zA-Z0-9_.-]*)\}", replace, value)
        expanded = re.sub(r"([ _.-])\1+", r"\1", expanded)
        return expanded.strip(" _-.")

    def prepare_group(self, group: NamingGroup,
                      workflow: dict[str, Any] | None = None) -> None:
        workflow = workflow or self.active_workflow()
        fields = workflow_field_map(workflow)
        is_default = workflow.get("kind") == "default"
        numbering = workflow.get("numbering", {})
        numbering_mode = str(
            workflow.get("numbering_mode", "numeric" if is_default else "always")
        )
        profile_numbering_enabled = any(
            profile.get("numbering", {}).get("enabled")
            for profile in workflow.get("profiles", [])
        )
        numbering_active = (
            (numbering.get("enabled") or profile_numbering_enabled)
            and (numbering_mode == "always" or self.state.mode == "numeric")
        )
        if not numbering_active:
            for record in group.records:
                for field_id in list(record.workflow_number_fields):
                    definition = fields.get(field_id)
                    if definition:
                        record.workflow_values[field_id] = self.initial_field_value(
                            definition, group, record
                        )
                    record.workflow_number_fields.discard(field_id)
                    record.workflow_auto_fields.discard(field_id)
        if is_default:
            for record in group.records:
                record.name = record.base_name
            apply_filename_parse(
                group.records,
                self.state.parse_template,
                self.state.parse_use_name,
                parser=lambda stem, template: self.parse_filename(
                    workflow, stem, template
                ),
            )
        if ("name" in fields
                and fields["name"].get("scope") in {"record", "suffix"}
                and self.state.mode != "numeric" and self.state.parse_use_name):
            for record in group.records:
                if "name" not in record.workflow_manual_fields:
                    record.workflow_values["name"] = record.name
                    record.workflow_auto_fields.add("name")
        excel_field = self.excel_field(workflow, fields)
        for record in group.records:
            if (self.state.mode != "excel" and record.excel_source and excel_field
                    and excel_field not in record.workflow_manual_fields):
                record.workflow_values[excel_field] = self.initial_field_value(
                    fields[excel_field], group, record
                )
        if numbering_active and numbering_mode == "numeric" and numbering.get("enabled"):
            number_field = str(numbering.get("field", ""))
            if number_field in fields:
                for record in group.records:
                    record.workflow_values[number_field] = ""
                    record.workflow_number_fields.add(number_field)
        if numbering_active:
            self.assign_numbers(workflow, group, {
                "start": self.state.numeric_start,
                "width": self.state.numeric_width,
                "step": self.state.numeric_step,
            })
        self._apply_excel_state(group, workflow, fields)
        for record in group.records:
            record.workflow_base_target_name = self.compose_target(workflow, group, record)
            record.target_name = record.workflow_base_target_name
            missing = [
                definition["label"]
                for field_id, definition in fields.items()
                if definition.get("required")
                and not self.value(workflow, group, record, field_id).strip()
            ]
            if not self.state.extension_enabled.get(record.extension.casefold(), True):
                record.selected = False
                record.status = "Skipped"
                record.status_detail = "Extension not selected"
            elif not record.selected:
                record.status = "Skipped" if record.status != "未匹配" else record.status
                if not record.status_detail:
                    record.status_detail = "Not selected"
            elif missing:
                record.status = "Conflict"
                record.status_detail = "缺少工作流必填字段: " + ", ".join(missing)
            else:
                error = self.engine.validate_filename(record.target_name)
                record.status = "Conflict" if error else "Ready"
                record.status_detail = error or ""
        self.resolve_target_conflicts()

    def _apply_excel_state(self, group: NamingGroup, workflow: dict[str, Any],
                           fields: dict[str, dict]) -> None:
        excel_field = self.excel_field(workflow, fields)
        if self.state.mode != "excel":
            return
        mapping = self.state.excel_mappings.get(group.key, {})
        skipped = self.state.excel_skipped.get(group.key, set())
        for record in group.records:
            if record.path in mapping:
                if excel_field and excel_field not in record.workflow_manual_fields:
                    record.workflow_values[excel_field] = mapping[record.path]
                    record.workflow_auto_fields.discard(excel_field)
                record.selected = True
                record.status = "Ready"
                record.status_detail = ""
            elif record.path in skipped:
                record.selected = False
                record.status = "Skipped"
                record.status_detail = "Excel B 列为空"
            else:
                record.selected = False
                record.status = "未匹配"
                record.status_detail = "Excel 未提供名称"
