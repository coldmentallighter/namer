"""Bounded expression and condition evaluation for workflow declarations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.files import FileRecord


def path_value(value: Any, path: str) -> Any:
    current = value
    for part in str(path or "").split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def number(value: Any) -> float | None:
    try:
        if isinstance(value, bool) or value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def expression_value(expression: dict[str, Any], context: dict[str, Any]) -> Any:
    if "path" in expression:
        return path_value(context, expression["path"])
    if "value" in expression:
        return expression["value"]
    operator = expression.get("op")
    values = [expression_value(item, context) for item in expression.get("args", [])]
    if operator == "coalesce":
        return next((value for value in values if value is not None and str(value).strip()), None)
    if operator == "concat":
        return "".join(str(value) for value in values if value is not None)
    if operator in {"lower", "upper"}:
        if not values or values[0] is None:
            return None
        return str(values[0]).casefold() if operator == "lower" else str(values[0]).upper()
    if operator == "abs":
        value = number(values[0]) if values else None
        return abs(value) if value is not None else None
    if operator == "round":
        value = number(values[0]) if values else None
        return round(value, int(expression.get("digits", 0))) if value is not None else None
    numbers = [number(value) for value in values]
    if any(value is None for value in numbers) or not numbers:
        return None
    if operator == "add":
        return sum(numbers)
    if operator == "subtract":
        return numbers[0] - sum(numbers[1:])
    if operator == "multiply":
        result = 1.0
        for value in numbers:
            result *= value
        return result
    if operator == "divide":
        result = numbers[0]
        for value in numbers[1:]:
            if value == 0:
                return None
            result /= value
        return result
    if operator == "mod":
        if len(numbers) != 2 or numbers[1] == 0:
            return None
        return numbers[0] % numbers[1]
    if operator == "min":
        return min(numbers)
    if operator == "max":
        return max(numbers)
    return None


def _same_value(left: Any, right: Any) -> bool:
    left_number, right_number = number(left), number(right)
    if left_number is not None and right_number is not None:
        return left_number == right_number
    return str(left).casefold() == str(right).casefold()


def condition_matches(condition: dict[str, Any], context: dict[str, Any]) -> bool:
    if "all" in condition:
        return all(condition_matches(item, context) for item in condition["all"])
    if "any" in condition:
        return any(condition_matches(item, context) for item in condition["any"])
    if "not" in condition:
        return not condition_matches(condition["not"], context)
    actual = path_value(context, condition.get("path", ""))
    operator = condition.get("op")
    if operator == "exists":
        return actual is not None and actual != ""
    expected = (path_value(context, condition["value_from"])
                if condition.get("value_from") else condition.get("value"))
    if actual is None or expected is None:
        return operator == "not_equals" and actual is not None
    if operator == "equals":
        return _same_value(actual, expected)
    if operator == "not_equals":
        return not _same_value(actual, expected)
    if operator in {"contains", "starts_with", "ends_with"}:
        actual_text = str(actual or "").casefold()
        expected_text = str(expected or "").casefold()
        return {
            "contains": expected_text in actual_text,
            "starts_with": actual_text.startswith(expected_text),
            "ends_with": actual_text.endswith(expected_text),
        }[operator]
    if operator in {"in", "not_in"}:
        if not isinstance(expected, (list, tuple, set)):
            return operator == "not_in"
        matched = any(_same_value(actual, item) for item in expected)
        return matched if operator == "in" else not matched
    actual_number, expected_number = number(actual), number(expected)
    if actual_number is None or expected_number is None:
        return False
    return {
        "gt": actual_number > expected_number,
        "gte": actual_number >= expected_number,
        "lt": actual_number < expected_number,
        "lte": actual_number <= expected_number,
    }[operator]


def workflow_context(record: FileRecord, workflow: dict[str, Any]) -> dict[str, Any]:
    context = {
        "metadata": record.metadata,
        "record": {
            "name": record.name,
            "base_name": record.base_name,
            "stem": record.stem,
            "filename": record.original_name,
            "extension": record.extension.lstrip("."),
            "folder_name": record.folder_name,
            "relative_folder": record.relative_folder,
            "parsed_fields": record.parsed_fields,
        },
        "derived": {},
    }
    for item in workflow.get("derived", []):
        context["derived"][item["id"]] = expression_value(item["expression"], context)
    record.workflow_derived = context["derived"]
    return context


def action_value(action: dict[str, Any], context: dict[str, Any]) -> Any:
    return action["value"] if "value" in action else path_value(context, action.get("value_from", ""))


def action_map(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {action["id"]: action for action in workflow.get("actions", [])}


def append_workflow_suffix(name: str, value: Any, action: dict[str, Any], separator: str) -> str:
    text = str(name or "")
    value_text = str(value or "").strip()
    suffix = str(action.get("suffix", ""))
    if not value_text or not suffix:
        return text
    token = f"{value_text}{suffix}"
    if text.casefold().endswith(token.casefold()):
        return text
    joiner = str(action.get("separator") or separator)
    if text.casefold().endswith(value_text.casefold()):
        return f"{text[:-len(value_text)]}{token}"
    return f"{text}{joiner}{token}" if text else token


def apply_rules(workflow: dict[str, Any], record: FileRecord,
                normalise: Callable[[dict[str, Any], str, Any], str]) -> None:
    context = workflow_context(record, workflow)
    rules = sorted(workflow.get("rules", []), key=lambda rule: int(rule.get("priority", 0)), reverse=True)
    assigned: set[str] = set()
    for rule in rules:
        if not condition_matches(rule["when"], context):
            continue
        for action in rule["then"]:
            field_id = action["field"]
            value = action_value(action, context)
            if value is None or str(value).strip() == "":
                continue
            value = normalise(workflow, field_id, str(value))
            candidates = record.workflow_candidates.setdefault(field_id, [])
            if value not in candidates:
                candidates.append(value)
            details = record.workflow_candidate_details.setdefault(field_id, [])
            detail = {
                "value": value,
                "rule_id": rule["id"],
                "reason": action.get("reason") or f"命中规则：{rule['id']}",
                "mode": action["mode"],
            }
            if not any(item.get("value") == value and item.get("rule_id") == rule["id"] for item in details):
                details.append(detail)
            if (action["mode"] == "assign" and field_id not in assigned
                    and field_id not in record.workflow_manual_fields
                    and (not record.workflow_values.get(field_id) or field_id in record.workflow_auto_fields)):
                record.workflow_values[field_id] = value
                record.workflow_auto_fields.add(field_id)
                assigned.add(field_id)
