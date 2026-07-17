from datetime import UTC, datetime

import pytest

from captains_chair.courses import readiness_report
from captains_chair.models import (
    CourseKind,
    HarnessResult,
    ModelProfile,
    ModelTarget,
    ReasoningEffort,
    RequirementStatus,
)
from captains_chair.readiness import (
    REQUIRED_READINESS_CATEGORIES,
    ReadinessReviewDecision,
    apply_readiness_review,
    build_readiness_prompt,
    readiness_evidence_sha,
)
from tests.test_courses import course


def decision(verdict: str = "ready") -> ReadinessReviewDecision:
    return ReadinessReviewDecision.model_validate(
        {
            "verdict": verdict,
            "summary": "The course has enough verified context to execute.",
            "checks": [
                {
                    "category": category,
                    "status": "verified",
                    "finding": f"{category} is covered",
                    "evidence": [f"evidence:{category}"],
                }
                for category in REQUIRED_READINESS_CATEGORIES
            ],
            "requirements": [
                {
                    "key": "success",
                    "verified": True,
                    "finding": "Success is measurable.",
                    "evidence": ["README.md"],
                }
            ],
            "next_questions": [],
        }
    )


def result() -> HarnessResult:
    return HarnessResult(
        role="readiness_reviewer",
        output=decision().model_dump(mode="json"),
        attempts=(),
        resolved_model="frontier-reviewer",
        session_id="session-1",
    )


def models() -> ModelProfile:
    return ModelProfile(
        primary=ModelTarget(
            model="frontier-reviewer",
            provider="openai",
            thinking=ReasoningEffort.MEDIUM,
        ),
        allow_fallback=False,
        max_attempts=1,
    )


@pytest.mark.parametrize("kind", tuple(CourseKind))
def test_review_persists_provenance_and_unlocks_all_course_kinds(kind: CourseKind) -> None:
    value = course().model_copy(
        update={
            "kind": kind,
            "readiness": (
                course().readiness[0].model_copy(
                    update={
                        "status": RequirementStatus.ANSWERED,
                        "answer": "The ranked search flow meets its documented latency target.",
                    }
                ),
            ),
        }
    )

    reviewed = apply_readiness_review(
        value,
        decision(),
        result(),
        models(),
        provider="openclaw",
        reviewed_at=datetime(2026, 7, 15, tzinfo=UTC),
    )

    assert readiness_report(reviewed).ready is True
    assert reviewed.readiness[0].status == RequirementStatus.VERIFIED
    assert reviewed.readiness_review is not None
    assert reviewed.readiness_review.provider == "openai"
    assert reviewed.readiness_review.model == "frontier-reviewer"
    assert reviewed.readiness_review.reasoning == ReasoningEffort.MEDIUM
    assert reviewed.readiness_review.prompt_version == "course-readiness-v2"
    assert reviewed.readiness_review.source_evidence_sha == readiness_evidence_sha({})
    assert reviewed.readiness_review.session_id == "session-1"


def test_ready_verdict_rejects_an_incomplete_category_set() -> None:
    payload = decision().model_dump(mode="json")
    payload["checks"] = payload["checks"][:-1]
    incomplete = ReadinessReviewDecision.model_validate(payload)
    value = course().model_copy(
        update={
            "readiness": (
                course().readiness[0].model_copy(
                    update={"status": RequirementStatus.ANSWERED, "answer": "Known answer"}
                ),
            )
        }
    )

    with pytest.raises(ValueError, match="categories do not match policy"):
        apply_readiness_review(value, incomplete, result(), models(), provider="openclaw")


def test_prompt_includes_hash_bound_live_evidence_and_collection_guidance() -> None:
    evidence: dict[str, object] = {
        "github": {"default_branch_sha": "abc123", "collection_errors": {}}
    }

    prompt = build_readiness_prompt(course(), evidence)

    assert readiness_evidence_sha(evidence) in prompt
    assert '"default_branch_sha": "abc123"' in prompt
    assert "authenticated machine evidence" in prompt
    assert "do not treat successfully collected facts as locally unverifiable" in prompt
    assert "greenfield course" in prompt
    assert "repository_lifecycle.provisioning_enabled" in prompt
