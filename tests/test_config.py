import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from captains_chair.config import load_config
from captains_chair.models import (
    ActionKind,
    AppConfig,
    CodexWorkboardConfig,
    CompletionPolicy,
    ExternalWorkboardConfig,
    HermesWorkboardConfig,
    OpenClawWorkboardConfig,
    OperationMode,
    PlanDecision,
    UsageConfig,
    WorkerAssignments,
)
from tests.helpers import app_config, repo_config


def test_checked_in_configuration_schema_matches_typed_models() -> None:
    schema_path = Path(__file__).parents[1] / "schemas" / "config.schema.json"

    checked_in = json.loads(schema_path.read_text(encoding="utf-8"))

    assert checked_in == AppConfig.model_json_schema()


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
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS, completion=CompletionPolicy.CONTROL_PLANE_COMPLETE)
    config = app_config(tmp_path, repo)
    payload = config.model_dump(mode="python")
    payload["usage"] = UsageConfig(allow_incomplete_telemetry=True).model_dump(mode="python")

    with pytest.raises(ValidationError, match="only permitted before autonomous"):
        AppConfig.model_validate(payload)


def test_future_runtime_configs_validate_without_claiming_implementation(tmp_path: Path) -> None:
    base = app_config(tmp_path, repo_config(tmp_path))
    payload = base.model_dump(mode="python")
    payload["orchestrators"] = {
        "hermes": HermesWorkboardConfig(workers=WorkerAssignments(
            captain="captain", coder="coder", reviewer="reviewer", tester="tester", ux_reviewer="ux",
            final_reviewer="final", merger="merge", verifier="verify",
        )).model_dump(mode="python"),
        "codex": CodexWorkboardConfig(workers=WorkerAssignments(
            captain="captain", coder="coder", reviewer="reviewer", tester="tester", ux_reviewer="ux",
            final_reviewer="final", merger="merge", verifier="verify",
        )).model_dump(mode="python"),
    }
    payload["repos"] = [repo_config(tmp_path).model_copy(update={"orchestrator": "hermes"}).model_dump(mode="python")]

    configured = AppConfig.model_validate(payload)

    assert configured.orchestrators["hermes"].kind == "hermes_workboard"
    assert configured.orchestrators["codex"].kind == "codex_workboard"


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


def test_public_example_routes_implementation_roles_to_codex_53() -> None:
    example = Path(__file__).parents[1] / "config" / "config.example.yaml"

    configured = load_config(example)

    assert configured.models.coder.primary.model == "codex/gpt-5.3-codex"
    assert configured.models.coder.primary.thinking == "medium"
    assert configured.models.tester is not None
    assert configured.models.tester.primary.model == "codex/gpt-5.3-codex"
    assert configured.models.ux_reviewer is not None
    assert configured.models.ux_reviewer.primary.model == "codex/gpt-5.3-codex"
    assert configured.harness_model_overrides["codex"].coder.primary.model == "gpt-5.3-codex"
    assert configured.harness_model_overrides["codex"].tester is not None
    assert configured.harness_model_overrides["codex"].tester.primary.model == "gpt-5.3-codex"
    assert configured.harness_model_overrides["codex"].ux_reviewer is not None
    assert configured.harness_model_overrides["codex"].ux_reviewer.primary.model == "gpt-5.3-codex"


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
