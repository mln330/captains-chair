"""Small host-facing contracts that keep runtime interaction replaceable."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from captains_chair.courses import (
    CourseError,
    planning_session,
    resolve_checkpoint,
    resolve_readiness_requirement,
)
from captains_chair.models import (
    CheckpointStatus,
    Course,
    RepoConfig,
    RequirementStatus,
)
from captains_chair.state import StateStore


@runtime_checkable
class UsageTelemetryAdapter(Protocol):
    """Synchronize provider-native usage before admitting a model call."""

    def synchronize(self, state: StateStore, repo: RepoConfig) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CallbackUsageTelemetryAdapter:
    """Adapter for hosts that already expose usage as a callback."""

    callback: Callable[[StateStore, RepoConfig], dict[str, Any]]

    def synchronize(self, state: StateStore, repo: RepoConfig) -> dict[str, Any]:
        return self.callback(state, repo)


@runtime_checkable
class InteractionAdapter(Protocol):
    """Host interaction boundary for planning handoffs and durable answers."""

    def planning_session(self, course: Course) -> dict[str, Any]: ...

    def resolve_requirement(
        self,
        course: Course,
        requirement_key: str,
        status: RequirementStatus,
        answer: str | None = None,
        evidence: tuple[str, ...] = (),
        *,
        verified_by: str | None = None,
        verified_at: datetime | None = None,
        verification_model: str | None = None,
    ) -> Course: ...

    def resolve_checkpoint(
        self,
        course: Course,
        checkpoint_key: str,
        status: CheckpointStatus,
        resolved_by: str | None = None,
        resolved_at: datetime | None = None,
        evidence: tuple[str, ...] = (),
    ) -> Course: ...


class NativeInteractionAdapter:
    """Default deterministic interaction implementation shared by all hosts."""

    def planning_session(self, course: Course) -> dict[str, Any]:
        return planning_session(course)

    def resolve_requirement(
        self,
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
        if status == RequirementStatus.VERIFIED:
            raise CourseError(
                "owner-facing requirement updates cannot self-verify; run the independent readiness review"
            )
        return resolve_readiness_requirement(
            course,
            requirement_key,
            status,
            answer,
            evidence,
            verified_by=verified_by,
            verified_at=verified_at,
            verification_model=verification_model,
        )

    def resolve_checkpoint(
        self,
        course: Course,
        checkpoint_key: str,
        status: CheckpointStatus,
        resolved_by: str | None = None,
        resolved_at: datetime | None = None,
        evidence: tuple[str, ...] = (),
    ) -> Course:
        return resolve_checkpoint(course, checkpoint_key, status, resolved_by, resolved_at, evidence)


__all__ = [
    "CallbackUsageTelemetryAdapter",
    "InteractionAdapter",
    "NativeInteractionAdapter",
    "UsageTelemetryAdapter",
]
