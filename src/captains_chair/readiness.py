"""Independent, input-bound course readiness review."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from captains_chair.models import (
    Course,
    HarnessResult,
    ReadinessCheckStatus,
    ReadinessRequirement,
    ReadinessReviewCheck,
    ReadinessReviewRecord,
    ReadinessReviewVerdict,
    ReasoningEffort,
    RequirementStatus,
    RoleModels,
    StrictModel,
)

READINESS_PROMPT_VERSION = "course-readiness-v2"
REQUIRED_READINESS_CATEGORIES = (
    "goals",
    "non-goals",
    "users",
    "architecture constraints",
    "permissions",
    "secret references",
    "external access",
    "environments",
    "test data",
    "CI",
    "deployment",
    "rollback",
    "observability",
    "security",
    "UX inputs",
    "token policy",
    "exit criteria",
)


class ReadinessCategory(StrEnum):
    GOALS = "goals"
    NON_GOALS = "non-goals"
    USERS = "users"
    ARCHITECTURE_CONSTRAINTS = "architecture constraints"
    PERMISSIONS = "permissions"
    SECRET_REFERENCES = "secret references"
    EXTERNAL_ACCESS = "external access"
    ENVIRONMENTS = "environments"
    TEST_DATA = "test data"
    CI = "CI"
    DEPLOYMENT = "deployment"
    ROLLBACK = "rollback"
    OBSERVABILITY = "observability"
    SECURITY = "security"
    UX_INPUTS = "UX inputs"
    TOKEN_POLICY = "token policy"
    EXIT_CRITERIA = "exit criteria"


class ReadinessCheckDecision(StrictModel):
    category: ReadinessCategory
    status: Literal["verified", "not_applicable", "blocked"]
    finding: str = Field(min_length=1)
    evidence: tuple[str, ...] = ()


class RequirementDecision(StrictModel):
    key: str = Field(min_length=1)
    verified: bool
    finding: str = Field(min_length=1)
    evidence: tuple[str, ...] = ()


class ReadinessReviewDecision(StrictModel):
    verdict: Literal["ready", "needs_input"]
    summary: str = Field(min_length=1)
    checks: tuple[ReadinessCheckDecision, ...]
    requirements: tuple[RequirementDecision, ...]
    next_questions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_unique_keys(self) -> ReadinessReviewDecision:
        categories = [item.category.value for item in self.checks]
        if len(categories) != len(set(categories)):
            raise ValueError("readiness check categories must be unique")
        keys = [item.key for item in self.requirements]
        if len(keys) != len(set(keys)):
            raise ValueError("readiness requirement decisions must be unique")
        return self


def readiness_input_sha(course: Course) -> str:
    """Fingerprint semantic planning inputs without transient execution state."""
    payload = {
        "version": course.version,
        "key": course.key,
        "repository": course.repository,
        "kind": course.kind.value,
        "title": course.title,
        "goal": course.goal,
        "non_goals": course.non_goals,
        "scope": course.scope,
        "users": course.users,
        "architecture_constraints": course.architecture_constraints,
        "acceptance_criteria": course.acceptance_criteria,
        "exit_criteria": course.exit_criteria,
        "readiness": [
            {
                "key": item.key,
                "category": item.category,
                "question": item.question,
                "required": item.required,
                "answer": item.answer,
                "owner_decision_required": item.owner_decision_required,
                "waived": item.status == RequirementStatus.WAIVED,
            }
            for item in course.readiness
        ],
        "work_packages": [
            {
                "key": item.key,
                "title": item.title,
                "objective": item.objective,
                "dependencies": item.dependencies,
                "acceptance_criteria": item.acceptance_criteria,
                "checks": item.checks,
                "qa_profiles": item.qa_profiles,
                "checkpoint_keys": item.checkpoint_keys,
                "model_profiles": item.model_profiles,
                "risk": item.risk,
                "source_issue": item.source_issue,
            }
            for item in course.work_packages
        ],
        "checkpoints": [
            {
                "key": item.key,
                "title": item.title,
                "kind": item.kind.value,
                "reason": item.reason,
                "blocks_work_packages": item.blocks_work_packages,
                "required": item.required,
                "owner_decision_required": item.owner_decision_required,
            }
            for item in course.checkpoints
        ],
        "qa_profiles": course.qa_profiles,
        "model_profiles": course.model_profiles,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def readiness_evidence_sha(evidence: dict[str, object]) -> str:
    encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_readiness_prompt(course: Course, evidence: dict[str, object] | None = None) -> str:
    categories = "\n".join(f"- {category}" for category in REQUIRED_READINESS_CATEGORIES)
    context = json.dumps(course.model_dump(mode="json", exclude={"readiness_review"}), indent=2)
    live_context = json.dumps(evidence or {}, indent=2, default=str)
    return (
        "You are the independent readiness reviewer for Captain's Chair. Inspect the repository "
        "and assess whether this course contains everything an agent crew needs to execute to completion. "
        "Do not edit files. Owner answers are inputs, not proof; verify them against repository evidence "
        "where possible. Mark a category not_applicable only with a concrete reason. Return exactly one "
        "check for every category below and exactly one decision for every readiness requirement. A ready "
        "verdict requires an actionable, acyclic work-package graph and no blocked category or required "
        "requirement. A provided live evidence envelope is authenticated machine evidence, not an owner "
        "claim. Treat a named collection error as unavailable evidence, but do not treat successfully "
        "collected facts as locally unverifiable. Never infer secret values; secret names and configured "
        "protection rules are sufficient when the course does not require a live deployment. Ask concise "
        "owner questions only for information that cannot be discovered from either source.\n\n"
        f"Required categories:\n{categories}\n\nCourse context:\n{context}\n\n"
        f"Live evidence envelope (SHA-256 {readiness_evidence_sha(evidence or {})}):\n{live_context}"
    )


def apply_readiness_review(
    course: Course,
    decision: ReadinessReviewDecision,
    result: HarnessResult,
    models: RoleModels,
    *,
    provider: str,
    source_evidence: dict[str, object] | None = None,
    reviewed_at: datetime | None = None,
) -> Course:
    expected_categories = set(REQUIRED_READINESS_CATEGORIES)
    actual_categories = {item.category.value for item in decision.checks}
    if actual_categories != expected_categories:
        missing = sorted(expected_categories - actual_categories)
        extra = sorted(actual_categories - expected_categories)
        raise ValueError(f"readiness review categories do not match policy; missing={missing}, extra={extra}")
    expected_requirements = {item.key for item in course.readiness}
    actual_requirements = {item.key for item in decision.requirements}
    if actual_requirements != expected_requirements:
        missing = sorted(expected_requirements - actual_requirements)
        extra = sorted(actual_requirements - expected_requirements)
        raise ValueError(f"readiness requirement decisions do not match course; missing={missing}, extra={extra}")
    by_key = {item.key: item for item in decision.requirements}
    if decision.verdict == "ready" and not course.work_packages:
        raise ValueError("ready readiness review requires an actionable work-package graph")
    if decision.verdict == "ready" and any(
        item.required
        and item.status != RequirementStatus.WAIVED
        and not by_key[item.key].verified
        for item in course.readiness
    ):
        raise ValueError("ready readiness review cannot leave a required requirement unverified")

    now = reviewed_at or datetime.now(UTC)
    updated_requirements: list[ReadinessRequirement] = []
    for requirement in course.readiness:
        assessment = by_key[requirement.key]
        if requirement.status == RequirementStatus.WAIVED:
            updated_requirements.append(requirement)
            continue
        if assessment.verified and requirement.answer and assessment.evidence:
            updated_requirements.append(
                requirement.model_copy(
                    update={
                        "status": RequirementStatus.VERIFIED,
                        "evidence": assessment.evidence,
                        "verified_by": "readiness_reviewer",
                        "verified_at": now,
                        "verification_model": result.resolved_model,
                    }
                )
            )
        else:
            updated_requirements.append(
                requirement.model_copy(
                    update={
                        "status": RequirementStatus.BLOCKED
                        if requirement.required
                        else requirement.status,
                        "evidence": (),
                        "verified_by": None,
                        "verified_at": None,
                        "verification_model": None,
                    }
                )
            )

    target = next(
        (item for item in (models.primary, *models.fallbacks) if item.model == result.resolved_model),
        models.primary,
    )
    decision_payload = decision.model_dump(mode="json")
    evidence_sha = hashlib.sha256(
        json.dumps(decision_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    checks = tuple(
        ReadinessReviewCheck(
            category=item.category.value,
            status=ReadinessCheckStatus(item.status),
            finding=item.finding,
            evidence=item.evidence,
        )
        for item in decision.checks
    )
    record = ReadinessReviewRecord(
        verdict=ReadinessReviewVerdict(decision.verdict),
        summary=decision.summary,
        input_sha=readiness_input_sha(course),
        evidence_sha=evidence_sha,
        source_evidence_sha=readiness_evidence_sha(source_evidence or {}),
        provider=target.provider or provider,
        model=result.resolved_model,
        reasoning=target.thinking if target else ReasoningEffort.HIGH,
        prompt_version=READINESS_PROMPT_VERSION,
        reviewer="readiness_reviewer",
        session_id=result.session_id,
        reviewed_at=now,
        checks=checks,
        next_questions=decision.next_questions,
    )
    return Course.model_validate(
        course.model_copy(
            update={"readiness": tuple(updated_requirements), "readiness_review": record}
        ).model_dump(mode="python")
    )


__all__ = [
    "READINESS_PROMPT_VERSION",
    "REQUIRED_READINESS_CATEGORIES",
    "ReadinessCategory",
    "ReadinessReviewDecision",
    "apply_readiness_review",
    "build_readiness_prompt",
    "readiness_input_sha",
    "readiness_evidence_sha",
]
