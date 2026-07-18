from __future__ import annotations

from pathlib import Path

import pytest

import make_it_so.engine as engine
from make_it_so.models import (
    ActionKind,
    CommentDisposition,
    CommentTriage,
    FinalReview,
    FinalVerdict,
    Finding,
    IndependentReview,
    PlanDecision,
    ReviewCommentDecision,
    ReviewVerdict,
    UXReview,
    WorkerResult,
)
from make_it_so.worktrees import Worktree
from tests.helpers import repo_config


def finding() -> Finding:
    return Finding(
        priority="P1",
        title="Missing authorization check",
        detail="The protected path is reachable without the expected check.",
        path="src/auth.py",
        line=42,
    )


def decision() -> PlanDecision:
    return PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement authorization",
        reason="The approved work package requires the protected path.",
        work_item_id="auth-1",
        acceptance_criteria=("Unauthorized users are rejected",),
    )


def test_engine_evidence_and_check_helpers(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    worktree = Worktree(
        path=tmp_path / "worktree",
        branch="make_it_so/work/auth-1",
        base="origin/main",
        push_branch="make_it_so/work/auth-1",
    )
    assert engine._fingerprint({"b": 2, "a": 1}) == engine._fingerprint({"a": 1, "b": 2})  # pyright: ignore[reportPrivateUsage]
    assert engine._pr_link(repo, 42) == "https://github.com/example/project/pull/42"  # pyright: ignore[reportPrivateUsage]
    assert engine._pr_link(repo, None) is None  # pyright: ignore[reportPrivateUsage]
    assert engine.worktree_check_command("pytest src", tmp_path, worktree.path) == ["pytest", "src"]
    assert engine.select_checks(("pytest", "npm test", "frontend-check"), ["src/a.py"], ("frontend/", "*.tsx")) == ("pytest",)
    assert engine.select_checks(("pytest", "npm test"), ["frontend/App.tsx"], ("frontend/", "*.tsx")) == ("pytest", "npm test")
    assert engine._failed_check_summary(  # pyright: ignore[reportPrivateUsage]
        [{"command": "pytest", "returncode": 1, "stderr_tail": "  failed\n test"}]
    ) == "pytest exited 1: failed test"
    assert engine._next_action(  # pyright: ignore[reportPrivateUsage]
        decision(), "requires owner"
    ) == "Implement authorization (requires owner)"


def test_worker_result_validation_and_blocker_classification() -> None:
    result = engine._worker_result_or_raise(  # pyright: ignore[reportPrivateUsage]
        WorkerResult(summary="done", changed_files=("src/auth.py",)).model_dump(mode="json")
    )
    assert result.summary == "done"
    with pytest.raises(engine.WorkerBlockedError, match="USER_SECRET"):
        engine._worker_result_or_raise(  # pyright: ignore[reportPrivateUsage]
            WorkerResult(summary="blocked", blocked=True, blocker="USER_SECRET: provide key").model_dump(mode="json")
        )
    with pytest.raises(engine.WorkerBlockedError, match="TECHNICAL"):
        engine._worker_result_or_raise(  # pyright: ignore[reportPrivateUsage]
            WorkerResult(summary="blocked", blocked=True).model_dump(mode="json")
        )


def test_worker_and_review_comments_include_evidence() -> None:
    repo = repo_config(Path("."))
    prompt = engine._worker_prompt(  # pyright: ignore[reportPrivateUsage]
        repo,
        decision(),
        Worktree(Path("work"), "make_it_so/work/auth-1", "origin/main", "make_it_so/work/auth-1"),
    )
    assert "make_it_so/work/auth-1" in prompt
    body = engine._pull_request_body(  # pyright: ignore[reportPrivateUsage]
        repo,
        decision(),
        ["src/auth.py"],
        [{"command": "pytest", "returncode": 0}],
        "gpt-5.3-codex-spark",
    )
    assert "Model: gpt-5.3-codex-spark" in body
    assert "Unauthorized users are rejected" in body

    review = IndependentReview(
        verdict=ReviewVerdict.REQUEST_CHANGES,
        summary="Please address the authorization issue.",
        findings=(finding(),),
        residual_risks=("The route needs a regression test.",),
    )
    comment = engine._review_comment("Independent review", "head-1", review)  # pyright: ignore[reportPrivateUsage]
    assert "src/auth.py:42" in comment
    assert "Residual risks" in comment

    triage = CommentTriage(
        head_sha="head-1",
        verdict=ReviewVerdict.REQUEST_CHANGES,
        summary="One comment should be addressed.",
        decisions=(
            ReviewCommentDecision(
                thread_id="thread-1",
                disposition=CommentDisposition.ADDRESS,
                rationale="It identifies a real gap.",
            ),
        ),
        accepted_findings=(finding(),),
        owner_decisions=("Confirm the migration window.",),
    )
    triage_comment = engine._comment_triage_comment("Comment triage", "head-1", triage)  # pyright: ignore[reportPrivateUsage]
    assert "thread-1" in triage_comment
    assert "Owner decisions" in triage_comment

    ux = UXReview(
        verdict=ReviewVerdict.PASS,
        summary="The primary flow is coherent.",
        findings=(finding(),),
        flows_tested=("Sign in",),
        contrast_passed=True,
        functionality_passed=True,
        cohesion_passed=False,
    )
    ux_comment = engine._ux_review_comment("UX review", "head-1", ux)  # pyright: ignore[reportPrivateUsage]
    assert "Contrast passed: True" in ux_comment
    assert "Sign in" in ux_comment

    final = FinalReview(
        verdict=FinalVerdict.READY_FOR_OWNER,
        summary="The package meets its acceptance criteria.",
        scope_match=True,
        checks_green=True,
        unresolved_threads=0,
        residual_risks=("Deployment remains separate.",),
        owner_blocker="USER_SECRET: configure production key",
    )
    final_comment = engine._final_review_comment("Final review", "head-1", final)  # pyright: ignore[reportPrivateUsage]
    assert "Verdict: `READY_FOR_OWNER`" in final_comment
    assert "USER_SECRET" in final_comment


@pytest.mark.parametrize(
    ("runs", "deploy_is_gate", "status", "message"),
    (
        ([], False, "waiting", "missing or still running"),
        ([{"headSha": "head", "workflowName": "CI", "status": "completed", "conclusion": "failure"}], False, "failed", "CI"),
        ([{"headSha": "head", "workflowName": "CI", "status": "completed", "conclusion": "success"}], False, "passed", "All required"),
        (
            [
                {"headSha": "head", "workflowName": "CI", "status": "completed", "conclusion": "success"},
                {"headSha": "head", "workflowName": "Deploy", "status": "completed", "conclusion": "failure"},
            ],
            False,
            "passed",
            "deployment failed",
        ),
        (
            [
                {"headSha": "head", "workflowName": "CI", "status": "completed", "conclusion": "success"},
                {"headSha": "head", "workflowName": "Deploy", "status": "completed", "conclusion": "failure"},
            ],
            True,
            "failed",
            "Deploy",
        ),
    ),
)
def test_post_merge_workflow_classification(
    runs: list[dict[str, object]],
    deploy_is_gate: bool,
    status: str,
    message: str,
) -> None:
    actual, reason, links = engine.classify_post_merge_runs(runs, "head", deploy_is_gate)
    assert actual == status
    assert message.lower() in reason.lower()
    assert links == []
