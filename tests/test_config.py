import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from captains_chair.config import load_config, load_project_manifest, write_json_schema
from captains_chair.models import (
    ActionKind,
    AppConfig,
    CompletionPolicy,
    Course,
    ExternalWorkboardConfig,
    ModelCapability,
    ModelExecutionMode,
    ModelProfile,
    ModelQualification,
    ModelTarget,
    OpenClawWorkboardConfig,
    OperationMode,
    PlanDecision,
    ProjectManifest,
    ReasoningEffort,
    RepoConfig,
    TokenUsageRecord,
    UsageConfig,
    WorkerAssignments,
)
from tests.helpers import app_config, model_policy, repo_config


def test_checked_in_configuration_schema_matches_typed_models() -> None:
    schema_path = Path(__file__).parents[1] / "schemas" / "config.schema.json"

    checked_in = json.loads(schema_path.read_text(encoding="utf-8"))

    assert checked_in == AppConfig.model_json_schema()


@pytest.mark.parametrize(
    ("schema_name", "model"),
    (
        ("course.schema.json", Course),
        ("project-manifest.schema.json", ProjectManifest),
        ("token-usage-record.schema.json", TokenUsageRecord),
    ),
)
def test_checked_in_domain_schemas_match_typed_models(schema_name: str, model: type[object]) -> None:
    schema_path = Path(__file__).parents[1] / "schemas" / schema_name

    checked_in = json.loads(schema_path.read_text(encoding="utf-8"))

    assert checked_in == model.model_json_schema()  # type: ignore[attr-defined]


def test_unknown_configuration_fields_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
version: 1
state_dir: /tmp/state
artifact_dir: /tmp/artifacts
harnesses: {}
models: {}
repos: []
reviewer_modle: typo
""",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_config(path)


def test_config_helpers_handle_manifests_and_schema_writes(tmp_path: Path) -> None:
    assert load_project_manifest(tmp_path, ".captains-chair/project.yaml") is None
    manifest_path = tmp_path / ".captains-chair" / "project.yaml"
    manifest_path.parent.mkdir()
    manifest_path.write_text(
        "version: 1\ngoal: Test project\ncanonical_docs: [README.md]\nplanning_doc: PLAN.md\nchecks: [pytest]\n",
        encoding="utf-8",
    )
    manifest = load_project_manifest(tmp_path, ".captains-chair/project.yaml")
    assert manifest is not None and manifest.goal == "Test project"

    schema_path = tmp_path / "nested" / "config.schema.json"
    write_json_schema(schema_path)
    assert schema_path.is_file()
    assert json.loads(schema_path.read_text(encoding="utf-8"))["title"] == "AppConfig"


def test_config_helpers_reject_non_object_yaml(tmp_path: Path) -> None:
    path = tmp_path / "not-object.yaml"
    path.write_text("- item\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected YAML object"):
        load_config(path)


@pytest.mark.parametrize(
    "field_value",
    ("../outside.md", "/tmp/outside.md", "C:\\outside.md", "."),
)
def test_repository_document_paths_cannot_escape_checkout(
    tmp_path: Path, field_value: str
) -> None:
    with pytest.raises(ValidationError, match="repository document paths"):
        RepoConfig(
            full_name="example/project",
            local_path=tmp_path,
            planning_doc=field_value,
        )

    with pytest.raises(ValidationError, match="repository document paths"):
        ProjectManifest(
            version=1,
            goal="Keep all project documents inside the repository.",
            canonical_docs=(field_value,),
            planning_doc="PLAN.md",
            checks=(),
        )


def test_final_review_fallback_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
version: 1
state_dir: /tmp/state
artifact_dir: /tmp/artifacts
harnesses: {}
models:
  baseline: {primary: {model: one}}
  planner: {primary: {model: one}}
  coder: {primary: {model: one}}
  reviewer: {primary: {model: one}}
  final_reviewer:
    primary: {model: strong}
    fallbacks: [{model: weak}]
repos: []
""",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="final_reviewer"):
        load_config(path)


def test_worker_roles_must_use_distinct_agent_ids() -> None:
    with pytest.raises(ValidationError, match="unique across roles"):
        WorkerAssignments(
            captain="captain",
            coder="coder",
            reviewer="coder",
            tester="tester",
            ux_reviewer="ux",
            final_reviewer="final",
            merger="merge",
            verifier="verify",
        )


def test_ux_reviewer_is_optional_for_existing_configs(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
version: 1
state_dir: /tmp/state
artifact_dir: /tmp/artifacts
harnesses: {}
models:
  baseline: {primary: {model: one}}
  planner: {primary: {model: one}}
  coder: {primary: {model: one}}
  reviewer: {primary: {model: one}}
  final_reviewer: {primary: {model: strong}, allow_fallback: false}
repos: []
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.models.ux_reviewer is None


def test_repo_cannot_reference_unknown_orchestrator(tmp_path: Path) -> None:
    repo = repo_config(tmp_path).model_copy(update={"orchestrator": "missing"})
    with pytest.raises(ValidationError, match="unknown orchestrators"):
        app_config(tmp_path, repo)


def test_repo_can_reference_typed_openclaw_orchestrator(tmp_path: Path) -> None:
    repo = repo_config(tmp_path).model_copy(update={"orchestrator": "workers"})
    base = app_config(tmp_path, repo_config(tmp_path))
    workers = OpenClawWorkboardConfig(
        workers=WorkerAssignments(
            captain="captain",
            coder="coder",
            reviewer="reviewer",
            tester="tester",
            ux_reviewer="ux",
            final_reviewer="final",
            merger="merge",
            verifier="verify",
        )
    )

    payload = base.model_dump(mode="python")
    payload["repos"] = [repo.model_dump(mode="python")]
    payload["orchestrators"] = {"workers": workers.model_dump(mode="python")}
    configured = AppConfig.model_validate(payload)

    assert configured.repo("example/project").orchestrator == "workers"
    workers_config = configured.orchestrators["workers"]
    assert isinstance(workers_config, OpenClawWorkboardConfig)
    assert workers_config.session_limit == 1000

    payload["orchestrators"]["workers"]["session_limit"] = 10001
    with pytest.raises(ValidationError, match="session_limit"):
        AppConfig.model_validate(payload)


def test_incomplete_telemetry_override_is_rejected_for_autonomous_repos(tmp_path: Path) -> None:
    repo = repo_config(
        tmp_path, mode=OperationMode.AUTONOMOUS, completion=CompletionPolicy.CONTROL_PLANE_COMPLETE
    )
    config = app_config(tmp_path, repo)
    payload = config.model_dump(mode="python")
    payload["usage"] = UsageConfig(allow_incomplete_telemetry=True).model_dump(mode="python")

    with pytest.raises(ValidationError, match="only permitted before autonomous"):
        AppConfig.model_validate(payload)


def test_future_runtime_configs_use_generic_extension_envelope(tmp_path: Path) -> None:
    base = app_config(tmp_path, repo_config(tmp_path))
    payload = base.model_dump(mode="python")
    payload["orchestrators"] = {
        "future-a": ExternalWorkboardConfig(
            kind="future_a",
            executable="future-a",
            workers=WorkerAssignments(
                captain="captain",
                coder="coder",
                reviewer="reviewer",
                tester="tester",
                ux_reviewer="ux",
                final_reviewer="final",
                merger="merge",
                verifier="verify",
            ),
        ).model_dump(mode="python"),
        "future-b": ExternalWorkboardConfig(
            kind="future_b",
            executable="future-b",
            workers=WorkerAssignments(
                captain="captain",
                coder="coder",
                reviewer="reviewer",
                tester="tester",
                ux_reviewer="ux",
                final_reviewer="final",
                merger="merge",
                verifier="verify",
            ),
        ).model_dump(mode="python"),
    }
    payload["repos"] = [
        repo_config(tmp_path).model_copy(update={"orchestrator": "future-a"}).model_dump(mode="python")
    ]

    configured = AppConfig.model_validate(payload)

    assert configured.orchestrators["future-a"].kind == "future_a"
    assert configured.orchestrators["future-b"].kind == "future_b"


def test_external_runtime_config_accepts_plugin_owned_kind_and_settings(tmp_path: Path) -> None:
    base = app_config(tmp_path, repo_config(tmp_path))
    external = ExternalWorkboardConfig(
        kind="hermes-next-workboard",
        executable="hermes-next",
        workers=WorkerAssignments(
            captain="captain",
            coder="coder",
            reviewer="reviewer",
            tester="tester",
            ux_reviewer="ux",
            final_reviewer="final",
            merger="merge",
            verifier="verify",
        ),
        settings={"workspace_mode": "disposable"},
    )
    payload = base.model_dump(mode="python")
    payload["orchestrators"] = {"hermes-next": external.model_dump(mode="python")}
    payload["repos"] = [
        repo_config(tmp_path).model_copy(update={"orchestrator": "hermes-next"}).model_dump(mode="python")
    ]

    configured = AppConfig.model_validate(payload)

    selected = configured.orchestrators["hermes-next"]
    assert isinstance(selected, ExternalWorkboardConfig)
    assert selected.settings == {"workspace_mode": "disposable"}


def test_public_example_uses_documented_balanced_model_routes() -> None:
    example = Path(__file__).parents[1] / "config" / "config.example.yaml"

    configured = load_config(example)

    assert configured.models.baseline.primary.model == "codex/gpt-5.6-terra"
    assert configured.models.planner.primary.model == "codex/gpt-5.6-terra"
    assert configured.models.planner.primary.thinking == "medium"
    assert configured.models.coder.primary.model == "codex/gpt-5.3-codex-spark"
    assert configured.models.coder.primary.thinking == "medium"
    assert configured.models.tester is not None
    assert configured.models.tester.primary.model == "codex/gpt-5.6-luna"
    assert configured.models.ux_reviewer is not None
    assert configured.models.ux_reviewer.primary.model == "codex/gpt-5.6-terra"
    assert configured.harness_model_overrides["codex"].coder.primary.model == "gpt-5.3-codex-spark"
    assert configured.harness_model_overrides["codex"].tester is not None
    assert configured.harness_model_overrides["codex"].tester.primary.model == "gpt-5.6-luna"
    assert configured.harness_model_overrides["codex"].ux_reviewer is not None
    assert configured.harness_model_overrides["codex"].ux_reviewer.primary.model == "gpt-5.6-terra"

    worker_models = configured.orchestrators["openclaw-workers"].worker_models
    assert worker_models.coder == "codex/gpt-5.6-terra"
    assert worker_models.tester == "codex/gpt-5.6-luna"
    assert worker_models.reviewer == "codex/gpt-5.6-terra"
    assert worker_models.final_reviewer == "codex/gpt-5.6-sol"


def test_model_route_rejects_unsupported_effort_and_execution_mode() -> None:
    capability = ModelCapability(
        supported_efforts=frozenset({ReasoningEffort.LOW, ReasoningEffort.MEDIUM}),
        supported_execution_modes=frozenset({ModelExecutionMode.STANDARD}),
    )

    with pytest.raises(ValidationError, match="reasoning effort"):
        ModelTarget(model="bounded", thinking=ReasoningEffort.HIGH, capability=capability)
    with pytest.raises(ValidationError, match="execution mode"):
        ModelTarget(
            model="bounded",
            thinking=ReasoningEffort.MEDIUM,
            execution_mode=ModelExecutionMode.PRO,
            capability=capability,
        )


def test_autonomous_model_route_requires_explicit_qualification() -> None:
    with pytest.raises(ValidationError, match="qualification=autonomous"):
        ModelTarget(model="local-coder", autonomous_eligible=True)

    route = ModelTarget(
        model="qualified-coder",
        qualification=ModelQualification.AUTONOMOUS,
        autonomous_eligible=True,
    )
    assert route.autonomous_eligible is True


def test_named_model_profile_overrides_legacy_fixed_role() -> None:
    policy = model_policy().model_copy(
        update={
            "profiles": {
                "fast_coder": ModelProfile(
                    primary=ModelTarget(model="gpt-5.3-codex-spark", thinking=ReasoningEffort.MEDIUM)
                )
            }
        }
    )

    assert policy.for_role("fast_coder").primary.model == "gpt-5.3-codex-spark"


def test_model_policy_layers_resolve_most_specific_route() -> None:
    config = app_config(Path("/tmp"), repo_config(Path("/tmp")))
    repo_route = ModelProfile(primary=ModelTarget(model="repo-coder"))
    course_route = ModelProfile(primary=ModelTarget(model="course-coder"))
    package_route = ModelProfile(primary=ModelTarget(model="package-coder"))
    stage_route = ModelProfile(primary=ModelTarget(model="stage-coder"))

    policy = config.model_policy(
        "test",
        repo_profiles={"coder": repo_route},
        course_profiles={"coder": course_route},
        work_package_profiles={"coder": package_route},
        stage_profiles={"coder": stage_route},
    )

    assert policy.for_role("coder").primary.model == "stage-coder"
    assert policy.effective_for_role("coder").primary.model == "stage-coder"


def test_issue_label_and_retarget_actions_require_typed_targets() -> None:
    with pytest.raises(ValidationError, match="label_issue requires target_issue"):
        PlanDecision(
            action=ActionKind.LABEL_ISSUE,
            summary="Label an issue",
            reason="The issue needs triage metadata.",
            issue_labels=("triage",),
        )
    with pytest.raises(ValidationError, match="issue_labels"):
        PlanDecision(
            action=ActionKind.LABEL_ISSUE,
            summary="Label an issue",
            reason="The issue needs triage metadata.",
            target_issue=12,
        )
    with pytest.raises(ValidationError, match="retarget_issue requires issue_milestone"):
        PlanDecision(
            action=ActionKind.RETARGET_ISSUE,
            summary="Retarget an issue",
            reason="The issue needs a new owner.",
            target_issue=12,
        )
