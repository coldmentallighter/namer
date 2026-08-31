"""Compatibility imports for callers of the pre-plugin module paths."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def load_legacy_module(workflow_id: str, module_id: str) -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "workflows" / workflow_id / "modules" / f"{module_id}.py"
    spec = importlib.util.spec_from_file_location(f"_ffnf_legacy_{workflow_id}_{module_id}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载工作流模块: {workflow_id}.{module_id}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
