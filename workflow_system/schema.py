"""Pure workflow schema validation with zero I/O.

Split out of the former ``workflow_system/catalog.py`` god module.  This
module is a leaf: no filesystem access, no module loading.  The engine only
needs ``workflow_field_map`` from here, which is what decouples ``engine``
from the hot-reloading catalog (R3).
"""

from __future__ import annotations

import copy
import re
from typing import Any


WORKFLOW_SCHEMA_VERSION = 1
WORKFLOW_FILE_NAME = "workflow.json"
_FIELD_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_METADATA_PATH = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_RULE_OPERATORS = {
    "equals", "not_equals", "contains", "starts_with", "ends_with",
    "in", "not_in", "exists", "gt", "gte", "lt", "lte",
}
_EXPRESSION_OPERATORS = {
    "add", "subtract", "multiply", "divide", "mod", "min", "max",
    "abs", "round", "lower", "upper", "concat", "coalesce",
}
_ACTION_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ACTION_KINDS = {"append_field_suffix"}
_CONTEXT_ROOTS = {"metadata", "record", "derived"}
_INITIAL_SOURCES = {"", "stem", "directory.meta", "directory.group", "directory.child"}
_MODULE_TRIGGERS = {"on_user_request", "after_scan"}


def _normalise_template(template: Any) -> list[dict[str, str]]:
    if isinstance(template, str):
        parts: list[dict[str, str]] = []
        cursor = 0
        for match in re.finditer(r"\{([a-zA-Z][a-zA-Z0-9_]*)\}", template):
            literal = template[cursor:match.start()]
            if literal:
                parts.append({"literal": literal})
            parts.append({"field": match.group(1)})
            cursor = match.end()
        if template[cursor:]:
            parts.append({"literal": template[cursor:]})
        return parts
    if not isinstance(template, list):
        raise ValueError("工作流 template 必须是字段列表或占位符字符串")
    result: list[dict[str, str]] = []
    for part in template:
        if isinstance(part, str):
            result.append({"field": part})
        elif isinstance(part, dict) and (part.get("field") or part.get("literal") is not None):
            if part.get("field"):
                result.append({"field": str(part["field"])})
            else:
                result.append({"literal": str(part.get("literal", ""))})
        else:
            raise ValueError("工作流 template 中存在无效片段")
    return result


def _validate_condition(condition: Any, location: str = "when") -> dict[str, Any]:
    if not isinstance(condition, dict):
        raise ValueError(f"工作流规则 {location} 必须是对象")
    for combinator in ("all", "any"):
        if combinator in condition:
            items = condition[combinator]
            if not isinstance(items, list) or not items:
                raise ValueError(f"工作流规则 {location}.{combinator} 必须是非空数组")
            return {
                combinator: [
                    _validate_condition(item, f"{location}.{combinator}[{index}]")
                    for index, item in enumerate(items)
                ]
            }
    if "not" in condition:
        return {"not": _validate_condition(condition["not"], f"{location}.not")}
    path = str(condition.get("path", "")).strip()
    operator = str(condition.get("op", "")).strip().casefold()
    if (not _METADATA_PATH.fullmatch(path)
            or path.split(".", 1)[0] not in _CONTEXT_ROOTS):
        raise ValueError(f"工作流规则 {location}.path 无效")
    if operator not in _RULE_OPERATORS:
        raise ValueError(f"工作流规则操作符无效: {operator}")
    has_value = "value" in condition
    value_from = str(condition.get("value_from", "")).strip()
    if operator != "exists" and has_value == bool(value_from):
        raise ValueError(f"工作流规则 {location} 必须二选一提供 value 或 value_from")
    if operator == "exists" and value_from:
        raise ValueError(f"工作流规则 {location} 的 exists 不需要 value_from")
    if operator in {"in", "not_in"} and not isinstance(condition.get("value"), list):
        raise ValueError(f"工作流规则 {location}.value 必须是数组")
    result = {"path": path, "op": operator}
    if has_value:
        result["value"] = copy.deepcopy(condition["value"])
    elif value_from:
        if (not _METADATA_PATH.fullmatch(value_from)
                or value_from.split(".", 1)[0] not in _CONTEXT_ROOTS):
            raise ValueError(f"工作流规则 {location}.value_from 无效")
        result["value_from"] = value_from
    return result


def _validate_expression(expression: Any, location: str) -> dict[str, Any]:
    if not isinstance(expression, dict):
        raise ValueError(f"工作流派生表达式必须是对象: {location}")
    path = str(expression.get("path", "")).strip()
    if path:
        if (not _METADATA_PATH.fullmatch(path)
                or path.split(".", 1)[0] not in _CONTEXT_ROOTS):
            raise ValueError(f"工作流派生表达式 path 无效: {path}")
        return {"path": path}
    if "value" in expression:
        return {"value": copy.deepcopy(expression["value"])}
    operator = str(expression.get("op", "")).strip().casefold()
    if operator not in _EXPRESSION_OPERATORS:
        raise ValueError(f"工作流派生表达式操作符无效: {operator}")
    args = expression.get("args")
    if not isinstance(args, list) or not args:
        raise ValueError(f"工作流派生表达式 args 必须是非空数组: {location}")
    result = {
        "op": operator,
        "args": [_validate_expression(item, f"{location}.args[{index}]") for index, item in enumerate(args)],
    }
    if "digits" in expression:
        result["digits"] = max(0, int(expression["digits"]))
    return result


def _normalise_derived(derived: Any) -> list[dict[str, Any]]:
    if derived is None:
        return []
    if not isinstance(derived, list):
        raise ValueError("工作流 derived 必须是数组")
    result: list[dict[str, Any]] = []
    derived_ids: set[str] = set()
    for index, item in enumerate(derived):
        if not isinstance(item, dict):
            raise ValueError(f"工作流派生项必须是对象: {index}")
        derived_id = str(item.get("id", "")).strip()
        if not _FIELD_ID.fullmatch(derived_id) or derived_id in derived_ids:
            raise ValueError(f"工作流派生项 id 无效或重复: {derived_id}")
        result.append({
            "id": derived_id,
            "expression": _validate_expression(item.get("expression"), f"{derived_id}.expression"),
        })
        derived_ids.add(derived_id)
    return result


def _normalise_rules(rules: Any, field_ids: set[str], fields: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if rules is None:
        return []
    if not isinstance(rules, list):
        raise ValueError("工作流 rules 必须是数组")
    result: list[dict[str, Any]] = []
    rule_ids: set[str] = set()
    for index, raw_rule in enumerate(rules):
        if not isinstance(raw_rule, dict):
            raise ValueError(f"工作流规则必须是对象: {index}")
        rule_id = str(raw_rule.get("id", f"rule-{index + 1}")).strip()
        if not _FIELD_ID.fullmatch(rule_id) or rule_id in rule_ids:
            raise ValueError(f"工作流规则 id 无效或重复: {rule_id}")
        scope = str(raw_rule.get("scope", "record")).strip()
        if scope != "record":
            raise ValueError(f"当前工作流规则只支持 record 作用域: {rule_id}")
        actions = raw_rule.get("then")
        if isinstance(actions, dict):
            actions = [actions]
        if not isinstance(actions, list) or not actions:
            raise ValueError(f"工作流规则 then 必须是非空对象或数组: {rule_id}")
        normalised_actions: list[dict[str, Any]] = []
        for action in actions:
            if not isinstance(action, dict):
                raise ValueError(f"工作流规则动作无效: {rule_id}")
            field_id = str(action.get("field", "")).strip()
            if field_id not in field_ids:
                raise ValueError(f"工作流规则引用了不存在的字段: {field_id}")
            if fields[field_id].get("scope") not in {"record", "suffix"}:
                raise ValueError(f"工作流规则目标必须是文件级字段: {field_id}")
            has_value = "value" in action
            value_from = str(action.get("value_from", "")).strip()
            if has_value == bool(value_from):
                raise ValueError(f"工作流规则动作必须二选一提供 value 或 value_from: {rule_id}")
            if (value_from and (
                    not _METADATA_PATH.fullmatch(value_from)
                    or value_from.split(".", 1)[0] not in _CONTEXT_ROOTS)):
                raise ValueError(f"工作流规则 value_from 无效: {value_from}")
            mode = str(action.get("mode", raw_rule.get("mode", "suggest"))).strip().casefold()
            if mode not in {"suggest", "assign"}:
                raise ValueError(f"工作流规则模式无效: {mode}")
            normalised_action: dict[str, Any] = {"field": field_id, "mode": mode}
            if has_value:
                normalised_action["value"] = copy.deepcopy(action["value"])
            else:
                normalised_action["value_from"] = value_from
            if str(action.get("reason", raw_rule.get("reason", ""))).strip():
                normalised_action["reason"] = str(action.get("reason", raw_rule.get("reason", ""))).strip()
            normalised_actions.append(normalised_action)
        result.append({
            "id": rule_id,
            "scope": scope,
            "when": _validate_condition(raw_rule.get("when", {}), f"{rule_id}.when"),
            "then": normalised_actions,
            "priority": int(raw_rule.get("priority", 0)),
        })
        rule_ids.add(rule_id)
    return result


def _normalise_metadata_providers(providers: Any) -> list[dict[str, Any]]:
    if providers is None:
        return []
    if not isinstance(providers, list):
        raise ValueError("工作流 metadata_providers 必须是数组")
    result: list[dict[str, Any]] = []
    provider_ids: set[str] = set()
    for item in providers:
        if isinstance(item, str):
            item = {"provider": item}
        if not isinstance(item, dict):
            raise ValueError("工作流 metadata provider 必须是对象")
        provider_id = str(item.get("provider", "")).strip()
        if not _ACTION_ID.fullmatch(provider_id) or provider_id in provider_ids:
            raise ValueError(f"工作流 metadata provider 无效或重复: {provider_id}")
        options = item.get("options", {})
        if not isinstance(options, dict):
            raise ValueError(f"工作流 metadata provider options 必须是对象: {provider_id}")
        result.append({"provider": provider_id, "options": copy.deepcopy(options)})
        provider_ids.add(provider_id)
    return result


def _normalise_workflow_modules(modules: Any, fields: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if modules is None:
        return []
    if not isinstance(modules, list):
        raise ValueError("工作流 modules 必须是数组")
    result: list[dict[str, Any]] = []
    module_ids: set[str] = set()
    for index, raw_module in enumerate(modules):
        if not isinstance(raw_module, dict):
            raise ValueError(f"工作流 module 必须是对象: {index}")
        module_id = str(raw_module.get("id", "")).strip()
        if not _ACTION_ID.fullmatch(module_id) or module_id in module_ids:
            raise ValueError(f"工作流 module id 无效或重复: {module_id}")
        trigger = str(raw_module.get("trigger", "on_user_request")).strip().casefold()
        if trigger not in _MODULE_TRIGGERS:
            raise ValueError(f"工作流 module trigger 无效: {module_id}.{trigger}")
        outputs = raw_module.get("outputs", [])
        if not isinstance(outputs, list) or not outputs:
            raise ValueError(f"工作流 module outputs 必须是非空数组: {module_id}")
        normalised_outputs: list[dict[str, str]] = []
        output_ids: set[str] = set()
        for output_index, raw_output in enumerate(outputs):
            if not isinstance(raw_output, dict):
                raise ValueError(f"工作流 module output 必须是对象: {module_id}[{output_index}]")
            output_id = str(raw_output.get("id", "")).strip()
            if not _FIELD_ID.fullmatch(output_id) or output_id in output_ids:
                raise ValueError(f"工作流 module output id 无效或重复: {module_id}.{output_id}")
            field_id = str(raw_output.get("field", "")).strip()
            definition = fields.get(field_id)
            if definition is None:
                raise ValueError(f"工作流 module output 引用了不存在的字段: {field_id}")
            if not definition.get("editable", True):
                raise ValueError(f"工作流 module output 不能绑定不可编辑字段: {field_id}")
            field_scope = str(definition.get("scope", "record"))
            expected_scope = "record" if field_scope in {"record", "suffix"} else field_scope
            scope = str(raw_output.get("scope", expected_scope)).strip().casefold()
            if scope != expected_scope:
                raise ValueError(
                    f"工作流 module output scope 与字段不一致: {module_id}.{output_id}"
                )
            mode = str(raw_output.get("mode", "suggest")).strip().casefold()
            if mode != "suggest":
                raise ValueError(f"工作流 module output 当前只支持 suggest: {module_id}.{output_id}")
            output_format = str(raw_output.get("format", "raw")).strip().casefold()
            if output_format != "raw":
                raise ValueError(f"工作流 module output 当前只支持 raw: {module_id}.{output_id}")
            normalised_outputs.append({
                "id": output_id,
                "field": field_id,
                "scope": scope,
                "mode": mode,
                "format": output_format,
            })
            output_ids.add(output_id)
        options = raw_module.get("options", {})
        if not isinstance(options, dict):
            raise ValueError(f"工作流 module options 必须是对象: {module_id}")
        result.append({
            "id": module_id,
            "label": str(raw_module.get("label", module_id)),
            "description": str(raw_module.get("description", "") or ""),
            "trigger": trigger,
            "outputs": normalised_outputs,
            "options": copy.deepcopy(options),
        })
        module_ids.add(module_id)
    return result


def _normalise_actions(actions: Any, field_ids: set[str], fields: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if actions is None:
        return []
    if not isinstance(actions, list):
        raise ValueError("工作流 actions 必须是数组")
    result: list[dict[str, Any]] = []
    action_ids: set[str] = set()
    for index, item in enumerate(actions):
        if not isinstance(item, dict):
            raise ValueError(f"工作流 action 必须是对象: {index}")
        action_id = str(item.get("id", "")).strip()
        if not _ACTION_ID.fullmatch(action_id) or action_id in action_ids:
            raise ValueError(f"工作流 action id 无效或重复: {action_id}")
        kind = str(item.get("kind", "")).strip().casefold()
        if kind not in _ACTION_KINDS:
            raise ValueError(f"工作流 action 类型无效: {kind}")
        field_id = str(item.get("field", "")).strip()
        if field_id not in field_ids:
            raise ValueError(f"工作流 action 引用了不存在的字段: {field_id}")
        if fields[field_id].get("scope") not in {"record", "suffix"}:
            raise ValueError(f"工作流 action 目标必须是文件级字段: {field_id}")
        value_from = str(item.get("value_from", "")).strip()
        has_value = "value" in item
        if has_value == bool(value_from):
            raise ValueError(f"工作流 action 必须二选一提供 value 或 value_from: {action_id}")
        if value_from:
            parts = value_from.split(".")
            if (not _METADATA_PATH.fullmatch(value_from)
                    or not parts or parts[0] not in _CONTEXT_ROOTS):
                raise ValueError(f"工作流 action value_from 无效: {value_from}")
        suffix = str(item.get("suffix", ""))
        if kind == "append_field_suffix" and not suffix:
            raise ValueError(f"追加后缀 action 必须提供 suffix: {action_id}")
        normalised: dict[str, Any] = {
            "id": action_id,
            "label": str(item.get("label", action_id)),
            "kind": kind,
            "field": field_id,
            "suffix": suffix,
            "separator": str(item.get("separator", "") or ""),
        }
        if has_value:
            normalised["value"] = copy.deepcopy(item["value"])
        else:
            normalised["value_from"] = value_from
        if item.get("description"):
            normalised["description"] = str(item["description"])
        result.append(normalised)
        action_ids.add(action_id)
    return result


def _normalise_numbering(numbering: Any, field_ids: set[str], location: str = "numbering") -> dict[str, Any]:
    if numbering in (None, ""):
        numbering = {}
    if not isinstance(numbering, dict):
        raise ValueError(f"{location} 必须是对象")
    result = {
        "enabled": bool(numbering.get("enabled", False)),
        "field": str(numbering.get("field", "")),
        "width": max(1, int(numbering.get("width", 2))),
        "start": int(numbering.get("start", 1)),
        "step": max(1, int(numbering.get("step", 1))),
        "group_by": [str(field_id) for field_id in numbering.get("group_by", [])],
        "manual": bool(numbering.get("manual", True)),
        "skip_disk_existing": bool(numbering.get("skip_disk_existing", True)),
    }
    if result["enabled"] and result["field"] not in field_ids:
        raise ValueError(f"{location}.field 必须引用工作流字段")
    if any(field_id not in field_ids for field_id in result["group_by"]):
        raise ValueError(f"{location}.group_by 引用了不存在的字段")
    return result


def _normalise_profiles(profiles: Any, field_ids: set[str]) -> list[dict[str, Any]]:
    if profiles in (None, ""):
        return []
    if not isinstance(profiles, list):
        raise ValueError("工作流 profiles 必须是数组")
    result: list[dict[str, Any]] = []
    profile_ids: set[str] = set()
    for index, raw_profile in enumerate(profiles):
        if not isinstance(raw_profile, dict):
            raise ValueError(f"工作流 profile 必须是对象: {index}")
        profile_id = str(raw_profile.get("id", "")).strip()
        if not _FIELD_ID.fullmatch(profile_id) or profile_id in profile_ids:
            raise ValueError(f"工作流 profile id 无效或重复: {profile_id}")
        ordered_segments = [str(field_id) for field_id in raw_profile.get("ordered_segments", [])]
        if not ordered_segments or len(ordered_segments) != len(set(ordered_segments)):
            raise ValueError(f"profile.ordered_segments 必须是非空且不重复的字段列表: {profile_id}")
        if any(field_id not in field_ids for field_id in ordered_segments):
            raise ValueError(f"profile.ordered_segments 引用了不存在的字段: {profile_id}")
        optional_segments = [str(field_id) for field_id in raw_profile.get("optional_segments", [])]
        if any(field_id not in ordered_segments for field_id in optional_segments):
            raise ValueError(f"profile.optional_segments 必须属于 ordered_segments: {profile_id}")
        defaults = raw_profile.get("defaults", {})
        if not isinstance(defaults, dict) or any(str(field_id) not in field_ids for field_id in defaults):
            raise ValueError(f"profile.defaults 引用了不存在的字段: {profile_id}")
        parse_templates = raw_profile.get("parse_templates", [])
        if not isinstance(parse_templates, list) or any(not isinstance(item, str) for item in parse_templates):
            raise ValueError(f"profile.parse_templates 必须是字符串数组: {profile_id}")
        for parse_template in parse_templates:
            references = re.findall(r"\{([a-zA-Z][a-zA-Z0-9_]*)\}", parse_template)
            if not references or any(field_id not in field_ids for field_id in references):
                raise ValueError(f"profile.parse_templates 引用了不存在的字段: {profile_id}")
        parse_patterns = raw_profile.get("parse_patterns", [])
        if not isinstance(parse_patterns, list) or any(not isinstance(item, str) for item in parse_patterns):
            raise ValueError(f"profile.parse_patterns 必须是字符串数组: {profile_id}")
        for parse_pattern in parse_patterns:
            if (len(parse_pattern) > 4096 or not parse_pattern.startswith("^") or not parse_pattern.endswith("$")
                    or re.search(r"\(\?<?[=!]|\(\?P=|\\[1-9]", parse_pattern)
                    or re.search(r"\([^)]*[+*][^)]*\)[+*{]", parse_pattern)):
                raise ValueError(f"profile.parse_patterns 必须锚定且不能包含回溯引用或高风险嵌套量词: {profile_id}")
            try:
                compiled_pattern = re.compile(parse_pattern, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"profile.parse_patterns 包含无效正则: {profile_id}") from exc
            if any(field_id not in field_ids for field_id in compiled_pattern.groupindex):
                raise ValueError(f"profile.parse_patterns 引用了不存在的字段: {profile_id}")
        fixed_prefix_tokens = [str(token) for token in raw_profile.get("fixed_prefix_tokens", [])]
        fixed_suffix_tokens = [str(token) for token in raw_profile.get("fixed_suffix_tokens", [])]
        if any(not token.strip() for token in fixed_prefix_tokens + fixed_suffix_tokens):
            raise ValueError(f"profile 固定 token 不能为空: {profile_id}")
        normalised = copy.deepcopy(raw_profile)
        normalised.update({
            "id": profile_id,
            "label": str(raw_profile.get("label", profile_id)),
            "ordered_segments": ordered_segments,
            "optional_segments": optional_segments,
            "defaults": {str(field_id): str(value or "") for field_id, value in defaults.items()},
            "parse_templates": parse_templates,
            "parse_patterns": parse_patterns,
            "fixed_prefix_tokens": fixed_prefix_tokens,
            "fixed_suffix_tokens": fixed_suffix_tokens,
            "priority": int(raw_profile.get("priority", 0)),
            "variant_style": str(raw_profile.get("variant_style", "") or ""),
            "asset_index_style": str(raw_profile.get("asset_index_style", "") or ""),
            "numbering": _normalise_numbering(
                raw_profile.get("numbering", {}), field_ids, f"profiles.{profile_id}.numbering"
            ),
        })
        result.append(normalised)
        profile_ids.add(profile_id)
    return result


def validate_workflow(value: dict[str, Any], *, allow_builtin: bool = True) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("工作流必须是 JSON 对象")
    result = copy.deepcopy(value)
    result["schema_version"] = int(result.get("schema_version", WORKFLOW_SCHEMA_VERSION))
    if result["schema_version"] != WORKFLOW_SCHEMA_VERSION:
        raise ValueError(f"不支持的工作流 schema_version: {result['schema_version']}")
    workflow_id = str(result.get("id", "")).strip()
    if not _FIELD_ID.fullmatch(workflow_id):
        raise ValueError("工作流 id 只能使用小写字母、数字、短横线和下划线，且必须以字母开头")
    if not str(result.get("name", "")).strip():
        raise ValueError("工作流名称不能为空")
    if result.get("builtin") and not allow_builtin:
        raise ValueError("导入工作流不能声明为内置工作流")
    result["builtin"] = bool(result.get("builtin", False))
    fields = result.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ValueError("工作流至少需要一个字段")
    field_ids: set[str] = set()
    normalised_fields: list[dict[str, Any]] = []
    for field_value in fields:
        if not isinstance(field_value, dict):
            raise ValueError("工作流字段必须是对象")
        field_value = copy.deepcopy(field_value)
        raw_extractors = field_value.get("extractors", [])
        if not isinstance(raw_extractors, list):
            raise ValueError(f"工作流字段 extractors 必须是数组: {field_value.get('id', '')}")
        field_id = str(field_value.get("id", "")).strip()
        if not _FIELD_ID.fullmatch(field_id) or field_id in field_ids:
            raise ValueError(f"工作流字段 id 无效或重复: {field_id}")
        scope = str(field_value.get("scope", "record"))
        if scope not in {"workflow", "group", "record", "suffix"}:
            raise ValueError(f"工作流字段作用域无效: {field_id}")
        kind = str(field_value.get("kind", "text"))
        if kind not in {"text", "choice", "number", "fixed"}:
            raise ValueError(f"工作流字段类型无效: {field_id}")
        initial_source = str(field_value.get("initial_source", "") or "").strip()
        if initial_source not in _INITIAL_SOURCES:
            raise ValueError(f"工作流字段 initial_source 无效: {field_id}")
        field_value.update({
            "id": field_id,
            "label": str(field_value.get("label", field_id)),
            "scope": scope,
            "kind": kind,
            "required": bool(field_value.get("required", False)),
            "editable": bool(field_value.get("editable", kind != "fixed")),
            "default": str(field_value.get("default", "") or ""),
            "sources": [str(source) for source in field_value.get("sources", ["manual", "excel", "filename", "metadata"])],
            "quick_tags": field_value.get("quick_tags", []),
            "extractor": str(field_value.get("extractor", "") or ""),
            "extractors": [str(extractor) for extractor in raw_extractors],
            "normalizer": str(field_value.get("normalizer", "") or ""),
            "initial_source": initial_source,
        })
        if not isinstance(field_value["quick_tags"], list):
            raise ValueError(f"工作流快捷标签必须是数组: {field_id}")
        for tag in field_value["quick_tags"]:
            if isinstance(tag, str):
                continue
            if not isinstance(tag, dict) or not str(tag.get("value", "")).strip():
                raise ValueError(f"工作流快捷标签无效: {field_id}")
        field_ids.add(field_id)
        normalised_fields.append(field_value)
    result["fields"] = normalised_fields
    filename_parser = str(result.get("filename_parser", "") or "").strip()
    if filename_parser and not _ACTION_ID.fullmatch(filename_parser):
        raise ValueError(f"工作流 filename_parser 无效: {filename_parser}")
    result["filename_parser"] = filename_parser
    resource_filter = result.get("resource_filter", {})
    if resource_filter in (None, ""):
        resource_filter = {}
    if not isinstance(resource_filter, dict):
        raise ValueError("工作流 resource_filter 必须是对象")
    resource_kinds = {"audio", "midi", "preset", "artwork", "document", "other"}
    included_resources = [str(item).strip().casefold() for item in resource_filter.get("include", [])]
    if any(item not in resource_kinds for item in included_resources):
        raise ValueError("工作流 resource_filter.include 包含无效资源类型")
    mismatch_mode = str(resource_filter.get("on_mismatch", "include") or "include").strip().casefold()
    if mismatch_mode not in {"include", "skip"}:
        raise ValueError("工作流 resource_filter.on_mismatch 只能是 include 或 skip")
    result["resource_filter"] = {"include": included_resources, "on_mismatch": mismatch_mode}
    grouping = result.get("grouping", {})
    if grouping in (None, ""):
        grouping = {}
    if not isinstance(grouping, dict):
        raise ValueError("工作流 grouping 必须是对象")
    grouping_mode = str(grouping.get("mode", "extension") or "extension").strip().casefold()
    grouping_filter = str(grouping.get("filter", "all") or "all").strip().casefold()
    if grouping_mode not in {"extension", "directory"}:
        raise ValueError("工作流 grouping.mode 只能是 extension 或 directory")
    if grouping_filter not in {"all", "image"}:
        raise ValueError("工作流 grouping.filter 只能是 all 或 image")
    result["grouping"] = {"mode": grouping_mode, "filter": grouping_filter}
    result["metadata_providers"] = _normalise_metadata_providers(result.get("metadata_providers", []))
    result["modules"] = _normalise_workflow_modules(result.get("modules", []), workflow_field_map(result))
    result["actions"] = _normalise_actions(result.get("actions", []), field_ids, workflow_field_map(result))
    result["derived"] = _normalise_derived(result.get("derived", []))
    result["rules"] = _normalise_rules(result.get("rules", []), field_ids, workflow_field_map(result))
    result["profiles"] = _normalise_profiles(result.get("profiles", []), field_ids)
    profile_ids = {profile["id"] for profile in result["profiles"]}
    profile_field = str(result.get("profile_field", "") or "")
    if profile_field:
        definition = workflow_field_map(result).get(profile_field)
        if not definition or definition.get("kind") != "choice" or definition.get("scope") != "record":
            raise ValueError("profile_field 必须引用 record 作用域的 choice 字段")
    if result["profiles"] and not profile_field:
        raise ValueError("声明 profiles 时必须提供 profile_field")
    default_profile = str(result.get("default_profile", "") or "")
    if default_profile and default_profile not in profile_ids:
        raise ValueError("default_profile 必须引用已声明的 profile")
    result["profile_field"] = profile_field
    result["default_profile"] = default_profile
    result["template"] = _normalise_template(result.get("template", []))
    for part in result["template"]:
        if part.get("field") and part["field"] not in field_ids:
            raise ValueError(f"template 引用了不存在的字段: {part['field']}")
    suffix_modes = result.get("suffix_modes", {})
    if not isinstance(suffix_modes, dict):
        raise ValueError("suffix_modes 必须是对象")
    result["suffix_modes"] = {
        str(mode): [str(field_id) for field_id in field_ids_value]
        for mode, field_ids_value in suffix_modes.items()
        if isinstance(field_ids_value, list)
    }
    action_ids = {action["id"] for action in result["actions"]}
    for mode_fields in result["suffix_modes"].values():
        if any(field_id not in field_ids and field_id not in action_ids for field_id in mode_fields):
            raise ValueError("suffix_modes 引用了不存在的字段或 action")
    result["suffix_options"] = result.get("suffix_options", []) if isinstance(result.get("suffix_options", []), list) else []
    suffix_field = str(result.get("suffix_field", "") or "")
    if suffix_field and suffix_field not in field_ids:
        raise ValueError("suffix_field 必须引用工作流字段")
    if suffix_field and next(field for field in normalised_fields if field["id"] == suffix_field)["scope"] != "workflow":
        raise ValueError("suffix_field 必须引用 workflow 作用域字段")
    result["suffix_field"] = suffix_field
    excel_field = str(result.get("excel_field", "") or "")
    if excel_field and excel_field not in field_ids:
        raise ValueError("excel_field 必须引用工作流字段")
    if excel_field and next(field for field in normalised_fields if field["id"] == excel_field)["scope"] not in {"record", "suffix"}:
        raise ValueError("excel_field 必须引用文件级字段")
    result["excel_field"] = excel_field
    excel_placeholders = result.get("excel_placeholders", {})
    if not isinstance(excel_placeholders, dict):
        raise ValueError("excel_placeholders 必须是对象")
    normalised_placeholders: dict[str, str] = {}
    for placeholder, path in excel_placeholders.items():
        placeholder_id = str(placeholder).strip().casefold()
        path_text = str(path).strip()
        if not _ACTION_ID.fullmatch(placeholder_id):
            raise ValueError(f"Excel 占位符无效: {placeholder}")
        parts = path_text.split(".")
        if (not _METADATA_PATH.fullmatch(path_text) or not parts
                or parts[0] not in _CONTEXT_ROOTS):
            raise ValueError(f"Excel 占位符路径无效: {path_text}")
        normalised_placeholders[placeholder_id] = path_text
    result["excel_placeholders"] = normalised_placeholders
    result["numbering"] = _normalise_numbering(result.get("numbering", {}), field_ids)
    collision_suffix = result.get("collision_suffix", {})
    if collision_suffix in (None, ""):
        collision_suffix = {}
    if not isinstance(collision_suffix, dict):
        raise ValueError("collision_suffix 必须是对象")
    result["collision_suffix"] = {
        "enabled": bool(collision_suffix.get("enabled", True)),
        "width": max(1, int(collision_suffix.get("width", 2))),
        "start": max(1, int(collision_suffix.get("start", 1))),
    }
    result["separator"] = str(result.get("separator", "_") or "_")
    result["name_modes"] = [str(mode) for mode in result.get("name_modes", ["original"])]
    return result


def workflow_field_map(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {field["id"]: field for field in workflow.get("fields", [])}


CORE_FALLBACK_WORKFLOW = validate_workflow({
    "schema_version": WORKFLOW_SCHEMA_VERSION,
    "id": "core-fallback",
    "name": "基础模式",
    "version": "1.0.0",
    "description": "未安装工作流时保留原文件名，供核心工作台继续运行。",
    "builtin": True,
    "kind": "core",
    "separator": "_",
    "name_modes": ["original"],
    "fields": [
        {
            "id": "name",
            "label": "名称",
            "scope": "record",
            "kind": "text",
            "initial_source": "stem",
        },
    ],
    "template": [{"field": "name"}],
    "metadata_providers": [],
    "modules": [],
    "actions": [],
    "suffix_modes": {},
    "suffix_options": [],
    "numbering": {"enabled": False, "field": ""},
})
