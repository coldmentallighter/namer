from __future__ import annotations

import io
import json
import shutil
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

import web_app
from workflow_config import load_workflow_package


class WorkflowApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name) / "Root"
        (cls.root / "Drums").mkdir(parents=True)
        for name in ("One.wav", "Two.wav", "Three.wav", "Four.wav", "Five.wav"):
            (cls.root / "Drums" / name).write_bytes(b"RIFF")
        install_root = web_app.STATE.workflow_catalog.install_root
        cls.initial_installed_workflow_dirs = {
            path.name for path in install_root.iterdir() if path.is_dir()
        } if install_root.is_dir() else set()
        cls.server, cls.url = web_app.run_server(0, False)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    def setUp(self):
        self.post_json("/api/workflow/select", {"workflow_id": "default"})

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        web_app.STATE.workflow_catalog.user_workflows.clear()
        web_app.STATE.workflow_catalog.current_workflow = "default"
        web_app.STATE.workflow_id = "default"
        web_app.STATE.workflow_catalog.save()
        install_root = web_app.STATE.workflow_catalog.install_root
        if install_root.is_dir():
            for path in install_root.iterdir():
                if path.is_dir() and path.name not in cls.initial_installed_workflow_dirs:
                    shutil.rmtree(path)
        web_app.STATE.workflow_catalog.refresh(force=True)
        config_path = web_app.STATE.workflow_catalog.path
        if config_path.exists():
            config_path.unlink()
        cls.temp.cleanup()

    def post_json(self, path: str, payload: dict) -> dict:
        request = Request(
            self.url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_sample_pack_profiles_round_trip_representative_names(self):
        cases = {
            "CL_IE_Amb_ArpLand_Em7-A7.wav": ("coldlight", "Em7-A7"),
            "Shaw_Tyan_BASS_01_Phaser_A.wav": ("shaw-bass", "A"),
            "Shaw_Tyan_Atmos_Organ_B_G#.wav": ("shaw-tyan", "B_G#"),
            "BT_Syn_Melodic_130_Eb_Unifauna_v1_FA.wav": ("botanica", "Eb"),
        }
        for index, (filename, (profile_id, key_or_chord)) in enumerate(cases.items()):
            root = Path(self.temp.name) / f"ProfileCase{index}"
            root.mkdir()
            (root / filename).write_bytes(b"RIFF")
            self.post_json("/api/workflow/select", {"workflow_id": "sample-pack"})
            self.post_json("/api/scan", {"root": str(root)})
            state = self.post_json("/api/workflow-fill", {})["state"]
            record = state["records"][0]
            self.assertEqual(record["workflow_values"]["profile_id"], profile_id)
            self.assertEqual(record["workflow_values"]["key_or_chord"], key_or_chord)
            self.assertEqual(record["target_name"], filename)

    def test_sample_pack_profile_numbering_only_assigns_asset_index(self):
        root = Path(self.temp.name) / "ShawBassNumbering"
        root.mkdir()
        for name in ("Alpha.wav", "Beta.wav", "Gamma.wav"):
            (root / name).write_bytes(b"RIFF")
        self.post_json("/api/workflow/select", {"workflow_id": "sample-pack"})
        state = self.post_json("/api/scan", {"root": str(root)})["state"]
        group_key = state["current_group_key"]
        for field, value in (("author_code", "Shaw"), ("pack_code", "Tyan")):
            state = self.post_json("/api/workflow-value", {
                "group_key": group_key, "field": field, "value": value,
            })["state"]
        for record in state["records"]:
            path = record["path"]
            for field, value in (("profile_id", "shaw-bass"), ("resource_type", "BASS")):
                state = self.post_json("/api/workflow-value", {
                    "group_key": group_key, "field": field, "value": value, "path": path,
                })["state"]
        self.assertEqual(
            [record["workflow_values"]["asset_index"] for record in state["records"]],
            ["01", "02", "03"],
        )
        self.assertTrue(all(record["workflow_values"]["variant"] == "" for record in state["records"]))

    def test_sample_pack_filename_bpm_wins_with_metadata_conflict_warning(self):
        root = Path(self.temp.name) / "BpmConflict"
        root.mkdir()
        acid = bytearray(24)
        struct.pack_into("<f", acid, 20, 120.0)
        body = b"WAVE" + b"acid" + struct.pack("<I", len(acid)) + acid
        source = root / "BT_Atmos_130_Eb_Solace_FA.wav"
        source.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
        self.post_json("/api/workflow/select", {"workflow_id": "sample-pack"})
        state = self.post_json("/api/scan", {"root": str(root)})["state"]
        record = state["records"][0]
        metadata = record["metadata"]["sample_pack"]
        self.assertEqual(metadata["bpm"], "130")
        self.assertEqual(metadata["bpm_metadata"], "120")
        self.assertIn("冲突", metadata["bpm_warning"])

    def test_workflow_module_returns_strings_as_confirmable_candidates(self):
        root = Path(self.temp.name) / "ModuleCandidateRoot"
        root.mkdir()
        source = root / "Loop_123BPM.wav"
        source.write_bytes(b"RIFF")
        self.post_json("/api/workflow/select", {"workflow_id": "sample-pack"})
        scanned = self.post_json("/api/scan", {"root": str(root)})["state"]
        self.assertNotIn("qualifier", scanned["records"][0]["workflow_candidates"])
        self.assertEqual(scanned["records"][0]["workflow_values"]["qualifier"], "")

        analyzed = self.post_json("/api/workflow-module/run", {"module_id": "sample_pack"})

        result_item = analyzed["result"]["items"][0]
        self.assertTrue(result_item["id"].startswith("item-"))
        self.assertEqual(result_item["values"]["tempo_tag"], "Tempo_123")
        record = analyzed["state"]["records"][0]
        self.assertEqual(record["workflow_candidates"]["qualifier"], ["Tempo_123"])
        self.assertEqual(record["workflow_values"]["qualifier"], "")
        self.assertEqual(
            record["workflow_candidate_details"]["qualifier"][0]["module_id"],
            "sample_pack",
        )

        filled = self.post_json("/api/workflow-fill", {})["state"]["records"][0]
        self.assertEqual(filled["workflow_values"]["qualifier"], "Tempo_123")

    def test_sample_pack_identity_values_are_shared_across_folders(self):
        root = Path(self.temp.name) / "MultiFolderSampleRoot"
        (root / "Drums").mkdir(parents=True)
        (root / "Bass").mkdir()
        (root / "Drums" / "Kick.wav").write_bytes(b"RIFF")
        (root / "Bass" / "Sub.wav").write_bytes(b"RIFF")

        self.post_json("/api/scan", {"root": str(root)})
        selected = self.post_json("/api/workflow/select", {"workflow_id": "sample-pack"})
        groups = selected["state"]["groups"]
        self.assertEqual(len(groups), 2)
        first_group_key = groups[0]["key"]
        state = self.post_json("/api/workflow-value", {
            "group_key": first_group_key,
            "field": "author_code",
            "value": "ColdLight",
        })["state"]
        state = self.post_json("/api/workflow-value", {
            "group_key": first_group_key,
            "field": "pack_code",
            "value": "Nebula",
        })["state"]

        self.assertEqual(state["workflow"]["values"]["author_code"], "ColdLight")
        self.assertEqual(state["workflow"]["values"]["pack_code"], "Nebula")
        for record in state["records"]:
            self.assertTrue(record["target_name"].startswith("ColdLight_Nebula_"), record["target_name"])

    def test_default_workflow_uses_canonical_dynamic_fields(self):
        scanned = self.post_json("/api/scan", {"root": str(self.root)})
        selected = self.post_json("/api/workflow/select", {"workflow_id": "default"})
        self.assertNotIn("suffix_order", selected["state"]["workflow"]["values"])
        self.assertFalse(any(field["id"] == "suffix_order" for field in selected["state"]["workflow"]["active"]["fields"]))
        group_key = selected["state"]["current_group_key"]
        first_path = next(record["path"] for record in scanned["state"]["records"] if record["original_name"] == "One.wav")
        for field, value in (("meta_prefix", "CL"), ("group_prefix", "Loops"), ("child_prefix", "bright")):
            state = self.post_json("/api/workflow-value", {
                "group_key": group_key,
                "field": field,
                "value": value,
                **({"path": first_path} if field == "child_prefix" else {}),
            })["state"]
        first = next(record for record in state["records"] if record["original_name"] == "One.wav")
        self.assertIn("CL_Loops_bright_One.wav", first["target_name"])
        self.assertEqual(first["workflow_values"]["child_prefix"], "bright")
        self.assertNotIn("child_prefix", first)
        self.assertNotIn("name", first)
        self.assertNotIn("base_name", first)
        self.assertNotIn("prefix", state["groups"][0])
        self.assertNotIn("meta_prefix", state["groups"][0])
        self.assertNotIn("meta_prefix", state)

        ignored = self.post_json("/api/preview", {
            "group_key": group_key,
            "meta_prefix": "legacy-meta",
            "group_prefix": "legacy-group",
            "child_prefix": "legacy-child",
        })["state"]
        first = next(record for record in ignored["records"] if record["original_name"] == "One.wav")
        self.assertEqual(first["workflow_values"]["child_prefix"], "bright")
        self.assertIn("CL_Loops_bright_One.wav", first["target_name"])

    def test_data_table_workflow_previews_conflict_then_renames(self):
        root = Path(self.temp.name) / "TableRoot"
        root.mkdir()
        source = root / "Orders.csv"
        source.write_text("id,value\n1,ok\n", encoding="utf-8")
        scanned = self.post_json("/api/scan", {"root": str(root)})
        selected = self.post_json("/api/workflow/select", {"workflow_id": "data-table"})
        self.assertNotIn("suffix_order", selected["state"]["workflow"]["values"])
        self.assertFalse(any(field["id"] == "suffix_order" for field in selected["state"]["workflow"]["active"]["fields"]))
        group_key = selected["state"]["current_group_key"]
        record_path = scanned["state"]["records"][0]["path"]
        self.assertEqual(scanned["state"]["records"][0]["status"], "Ready")
        missing = self.post_json("/api/preview", {"group_key": group_key, "mode": "original", "extensions": [".csv"]})["state"]
        self.assertEqual(missing["records"][0]["status"], "Conflict")
        self.assertIn("项目/数据集名", missing["records"][0]["status_detail"])
        for field, value in (("meta_prefix", "ColdLight"), ("dataset_name", "Sales"),
                             ("data_domain", "业务"), ("table_topic", "Orders")):
            state = self.post_json("/api/workflow-value", {
                "group_key": group_key, "field": field, "value": value,
            })["state"]
        for field, value in (("data_level", "raw"), ("purpose", "analysis"),
                             ("grain", "day"), ("partition", "date"), ("batch", "01")):
            state = self.post_json("/api/workflow-value", {
                "group_key": group_key, "field": field, "value": value,
                "path": record_path,
            })["state"]
        record = state["records"][0]
        self.assertEqual(record["status"], "Ready")
        self.assertIn("ColdLight_Sales_业务_Orders_raw_analysis_day_date_01_v01_Draft.csv", record["target_name"])
        renamed = self.post_json("/api/rename", {"scope": "group"})
        self.assertEqual(renamed["success"], 1)
        self.assertTrue((root / "ColdLight_Sales_业务_Orders_raw_analysis_day_date_01_v01_Draft.csv").exists())

    def test_sample_pack_defaults_to_audio_and_midi_resource_boundary(self):
        root = Path(self.temp.name) / "FilteredRoot"
        root.mkdir()
        (root / "Audio.wav").write_bytes(b"RIFF")
        (root / "Pattern.mid").write_bytes(b"MThd")
        (root / "Preset.fxp").write_bytes(b"preset")
        (root / "Artwork.png").write_bytes(b"PNG")
        (root / "Readme.txt").write_text("docs", encoding="utf-8")
        self.post_json("/api/workflow/select", {"workflow_id": "sample-pack"})
        state = self.post_json("/api/scan", {"root": str(root)})["state"]
        self.assertEqual(state["extension_enabled"], {
            ".fxp": False, ".mid": True, ".png": False, ".txt": False, ".wav": True,
        })
        statuses = {}
        for group in state["groups"]:
            selected = self.post_json("/api/select-group", {"key": group["key"]})["state"]
            statuses[group["extension"]] = selected["records"][0]["status"]
        self.assertEqual(statuses[".wav"], "Ready")
        self.assertEqual(statuses[".mid"], "Ready")
        self.assertEqual(statuses[".fxp"], "Skipped")
        self.assertEqual(statuses[".png"], "Skipped")
        self.assertEqual(statuses[".txt"], "Skipped")

    def test_image_workflow_suggests_orientation_and_safe_aspect_ratio(self):
        if "image-assets" not in web_app.STATE.workflow_catalog.all_ids():
            self.skipTest("未安装工作流：image-assets")
        root = Path(self.temp.name) / "ImageRoot"
        root.mkdir()
        (root / "Wide.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
            + (1920).to_bytes(4, "big") + (1080).to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00"
        )
        scanned = self.post_json("/api/scan", {"root": str(root)})
        selected = self.post_json("/api/workflow/select", {"workflow_id": "image-assets"})
        record = selected["state"]["records"][0]
        self.assertEqual(record["metadata"]["image"]["aspect_ratio"], "16:9")
        self.assertEqual(record["workflow_candidates"]["orientation"], ["横屏"])
        self.assertEqual(record["workflow_candidates"]["aspect_ratio"], ["16x9"])
        self.assertEqual(record["workflow_candidate_details"]["orientation"][0]["rule_id"], "image-landscape")
        self.assertEqual(record["workflow_derived"]["pixel_area"], 2073600.0)

        group_key = selected["state"]["current_group_key"]
        path = record["path"]
        state = self.post_json("/api/workflow-value", {
            "group_key": group_key, "field": "orientation", "value": "横屏", "path": path,
        })["state"]
        state = self.post_json("/api/workflow-value", {
            "group_key": group_key, "field": "aspect_ratio", "value": "16x9", "path": path,
        })["state"]
        final_record = state["records"][0]
        self.assertIn("横屏_16x9_Wide.png", final_record["target_name"])

    def test_image_workflow_snaps_complex_ratio_in_preview(self):
        if "image-assets" not in web_app.STATE.workflow_catalog.all_ids():
            self.skipTest("未安装工作流：image-assets")
        root = Path(self.temp.name) / "ComplexImageRoot"
        root.mkdir()
        image = root / "UltraWide.png"
        image.write_bytes(
            b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
            + (7672).to_bytes(4, "big") + (3264).to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00"
        )
        self.post_json("/api/scan", {"root": str(root)})
        selected = self.post_json("/api/workflow/select", {"workflow_id": "image-assets"})
        record = selected["state"]["records"][0]
        self.assertEqual(record["metadata"]["image"]["aspect_ratio_exact"], "959x408")
        self.assertEqual(record["workflow_candidates"]["aspect_ratio"], ["21x9"])

    def test_workflow_fill_applies_all_image_candidates_in_one_request(self):
        if "image-assets" not in web_app.STATE.workflow_catalog.all_ids():
            self.skipTest("未安装工作流：image-assets")
        root = Path(self.temp.name) / "ImageFillRoot"
        root.mkdir()
        (root / "Wide.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
            + (1920).to_bytes(4, "big") + (1080).to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00"
        )
        self.post_json("/api/scan", {"root": str(root)})
        selected = self.post_json("/api/workflow/select", {"workflow_id": "image-assets"})
        self.assertEqual(selected["state"]["records"][0]["workflow_values"]["orientation"], "")
        self.assertEqual(selected["state"]["records"][0]["workflow_values"]["aspect_ratio"], "")

        filled = self.post_json("/api/workflow-fill", {})

        record = filled["state"]["records"][0]
        self.assertEqual(filled["filled"], 2)
        self.assertEqual(record["workflow_values"]["orientation"], "横屏")
        self.assertEqual(record["workflow_values"]["aspect_ratio"], "16x9")
        self.assertEqual(record["target_name"], "横屏_16x9_Wide.png")

    def test_wallpaper_conflicts_get_ordered_suffix_after_date(self):
        root = Path(self.temp.name) / "WallpaperConflictRoot"
        root.mkdir()
        png_header = (
            b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
            + (1920).to_bytes(4, "big") + (1080).to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00"
        )
        for name in ("wallhaven-a.png", "wallhaven-b.png"):
            (root / name).write_bytes(png_header)
        self.post_json("/api/workflow/select", {"workflow_id": "wallpaper-assets"})
        scanned = self.post_json("/api/scan", {"root": str(root)})
        records = scanned["state"]["records"]

        self.assertEqual(len(records), 2)
        first_stem = Path(records[0]["target_name"]).stem
        second_stem = Path(records[1]["target_name"]).stem
        acquired_date = records[0]["workflow_values"]["acquired_date"]
        self.assertTrue(first_stem.endswith(f"_{acquired_date}"))
        self.assertEqual(second_stem, f"{first_stem}_01")
        self.assertEqual([record["status"] for record in records], ["Ready", "Ready"])

        Path(records[0]["path"]).with_name(records[0]["target_name"]).write_bytes(b"existing")
        previewed = self.post_json("/api/preview", {"group_key": scanned["state"]["current_group_key"]})["state"]["records"]
        first_preview_stem = Path(previewed[0]["target_name"]).stem
        self.assertTrue(first_preview_stem.endswith("_01"))
        self.assertEqual(Path(previewed[1]["target_name"]).stem, f"{first_preview_stem[:-3]}_02")
        self.assertEqual([record["status"] for record in previewed], ["Ready", "Ready"])

    def test_wallpaper_workflow_merges_image_extensions_by_directory(self):
        root = Path(self.temp.name) / "MixedImageRoot"
        root.mkdir()
        png_header = (
            b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
            + (1920).to_bytes(4, "big") + (1080).to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00"
        )
        (root / "wallhaven-png.png").write_bytes(png_header)
        (root / "wallhaven-jpg.jpg").write_bytes(png_header)
        (root / "ignore.txt").write_text("not an image", encoding="utf-8")
        self.post_json("/api/workflow/select", {"workflow_id": "wallpaper-assets"})

        scanned = self.post_json("/api/scan", {"root": str(root)})
        state = scanned["state"]
        self.assertEqual(state["total_file_count"], 2)
        self.assertEqual(len(state["groups"]), 1)
        group = state["groups"][0]
        self.assertEqual(group["extension"], ".image")
        self.assertEqual(group["extensions"], [".jpg", ".png"])
        self.assertEqual({record["extension"] for record in state["records"]}, {".jpg", ".png"})

        filled = self.post_json("/api/workflow-fill", {})
        targets = {record["extension"]: record["target_name"] for record in filled["state"]["records"]}
        self.assertTrue(targets[".jpg"].startswith("横屏_16x9_1920x1080_MixedImageRoot_wallhaven_"))
        self.assertTrue(targets[".jpg"].endswith(".jpg"))
        self.assertTrue(targets[".png"].endswith(".png"))
        renamed = self.post_json("/api/rename", {"scope": "group"})
        self.assertEqual(renamed["success"], 2)
        self.assertEqual(renamed["failed"], 0)
        self.assertEqual(len(list(root.glob("*.jpg"))), 1)
        self.assertEqual(len(list(root.glob("*.png"))), 1)

        restored = self.post_json("/api/workflow/select", {"workflow_id": "default"})["state"]
        self.assertEqual({group["extension"] for group in restored["groups"]}, {".jpg", ".png", ".txt"})
        self.assertEqual(restored["total_file_count"], 3)

    def test_wallpaper_source_quick_tag_writes_to_record(self):
        root = Path(self.temp.name) / "WallpaperSourceTagRoot"
        root.mkdir()
        png_header = (
            b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
            + (1920).to_bytes(4, "big") + (1080).to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00"
        )
        image = root / "untagged.png"
        image.write_bytes(png_header)
        selected = self.post_json("/api/workflow/select", {"workflow_id": "wallpaper-assets"})
        self.assertIn(
            {"label": "Pixiv", "value": "Pixiv"},
            next(field for field in selected["state"]["workflow"]["active"]["fields"] if field["id"] == "source")["quick_tags"],
        )
        scanned = self.post_json("/api/scan", {"root": str(root)})
        group_key = scanned["state"]["current_group_key"]
        path = scanned["state"]["records"][0]["path"]
        updated = self.post_json("/api/workflow-value", {
            "group_key": group_key,
            "field": "source",
            "value": "Pixiv",
            "path": path,
        })["state"]
        record = updated["records"][0]
        self.assertEqual(record["workflow_values"]["source"], "Pixiv")
        self.assertIn("_Pixiv_", record["target_name"])

    def test_workflow_export_and_import_as_copy(self):
        with urlopen(self.url + "/api/workflow-export?workflow_id=sample-pack") as response:
            self.assertEqual(response.headers.get_content_type(), "application/zip")
            package = response.read()
        restored = load_workflow_package(package, "sample-pack.ffnf-workflow")
        self.assertEqual(restored["id"], "sample-pack")
        boundary = "----WorkflowBoundary"
        body = b"".join([
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"strategy\"\r\n\r\ncopy\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"trust_modules\"\r\n\r\ntrue\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"sample-pack.ffnf-workflow\"\r\nContent-Type: application/zip\r\n\r\n".encode(),
            package,
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        request = Request(self.url + "/api/workflow/import", data=body, headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }, method="POST")
        with urlopen(request) as response:
            result = json.loads(response.read().decode("utf-8"))
        self.assertTrue(result["copied"])
        self.assertNotEqual(result["imported"]["id"], "sample-pack")


if __name__ == "__main__":
    unittest.main()
