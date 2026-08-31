from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from workflow_system.catalog import WorkflowCatalog, discover_workflows
from workflow_system.package import (
    load_workflow_bundle,
    load_workflow_package,
    package_workflow,
)
from workflow_system.schema import CORE_FALLBACK_WORKFLOW, validate_workflow


class WorkflowConfigTests(unittest.TestCase):
    def installed_workflow(self, workflow_id: str) -> dict:
        workflows, _errors = discover_workflows()
        if workflow_id not in workflows:
            self.skipTest(f"未安装工作流：{workflow_id}")
        return workflows[workflow_id]

    @staticmethod
    def write_workflow(root: Path, folder: str, workflow_id: str, name: str = "Test") -> Path:
        manifest = root / folder / "workflow.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({
            "id": workflow_id,
            "name": name,
            "fields": [{"id": "name", "label": "Name", "scope": "record", "kind": "text"}],
            "template": [{"field": "name"}],
        }), encoding="utf-8")
        return manifest

    def test_installed_workflows_are_portable_packages(self):
        workflows, _errors = discover_workflows()
        for workflow in workflows.values():
            packaged = package_workflow(workflow)
            restored = load_workflow_package(packaged, f"{workflow['id']}.ffnf-workflow")
            self.assertEqual(restored["id"], workflow["id"])
            self.assertEqual(restored["template"], workflow["template"])
            self.assertTrue(restored["fields"])
            with zipfile.ZipFile(io.BytesIO(packaged)) as archive:
                names = set(archive.namelist())
            self.assertFalse(any("__pycache__" in Path(name).parts or name.endswith(".pyc") for name in names))
            if workflow["id"] == "sample-pack":
                self.assertIn("module-manifest.json", names)
                self.assertIn("modules/sample_pack.py", names)
            if workflow["id"] == "wallpaper-assets":
                self.assertIn("modules/image_assets.py", names)
                self.assertIn("modules/wallpaper.py", names)

    def test_module_package_requires_explicit_trust(self):
        workflow = self.installed_workflow("sample-pack")
        restored, package_files = load_workflow_bundle(
            package_workflow(workflow), "sample-pack.ffnf-workflow"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            catalog = WorkflowCatalog(Path(temp_dir) / "config.json")
            with self.assertRaisesRegex(ValueError, "确认信任"):
                catalog.install_package(restored, package_files, "copy")

    def test_catalog_persists_preferences_and_copies_conflicts(self):
        sample_pack_workflow = self.installed_workflow("sample-pack")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            catalog = WorkflowCatalog(path)
            catalog.theme = "dark"
            imported, existed = catalog.upsert_import(sample_pack_workflow)
            self.assertTrue(existed)
            self.assertNotEqual(imported["id"], sample_pack_workflow["id"])
            copied, existed = catalog.upsert_import(sample_pack_workflow)
            self.assertTrue(existed)
            self.assertNotEqual(imported["id"], copied["id"])
            self.assertEqual(catalog.theme, "dark")
            reloaded = WorkflowCatalog(path)
            self.assertEqual(reloaded.theme, "dark")
            self.assertIn(copied["id"], reloaded.all_ids())

    def test_validation_rejects_unknown_template_field(self):
        invalid = json.loads(json.dumps(self.installed_workflow("sample-pack")))
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

    def test_validation_binds_module_output_slot_to_field_scope(self):
        workflow = {
            "id": "module-output",
            "name": "Module output",
            "fields": [{"id": "tag", "label": "Tag", "scope": "record", "kind": "text"}],
            "modules": [{
                "id": "analysis",
                "trigger": "on_user_request",
                "outputs": [{"id": "tag_value", "field": "tag", "scope": "record"}],
            }],
        }
        normalized = validate_workflow(workflow)
        self.assertEqual(normalized["modules"][0]["outputs"][0]["field"], "tag")
        workflow["modules"][0]["outputs"][0]["scope"] = "group"
        with self.assertRaisesRegex(ValueError, "scope"):
            validate_workflow(workflow)

    def test_wallpaper_workflow_exposes_source_quick_tags(self):
        workflow = self.installed_workflow("wallpaper-assets")
        source = next(field for field in workflow["fields"] if field["id"] == "source")
        self.assertIn({"label": "Pixiv", "value": "Pixiv"}, source["quick_tags"])
        self.assertIn({"label": "Wallhaven", "value": "Wallhaven"}, source["quick_tags"])
        self.assertTrue(workflow["builtin"])
        self.assertEqual(validate_workflow(workflow)["fields"][-2]["quick_tags"], source["quick_tags"])

    def test_sample_pack_profiles_split_identity_and_numbering_semantics(self):
        sample_pack_workflow = self.installed_workflow("sample-pack")
        fields = {field["id"]: field for field in sample_pack_workflow["fields"]}
        self.assertEqual(fields["author_code"]["scope"], "workflow")
        self.assertEqual(fields["pack_code"]["scope"], "workflow")
        self.assertFalse(fields["author_code"].get("required", False))
        self.assertIn("key_or_chord", fields)
        self.assertIn("asset_index", fields)
        self.assertIn("variant", fields)
        self.assertNotIn("number", fields)
        self.assertEqual(sample_pack_workflow["resource_filter"]["include"], ["audio", "midi"])
        profiles = {profile["id"]: profile for profile in sample_pack_workflow["profiles"]}
        self.assertEqual(profiles["botanica"]["fixed_suffix_tokens"], ["FA"])
        self.assertEqual(profiles["shaw-bass"]["numbering"]["field"], "asset_index")
        self.assertFalse(sample_pack_workflow["numbering"]["enabled"])

    def test_validation_rejects_profile_with_unknown_segment(self):
        invalid = json.loads(json.dumps(self.installed_workflow("sample-pack")))
        invalid["profiles"][0]["ordered_segments"].append("does_not_exist")
        with self.assertRaisesRegex(ValueError, "ordered_segments"):
            validate_workflow(invalid)

    def test_catalog_discovers_updates_and_removes_workflows_at_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workflows"
            root.mkdir()
            catalog = WorkflowCatalog(Path(temp_dir) / "config.json", root)
            self.assertEqual(catalog.current_workflow, CORE_FALLBACK_WORKFLOW["id"])
            self.assertEqual([item["id"] for item in catalog.all()], [CORE_FALLBACK_WORKFLOW["id"]])

            manifest = self.write_workflow(root, "extra-pack", "extra-pack")
            added = catalog.refresh()
            self.assertEqual(added["added"], ["extra-pack"])
            self.assertEqual(catalog.current_workflow, "extra-pack")

            self.write_workflow(root, "extra-pack", "extra-pack", "Updated workflow")
            updated = catalog.refresh()
            self.assertEqual(updated["updated"], ["extra-pack"])
            self.assertEqual(catalog.get("extra-pack")["name"], "Updated workflow")

            manifest.unlink()
            removed = catalog.refresh()
            self.assertEqual(removed["removed"], ["extra-pack"])
            self.assertEqual(catalog.current_workflow, CORE_FALLBACK_WORKFLOW["id"])
            self.assertIn("缺少 workflow.json", [item["error"] for item in catalog.diagnostics()])

    def test_discovery_isolates_invalid_workflow_plugins(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workflows"
            self.write_workflow(root, "healthy", "healthy")
            invalid = root / "invalid" / "workflow.json"
            invalid.parent.mkdir()
            invalid.write_text("{broken", encoding="utf-8")
            (root / "missing").mkdir()

            workflows, errors = discover_workflows(root)

            self.assertEqual(set(workflows), {"healthy"})
            self.assertEqual({item["folder"] for item in errors}, {"invalid", "missing"})

    def test_catalog_merges_install_roots_and_isolates_id_conflicts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundled = Path(temp_dir) / "bundled"
            installed = Path(temp_dir) / "installed"
            self.write_workflow(bundled, "alpha", "alpha", "Bundled alpha")
            self.write_workflow(installed, "beta", "beta", "Installed beta")
            self.write_workflow(installed, "duplicate", "alpha", "Conflicting alpha")

            catalog = WorkflowCatalog(
                Path(temp_dir) / "config.json",
                (bundled, installed),
            )

            self.assertEqual(catalog.all_ids(), {"alpha", "beta"})
            self.assertEqual(catalog.get("alpha")["name"], "Bundled alpha")
            self.assertTrue(any("id 与其他安装目录冲突" in item["error"] for item in catalog.diagnostics()))


if __name__ == "__main__":
    unittest.main()
