from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import yaml

from make_it_so.models import AppConfig, ProjectManifest


def _read_yaml(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"expected YAML object in {path}")
    return cast(dict[str, Any], raw)


def load_config(path: Path) -> AppConfig:
    return AppConfig.model_validate(_read_yaml(path))


def load_project_manifest(repo_path: Path, relative_path: str) -> ProjectManifest | None:
    path = repo_path / relative_path
    if not path.is_file():
        return None
    return ProjectManifest.model_validate(_read_yaml(path))


def write_json_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(AppConfig.model_json_schema(), indent=2) + "\n", encoding="utf-8")
