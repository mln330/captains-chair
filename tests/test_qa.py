from pathlib import Path

import pytest

from captains_chair.models import ApplicationSurface, ProjectManifest, QAProfile
from captains_chair.qa import infer_surfaces, paths_match_surface, select_qa
from tests.helpers import repo_config


@pytest.mark.parametrize(
    ("path", "surface", "profile"),
    (
        ("frontend/App.tsx", ApplicationSurface.WEB_UI, "web-ui-qa"),
        ("src/cli.py", ApplicationSurface.CLI, "cli-qa"),
        ("api/openapi.yaml", ApplicationSurface.API, "api-qa"),
        ("src/client.py", ApplicationSurface.LIBRARY, "library-qa"),
        ("pipelines/orders.sql", ApplicationSurface.DATA_PIPELINE, "data-pipeline-qa"),
        ("infra/main.tf", ApplicationSurface.INFRASTRUCTURE_RELEASE, "infrastructure-release-qa"),
        ("README.md", ApplicationSurface.CUSTOM, "custom-qa"),
    ),
)
def test_every_supported_surface_selects_a_first_class_profile(
    tmp_path: Path,
    path: str,
    surface: ApplicationSurface,
    profile: str,
) -> None:
    selection = select_qa(repo_config(tmp_path), [path])

    assert surface in selection.surfaces
    assert profile in {item.key for item in selection.profiles}


def test_surface_inference_is_capability_based() -> None:
    surfaces = infer_surfaces(["src/cli.py", "frontend/Search.tsx", "infra/main.tf"])
    assert ApplicationSurface.CLI in surfaces
    assert ApplicationSurface.WEB_UI in surfaces
    assert ApplicationSurface.INFRASTRUCTURE_RELEASE in surfaces

    assert infer_surfaces(["README.md"]) == frozenset({ApplicationSurface.CUSTOM})


def test_explicit_manifest_profiles_override_detection(tmp_path: Path) -> None:
    repo = repo_config(tmp_path).model_copy(update={"checks": ("pytest",)})
    profile = QAProfile(
        key="cli-qa",
        title="CLI behavior",
        surfaces=frozenset({ApplicationSurface.CLI}),
        checks=("python -m pytest tests/cli",),
    )
    manifest = ProjectManifest(
        version=1,
        goal="Make the command line workflow reliable for operators.",
        canonical_docs=("README.md",),
        planning_doc="PLAN.md",
        checks=("pytest",),
        surfaces=frozenset({ApplicationSurface.CLI}),
        qa_profiles=(profile,),
    )
    selection = select_qa(repo, ["frontend/App.tsx"], manifest)
    assert selection.surfaces == frozenset({ApplicationSurface.CLI, ApplicationSurface.WEB_UI})
    assert [item.key for item in selection.profiles] == [
        "cli-qa",
        "web-ui-qa",
        "deterministic-checks",
    ]
    assert selection.worker_roles == {
        "cli-qa": "tester",
        "web-ui-qa": "ux_reviewer",
        "deterministic-checks": "tester",
    }


def test_web_ui_selection_uses_dedicated_ux_worker(tmp_path: Path) -> None:
    repo = repo_config(tmp_path).model_copy(
        update={"surfaces": frozenset({ApplicationSurface.WEB_UI}), "ux_enabled": True}
    )
    selection = select_qa(repo, [])
    assert selection.worker_roles[selection.profiles[0].key] == "ux_reviewer"


def test_surface_inference_covers_api_library_infrastructure_and_cli_markers() -> None:
    surfaces = infer_surfaces(
        [
            "api/openapi.yml",
            "Dockerfile",
            "scripts/runner.go",
            "src/commands.py",
            "infra/main.bicep",
        ]
    )
    assert surfaces == frozenset(
        {
            ApplicationSurface.API,
            ApplicationSurface.INFRASTRUCTURE_RELEASE,
            ApplicationSurface.LIBRARY,
            ApplicationSurface.CLI,
        }
    )


def test_qa_profile_selection_and_path_matching_are_capability_scoped(tmp_path: Path) -> None:
    repo = repo_config(tmp_path).model_copy(
        update={
            "surfaces": frozenset({ApplicationSurface.CLI}),
            "qa_profiles": (
                QAProfile(
                    key="web",
                    title="Web QA",
                    surfaces=frozenset({ApplicationSurface.WEB_UI}),
                    enabled=True,
                ),
                QAProfile(
                    key="disabled",
                    title="Disabled",
                    surfaces=frozenset({ApplicationSurface.CLI}),
                    enabled=False,
                ),
            ),
        }
    )
    selection = select_qa(repo, ["src/cli.py"])
    assert selection.profiles[0].key == "cli-qa"
    profile = QAProfile(
        key="web",
        title="Web QA",
        surfaces=frozenset({ApplicationSurface.WEB_UI}),
    )
    assert paths_match_surface("frontend/App.tsx", profile) is True
    assert paths_match_surface("src/cli.py", profile) is False
    assert paths_match_surface(
        "src/cli.py",
        profile.model_copy(update={"surfaces": frozenset()}),
    ) is True
