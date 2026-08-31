"""Target-name conflict resolution independent of server state."""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from core.files import FileRecord, NamingGroup, normalise_ext
from workflow_system.catalog import workflow_field_map
from .rules import action_map, action_value, append_workflow_suffix, workflow_context


def workflow_value(workflow: dict[str, Any], group: NamingGroup, record: FileRecord,
                   field_id: str, workflow_values: dict[str, str]) -> str:
    definition = workflow_field_map(workflow).get(field_id, {})
    if definition.get("scope") == "workflow":
        return str(workflow_values.get(field_id, definition.get("default", "")) or "")
    if definition.get("scope") == "group":
        return str(group.workflow_values.get(field_id, definition.get("default", "")) or "")
    return str(record.workflow_values.get(field_id, definition.get("default", "")) or "")


def workflow_profile(workflow: dict[str, Any], group: NamingGroup, record: FileRecord,
                     workflow_values: dict[str, str]) -> dict[str, Any] | None:
    profiles = {profile["id"]: profile for profile in workflow.get("profiles", [])}
    if not profiles:
        return None
    profile_field = str(workflow.get("profile_field", "") or "")
    profile_id = workflow_value(workflow, group, record, profile_field, workflow_values) if profile_field else ""
    return profiles.get(profile_id) or profiles.get(str(workflow.get("default_profile", "") or ""))


def compose_target(workflow: dict[str, Any], group: NamingGroup, record: FileRecord,
                   workflow_values: dict[str, str], suffix_mode: str, separator: str,
                   normalise: Callable[[dict[str, Any], str, Any], str]) -> str:
    fields = workflow_field_map(workflow)
    actions = action_map(workflow)
    profile = workflow_profile(workflow, group, record, workflow_values)
    template = ([{"field": field_id} for field_id in profile.get("ordered_segments", [])]
                if profile else workflow.get("template", []))
    has_literal = any(part.get("literal") is not None for part in template)
    values: list[str] = list(profile.get("fixed_prefix_tokens", [])) if profile else []
    for part in template:
        if part.get("field"):
            value = workflow_value(workflow, group, record, part["field"], workflow_values)
            if not value.strip() and profile:
                value = str(profile.get("defaults", {}).get(part["field"], "") or "")
            if value.strip():
                values.append(value)
        elif has_literal:
            values.append(str(part.get("literal", "")))

    suffix_fields = list(workflow.get("suffix_modes", {}).get(suffix_mode, []))
    configured_actions = set(suffix_fields) & set(actions)
    suffix_fields.extend(action_id for action_id in actions if action_id not in configured_actions)
    template_field_ids = {part.get("field") for part in template}
    context = workflow_context(record, workflow)
    for suffix_id in suffix_fields:
        if suffix_id in actions:
            if suffix_id not in record.workflow_actions:
                continue
            action = actions[suffix_id]
            value = action_value(action, context)
            if value is None or str(value).strip() == "":
                value = record.workflow_values.get(action["field"], "")
            value = normalise(workflow, action["field"], value)
            if action["kind"] == "append_field_suffix" and value.strip():
                if values:
                    values[-1] = append_workflow_suffix(values[-1], value, action, separator)
                else:
                    values.append(append_workflow_suffix("", value, action, separator))
            continue
        if suffix_id not in fields:
            continue
        value = workflow_value(workflow, group, record, suffix_id, workflow_values)
        if value.strip() and suffix_id not in template_field_ids:
            values.append(value)
    if profile:
        values.extend(str(token) for token in profile.get("fixed_suffix_tokens", []) if str(token).strip())
    if has_literal:
        stem = "".join(values).strip(separator)
    else:
        stem = separator.join(values)
        if separator:
            stem = re.sub(rf"(?:{re.escape(separator)}){{2,}}", separator, stem)
    return f"{stem or 'unnamed'}{normalise_ext(record.extension_original or record.extension)}"


def append_conflict_suffix(target_name: str, number: int, width: int = 2) -> str:
    extension = Path(target_name).suffix
    stem = target_name[:-len(extension)] if extension else target_name
    return f"{stem}_{number:0{max(1, width)}d}{extension}"


def _target_path_key(path: Path) -> str:
    try:
        return str(path.resolve()).casefold()
    except OSError:
        return str(path.absolute()).casefold()


def resolve_target_conflicts(records: Iterable[FileRecord], workflow: dict,
                             validate: Callable[[str], str | None]) -> None:
    records = [record for record in records if record.selected and not record.removed]
    if not records:
        return
    selected_sources = {_target_path_key(Path(record.path)) for record in records}
    units: list[list[FileRecord]] = []
    unit_by_key: dict[tuple[str, str], list[FileRecord]] = {}
    for record in records:
        key = ("association", record.association_id) if record.association_id else ("record", str(id(record)))
        unit = unit_by_key.get(key)
        if unit is None:
            unit = []
            unit_by_key[key] = unit
            units.append(unit)
        unit.append(record)

    collision = workflow.get("collision_suffix", {})
    collision_enabled = bool(collision.get("enabled", True))
    collision_width = max(1, int(collision.get("width", 2)))
    collision_start = max(1, int(collision.get("start", 1)))
    used_targets: set[str] = set()
    for unit in units:
        base_names = {id(record): (record.workflow_base_target_name or record.target_name) for record in unit}
        if not all(base_names.values()):
            continue
        sequence = (0,) if not collision_enabled else itertools.chain((0,), range(collision_start, 1000000))
        for number in sequence:
            names = {
                id(record): (base_names[id(record)] if number == 0
                             else append_conflict_suffix(base_names[id(record)], number, collision_width))
                for record in unit
            }
            keys = [_target_path_key(Path(record.path).with_name(names[id(record)])) for record in unit]
            if len(keys) != len(set(keys)) or any(key in used_targets for key in keys):
                continue
            if any(Path(record.path).with_name(names[id(record)]).exists()
                   and key not in selected_sources for record, key in zip(unit, keys)):
                continue
            for record in unit:
                record.target_name = names[id(record)]
            used_targets.update(keys)
            break
        else:
            for record in unit:
                record.status = "Conflict"
                record.status_detail = "目标名称重复且 collision_suffix 已禁用"

    for record in records:
        if record.status == "Conflict" and record.status_detail.startswith(("缺少工作流必填字段", "目标名称重复")):
            continue
        syntax_error = validate(record.target_name)
        record.status = "Conflict" if syntax_error else "Ready"
        record.status_detail = syntax_error or ""
