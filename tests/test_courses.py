from datetime import UTC, datetime
from pathlib import Path

import pytest

from captains_chair.courses import (
    CourseError,
    CourseStore,
    approve_course,
    eligible_work_packages,
    pause_course,
    planning_session,
    readiness_report,
    resolve_checkpoint,
    resolve_readiness_requirement,
    resume_course,
    set_work_package_status,
)
from captains_chair.models import (
    Checkpoint,
    CheckpointKind,
    CheckpointStatus,
    Course,
    CourseKind,
    CourseStatus,
    ReadinessCheckStatus,
    ReadinessRequirement,
    ReadinessReviewCheck,
    ReadinessReviewRecord,
    ReadinessReviewVerdict,
    ReasoningEffort,
    RequirementStatus,
    WorkPackage,
    WorkPackageStatus,
)
from captains_chair.readiness import REQUIRED_READINESS_CATEGORIES, readiness_input_sha


def course() -> Course:
    return Course(
        key="feature-search",
        repository="example/project",
        kind=CourseKind.FEATURE,
        title="Search improvements",
        goal="Make repository search faster and easier to use for existing users.",
        acceptance_criteria=("Search returns ranked results",),
        exit_criteria=("The documented search flows pass",),
        readiness=(
            ReadinessRequirement(
                key="success",
                category="goal",
                question="What does success mean?",
                status=RequirementStatus.UNKNOWN,
                owner_decision_required=True,
            ),
        ),
        work_packages=(
            WorkPackage(key="index", title="Index", objective="Build the index."),
            WorkPackage(
                key="query",
                title="Query",
                objective="Implement ranked queries.",
                dependencies=("index",),
            ),
            WorkPackage(
                key="ui",
                title="UI",
                objective="Update the search experience.",
                checkpoint_keys=("ui-demo",),
            ),
            WorkPackage(key="docs", title="Docs", objective="Document the feature."),
        ),
        checkpoints=(
            Checkpoint(
                key="ui-demo",
                title="Review search flow",
                kind=CheckpointKind.MILESTONE_DEMO,
                reason="Confirm the user flow before UI work proceeds.",
                blocks_work_packages=("ui",),
                owner_decision_required=False,
            ),
        ),
    )


def ready_course() -> Course:
    value = course()
    requirement = value.readiness[0].model_copy(
        update={
            "status": RequirementStatus.VERIFIED,
            "answer": "The search flow is fast and ranked.",
            "evidence": ("reviewed",),
            "verified_by": "readiness-reviewer",
            "verified_at": datetime(2026, 1, 1, tzinfo=UTC),
            "verification_model": "test-model",
        }
    )
    value = value.model_copy(update={"readiness": (requirement,)})
    review = ReadinessReviewRecord(
        verdict=ReadinessReviewVerdict.READY,
        summary="All course inputs are independently verified.",
        input_sha=readiness_input_sha(value),
        evidence_sha="a" * 64,
        provider="test",
        model="test-model",
        reasoning=ReasoningEffort.HIGH,
        prompt_version="test-v1",
        reviewer="readiness_reviewer",
        session_id="test-session",
        reviewed_at=datetime(2026, 1, 1, tzinfo=UTC),
        checks=tuple(
            ReadinessReviewCheck(
                category=category,
                status=ReadinessCheckStatus.VERIFIED,
                finding=f"{category} verified",
                evidence=("test evidence",),
            )
            for category in REQUIRED_READINESS_CATEGORIES
        ),
    )
    return value.model_copy(update={"readiness_review": review})


def rebind_readiness_review(value: Course) -> Course:
    """Represent a fresh independent review after a test changes course inputs."""
    if value.readiness_review is None:
        raise AssertionError("test course must already have a readiness review")
    review = value.readiness_review.model_copy(update={"input_sha": readiness_input_sha(value)})
    return value.model_copy(update={"readiness_review": review})


def test_readiness_requires_verified_required_items() -> None:
    report = readiness_report(course())
    assert report.ready is False
    assert report.unresolved == ("success",)
    assert report.owner_decisions == ("success",)

    report = readiness_report(ready_course())
    assert report.ready is True
    assert report.verified == ("success",)


def test_readiness_requirement_answer_can_be_verified_durably() -> None:
    answered = resolve_readiness_requirement(
        course(),
        "success",
        RequirementStatus.ANSWERED,
        "The search flow is fast and ranked.",
        ("owner",),
    )
    assert answered.readiness[0].status == RequirementStatus.ANSWERED
    verified = resolve_readiness_requirement(
        answered,
        "success",
        RequirementStatus.VERIFIED,
        evidence=("reviewed",),
        verified_by="readiness-reviewer",
        verification_model="test-model",
    )
    assert verified.readiness[0].status == RequirementStatus.VERIFIED
    assert readiness_report(verified).ready is False
    assert readiness_report(verified).review_current is False

    with pytest.raises(CourseError, match="needs an answer"):
        resolve_readiness_requirement(course(), "success", RequirementStatus.VERIFIED)
    with pytest.raises(CourseError, match="has no readiness requirement"):
        resolve_readiness_requirement(course(), "missing", RequirementStatus.ANSWERED, "answer")


def test_planning_session_returns_only_unresolved_questions_and_durable_context() -> None:
    session = planning_session(course())

    assert session["interaction"] == "host_agent_conversation"
    assert session["next_questions"] == ["What does success mean?"]
    assert session["mutation_requires_course_approval"] is True
    assert "feature-search" in session["prompt"]


def test_course_approval_records_human_provenance() -> None:
    timestamp = datetime(2026, 7, 14, tzinfo=UTC)
    approved = approve_course(ready_course(), "owner@example.com", timestamp)
    assert approved.status == CourseStatus.ENGAGED
    assert approved.approved_by == "owner@example.com"
    assert approved.approved_at == timestamp

    with pytest.raises(CourseError, match="not ready"):
        approve_course(course(), "owner@example.com")
    with pytest.raises(CourseError, match="requires an approver"):
        approve_course(ready_course(), "   ")


def test_readiness_review_is_invalidated_when_semantic_inputs_change() -> None:
    value = ready_course()
    assert readiness_report(value).ready is True

    changed = value.model_copy(update={"exit_criteria": ("A different exit criterion",)})

    assert readiness_report(changed).ready is False
    assert readiness_report(changed).review_current is False


def test_course_approval_requires_an_actionable_work_package() -> None:
    empty = ready_course().model_copy(update={"work_packages": ()})

    with pytest.raises(CourseError, match="at least one work package"):
        approve_course(empty, "owner@example.com")


def test_eligible_packages_respect_only_dependent_checkpoints() -> None:
    engaged = approve_course(ready_course(), "owner@example.com")
    eligible = {package.key for package in eligible_work_packages(engaged)}
    assert eligible == {"index", "docs"}

    after_index = {package.key for package in eligible_work_packages(engaged, {"index"})}
    assert after_index == {"query", "docs"}

    resolved = engaged.model_copy(
        update={
            "checkpoints": (
                engaged.checkpoints[0].model_copy(update={"status": "resolved"}),
            )
        }
    )
    assert {package.key for package in eligible_work_packages(resolved)} == {"index", "ui", "docs"}


def test_checkpoint_resolution_and_course_pause_are_scoped() -> None:
    engaged = approve_course(ready_course(), "owner@example.com")
    resolved = resolve_checkpoint(engaged, "ui-demo", CheckpointStatus.RESOLVED, evidence=("ui-flow.png",))
    assert resolved.checkpoints[0].status == CheckpointStatus.RESOLVED
    assert resolved.checkpoints[0].evidence == ("ui-flow.png",)
    paused = pause_course(resolved)
    assert paused.status == CourseStatus.PAUSED
    assert resume_course(paused).status == CourseStatus.ENGAGED
    with pytest.raises(CourseError, match="has no checkpoint"):
        resolve_checkpoint(engaged, "missing", CheckpointStatus.BLOCKED)
    owner_checkpoint = engaged.checkpoints[0].model_copy(update={"owner_decision_required": True})
    owner_course = engaged.model_copy(update={"checkpoints": (owner_checkpoint,)})
    with pytest.raises(CourseError, match="requires an approving actor"):
        resolve_checkpoint(owner_course, "ui-demo", CheckpointStatus.APPROVED)
    pending = resolve_checkpoint(engaged, "ui-demo", CheckpointStatus.BLOCKED)
    assert pending.checkpoints[0].status == CheckpointStatus.BLOCKED
    with pytest.raises(CourseError, match="only an engaged"):
        pause_course(course())
    with pytest.raises(CourseError, match="only a paused"):
        resume_course(engaged)


def test_checkpoint_resolution_records_actor_and_evidence() -> None:
    engaged = approve_course(ready_course(), "owner@example.com")
    approved = resolve_checkpoint(
        engaged,
        "ui-demo",
        CheckpointStatus.APPROVED,
        resolved_by="reviewer@example.com",
        evidence=("demo-link",),
    )
    checkpoint = approved.checkpoints[0]
    assert checkpoint.resolved_by == "reviewer@example.com"
    assert checkpoint.evidence == ("demo-link",)


def test_work_package_status_updates_only_the_selected_package() -> None:
    engaged = approve_course(ready_course(), "owner@example.com")
    updated = set_work_package_status(engaged, "index", WorkPackageStatus.EXECUTING)

    assert updated.work_packages[0].status == WorkPackageStatus.EXECUTING
    assert updated.work_packages[1:] == engaged.work_packages[1:]

    with pytest.raises(CourseError, match="no work package"):
        set_work_package_status(engaged, "missing", WorkPackageStatus.EXECUTING)
    assert set_work_package_status(course(), "index", WorkPackageStatus.EXECUTING).work_packages[0].status == (
        WorkPackageStatus.EXECUTING
    )


def test_course_store_round_trips_durable_yaml(tmp_path: Path) -> None:
    store = CourseStore(tmp_path / "repo")
    value = ready_course()
    path = store.save(value)
    assert path == tmp_path / "repo" / ".captains-chair" / "courses" / "feature-search.yaml"
    assert store.load("feature-search") == value
    assert store.list() == (value,)

    with pytest.raises(CourseError, match="file name"):
        store.path_for("../escape")


def test_course_rejects_unknown_graph_references() -> None:
    with pytest.raises(ValueError, match="unknown dependencies"):
        Course(
            key="broken",
            repository="example/project",
            kind=CourseKind.TAKEOVER,
            title="Broken course",
            goal="Understand and finish the existing repository safely.",
            work_packages=(
                WorkPackage(
                    key="work",
                    title="Work",
                    objective="Do work.",
                    dependencies=("missing",),
                ),
            ),
        )


def test_course_rejects_cyclic_work_package_dependencies() -> None:
    with pytest.raises(ValueError, match="contain a cycle"):
        Course(
            key="cyclic",
            repository="example/project",
            kind=CourseKind.FEATURE,
            title="Cyclic course",
            goal="Prove that a work package graph cannot deadlock itself forever.",
            work_packages=(
                WorkPackage(
                    key="index",
                    title="Build index",
                    objective="Build the index.",
                    dependencies=("docs",),
                ),
                WorkPackage(
                    key="docs",
                    title="Document index",
                    objective="Document the index.",
                    dependencies=("index",),
                ),
            ),
        )
