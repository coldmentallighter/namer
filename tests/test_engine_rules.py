from __future__ import annotations

import unittest

from core.files import FileRecord, NamingGroup
from engine import WorkflowEngine
from engine.rules import condition_matches, expression_value, path_value


class EngineRulesTests(unittest.TestCase):
    def test_engine_composes_scoped_values_without_server_state(self):
        record = FileRecord(
            path="C:/samples/kick.wav", root="C:/samples", extension=".wav",
            folder_name="samples", relative_folder="", original_name="kick.wav", stem="kick",
        )
        group = NamingGroup("group", "C:/samples", "samples", ".wav", [record])
        workflow = {
            "fields": [
                {"id": "pack", "scope": "workflow", "default": ""},
                {"id": "kind", "scope": "group", "default": ""},
                {"id": "name", "scope": "record", "default": ""},
            ],
            "template": [{"field": "pack"}, {"field": "kind"}, {"field": "name"}],
        }
        group.workflow_values["kind"] = "Drum"
        record.workflow_values["name"] = "Kick"
        engine = WorkflowEngine(lambda _workflow, _field, value: str(value or ""), lambda _name: None)
        self.assertEqual(
            engine.compose_target(workflow, group, record, {"pack": "Studio"}, "", "_"),
            "Studio_Drum_Kick.wav",
        )

    def test_path_value_returns_nested_values_and_none_for_missing_paths(self):
        context = {"metadata": {"image": {"width": 1920}}}
        self.assertEqual(path_value(context, "metadata.image.width"), 1920)
        self.assertIsNone(path_value(context, "metadata.image.height"))

    def test_expression_value_composes_declared_operations(self):
        context = {"metadata": {"image": {"width": 1920, "height": 1080}}}
        expression = {
            "op": "concat",
            "args": [
                {"op": "divide", "args": [
                    {"path": "metadata.image.width"},
                    {"path": "metadata.image.height"},
                ]},
                {"value": ":landscape"},
            ],
        }
        self.assertEqual(expression_value(expression, context), "1.7777777777777777:landscape")
        self.assertIsNone(expression_value({
            "op": "divide", "args": [{"value": 10}, {"value": 0}],
        }, context))

    def test_condition_matches_nested_and_numeric_conditions(self):
        context = {
            "metadata": {"audio": {"bpm": "128", "kind": "Loop"}},
            "derived": {"has_voice": True},
        }
        condition = {"all": [
            {"path": "metadata.audio.bpm", "op": "gte", "value": 120},
            {"path": "metadata.audio.kind", "op": "in", "value": ["loop", "one-shot"]},
            {"not": {"path": "derived.has_voice", "op": "equals", "value": False}},
        ]}
        self.assertTrue(condition_matches(condition, context))
        self.assertFalse(condition_matches(
            {"path": "metadata.audio.missing", "op": "exists"}, context
        ))


if __name__ == "__main__":
    unittest.main()
