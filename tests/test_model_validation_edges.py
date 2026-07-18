from __future__ import annotations

from pathlib import Path

import pytest

from make_it_so.models import (
    AppConfig,
    Checkpoint,
    CheckpointKind,
    CheckpointStatus,
    CompletionPolicy,
    Course,
    CourseKind,
    OperationMode,
    QAProfile,
    ReadinessRequirement,
    WorkerAssignments,
    WorkPackage,
)
from tests.helpers import app_config, model_policy, repo_config


def test_worker_assignments_must_use_distinct_agents() -> None:
    with pytest.raises(ValueError, match="unique across roles"):
        WorkerAssignments(
            captain="same",
            coder="same",
            reviewer="reviewer",
            tester="tester",
            ux_reviewer="ux",
            final_reviewer="final",
            merger="merge",
            verifier="verify",
        )


def test_checkpoint_and_work_package_validators_reject_ambiguous_provenance() -> None:
    with pytest.raises(ValueError, match="owner_decision_required"):
        Checkpoint(
            key="approval",
            title="Approval",
            kind=CheckpointKind.HUMAN_DECISION,
            reason="The owner must approve the decision.",
            owner_decision_required=False,
        )
    with pytest.raises(ValueError, match="resolution provenance"):
        Checkpoint(
            key="resolved",
            title="Resolved",
            kind=CheckpointKind.MILESTONE_DEMO,
            reason="The demo passed.",
            status=CheckpointStatus.RESOLVED,
        )
    with pytest.raises(ValueError, match="depend on itself"):
        WorkPackage(key="self", title="Self", objective="Self", dependencies=("self",))
    with pytest.raises(ValueError, match="duplicate dependencies"):
        WorkPackage(key="duplicate", title="Duplicate", objective="Duplicate", dependencies=("x", "x"))


def test_course_graph_validators_reject_duplicate_and_unknown_references() -> None:
    base = Course(
        key="graph",
        repository="example/project",
        kind=CourseKind.FEATURE,
        title="Graph checks",
        goal="Exercise the graph validators in the durable course model.",
        work_packages=(WorkPackage(key="work", title="Work", objective="Work"),),
    )
    def invalid(**updates: object) -> None:
        value = base.model_dump(mode="json")
        value.update(updates)
        Course.model_validate(value)

    with pytest.raises(ValueError, match="work package keys"):
        invalid(work_packages=[base.work_packages[0].model_dump(mode="json")] * 2)
    with pytest.raises(ValueError, match="checkpoint keys"):
        invalid(
            checkpoints=[
                Checkpoint(
                    key="same",
                    title="One",
                    reason="First checkpoint",
                    kind=CheckpointKind.MILESTONE_DEMO,
                    owner_decision_required=False,
                ).model_dump(mode="json"),
                Checkpoint(
                    key="same",
                    title="Two",
                    reason="Second checkpoint",
                    kind=CheckpointKind.MILESTONE_DEMO,
                    owner_decision_required=False,
                ).model_dump(mode="json"),
            ]
        )
    with pytest.raises(ValueError, match="QA profile keys"):
        invalid(
            qa_profiles=[
                QAProfile(key="qa", title="QA", checks=("pytest",)).model_dump(mode="json"),
                QAProfile(key="qa", title="QA again", checks=("pytest",)).model_dump(mode="json"),
            ]
        )
    with pytest.raises(ValueError, match="readiness keys"):
        invalid(
            readiness=[
                ReadinessRequirement(key="r", category="goal", question="One?").model_dump(mode="json"),
                ReadinessRequirement(key="r", category="goal", question="Two?").model_dump(mode="json"),
            ]
        )


def test_repo_and_app_configuration_validate_cross_field_safety(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="auto_merge requires"):
        repo_config(tmp_path, completion=CompletionPolicy.AUTO_MERGE)
    base = repo_config(tmp_path)
    with pytest.raises(ValueError, match="unique"):
        AppConfig(
            version=1,
            state_dir=tmp_path / "state",
            artifact_dir=tmp_path / "artifacts",
            harnesses={},
            models=model_policy(),
            repos=(base, base),
        )

    with pytest.raises(ValueError, match="allow_incomplete_telemetry"):
        AppConfig.model_validate(
            {
                **app_config(tmp_path, base).model_dump(mode="json"),
                "usage": {"allow_incomplete_telemetry": True},
                "repos": [
                    base.model_copy(update={"operation_mode": OperationMode.AUTONOMOUS}).model_dump(mode="json")
                ],
            }
        )
