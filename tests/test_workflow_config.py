from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from workflow_config import (
    BUILTIN_WORKFLOWS,
    SAMPLE_PACK_WORKFLOW,
    WorkflowCatalog,
    load_workflow_package,
    package_workflow,
    validate_workflow,
)


class WorkflowConfigTests(unittest.TestCase):
    def test_builtin_workflows_are_portable_packages(self):
        for workflow in BUILTIN_WORKFLOWS.values():
            packaged = package_workflow(workflow)
            restored = load_workflow_package(packaged, f"{workflow['id']}.ffnf-workflow")
            self.assertEqual(restored["id"], workflow["id"])
            self.assertEqual(restored["template"], workflow["template"])
            self.assertTrue(restored["fields"])

    def test_catalog_persists_preferences_and_copies_conflicts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            catalog = WorkflowCatalog(path)
            catalog.theme = "dark"
            imported, existed = catalog.upsert_import(SAMPLE_PACK_WORKFLOW)
            self.assertTrue(existed)
            self.assertNotEqual(imported["id"], SAMPLE_PACK_WORKFLOW["id"])
            copied, existed = catalog.upsert_import(SAMPLE_PACK_WORKFLOW)
            self.assertTrue(existed)
            self.assertNotEqual(imported["id"], copied["id"])
            self.assertEqual(catalog.theme, "dark")
            reloaded = WorkflowCatalog(path)
            self.assertEqual(reloaded.theme, "dark")
            self.assertIn(copied["id"], reloaded.all_ids())

    def test_validation_rejects_unknown_template_field(self):
        invalid = json.loads(json.dumps(SAMPLE_PACK_WORKFLOW))
        invalid["template"].append({"field": "does_not_exist"})
        with self.assertRaises(ValueError):
            validate_workflow(invalid)

    def test_validation_normalizes_and_rejects_initial_source(self):
        workflow = {
            "id": "initial-source",
            "name": "Initial source",
            "fields": [{
                "id": "name", "label": "Name", "scope": "record", "kind": "text",
                "initial_source": "stem",
            }],
            "template": [{"field": "name"}],
        }
        self.assertEqual(validate_workflow(workflow)["fields"][0]["initial_source"], "stem")
        workflow["fields"][0]["initial_source"] = "legacy.name"
        with self.assertRaisesRegex(ValueError, "initial_source"):
            validate_workflow(workflow)

    def test_validation_normalizes_metadata_rules(self):
        workflow = {
            "id": "metadata-rules",
            "name": "Metadata rules",
            "fields": [{"id": "tag", "label": "Tag", "scope": "record", "kind": "text"}],
            "rules": [{
                "id": "wide",
                "when": {"path": "metadata.image.width", "op": "gt", "value": 1000},
                "then": {"field": "tag", "value": "wide", "reason": "width"},
            }],
        }
        normalized = validate_workflow(workflow)
        self.assertEqual(normalized["rules"][0]["when"]["op"], "gt")
        self.assertEqual(normalized["rules"][0]["then"][0]["mode"], "suggest")

    def test_wallpaper_workflow_exposes_source_quick_tags(self):
        workflow = BUILTIN_WORKFLOWS["wallpaper-assets"]
        source = next(field for field in workflow["fields"] if field["id"] == "source")
        self.assertIn({"label": "Pixiv", "value": "Pixiv"}, source["quick_tags"])
        self.assertIn({"label": "Wallhaven", "value": "Wallhaven"}, source["quick_tags"])
        self.assertTrue(workflow["builtin"])
        self.assertEqual(validate_workflow(workflow)["fields"][-2]["quick_tags"], source["quick_tags"])

    def test_sample_pack_profiles_split_identity_and_numbering_semantics(self):
        fields = {field["id"]: field for field in SAMPLE_PACK_WORKFLOW["fields"]}
        self.assertEqual(fields["author_code"]["scope"], "workflow")
        self.assertEqual(fields["pack_code"]["scope"], "workflow")
        self.assertFalse(fields["author_code"].get("required", False))
        self.assertIn("key_or_chord", fields)
        self.assertIn("asset_index", fields)
        self.assertIn("variant", fields)
        self.assertNotIn("number", fields)
        self.assertEqual(SAMPLE_PACK_WORKFLOW["resource_filter"]["include"], ["audio", "midi"])
        profiles = {profile["id"]: profile for profile in SAMPLE_PACK_WORKFLOW["profiles"]}
        self.assertEqual(profiles["botanica"]["fixed_suffix_tokens"], ["FA"])
        self.assertEqual(profiles["shaw-bass"]["numbering"]["field"], "asset_index")
        self.assertFalse(SAMPLE_PACK_WORKFLOW["numbering"]["enabled"])

    def test_validation_rejects_profile_with_unknown_segment(self):
        invalid = json.loads(json.dumps(SAMPLE_PACK_WORKFLOW))
        invalid["profiles"][0]["ordered_segments"].append("does_not_exist")
        with self.assertRaisesRegex(ValueError, "ordered_segments"):
            validate_workflow(invalid)


if __name__ == "__main__":
    unittest.main()
