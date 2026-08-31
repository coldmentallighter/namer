"""Filename composition, template parsing and numeric assignment.

Split out of the former ``core/files.py`` god module.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Sequence

from .fsutil import normalise_ext
from .models import FileRecord, NamingGroup


def compose_filename(meta_prefix: str, group_prefix: str, child_prefix: str,
                     name: str, extension: str, separator: str = "_") -> str:
    # Strip only for the emptiness decision; legal leading/trailing spaces are
    # preserved so a user's root prefix is not silently changed in preview.
    fields = [str(value) for value in (meta_prefix, group_prefix, child_prefix, name)
              if str(value).strip()]
    stem = separator.join(fields) if fields else "unnamed"
    return f"{stem}{normalise_ext(extension)}"


_TEMPLATE_FIELDS = {
    "name": r"(?P<name>.+?)",
    "stem": r"(?P<name>.+?)",
    "number": r"(?P<number>\d{1,5})",
    "type": r"(?P<type>.+?)",
    "category": r"(?P<category>.+?)",
    "pack": r"(?P<pack>.+?)",
    "*": r"(?P<unmatched>.+?)",
}


def _template_regex(template: str, field_patterns: dict[str, str] | None = None) -> tuple[re.Pattern[str] | None, list[str]]:
    """Compile a simple ``{field}`` filename template into a full regex."""
    patterns = dict(_TEMPLATE_FIELDS)
    if field_patterns:
        patterns.update({str(key).casefold(): str(value) for key, value in field_patterns.items()})
    if not template or template.strip().casefold() in {"auto", "自动"}:
        return None, []
    parts: list[str] = []
    fields: list[str] = []
    cursor = 0
    for match in re.finditer(r"\{([\w*]+)\}", template):
        parts.append(re.escape(template[cursor:match.start()]))
        field_name = match.group(1).casefold()
        pattern = patterns.get(field_name)
        if pattern is None:
            # Unknown placeholders are treated as literal text rather than
            # silently accepting a malformed template.
            return None, []
        group_name = field_name
        if field_name == "stem":
            group_name = "name"
        if group_name in fields:
            return None, []
        fields.append(group_name)
        parts.append(pattern.replace(f"?P<{group_name}>", f"?P<{group_name}>"))
        cursor = match.end()
    parts.append(re.escape(template[cursor:]))
    try:
        return re.compile("^" + "".join(parts) + "$", re.IGNORECASE), fields
    except re.error:
        return None, []


def parse_filename(stem: str, template: str = "auto",
                   field_patterns: dict[str, str] | None = None) -> dict[str, Any]:
    """Parse a filename stem for preview and optional name generation.

    The core only knows generic naming tokens.  A workflow may provide extra
    field patterns through a module-owned parser before calling this function.
    ``auto`` recognizes a trailing number without treating every number as a
    sequence number.
    """
    source = str(stem or "")
    auto_mode = not template or template.strip().casefold() in {"auto", "自动"}
    regex, fields = _template_regex(template, field_patterns)
    if not auto_mode and regex is None:
        return {"stem": source, "fields": {}, "unmatched": source,
                "confidence": 0.0, "matched": False, "template": template,
                "error": "模板无效或包含重复/未知字段"}
    if regex is not None:
        matched = regex.fullmatch(source)
        if not matched:
            return {"stem": source, "fields": {}, "unmatched": source,
                    "confidence": 0.0, "matched": False}
        values = {key: (value or "") for key, value in matched.groupdict().items()}
        unmatched = values.pop("unmatched", "")
        if "category" in values and "type" not in values:
            values["type"] = values["category"]
        return {"stem": source, "fields": values, "unmatched": unmatched,
                "confidence": 1.0, "matched": True, "template": template, "field_order": fields}

    working = source
    fields_out: dict[str, str] = {}
    number_match = re.search(r"(?:^|[_\-\s])([0-9]{1,5})$", working)
    if number_match:
        fields_out["number"] = number_match.group(1)
        working = working[:number_match.start()].rstrip(" _-.")
    tokens = [token for token in re.split(r"[_\-]+", working) if token]
    if tokens:
        if len(tokens) > 1:
            fields_out["type"] = tokens[0]
            fields_out["name"] = "_".join(tokens[1:])
        else:
            fields_out["name"] = tokens[0]
    elif source:
        fields_out["name"] = source
    recognized = sum(bool(value) for value in fields_out.values())
    confidence = min(1.0, recognized / 4.0) if source else 0.0
    return {"stem": source, "fields": fields_out, "unmatched": "",
            "confidence": confidence, "matched": bool(fields_out), "template": "auto",
            "field_order": list(fields_out)}


def apply_filename_parse(records: Sequence[FileRecord], template: str = "auto",
                         use_name: bool = False,
                         parser: Callable[[str, str], dict[str, Any]] | None = None) -> None:
    for record in records:
        previous_parsed_name = record.parsed_fields.get("name", "")
        previous_name = record.name
        parsed = parser(record.stem, template) if parser else parse_filename(record.stem, template)
        record.parsed_fields = dict(parsed.get("fields", {}))
        record.parse_unmatched = str(parsed.get("unmatched", ""))
        record.parse_confidence = float(parsed.get("confidence", 0.0) or 0.0)
        record.parse_error = str(parsed.get("error", ""))
        if use_name:
            record.name = record.parsed_fields.get("name") or record.stem
        elif previous_parsed_name and previous_name == previous_parsed_name:
            record.name = record.stem


def preview_group(group: NamingGroup, meta_prefix: str, separator: str = "_") -> None:
    for record in group.records:
        record.target_name = compose_filename(meta_prefix, group.prefix, record.child_prefix,
                                               record.name, record.extension_original or record.extension, separator)
        record.status = "Ready" if record.selected else "Skipped"
        record.status_detail = "" if record.selected else "Not selected"


def assign_numeric(group: NamingGroup, start: int = 1, width: int = 2,
                   step: int = 1, meta_prefix: str = "", separator: str = "_") -> None:
    current = start
    for record in group.records:
        if record.selected and record.status not in {"Conflict", "Error", "Skipped", "未匹配"}:
            record.name = str(current).zfill(max(1, width))
            current += step
            record.target_name = compose_filename(meta_prefix, group.prefix, record.child_prefix,
                                               record.name, record.extension_original or record.extension, separator)
