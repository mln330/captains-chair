from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _entry_module() -> ModuleType:
    path = Path(__file__).parents[1] / "packaging" / "sidecar_entry.py"
    spec = importlib.util.spec_from_file_location("make_it_so_packaged_entry", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_packaged_entry_routes_worker_commands_to_cli() -> None:
    module = _entry_module()
    assert module._uses_cli(["--config", "/tmp/config.yaml", "worker-protocol", "--role", "coder"])


def test_packaged_entry_keeps_sidecar_and_schedules_on_sidecar_runtime() -> None:
    module = _entry_module()
    assert not module._uses_cli(["--config", "/tmp/config.yaml"])
    assert not module._uses_cli(
        ["--once", "reconcile", "--background", "--config", "/tmp/config.yaml"]
    )
