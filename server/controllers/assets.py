"""Static assets, read-only APIs, downloads, and media responses."""

from __future__ import annotations

from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
import mimetypes
from pathlib import Path
from typing import Any

from server.state import StateManager
from server.transport import send_attachment, send_bytes, send_file_range, send_json
from workflow_system.catalog import workflow_summary
from workflow_system.values import WorkflowValueStore


class AssetController:
    def __init__(self, state: StateManager, web_root: Path,
                 value_store: Callable[[], WorkflowValueStore],
                 **services: Callable[..., Any]) -> None:
        self.state = state
        self.web_root = web_root
        self._value_store = value_store
        self.s = services

    def serve(self, handler: BaseHTTPRequestHandler, path: str,
              query: dict[str, list[str]]) -> None:
        if path == "/":
            self._asset(handler, "index.html", "text/html; charset=utf-8")
        elif path == "/tag-manager":
            self._asset(handler, "tag-manager.html", "text/html; charset=utf-8")
        elif path == "/workflow-manager":
            self._asset(handler, "workflow-manager.html", "text/html; charset=utf-8")
        elif path.startswith("/assets/"):
            name = path.removeprefix("/assets/")
            content_type = mimetypes.guess_type(name)[0] or "text/plain"
            if content_type.startswith("text/"):
                content_type = f"{content_type}; charset=utf-8"
            self._asset(handler, name, content_type)
        elif path == "/api/state":
            send_json(handler, {"ok": True, "state": self.s["state_json"]()})
        elif path in {"/api/workflows", "/api/workflow"}:
            self._workflows(handler, query)
        elif path == "/api/workflow-values":
            workflow_id = query.get("workflow_id", query.get("id", [self.state.workflow_id]))[0]
            workflow = self.state.workflow_catalog.get(workflow_id)
            send_json(handler, {"ok": True, "data": self._value_store().read(workflow)})
        elif path == "/api/config":
            send_json(handler, {"ok": True, "config": {
                "theme": self.state.workflow_catalog.theme,
                "current_workflow": self.state.workflow_id,
            }})
        elif path in {"/api/workflow-export", "/api/workflow/export"}:
            workflow_id = query.get("id", query.get("workflow_id", [self.state.workflow_id]))[0]
            workflow = self.state.workflow_catalog.get(workflow_id)
            send_attachment(
                handler,
                self.state.workflow_catalog.package(workflow_id),
                f"{workflow['id']}.ffnf-workflow",
                "application/zip",
            )
        elif path == "/audio":
            self._audio(handler, query)
        else:
            send_json(handler, {"ok": False, "error": "Not found"}, 404)

    def _workflows(self, handler: BaseHTTPRequestHandler,
                   query: dict[str, list[str]]) -> None:
        requested_id = query.get("workflow_id", query.get("id", [self.state.workflow_id]))[0]
        if requested_id == self.state.workflow_id:
            info = self.s["workflow_state"]()
        else:
            selected = self.state.workflow_catalog.get(requested_id)
            info = {
                "active_id": selected["id"],
                "active": selected,
                "available": [
                    workflow_summary(item) for item in self.state.workflow_catalog.all()
                ],
                "values": {},
                "revision": self.state.workflow_catalog.revision,
                "load_errors": self.state.workflow_catalog.diagnostics(),
            }
        send_json(handler, {
            "ok": True,
            "workflows": info["available"],
            "active": info["active"],
            "active_id": info["active_id"],
            "workflow": info,
        })

    def _asset(self, handler: BaseHTTPRequestHandler, name: str,
               content_type: str) -> None:
        try:
            asset = (self.web_root / name).resolve()
            if not asset.is_relative_to(self.web_root.resolve()) or not asset.is_file():
                raise FileNotFoundError(name)
            send_bytes(handler, asset.read_bytes(), content_type)
        except OSError:
            send_json(handler, {"ok": False, "error": "资源不存在"}, 404)

    def _audio(self, handler: BaseHTTPRequestHandler,
               query: dict[str, list[str]]) -> None:
        path = self.s["safe_audio_path"](query.get("path", [""])[0])
        if not path:
            send_json(handler, {"ok": False, "error": "音频路径无效"}, 404)
            return
        try:
            send_file_range(handler, path, self.s["audio_content_type"](path))
        except OSError as exc:
            send_json(handler, {"ok": False, "error": str(exc)}, 404)
