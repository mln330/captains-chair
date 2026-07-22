"""Course milestone gates and bounded plan mutations."""

from __future__ import annotations

from datetime import UTC, datetime

from make_it_so.models import (
    Checkpoint,
    CheckpointKind,
    CheckpointStatus,
    Course,
    MilestoneApprovalPolicy,
    MilestoneChangeKind,
    MilestoneChangeRequest,
    OperationMode,
    RepoConfig,
    WorkPackageStatus,
)


class MilestoneError(RuntimeError):
    """A milestone mutation would make the course unsafe to advance."""


_PATCHABLE_FIELDS = frozenset(
    {
        "title",
        "objective",
        "dependencies",
        "acceptance_criteria",
        "checks",
        "qa_profiles",
        "test_evidence_policy",
        "checkpoint_keys",
        "model_profiles",
        "risk",
    }
)


def milestone_approval_required(repo: RepoConfig) -> bool:
    """Return whether completion must pause for owner approval."""
    if repo.milestone_approval == MilestoneApprovalPolicy.EACH_MILESTONE:
        return True
    if repo.milestone_approval == MilestoneApprovalPolicy.NONE:
        return False
    return repo.operation_mode == OperationMode.SUPERVISED


def milestone_checkpoint_key(work_package_key: str) -> str:
    return f"milestone-{work_package_key}"


def add_milestone_checkpoint(
    course: Course,
    work_package_key: str,
    *,
    reason: str,
    owner_decision_required: bool,
    resolved_by: str | None = None,
    evidence: tuple[str, ...] = (),
) -> Course:
    """Add an idempotent gate that blocks only dependent milestones."""
    package = next((item for item in course.work_packages if item.key == work_package_key), None)
    if package is None:
        raise MilestoneError(f"course {course.key!r} has no milestone {work_package_key!r}")
    key = milestone_checkpoint_key(work_package_key)
    existing = next((item for item in course.checkpoints if item.key == key), None)
    if existing is not None:
        return course
    dependents = tuple(item.key for item in course.work_packages if work_package_key in item.dependencies)
    status = CheckpointStatus.PENDING
    resolved_at = None
    if not owner_decision_required:
        status = CheckpointStatus.RESOLVED
        resolved_at = datetime.now(UTC)
    checkpoint = Checkpoint(
        key=key,
        title=f"Milestone review: {package.title}",
        kind=CheckpointKind.MILESTONE_DEMO,
        reason=reason,
        blocks_work_packages=dependents,
        status=status,
        required=bool(dependents),
        owner_decision_required=owner_decision_required,
        resolved_by=resolved_by if not owner_decision_required else None,
        resolved_at=resolved_at,
        evidence=evidence,
    )
    return course.model_copy(update={"checkpoints": (*course.checkpoints, checkpoint)})


def validate_milestone_changes(course: Course, changes: tuple[MilestoneChangeRequest, ...]) -> None:
    if not changes:
        raise MilestoneError("at least one milestone change is required")
    keys = {item.key for item in course.work_packages}
    for change in changes:
        if change.kind == MilestoneChangeKind.ADD:
            assert change.work_package is not None
            key = change.work_package.key
            if key in keys:
                raise MilestoneError(f"milestone {key!r} already exists")
            keys.add(key)
        elif change.kind in {MilestoneChangeKind.UPDATE, MilestoneChangeKind.REMOVE}:
            assert change.work_package_key is not None
            key = change.work_package_key
            if key not in keys:
                raise MilestoneError(f"milestone {key!r} does not exist")
            if change.kind == MilestoneChangeKind.UPDATE:
                invalid = set(change.patch) - _PATCHABLE_FIELDS
                if invalid:
                    raise MilestoneError(f"milestone update contains protected fields: {sorted(invalid)}")
            else:
                package = next(item for item in course.work_packages if item.key == key)
                if package.status in {WorkPackageStatus.EXECUTING, WorkPackageStatus.REVIEWING}:
                    raise MilestoneError(f"active milestone {key!r} cannot be removed")
                dependents = [item.key for item in course.work_packages if key in item.dependencies]
                if dependents:
                    raise MilestoneError(f"milestone {key!r} still has dependents: {', '.join(dependents)}")
                keys.remove(key)


def apply_milestone_changes(course: Course, changes: tuple[MilestoneChangeRequest, ...]) -> Course:
    """Apply only graph-safe changes and increment the durable plan revision."""
    validate_milestone_changes(course, changes)
    packages = list(course.work_packages)
    for change in changes:
        if change.kind == MilestoneChangeKind.ADD:
            assert change.work_package is not None
            packages.append(change.work_package)
        elif change.kind == MilestoneChangeKind.UPDATE:
            assert change.work_package_key is not None
            packages = [
                item.model_copy(update=change.patch) if item.key == change.work_package_key else item
                for item in packages
            ]
        elif change.kind == MilestoneChangeKind.REMOVE:
            assert change.work_package_key is not None
            packages = [item for item in packages if item.key != change.work_package_key]
    try:
        return Course.model_validate(
            course.model_copy(
                update={"work_packages": tuple(packages), "plan_revision": course.plan_revision + 1}
            ).model_dump(mode="python")
        )
    except ValueError as exc:
        raise MilestoneError(f"milestone changes produce an invalid course graph: {exc}") from exc


__all__ = [
    "MilestoneError",
    "add_milestone_checkpoint",
    "apply_milestone_changes",
    "milestone_approval_required",
    "milestone_checkpoint_key",
    "validate_milestone_changes",
]
