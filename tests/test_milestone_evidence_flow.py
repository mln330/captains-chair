from __future__ import annotations

from pathlib import Path
from typing import Any

import make_it_so.sidecar as sidecar
from make_it_so.completion_gate import GitHubCompletionValidator
from make_it_so.courses import CourseStore
from make_it_so.evidence import validate_test_evidence
from make_it_so.models import (
    ActionKind,
    CompletionPolicy,
    Course,
    PullRequestGate,
    RepoConfig,
)
from make_it_so.models import (
    TestEvidencePolicy as EvidencePolicy,
)
from make_it_so.orchestration import (
    QueueCard,
    QueueStatus,
    WorkflowOrchestrator,
    build_workflow,
)
from tests.fakes import InMemoryWorkQueue
from tests.helpers import repo_config
from tests.test_courses import course
from tests.test_orchestration import worker_config

HEAD = "abcdef1234567"
_milestone_rows = sidecar._milestone_rows  # pyright: ignore[reportPrivateUsage]


def policy() -> EvidencePolicy:
    return EvidencePolicy(
        minimum_pass_rate=100.0,
        require_command=True,
        require_screenshot=True,
        minimum_screenshots=1,
    )


def test_evidence_validator_fails_closed_then_accepts_complete_proof() -> None:
    requirement = policy()
    missing = validate_test_evidence({}, requirement, HEAD)
    assert missing.allowed is False
    assert missing.summary["status"] == "missing"

    stale = validate_test_evidence(
        {
            "test_evidence": {
                "status": "passed",
                "head_sha": "deadbeef",
                "tests_total": 4,
                "tests_passed": 4,
                "tests_failed": 0,
            }
        },
        requirement,
        HEAD,
    )
    assert stale.allowed is False
    assert stale.summary["status"] == "stale"

    failed = validate_test_evidence(
        {
            "test_evidence": {
                "status": "passed",
                "head_sha": HEAD,
                "command": "pytest",
                "tests_total": 4,
                "tests_passed": 3,
                "tests_failed": 1,
            }
        },
        requirement,
        HEAD,
    )
    assert failed.allowed is False
    assert "below" in failed.reason

    valid = validate_test_evidence(
        {
            "test_evidence": {
                "status": "passed",
                "head_sha": HEAD,
                "commands": ["pytest -q", "npm run test:e2e"],
                "tests_total": 8,
                "tests_passed": 8,
                "tests_failed": 0,
                "tests_skipped": 0,
                "pass_rate": 100,
                "screenshots": [
                    {
                        "kind": "screenshot",
                        "title": "desktop flow",
                        "url": "https://example.test/evidence/desktop.png",
                    }
                ],
            }
        },
        requirement,
        HEAD,
    )
    assert valid.allowed is True
    assert valid.summary["screenshots"][0]["url"].endswith("desktop.png")


def _course_with_evidence_policy() -> Course:
    value = course()
    packages = tuple(
        package.model_copy(update={"test_evidence_policy": policy()}) if package.key == "ui" else package
        for package in value.work_packages
    )
    return value.model_copy(update={"work_packages": packages})


def _proof(*, include_screenshot: bool = True, head_sha: str = HEAD, passed: int = 8) -> list[dict[str, Any]]:
    screenshots = (
        [
            {
                "kind": "screenshot",
                "title": "mobile flow",
                "url": "https://example.test/evidence/mobile.png",
            }
        ]
        if include_screenshot
        else []
    )
    return [
        {
            "status": "passed",
            "note": "test evidence captured",
            "url": None,
            "model": "codex/gpt-5.6-luna",
            "provider": "codex",
            "evidence": ["targeted checks passed"],
            "test_evidence": {
                "status": "passed",
                "head_sha": head_sha,
                "commands": ["pytest -q"],
                "tests_total": 8,
                "tests_passed": passed,
                "tests_failed": 8 - passed,
                "tests_skipped": 0,
                "pass_rate": passed * 100 / 8,
                "screenshots": screenshots,
                "artifacts": screenshots,
            },
        }
    ]


def test_mock_flow_threads_policy_and_dashboard_evidence(tmp_path: Path) -> None:
    repo = repo_config(tmp_path).model_copy(update={"completion_policy": CompletionPolicy.OWNER_APPROVAL})
    CourseStore(repo.local_path).save(_course_with_evidence_policy())
    from make_it_so.models import PlanDecision

    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement the search flow",
        reason="The next planned milestone is ready.",
        course_key="feature-search",
        work_package_key="ui",
        changed_paths=("frontend/SearchResults.tsx",),
    )
    workflow = build_workflow(repo, decision, "mock-evidence-flow", worker_config())
    qa_card = next(card for card in workflow.stages if "stage:ux_review" in card.labels)
    assert qa_card.metadata["testEvidenceRequired"] is True
    assert qa_card.metadata["testEvidenceScreenshotRequired"] is True
    assert "Milestone test-evidence contract" in qa_card.notes

    card = QueueCard(
        id="qa-card",
        title="UI QA",
        status=QueueStatus.DONE,
        labels=("workflow:mock-evidence-flow", "stage:ux_review"),
        source_url="https://github.com/example/project/pull/42",
        metadata={
            "courseKey": "feature-search",
            "workPackageKey": "ui",
            "discoveredHeadSha": HEAD,
            "testEvidenceRequired": True,
            "testEvidencePolicy": policy().model_dump(mode="json"),
            "testEvidenceScreenshotRequired": True,
            "proof": _proof(),
        },
    )
    rows = _milestone_rows(repo, [card])
    row = next(item for item in rows if item["work_package_key"] == "ui")
    assert row["evidence"]["status"] == "passed"
    assert row["evidence"]["pass_rate"] == 100.0
    assert len(row["evidence"]["screenshots"]) == 1


class _GateProvider:
    def gate(self, repo: RepoConfig, number: int, review_head_sha: str | None) -> PullRequestGate:
        del repo, number, review_head_sha
        return PullRequestGate(
            number=42,
            head_sha=HEAD,
            mergeable=True,
            merge_state="CLEAN",
            draft=False,
            checks_green=True,
            required_checks=(),
            unresolved_threads=0,
            review_head_sha=HEAD,
        )


def test_final_review_requires_milestone_evidence_in_mock_flow(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, completion=CompletionPolicy.OWNER_APPROVAL)
    validator = GitHubCompletionValidator(_GateProvider())
    metadata = {
        "courseKey": "feature-search",
        "workPackageKey": "ui",
        "testEvidenceRequired": True,
        "testEvidencePolicy": policy().model_dump(mode="json"),
        "testEvidenceScreenshotRequired": True,
    }
    implementation = QueueCard(
        id="implementation",
        title="Implementation",
        status=QueueStatus.DONE,
        labels=("workflow:mock-final", "stage:implementation"),
        source_url="https://github.com/example/project/pull/42",
        metadata={"proof": [{"status": "passed", "note": "PR opened"}]},
    )
    test_card = QueueCard(
        id="test",
        title="Test",
        status=QueueStatus.DONE,
        labels=("workflow:mock-final", "stage:test"),
        source_url="https://github.com/example/project/pull/42",
        metadata={**metadata, "proof": _proof(include_screenshot=False)},
    )
    final = QueueCard(
        id="final",
        title="Final review",
        status=QueueStatus.DONE,
        labels=("workflow:mock-final", "stage:final_review"),
        source_url="https://github.com/example/project/pull/42",
        metadata={**metadata, "proof": [{"status": "passed", "note": f"READY_FOR_OWNER:{HEAD}"}]},
    )
    blocked = validator.validate(repo, final, [implementation, test_card, final])
    assert blocked.allowed is False
    assert "lacks current-head test evidence" in blocked.reason

    test_card = test_card.model_copy(update={"metadata": {**metadata, "proof": _proof()}})
    passed = validator.validate(repo, final, [implementation, test_card, final])
    assert passed.allowed is True


def test_orchestrator_retries_evidence_required_test_card_without_qa_profile(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = InMemoryWorkQueue()
    card = QueueCard(
        id="test-card",
        title="Run milestone checks",
        status=QueueStatus.REVIEW,
        labels=("workflow:mock-orchestrator", "stage:test"),
        source_url="https://github.com/example/project/pull/42",
        metadata={
            "discoveredHeadSha": HEAD,
            "testEvidenceRequired": True,
            "testEvidencePolicy": policy().model_dump(mode="json"),
            "testEvidenceScreenshotRequired": True,
            "proof": _proof(include_screenshot=False),
        },
    )
    queue.cards[card.id] = card

    orchestrator = WorkflowOrchestrator(
        queue,
        worker_config(),
        completion_validator=GitHubCompletionValidator(_GateProvider()),
    )
    result = orchestrator.reconcile(repo, dispatch=False, dispatch_reason="evidence test")

    assert result.protocol_retries
    retries = [item for item in queue.cards.values() if item.id != card.id and "retry" in item.title.lower()]
    assert retries
    assert "needs at least 1 screenshot" in (retries[0].notes or "")
