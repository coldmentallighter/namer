"""Local server lifecycle and native-system integration."""

from __future__ import annotations

from collections.abc import Callable
import sys
from typing import Any

from server.launcher import Server
from server.state import StateManager


class SystemController:
    def __init__(self, state: StateManager, sync_catalog: Callable[[], dict[str, Any]],
                 pick_windows_folder: Callable[[], str]) -> None:
        self.state = state
        self.sync_catalog = sync_catalog
        self.pick_windows_folder = pick_windows_folder

    def heartbeat(self, server: Server) -> dict[str, Any]:
        server.client_heartbeat()
        with self.state.lock:
            self.sync_catalog()
            revision = self.state.workflow_catalog.revision
            workflow_id = self.state.workflow_id
        return {
            "ok": True,
            "workflow_revision": revision,
            "active_workflow_id": workflow_id,
        }

    @staticmethod
    def client_closed(server: Server) -> dict[str, Any]:
        server.client_closed()
        return {"ok": True}

    def pick_folder(self) -> dict[str, Any]:
        try:
            if sys.platform == "win32":
                selected = self.pick_windows_folder()
            else:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                selected = filedialog.askdirectory(title="选择根目录")
                root.destroy()
        except Exception as exc:
            raise RuntimeError(f"无法打开本机文件夹选择器: {exc}") from exc
        return {"ok": True, "path": selected or ""}
