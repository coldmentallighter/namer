from __future__ import annotations

import json
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote
from urllib.request import Request, urlopen

from openpyxl import Workbook

import web_app
from workflow_system.values import WorkflowValueStore


class WebApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name) / "Web 根目录"
        (cls.root / "Drums").mkdir(parents=True)
        (cls.root / "Drums" / "Kick 01.wav").write_bytes(b"RIFF")
        (cls.root / "Drums" / "Kick 02.png").write_bytes(b"PNG")
        cls.server, cls.url = web_app.run_server(0, False)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    def setUp(self):
        self.post("/api/workflow/select", {"workflow_id": "default"})

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp.cleanup()

    def post(self, path, payload):
        request = Request(self.url + path, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_index_and_scan_preview_api(self):
        with urlopen(self.url) as response:
            html = response.read().decode("utf-8")
        self.assertIn("File Namer", html)
        self.assertIn('id="audioSeek"', html)
        self.assertNotIn('id="stopAudio"', html)
        self.assertIn('id="redoButton"', html)
        self.assertIn('id="openTagManager"', html)
        self.assertIn('target="tag-manager"', html)
        self.assertNotIn('target="_blank"', html)
        self.assertIn('id="fileTable"', html)
        self.assertIn('id="fileTableShell"', html)
        self.assertIn('id="fileTableHeightResizer"', html)
        self.assertEqual(html.count('<col data-column-id='), 11)
        with urlopen(self.url + "/assets/app.js") as response:
            app_js = response.read().decode("utf-8")
        self.assertIn("setupFileTableColumnResizers", app_js)
        self.assertIn("offline-file-namer-file-table-column-widths-v1", app_js)
        self.assertIn("setupFileTableHeightResizer", app_js)
        self.assertIn("offline-file-namer-file-table-height-v1", app_js)
        with urlopen(self.url + "/assets/styles.css") as response:
            styles = response.read().decode("utf-8")
        self.assertIn(".column-resizer", styles)
        self.assertIn(".table-height-resizer", styles)
        self.assertIn("height: var(--file-table-height", styles)
        self.assertIn("table-layout: fixed", styles)
        self.assertIn(".file-name { width: 100%", styles)
        self.assertIn(".folder-cell { width: 100%", styles)
        self.assertIn(".parse-cell { width: 100%", styles)
        self.assertNotIn(".preview-cell { color: #244e43 !important; max-width", styles)
        with urlopen(self.url + "/tag-manager") as response:
            manager_html = response.read().decode("utf-8")
        self.assertIn("快捷标签管理", manager_html)
        self.assertIn('id="deleteTagModal"', manager_html)
        self.assertIn('target="main-app"', manager_html)
        self.assertNotIn("Beta 版", manager_html)
        self.assertNotIn("演示", manager_html)
        scanned = self.post("/api/scan", {"root": str(self.root)})
        self.assertTrue(scanned["ok"])
        self.assertEqual(scanned["state"]["total_file_count"], 2)
        self.assertEqual(scanned["state"]["groups"][0]["count"], 1)
        group_key = scanned["state"]["current_group_key"]
        self.post("/api/workflow-value", {"group_key": group_key, "field": "meta_prefix", "value": "[EDM]"})
        preview = self.post("/api/preview", {"group_key": group_key, "separator": "_", "mode": "numeric", "numeric": {"start": 1, "width": 2, "step": 1}, "extensions": [".wav", ".png"]})
        self.assertTrue(preview["ok"])
        self.assertTrue(preview["state"]["records"][0]["target_name"].endswith("_01.wav"))
        skipped = self.post("/api/preview", {"group_key": group_key, "extensions": [".png"]})
        self.assertFalse(skipped["state"]["records"][0]["selected"])
        self.assertEqual(skipped["state"]["records"][0]["status"], "Skipped")
        restored = self.post("/api/preview", {"group_key": group_key, "extensions": [".wav", ".png"]})
        self.assertTrue(restored["state"]["records"][0]["selected"])
        self.assertEqual(restored["state"]["records"][0]["status"], "Ready")
        self.post("/api/record", {"path": restored["state"]["records"][0]["path"], "selected": False})
        self.post("/api/preview", {"group_key": group_key, "extensions": [".png"]})
        manually_skipped = self.post("/api/preview", {"group_key": group_key, "extensions": [".wav", ".png"]})
        self.assertFalse(manually_skipped["state"]["records"][0]["selected"])

    def test_workflow_values_api_reads_writes_and_toggles(self):
        old_store = web_app.WORKFLOW_VALUE_STORE
        with tempfile.TemporaryDirectory() as temp_dir:
            web_app.WORKFLOW_VALUE_STORE = WorkflowValueStore(Path(temp_dir))
            try:
                with urlopen(self.url + "/api/workflow-values?workflow_id=sample-pack") as response:
                    initial = json.loads(response.read().decode("utf-8"))
                self.assertTrue(initial["ok"])
                self.assertFalse(initial["data"]["exists"])

                added = self.post("/api/workflow-values/tag", {
                    "workflow_id": "sample-pack",
                    "field_id": "author_code",
                    "tag": {"label": "API Test", "value": "api-test", "aliases": ["api"]},
                })
                self.assertTrue(added["ok"])
                added_tag = next(tag for tag in added["data"]["tags"]["author_code"] if tag["value"] == "api-test")
                self.assertTrue(added["data"]["exists"])

                self.post("/api/workflow/select", {"workflow_id": "sample-pack"})
                with urlopen(self.url + "/api/state") as response:
                    active_state = json.loads(response.read().decode("utf-8"))["state"]
                author_code = next(field for field in active_state["workflow"]["active"]["fields"] if field["id"] == "author_code")
                self.assertIn({"label": "API Test", "value": "api-test"}, author_code["quick_tags"])

                toggled = self.post("/api/workflow-values/tag", {
                    "workflow_id": "sample-pack",
                    "field_id": "author_code",
                    "action": "toggle",
                    "tag_id": added_tag["id"],
                })
                toggled_tag = next(tag for tag in toggled["data"]["tags"]["author_code"] if tag["id"] == added_tag["id"])
                self.assertFalse(toggled_tag["enabled"])

                deleted = self.post("/api/workflow-values/tag", {
                    "workflow_id": "sample-pack",
                    "field_id": "author_code",
                    "action": "delete",
                    "tag_id": added_tag["id"],
                })
                self.assertNotIn(added_tag["id"], [tag["id"] for tag in deleted["data"]["tags"]["author_code"]])

                with urlopen(self.url + "/api/state") as response:
                    active_state = json.loads(response.read().decode("utf-8"))["state"]
                author_code = next(field for field in active_state["workflow"]["active"]["fields"] if field["id"] == "author_code")
                self.assertNotIn("api-test", [tag["value"] for tag in author_code["quick_tags"]])
            finally:
                web_app.WORKFLOW_VALUE_STORE = old_store

    def test_directory_mapping_and_parse_preview_api(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root) / "Root" / "Pack" / "Loops"
            root.mkdir(parents=True)
            (root / "Loop_Drum_03_150BPM.wav").write_bytes(b"RIFF")
            scanned = self.post("/api/scan", {"root": str(Path(temp_root) / "Root")})
            mapped = self.post("/api/directory-mapping", {"mapping": {"meta": 1, "group": 2, "child": -1}})
            group = mapped["state"]["groups"][0]
            self.assertEqual(group["workflow_values"]["group_prefix"], "Loops")
            self.assertEqual(mapped["state"]["records"][0]["workflow_values"]["child_prefix"], "Loops")
            selected = self.post("/api/workflow/select", {"workflow_id": "sample-pack"})
            group = selected["state"]["groups"][0]
            parsed = self.post("/api/parse-preview", {"group_key": group["key"], "template": "{type}_{name}_{number}_{bpm}", "use_name": True})
            self.assertEqual(parsed["parsed"][0]["fields"]["bpm"], "150")
            self.assertIn("Drum", parsed["state"]["records"][0]["workflow_values"]["name"])

    def test_profile_places_middle_bpm_without_suffix_action(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root) / "BpmRoot" / "Loops"
            root.mkdir(parents=True)
            filename = "BT_Atmos_130_Eb_Solace_FA.wav"
            (root / filename).write_bytes(b"RIFF")
            self.post("/api/workflow/select", {"workflow_id": "sample-pack"})
            self.post("/api/scan", {"root": str(Path(temp_root) / "BpmRoot")})
            filled = self.post("/api/workflow-fill", {})
            record = filled["state"]["records"][0]
            self.assertEqual(record["workflow_values"]["bpm"], "130")
            self.assertEqual(record["workflow_values"]["key_or_chord"], "Eb")
            self.assertEqual(record["workflow_actions"], [])
            self.assertEqual(record["target_name"], filename)

    def test_associated_cross_format_files_rename_together(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root) / "AssociationRoot" / "Pairs"
            root.mkdir(parents=True)
            wav = root / "Chord_Vibe_135BPM.wav"
            midi = root / "Chord_Vibe_135BPM.mid"
            wav.write_bytes(b"RIFF")
            midi.write_bytes(b"MThd")
            old_history = web_app.STATE.history_path
            web_app.STATE.history_path = Path(temp_root) / "history.json"
            try:
                scanned = self.post("/api/scan", {"root": str(root.parent)})
                wav_group = next(group for group in scanned["state"]["groups"] if group["extension"] == ".wav")
                selected = self.post("/api/select-group", {"key": wav_group["key"]})
                wav_record = selected["state"]["records"][0]
                for field in ("meta_prefix", "group_prefix"):
                    self.post("/api/workflow-value", {"group_key": wav_group["key"], "field": field, "value": ""})
                self.post("/api/workflow-value", {
                    "group_key": wav_group["key"], "path": wav_record["path"],
                    "field": "child_prefix", "value": "",
                })
                self.post("/api/workflow-value", {
                    "group_key": wav_group["key"], "path": wav_record["path"],
                    "field": "name", "value": "Renamed_Chord",
                })
                self.post("/api/preview", {
                    "group_key": wav_group["key"], "mode": "original", "extensions": [".wav", ".mid"],
                })
                renamed = self.post("/api/rename", {"scope": "single", "path": str(wav)})
                self.assertEqual(renamed["success"], 2)
                self.assertTrue((root / "Renamed_Chord.wav").exists())
                self.assertTrue((root / "Renamed_Chord.mid").exists())
                self.assertEqual(len(renamed["state"]["associations"]), 1)
                undone = self.post("/api/undo", {})
                self.assertTrue(undone["ok"], undone.get("errors"))
                self.assertTrue(wav.exists())
                self.assertTrue(midi.exists())
                self.assertEqual(undone["state"]["records"][0]["original_name"], wav.name)
                self.assertEqual(undone["state"]["records"][0]["path"], str(wav))
                self.assertEqual(undone["state"]["records"][0]["status"], "Ready")
            finally:
                web_app.STATE.history_path = old_history

    def test_name_mode_returns_to_base_name_and_association_leader_is_stable(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root) / "ModeRoot" / "Pairs"
            root.mkdir(parents=True)
            (root / "ASD.wav").write_bytes(b"RIFF")
            (root / "Linked.wav").write_bytes(b"RIFF2")
            (root / "Linked.mid").write_bytes(b"MThd")
            scanned = self.post("/api/scan", {"root": str(root.parent)})
            wav_group = next(group for group in scanned["state"]["groups"] if group["extension"] == ".wav")
            mid_group = next(group for group in scanned["state"]["groups"] if group["extension"] == ".mid")

            for group in (wav_group, mid_group):
                for field in ("meta_prefix", "group_prefix"):
                    self.post("/api/workflow-value", {"group_key": group["key"], "field": field, "value": ""})

            numeric = self.post("/api/preview", {
                "group_key": wav_group["key"], "mode": "numeric", "numeric": {"start": 1, "width": 2, "step": 1},
                "extensions": [".wav", ".mid"],
            })
            asd_numeric = next(record for record in numeric["state"]["records"] if record["original_name"] == "ASD.wav")
            self.assertEqual(asd_numeric["workflow_values"]["name"], "01")
            original = self.post("/api/preview", {
                "group_key": wav_group["key"], "mode": "original", "extensions": [".wav", ".mid"],
            })
            asd_original = next(record for record in original["state"]["records"] if record["original_name"] == "ASD.wav")
            self.assertEqual(asd_original["workflow_values"]["name"], "ASD")

            linked = next(record for record in original["state"]["records"] if record["original_name"] == "Linked.wav")
            self.post("/api/workflow-value", {
                "group_key": wav_group["key"], "path": linked["path"], "field": "name", "value": "Stable_Link",
            })
            self.post("/api/preview", {
                "group_key": wav_group["key"], "mode": "original", "extensions": [".wav", ".mid"],
            })
            sibling = self.post("/api/preview", {
                "group_key": mid_group["key"], "mode": "original", "extensions": [".wav", ".mid"],
            })
            self.assertEqual(sibling["state"]["records"][0]["target_name"], "Stable_Link.mid")

    def test_export_scan_does_not_replace_rename_task(self):
        scanned = self.post("/api/scan", {"root": str(self.root)})
        record = scanned["state"]["records"][0]
        self.post("/api/workflow-value", {
            "group_key": scanned["state"]["current_group_key"], "path": record["path"],
            "field": "name", "value": "KeepThisEdit",
        })
        export_scan = self.post("/api/export-scan", {"root": str(self.root)})
        self.assertEqual(export_scan["total_file_count"], 2)
        with urlopen(self.url + "/api/state") as response:
            state = json.loads(response.read().decode("utf-8"))["state"]
        self.assertEqual(state["records"][0]["workflow_values"]["name"], "KeepThisEdit")

    def test_export_api_returns_statistics(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root) / "ExportRoot" / "Loops"
            root.mkdir(parents=True)
            (root / "Kick 01.wav").write_bytes(b"RIFF")
            payload = self.post("/api/export", {"root": str(root.parent), "extensions": [".wav"]})
            self.assertNotIn("export_mode", payload)
            self.assertGreaterEqual(payload["export_stats"]["file_count"], 1)
            self.assertIn(str(root.parent / "Loops.ffnf.xlsx"), payload["xlsx_outputs"])

    def test_history_path_is_relative_to_source_directory(self):
        expected = Path(web_app.__file__).resolve().parent / "history" / "history.json"
        self.assertEqual(web_app.STATE.history_path, expected)

    def test_windows_folder_picker_uses_native_provider(self):
        class DummyHandler:
            def __init__(self):
                self.wfile = io.BytesIO()

            def send_response(self, status):
                self.status = status

            def send_header(self, name, value):
                pass

            def end_headers(self):
                pass

        handler = DummyHandler()
        with patch.object(web_app.sys, "platform", "win32"), patch.object(
            web_app, "_pick_windows_folder", return_value=r"C:\Samples"
        ) as picker:
            web_app.Handler._pick_folder(handler)
        picker.assert_called_once_with()
        self.assertEqual(json.loads(handler.wfile.getvalue().decode("utf-8"))["path"], r"C:\Samples")

    def test_execute_all_groups_creates_one_undo_operation(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root) / "BatchRoot"
            (root / "WAV").mkdir(parents=True)
            (root / "MID").mkdir()
            wav = root / "WAV" / "Kick.wav"
            mid = root / "MID" / "Pattern.mid"
            wav.write_bytes(b"RIFF")
            mid.write_bytes(b"MThd")
            old_history = web_app.STATE.history_path
            web_app.STATE.history_path = Path(temp_root) / "history.json"
            try:
                scanned = self.post("/api/scan", {"root": str(root)})
                for group in scanned["state"]["groups"]:
                    self.post("/api/workflow-value", {
                        "group_key": group["key"], "field": "meta_prefix", "value": "Pack",
                    })
                preview = self.post("/api/preview", {
                    "group_key": scanned["state"]["current_group_key"],
                    "separator": "_",
                    "mode": "original",
                    "extensions": [".wav", ".mid"],
                })
                self.assertTrue(preview["ok"])
                renamed = self.post("/api/rename", {"scope": "all"})
                self.assertEqual(renamed["success"], 2)
                history = json.loads(web_app.STATE.history_path.read_text(encoding="utf-8"))
                self.assertEqual(len(history), 1)
                self.assertEqual(len(history[0]["items"]), 2)
                undone = self.post("/api/undo", {})
                self.assertTrue(undone["ok"], undone.get("errors"))
                self.assertTrue(wav.exists())
                self.assertTrue(mid.exists())
                redone = self.post("/api/redo", {})
                self.assertTrue(redone["ok"], redone.get("errors"))
                self.assertFalse(wav.exists())
                self.assertFalse(mid.exists())
                self.assertTrue((root / "WAV" / "Pack_WAV_Kick.wav").exists())
                self.assertTrue((root / "MID" / "Pack_MID_Pattern.mid").exists())
                undone_again = self.post("/api/undo", {})
                self.assertTrue(undone_again["ok"], undone_again.get("errors"))
                self.assertTrue(wav.exists())
                self.assertTrue(mid.exists())
                log_messages = [entry["message"] for entry in undone_again["state"]["logs"]]
                self.assertTrue(any("已撤销" in message and "WAV" in message and "Kick.wav" in message for message in log_messages))
                self.assertTrue(any("已还原" in message and "MID" in message and "Pattern.mid" in message for message in log_messages))
            finally:
                web_app.STATE.history_path = old_history

    def test_multipart_excel_import_without_cgi(self):
        scanned = self.post("/api/scan", {"root": str(self.root)})
        group_key = scanned["state"]["current_group_key"]
        workbook_path = self.root / "mapping.xlsx"
        workbook = Workbook()
        workbook.active.append(["Kick 01.wav", "Renamed"])
        workbook.save(workbook_path)
        boundary = "----OfflineNamerBoundary"
        file_data = workbook_path.read_bytes()
        body = b"".join([
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"group_key\"\r\n\r\n{group_key}\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"mapping.xlsx\"\r\nContent-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n".encode(),
            file_data,
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        request = Request(self.url + "/api/import-excel", data=body,
                          headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
        with urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["match"]["matched"], 1)

    def test_audio_endpoint_supports_byte_ranges(self):
        scanned = self.post("/api/scan", {"root": str(self.root)})
        audio_path = scanned["state"]["records"][0]["path"]
        request = Request(self.url + "/audio?path=" + quote(audio_path), headers={"Range": "bytes=1-2"})
        with urlopen(request) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Accept-Ranges"], "bytes")
            self.assertEqual(response.headers["Content-Range"], "bytes 1-2/4")
            self.assertEqual(response.read(), b"IF")


if __name__ == "__main__":
    unittest.main()
