"""Durable course planning and dependency-scoped readiness primitives."""

from __future__ import annotations

import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from captains_chair.models import (
    CheckpointStatus,
    Course,
    CourseStatus,
    RequirementStatus,
    StrictModel,
    WorkPackage,
    WorkPackageStatus,
)


class CourseError(RuntimeError):
    """A course cannot be loaded, approved, or advanced safely."""


class ReadinessReport(StrictModel):
    version: int = 1
    course_key: str
    ready: bool
    required_count: int
    unresolved: tuple[str, ...] = ()
    owner_decisions: tuple[str, ...] = ()
    verified: tuple[str, ...] = ()


def readiness_report(course: Course) -> ReadinessReport:
    unresolved: list[str] = []
    owner_decisions: list[str] = []
    verified: list[str] = []
    required = [requirement for requirement in course.readiness if requirement.required]
    for requirement in required:
        if requirement.status in {RequirementStatus.VERIFIED, RequirementStatus.WAIVED}:
            verified.append(requirement.key)
            continue
        unresolved.append(requirement.key)
        if requirement.owner_decision_required:
            owner_decisions.append(requirement.key)
    return ReadinessReport(
        course_key=course.key,
        ready=not unresolved,
        required_count=len(required),
        unresolved=tuple(unresolved),
        owner_decisions=tuple(owner_decisions),
        verified=tuple(verified),
    )


def planning_session(course: Course) -> dict[str, Any]:
    """Build a bounded handoff for the host agent's planning conversation.

    The sidecar does not own a long-lived model transcript. It returns the
    durable course context and the unanswered questions so OpenClaw or Codex
    can continue the conversation in its native interaction surface while
    answers remain persisted through the course API.
    """
    report = readiness_report(course)
    unresolved = [item for item in course.readiness if item.key in report.unresolved]
    questions = [item.question for item in unresolved]
    prompt = (
        f"You are planning course {course.key!r} for {course.repository}. "
        "Use repository inspection to answer what can be verified locally. "
        "Ask the builder only the unresolved questions below, record answers "
        "through the readiness API, then present the course charter and wait "
        "for explicit approval before mutation.\n\n"
        f"Goal: {course.goal}\n"
        f"Unresolved questions: {questions or ['None; review the charter and request approval.']}"
    )
    return {
        "course_key": course.key,
        "repository": course.repository,
        "status": course.status.value,
        "readiness": report.model_dump(mode="json"),
        "next_questions": questions,
        "prompt": prompt,
        "interaction": "host_agent_conversation",
        "mutation_requires_course_approval": True,
    }


def resolve_readiness_requirement(
    course: Course,
    requirement_key: str,
    status: RequirementStatus,
    answer: str | None = None,
    evidence: tuple[str, ...] = (),
    *,
    verified_by: str | None = None,
    verified_at: datetime | None = None,
    verification_model: str | None = None,
) -> Course:
    """Persist an answer or verification result without changing course approval."""
    requirement = next((item for item in course.readiness if item.key == requirement_key), None)
    if requirement is None:
        raise CourseError(f"course {course.key!r} has no readiness requirement {requirement_key!r}")
    answer_value = answer.strip() if answer is not None else requirement.answer
    if status in {RequirementStatus.ANSWERED, RequirementStatus.VERIFIED} and not answer_value:
        raise CourseError(f"readiness requirement {requirement_key!r} needs an answer")
    if status == RequirementStatus.VERIFIED and (
        not evidence
        or not (verified_by or "").strip()
        or not (verification_model or "").strip()
    ):
        raise CourseError(
            f"readiness requirement {requirement_key!r} needs independent verification provenance"
        )
    updated = requirement.model_copy(
        update={
            "status": status,
            "answer": answer_value,
            "evidence": evidence,
            "verified_by": (verified_by or "").strip() or None,
            "verified_at": (verified_at or datetime.now(UTC))
            if status == RequirementStatus.VERIFIED
            else None,
            "verification_model": (verification_model or "").strip() or None,
        }
    )
    updated = type(requirement).model_validate(updated.model_dump(mode="python"))
    requirements = tuple(updated if item.key == requirement_key else item for item in course.readiness)
    return course.model_copy(update={"readiness": requirements})


def mark_readiness_review(course: Course) -> Course:
    if course.status in {CourseStatus.ENGAGED, CourseStatus.COMPLETED}:
        raise CourseError(f"cannot move {course.status.value} course back to readiness review")
    return course.model_copy(update={"status": CourseStatus.READINESS_REVIEW})


def approve_course(course: Course, approved_by: str, approved_at: datetime | None = None) -> Course:
    approver = approved_by.strip()
    if not approver:
        raise CourseError("course approval requires an approver")
    if not course.work_packages:
        raise CourseError("course approval requires at least one work package")
    report = readiness_report(course)
    if not report.ready:
        unresolved = ", ".join(report.unresolved)
        raise CourseError(f"course {course.key!r} is not ready for approval: {unresolved}")
    timestamp = approved_at or datetime.now(UTC)
    return course.model_copy(
        update={
            "status": CourseStatus.ENGAGED,
            "approved_by": approver,
            "approved_at": timestamp,
        }
    )


def resolve_checkpoint(
    course: Course,
    checkpoint_key: str,
    status: CheckpointStatus,
    resolved_by: str | None = None,
    resolved_at: datetime | None = None,
    evidence: tuple[str, ...] = (),
) -> Course:
    checkpoint = next((item for item in course.checkpoints if item.key == checkpoint_key), None)
    if checkpoint is None:
        raise CourseError(f"course {course.key!r} has no checkpoint {checkpoint_key!r}")
    if status in {CheckpointStatus.PENDING, CheckpointStatus.BLOCKED}:
        updated = checkpoint.model_copy(update={"status": status})
    else:
        actor = (resolved_by or "").strip()
        if checkpoint.owner_decision_required and not actor:
            raise CourseError(f"checkpoint {checkpoint_key!r} requires an approving actor")
        updated = checkpoint.model_copy(
            update={
                "status": status,
                "resolved_by": actor or None,
                "resolved_at": resolved_at or datetime.now(UTC),
                "evidence": evidence,
            }
        )
    checkpoints = tuple(updated if item.key == checkpoint_key else item for item in course.checkpoints)
    return course.model_copy(update={"checkpoints": checkpoints})


def pause_course(course: Course) -> Course:
    if course.status != CourseStatus.ENGAGED:
        raise CourseError(f"only an engaged course can be paused, not {course.status.value}")
    return course.model_copy(update={"status": CourseStatus.PAUSED})


def resume_course(course: Course) -> Course:
    if course.status != CourseStatus.PAUSED:
        raise CourseError(f"only a paused course can be resumed, not {course.status.value}")
    if not course.approved_by or course.approved_at is None:
        raise CourseError("paused course is missing approval provenance")
    return course.model_copy(update={"status": CourseStatus.ENGAGED})


def eligible_work_packages(course: Course, completed: set[str] | frozenset[str] = frozenset()) -> tuple[WorkPackage, ...]:
    """Return ready packages while respecting only their own dependencies/checkpoints."""
    if course.status != CourseStatus.ENGAGED:
        return ()
    completed_keys = set(completed)
    checkpoint_by_key = {checkpoint.key: checkpoint for checkpoint in course.checkpoints}
    eligible: list[WorkPackage] = []
    allowed_statuses = {WorkPackageStatus.PLANNED, WorkPackageStatus.READY}
    passed_checkpoints = {CheckpointStatus.APPROVED, CheckpointStatus.RESOLVED, CheckpointStatus.WAIVED}
    for package in course.work_packages:
        if package.key in completed_keys:
            continue
        if package.status not in allowed_statuses:
            continue
        if not set(package.dependencies).issubset(completed_keys):
            continue
        package_checkpoint_keys = set(package.checkpoint_keys)
        package_checkpoint_keys.update(
            checkpoint.key
            for checkpoint in course.checkpoints
            if package.key in checkpoint.blocks_work_packages
        )
        if any(
            checkpoint_by_key[key].status not in passed_checkpoints for key in package_checkpoint_keys
        ):
            continue
        eligible.append(package)
    return tuple(eligible)


def set_work_package_status(
    course: Course,
    work_package_key: str,
    status: WorkPackageStatus,
) -> Course:
    """Update one package without changing course approval or other packages."""
    package = next((item for item in course.work_packages if item.key == work_package_key), None)
    if package is None:
        raise CourseError(f"course {course.key!r} has no work package {work_package_key!r}")
    updated = package.model_copy(update={"status": status})
    packages = tuple(updated if item.key == work_package_key else item for item in course.work_packages)
    return course.model_copy(update={"work_packages": packages})


class CourseStore:
    """Read and write durable course files inside a managed repository."""

    def __init__(self, repository_path: Path) -> None:
        self.repository_path = repository_path

    @property
    def courses_path(self) -> Path:
        return self.repository_path / ".captains-chair" / "courses"

    def path_for(self, course_key: str) -> Path:
        candidate = course_key.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", candidate):
            raise CourseError("course key cannot be used as a file name")
        return self.courses_path / f"{candidate}.yaml"

    def save(self, course: Course) -> Path:
        path = self.path_for(course.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump(course.model_dump(mode="json"), sort_keys=False)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{course.key}.", suffix=".tmp", delete=False
        ) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        os.replace(temporary, path)
        return path

    def load(self, course_key: str) -> Course:
        path = self.path_for(course_key)
        if not path.is_file():
            raise CourseError(f"course file does not exist: {path}")
        try:
            raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
            return Course.model_validate(raw)
        except Exception as exc:
            raise CourseError(f"invalid course file {path}: {exc}") from exc

    def list(self) -> tuple[Course, ...]:
        if not self.courses_path.is_dir():
            return ()
        courses: list[Course] = []
        for path in sorted(self.courses_path.glob("*.yaml")):
            courses.append(self.load(path.stem))
        return tuple(courses)


__all__ = [
    "CourseError",
    "CourseStore",
    "ReadinessReport",
    "approve_course",
    "eligible_work_packages",
    "mark_readiness_review",
    "planning_session",
    "readiness_report",
    "resolve_readiness_requirement",
    "resolve_checkpoint",
    "pause_course",
    "resume_course",
    "set_work_package_status",
]
