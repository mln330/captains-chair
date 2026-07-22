"""Capability-driven QA selection without assuming an application category."""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import PurePosixPath

from make_it_so.models import (
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


BUILTIN_QA_PROFILES: dict[ApplicationSurface, QAProfile] = {
    ApplicationSurface.WEB_UI: QAProfile(
        key="web-ui-qa",
        title="UI acceptance: form, function, and finish",
        surfaces=frozenset({ApplicationSurface.WEB_UI}),
        required_tools=("browser",),
        reviewer_role="ui_qa_reviewer",
    ),
    ApplicationSurface.CLI: QAProfile(
        key="cli-qa",
        title="Command-line behavior and operator-flow QA",
        surfaces=frozenset({ApplicationSurface.CLI}),
        reviewer_role="qa_assistant",
    ),
    ApplicationSurface.API: QAProfile(
        key="api-qa",
        title="API contract and failure-mode QA",
        surfaces=frozenset({ApplicationSurface.API}),
        reviewer_role="qa_assistant",
    ),
    ApplicationSurface.LIBRARY: QAProfile(
        key="library-qa",
        title="Library compatibility and consumer-contract QA",
        surfaces=frozenset({ApplicationSurface.LIBRARY}),
        reviewer_role="qa_assistant",
    ),
    ApplicationSurface.DATA_PIPELINE: QAProfile(
        key="data-pipeline-qa",
        title="Data pipeline integrity and replay QA",
        surfaces=frozenset({ApplicationSurface.DATA_PIPELINE}),
        reviewer_role="qa_assistant",
    ),
    ApplicationSurface.INFRASTRUCTURE_RELEASE: QAProfile(
        key="infrastructure-release-qa",
        title="Infrastructure and release safety QA",
        surfaces=frozenset({ApplicationSurface.INFRASTRUCTURE_RELEASE}),
        reviewer_role="qa_assistant",
    ),
    ApplicationSurface.CUSTOM: QAProfile(
        key="custom-qa",
        title="Repository-specific acceptance QA",
        surfaces=frozenset({ApplicationSurface.CUSTOM}),
        reviewer_role="qa_assistant",
    ),
}


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
        if any(part.lower() in {"api", "controllers", "endpoints"} for part in path.parts[:-1]):
            surfaces.add(ApplicationSurface.API)
        if (
            name in {"dockerfile", "docker-compose.yml", "docker-compose.yaml"}
            or suffix in {".tf", ".bicep"}
            or any(part.lower() in {"terraform", "infra", "k8s", "helm", "workflows"} for part in path.parts[:-1])
        ):
            surfaces.add(ApplicationSurface.INFRASTRUCTURE_RELEASE)
        if (
            suffix in {".sql", ".dbt", ".parquet"}
            or name.startswith(("dag_", "pipeline_"))
            or any(part.lower() in {"data", "etl", "pipelines", "migrations", "warehouse"} for part in path.parts[:-1])
        ):
            surfaces.add(ApplicationSurface.DATA_PIPELINE)
        if suffix in {".py", ".js", ".ts", ".go", ".rs", ".java", ".cs", ".rb", ".php"}:
            surfaces.add(ApplicationSurface.LIBRARY)
        if (
            name in {"makefile", "justfile", "cli.py", "cli.ts", "command.py", "commands.py"}
            or any(part.lower() in {"bin", "scripts", "cli", "commands"} for part in path.parts[:-1])
        ):
            surfaces.add(ApplicationSurface.CLI)
    return frozenset(surfaces or {ApplicationSurface.CUSTOM})


def select_qa(
    repo: RepoConfig,
    changed_paths: list[str] | tuple[str, ...],
    manifest: ProjectManifest | None = None,
    actual_changed_paths: list[str] | tuple[str, ...] = (),
) -> QASelection:
    all_changed_paths = tuple(dict.fromkeys((*changed_paths, *actual_changed_paths)))
    declared_surfaces = repo.surfaces or (manifest.surfaces if manifest else frozenset())
    detected_surfaces: frozenset[ApplicationSurface] = (
        infer_surfaces(all_changed_paths) if all_changed_paths else frozenset()
    )
    surfaces = declared_surfaces.union(detected_surfaces) or frozenset({ApplicationSurface.CUSTOM})
    configured = repo.qa_profiles or (manifest.qa_profiles if manifest else ())
    selected: list[QAProfile] = list(
        profile
        for profile in configured
        if profile.enabled and (
            not profile.surfaces
            or bool(profile.surfaces.intersection(surfaces))
            or ApplicationSurface.CUSTOM in profile.surfaces
        )
    )
    covered = frozenset(
        surface
        for profile in selected
        for surface in profile.surfaces
        if surface != ApplicationSurface.CUSTOM
    )
    for surface in sorted(surfaces - covered, key=lambda item: item.value):
        profile = BUILTIN_QA_PROFILES[surface]
        selected.append(profile)

    global_checks = tuple(dict.fromkeys((*((manifest.checks if manifest else ())), *repo.checks)))
    configured_checks = {check for profile in selected for check in profile.checks}
    remaining_checks = tuple(check for check in global_checks if check not in configured_checks)
    if remaining_checks:
        selected.append(
            QAProfile(
                key="deterministic-checks",
                title="Deterministic repository checks",
                checks=remaining_checks,
                reviewer_role="qa_assistant",
            )
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
        profiles=tuple(selected),
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


__all__ = [
    "BUILTIN_QA_PROFILES",
    "QASelection",
    "infer_surfaces",
    "paths_match_surface",
    "select_qa",
]
