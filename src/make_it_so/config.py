from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import yaml

from make_it_so.models import AppConfig, ProjectManifest

# Older OpenClaw releases serialized worker settings at the application root.
# Keep the current schema strict, but migrate that known shape before validation
# so a sidecar restart cannot discard a previously registered repository.
_LEGACY_OPENCLAW_ORCHESTRATOR_FIELDS = frozenset(
    {
        "board_prefix",
        "workers",
        "worker_models",
        "max_runtime_seconds",
        "max_retries",
        "require_live_completion_validation",
        "merge_execution",
        "executable",
        "worker_runtimes",
        "codex_executable",
        "make_it_so_command",
        "auth_source_agent",
        "dispatch_timeout_seconds",
        "session_limit",
        "max_concurrent_subagents",
        "dispatch_strategy",
    }
)


def _migrate_legacy_openclaw_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Move legacy root-level OpenClaw settings into their typed orchestrator."""
    legacy = {
        key: raw[key]
        for key in _LEGACY_OPENCLAW_ORCHESTRATOR_FIELDS
        if key in raw
    }
    if not legacy:
        return raw

    migrated = dict(raw)
    for key in legacy:
        migrated.pop(key, None)

    orchestrators = dict(migrated.get("orchestrators") or {})
    candidates = [
        name
        for name, value in orchestrators.items()
        if isinstance(value, dict) and value.get("kind") == "openclaw_workboard"
    ]
    if len(candidates) != 1:
        raise ValueError(
            "legacy root-level OpenClaw settings require exactly one "
            "openclaw_workboard orchestrator"
        )

    name = candidates[0]
    orchestrator = dict(orchestrators[name])
    for key, value in legacy.items():
        # The nested, current location wins if a deployment already contains
        # both shapes during a rolling upgrade.
        orchestrator.setdefault(key, value)
    orchestrators[name] = orchestrator
    migrated["orchestrators"] = orchestrators
    return migrated


def _read_yaml(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"expected YAML object in {path}")
    return cast(dict[str, Any], raw)


def load_config(path: Path) -> AppConfig:
    return AppConfig.model_validate(_migrate_legacy_openclaw_fields(_read_yaml(path)))


def load_project_manifest(repo_path: Path, relative_path: str) -> ProjectManifest | None:
    path = repo_path / relative_path
    if not path.is_file():
        return None
    return ProjectManifest.model_validate(_read_yaml(path))


def write_json_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(AppConfig.model_json_schema(), indent=2) + "\n", encoding="utf-8")
