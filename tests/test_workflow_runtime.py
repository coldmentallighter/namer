from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from workflow_config import WorkflowCatalog
from workflow_runtime import WorkflowModuleError


class WorkflowRuntimeTests(unittest.TestCase):
    @staticmethod
    def write_runner_workflow(root: Path, workflow_id: str, source: str) -> Path:
        folder = root / workflow_id
        modules = folder / "modules"
        modules.mkdir(parents=True, exist_ok=True)
        (folder / "workflow.json").write_text(json.dumps({
            "id": workflow_id,
            "name": workflow_id,
            "fields": [{"id": "tag", "label": "Tag", "scope": "record", "kind": "text"}],
            "template": [{"field": "tag"}],
            "modules": [{
                "id": "analysis",
                "label": "Analysis",
                "trigger": "on_user_request",
                "outputs": [{"id": "tag_value", "field": "tag", "scope": "record"}],
            }],
        }), encoding="utf-8")
        (folder / "module-manifest.json").write_text(json.dumps({
            "schema_version": 1,
            "modules": [{
                "id": "analysis",
                "entrypoint": "modules/analysis.py",
                "runner": "run",
            }],
        }), encoding="utf-8")
        entrypoint = modules / "analysis.py"
        entrypoint.write_text(source, encoding="utf-8")
        return entrypoint

    def test_runner_is_workflow_scoped_and_reloads_with_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "workflows"
            root.mkdir()
            entrypoint = self.write_runner_workflow(
                root, "alpha",
                "from .helper import VALUE\n\ndef run(request):\n    return {'items': [{'id': item['id'], 'values': {'tag_value': VALUE}} for item in request['items']]}\n",
            )
            helper = entrypoint.parent / "helper.py"
            helper.write_text("VALUE = 'Alpha'\n", encoding="utf-8")
            self.write_runner_workflow(
                root, "beta",
                "def run(request):\n    return {'items': [{'id': item['id'], 'values': {'tag_value': 'Beta'}} for item in request['items']]}\n",
            )
            catalog = WorkflowCatalog(base / "config.json", root, base / "installed")
            item = [{"id": "item-001", "path": "unused"}]

            alpha, request = catalog.module_registry.run(
                catalog.get("alpha"), "analysis", "on_user_request", item
            )
            beta, _request = catalog.module_registry.run(
                catalog.get("beta"), "analysis", "on_user_request", item
            )

            self.assertEqual(request["items"][0]["id"], "item-001")
            self.assertEqual(alpha["items"][0]["values"]["tag_value"], "Alpha")
            self.assertEqual(beta["items"][0]["values"]["tag_value"], "Beta")

            helper.write_text("VALUE = 'AlphaReloaded'\n", encoding="utf-8")
            refreshed = catalog.refresh()
            reloaded, _request = catalog.module_registry.run(
                catalog.get("alpha"), "analysis", "on_user_request", item
            )
            self.assertTrue(refreshed["changed"])
            self.assertIn("alpha", refreshed["updated"])
            self.assertEqual(reloaded["items"][0]["values"]["tag_value"], "AlphaReloaded")

    def test_bad_module_is_isolated_and_non_string_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "workflows"
            root.mkdir()
            self.write_runner_workflow(
                root, "healthy",
                "def run(request):\n    return {'items': [{'id': item['id'], 'values': {'tag_value': 123}} for item in request['items']]}\n",
            )
            self.write_runner_workflow(root, "broken", "def run(:\n")
            catalog = WorkflowCatalog(base / "config.json", root, base / "installed")

            self.assertIn("healthy", catalog.all_ids())
            self.assertNotIn("broken", catalog.all_ids())
            self.assertTrue(any(item.get("workflow_id") == "broken" for item in catalog.diagnostics()))
            with self.assertRaisesRegex(WorkflowModuleError, "字符串"):
                catalog.module_registry.run(
                    catalog.get("healthy"), "analysis", "on_user_request",
                    [{"id": "item-001", "path": "unused"}],
                )

    def test_missing_declared_capability_disables_only_that_workflow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "workflows"
            root.mkdir()
            self.write_runner_workflow(
                root, "healthy",
                "def run(request):\n    return {'items': []}\n",
            )
            missing = root / "missing-provider"
            missing.mkdir()
            (missing / "workflow.json").write_text(json.dumps({
                "id": "missing-provider",
                "name": "Missing provider",
                "fields": [{"id": "tag", "label": "Tag", "scope": "record", "kind": "text"}],
                "metadata_providers": [{"provider": "private_metadata"}],
            }), encoding="utf-8")

            catalog = WorkflowCatalog(base / "config.json", root, base / "installed")

            self.assertIn("healthy", catalog.all_ids())
            self.assertNotIn("missing-provider", catalog.all_ids())
            self.assertTrue(any("private_metadata" in item["error"] for item in catalog.diagnostics()))


if __name__ == "__main__":
    unittest.main()
