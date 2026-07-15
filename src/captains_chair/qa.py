"""Capability-driven QA selection without assuming an application category."""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import PurePosixPath

from captains_chair.models import (
    ApplicationSurface,
    ProjectManifest,
    QAProfile,
    RepoConfig,
    StrictModel,
)


class QASelection(StrictModel):
    version: int = 1
    repository: str
    surfaces: frozenset[ApplicationSurface]
    profiles: tuple[QAProfile, ...]
    worker_roles: dict[str, str]


def infer_surfaces(paths: list[str] | tuple[str, ...]) -> frozenset[ApplicationSurface]:
    surfaces: set[ApplicationSurface] = set()
    for raw_path in paths:
        path = PurePosixPath(raw_path.replace("\\", "/"))
        name = path.name.lower()
        suffix = path.suffix.lower()
        if suffix in {".tsx", ".jsx", ".css", ".scss", ".html", ".vue", ".svelte"}:
            surfaces.add(ApplicationSurface.WEB_UI)
        if name in {"openapi.yaml", "openapi.yml", "swagger.yaml", "swagger.yml"} or "routes" in name:
            surfaces.add(ApplicationSurface.API)
        if name in {"dockerfile", "docker-compose.yml", "docker-compose.yaml"} or suffix in {".tf", ".bicep"}:
            surfaces.add(ApplicationSurface.INFRASTRUCTURE_RELEASE)
        if suffix in {".py", ".js", ".ts", ".go", ".rs", ".java", ".cs", ".rb", ".php"}:
            surfaces.add(ApplicationSurface.LIBRARY)
        if (
            name in {"makefile", "justfile", "cli.py", "cli.ts", "command.py", "commands.py"}
            or any(part.lower() in {"bin", "scripts", "cli", "commands"} for part in path.parts[:-1])
        ):
            surfaces.add(ApplicationSurface.CLI)
    return frozenset(surfaces or {ApplicationSurface.CUSTOM})


def select_qa(repo: RepoConfig, changed_paths: list[str] | tuple[str, ...], manifest: ProjectManifest | None = None) -> QASelection:
    surfaces = repo.surfaces or (manifest.surfaces if manifest and manifest.surfaces else infer_surfaces(changed_paths))
    configured = repo.qa_profiles or (manifest.qa_profiles if manifest else ())
    selected = tuple(
        profile
        for profile in configured
        if profile.enabled and (
            not profile.surfaces
            or bool(profile.surfaces.intersection(surfaces))
            or ApplicationSurface.CUSTOM in profile.surfaces
        )
    )
    if not selected:
        selected = (
            QAProfile(
                key="default-capability-qa",
                title="Capability-selected repository QA",
                surfaces=surfaces,
                checks=repo.checks,
                reviewer_role="ui_qa_reviewer"
                if ApplicationSurface.WEB_UI in surfaces and repo.ux_enabled
                else "qa_assistant",
            ),
        )
    worker_roles = {
        profile.key: (
            "ux_reviewer"
            if profile.reviewer_role in {"ui_qa_reviewer", "ux_reviewer"}
            or ApplicationSurface.WEB_UI in profile.surfaces
            else "tester"
        )
        for profile in selected
    }
    return QASelection(
        repository=repo.full_name,
        surfaces=surfaces,
        profiles=selected,
        worker_roles=worker_roles,
    )


def paths_match_surface(path: str, profile: QAProfile) -> bool:
    """Keep custom profiles useful for targeted package selection."""
    normalized = path.replace("\\", "/")
    return not profile.surfaces or any(
        surface == ApplicationSurface.WEB_UI and any(
            fnmatch(normalized, pattern) for pattern in ("*.tsx", "*.jsx", "*.css", "*.html")
        )
        for surface in profile.surfaces
    )


__all__ = ["QASelection", "infer_surfaces", "paths_match_surface", "select_qa"]
