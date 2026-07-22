from __future__ import annotations

from pathlib import Path
from typing import Any, TypeVar

import pytest
import yaml
from pydantic import BaseModel

from make_it_so.courses import CourseStore, approve_course, eligible_work_packages, set_work_package_status
from make_it_so.harness import HarnessAdapter
from make_it_so.milestones import (
    MilestoneError,
    add_milestone_checkpoint,
    apply_milestone_changes,
)
from make_it_so.models import (
    HarnessConfig,
    HarnessInvocation,
    MilestoneChangeKind,
    MilestoneChangeRequest,
    ModelTarget,
    ModelUsage,
    OperationMode,
    RoleModels,
    WorkPackageStatus,
)
from make_it_so.sidecar import SidecarServer
from tests.helpers import app_config, repo_config
from tests.test_courses import ready_course


def test_supervised_milestone_gate_blocks_only_dependent_work() -> None:
    course = approve_course(ready_course(), "owner")
    course = set_work_package_status(course, "index", WorkPackageStatus.COMPLETE)
    course = add_milestone_checkpoint(
        course,
        "index",
        reason="Number One requires owner milestone approval.",
        owner_decision_required=True,
    )
    ready = {item.key for item in eligible_work_packages(course)}
    assert "query" not in ready
    assert "docs" in ready


def test_autonomous_graph_safe_change_increments_plan_revision() -> None:
    course = ready_course()
    change = MilestoneChangeRequest(
        kind=MilestoneChangeKind.UPDATE,
        summary="Clarify the index milestone",
        reason="Current implementation evidence requires a more precise objective.",
        work_package_key="index",
        patch={"objective": "Build and validate the search index."},
    )
    updated = apply_milestone_changes(course, (change,))
    assert updated.plan_revision == course.plan_revision + 1
    assert updated.work_packages[0].objective == "Build and validate the search index."


def test_milestone_removal_rejects_dependents_and_active_work() -> None:
    course = ready_course()
    with pytest.raises(MilestoneError, match="still has dependents"):
        apply_milestone_changes(
            course,
            (
                MilestoneChangeRequest(
                    kind=MilestoneChangeKind.REMOVE,
                    summary="Remove index",
                    reason="No longer needed.",
                    work_package_key="index",
                ),
            ),
        )
    active = course.model_copy(
        update={
            "work_packages": (
                course.work_packages[0].model_copy(update={"status": WorkPackageStatus.EXECUTING}),
                *course.work_packages[1:],
            )
        }
    )
    with pytest.raises(MilestoneError, match="active milestone"):
        apply_milestone_changes(
            active,
            (
                MilestoneChangeRequest(
                    kind=MilestoneChangeKind.REMOVE,
                    summary="Remove index",
                    reason="No longer needed.",
                    work_package_key="index",
                ),
            ),
        )


class Output(BaseModel):
    value: str


OutputModel = TypeVar("OutputModel", bound=BaseModel)


class ContinuityHarness(HarnessAdapter):
    def __init__(self, config: HarnessConfig) -> None:
        super().__init__(config)
        self.provider_sessions: list[str] = []

    def invoke(
        self,
        *,
        prompt: str,
        model: ModelTarget,
        role: str,
        output_model: type[OutputModel],
        cwd: Path,
        writable: bool,
        session_id: str,
    ) -> dict[str, Any] | HarnessInvocation:
        del prompt, model, role, output_model, cwd, writable
        self.provider_sessions.append(session_id)
        return HarnessInvocation(payload={"value": "ok"}, usage=ModelUsage(total_tokens=3))


def test_number_one_continuity_has_one_provider_session_and_unique_telemetry_calls() -> None:
    harness = ContinuityHarness(HarnessConfig(kind="openclaw", executable="openclaw"))
    models = RoleModels(primary=ModelTarget(model="test-model"))
    first = harness.run(
        prompt="one",
        models=models,
        role="planner",
        output_model=Output,
        cwd=Path.cwd(),
        writable=False,
        continuation_session_id="number-one-course",
    )
    second = harness.run(
        prompt="two",
        models=models,
        role="planner",
        output_model=Output,
        cwd=Path.cwd(),
        writable=False,
        continuation_session_id="number-one-course",
    )
    assert first.session_id != second.session_id
    assert first.continuation_session_id == second.continuation_session_id == "number-one-course"
    assert harness.provider_sessions == ["attempt-0:number-one-course", "attempt-0:number-one-course"]


def test_sidecar_proposes_and_approves_stale_checked_milestone_change(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.SUPERVISED).model_copy(
        update={"require_engaged_course": False}
    )
    config = app_config(tmp_path, repo).model_copy(update={"repos": (repo,)})
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    course = ready_course().model_copy(update={"repository": repo.full_name})
    CourseStore(repo.local_path).save(course)
    server = SidecarServer(config_path)
    proposed = server.request(
        "course.milestone_change_propose",
        {
            "full_name": repo.full_name,
            "course_key": course.key,
            "summary": "Clarify index work",
            "reason": "Number One found an acceptance gap.",
            "changes": [
                {
                    "kind": "update",
                    "summary": "Clarify objective",
                    "reason": "The current objective is too broad.",
                    "work_package_key": "index",
                    "patch": {"objective": "Build and validate the search index."},
                }
            ],
        },
    )
    proposal_id = proposed["proposal"]["proposal_id"]
    applied = server.request(
        "course.milestone_change_approve",
        {"full_name": repo.full_name, "course_key": course.key, "proposal_id": proposal_id, "approved_by": "owner"},
    )
    assert applied["status"] == "applied"
    assert applied["course"]["plan_revision"] == 2
    assert applied["milestone_changes"][0]["status"] == "applied"
