from pathlib import Path
from typing import Any, TypeVar, cast

import pytest
from pydantic import BaseModel

from captains_chair.engine import ControlPlaneEngine, WorkerBlockedError
from captains_chair.github import GhGitHubProvider, RepositorySnapshot
from captains_chair.harness import HarnessAdapter
from captains_chair.models import (
    ActionKind,
    CompletionPolicy,
    EventRecord,
    FinalReview,
    FinalVerdict,
    HarnessConfig,
    IndependentReview,
    ModelTarget,
    OperationMode,
    PlanDecision,
    PullRequestGate,
    ReviewVerdict,
    RunState,
    UXReview,
)
from captains_chair.notifications import NotificationError, Notifier
from captains_chair.orchestration import EnqueuedWorkflow
from captains_chair.state import StateStore
from captains_chair.worktrees import Worktree
from tests.helpers import app_config, model_policy, repo_config

OutputModel = TypeVar("OutputModel", bound=BaseModel)


class ActivePrGitHub:
    def __init__(self, *, checks_green: bool = True) -> None:
        self.calls: list[str] = []
        self.merged: list[int] = []
        self.head_sha = "head-1"
        self.checks_green = checks_green
        self.files: list[dict[str, str]] = []

    def snapshot(self, repo: object) -> RepositorySnapshot:
        del repo
        self.calls.append("snapshot")
        return RepositorySnapshot(
            repo={"nameWithOwner": "example/project"},
            issues=[],
            pull_requests=[{"number": 35, "headRefOid": self.head_sha}],
            branches=["main", "captains_chair/docs/plan"],
            workflow_runs=[],
        )

    def pull_request(self, repo: object, number: int) -> dict[str, Any]:
        del repo
        self.calls.append(f"pull_request:{number}")
        return {
            "number": number,
            "url": f"https://github.test/example/project/pull/{number}",
            "headRefName": "captains_chair/docs/plan",
            "headRefOid": self.head_sha,
            "baseRefName": "main",
            "isDraft": True,
            "files": self.files,
        }

    def gate(self, repo: object, number: int, review_head_sha: str | None) -> PullRequestGate:
        del repo
        self.calls.append(f"gate:{number}:{review_head_sha}")
        return PullRequestGate(
            number=number,
            head_sha=self.head_sha,
            mergeable=True,
            merge_state="CLEAN",
            draft=False,
            checks_green=self.checks_green,
            required_checks=(),
            unresolved_threads=0,
            review_head_sha=review_head_sha,
        )

    def pull_request_diff(self, repo: object, number: int) -> str:
        del repo
        self.calls.append(f"diff:{number}")
        return "diff --git a/ISSUES_EXECUTION_PLAN.md b/ISSUES_EXECUTION_PLAN.md"

    def review_threads(self, repo: object, number: int) -> list[dict[str, Any]]:
        del repo
        self.calls.append(f"threads:{number}")
        return []

    def mark_ready(self, repo: object, number: int) -> None:
        del repo
        self.calls.append(f"ready:{number}")

    def comment_pull_request(self, repo: object, number: int, body: str) -> None:
        del repo, body
        self.calls.append(f"comment:{number}")

    def merge(self, repo: object, number: int) -> None:
        del repo
        self.calls.append(f"merge:{number}")
        self.merged.append(number)

    def default_branch_sha(self, repo: object) -> str:
        del repo
        self.calls.append("default_branch_sha")
        return "merged-main-head"


class ReviewHarness(HarnessAdapter):
    def __init__(
        self,
        owner_blocker: str | None = None,
        final_verdict: FinalVerdict = FinalVerdict.AUTO_MERGE_ALLOWED,
        review_verdict: ReviewVerdict = ReviewVerdict.PASS,
        ux_review_verdict: ReviewVerdict = ReviewVerdict.PASS,
    ) -> None:
        super().__init__(HarnessConfig(kind="codex", executable="codex"))
        self.roles: list[str] = []
        self.owner_blocker = owner_blocker
        self.final_verdict = final_verdict
        self.review_verdict = review_verdict
        self.ux_review_verdict = ux_review_verdict

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
    ) -> dict[str, Any]:
        del prompt, model, output_model, cwd, writable, session_id
        self.roles.append(role)
        if role == "independent-review":
            return IndependentReview(
                verdict=self.review_verdict,
                summary=(
                    "No blocking findings."
                    if self.review_verdict == ReviewVerdict.PASS
                    else "The current head needs a repair."
                ),
                residual_risks=()
                if self.review_verdict == ReviewVerdict.PASS
                else ("The authorization assertion is missing.",),
            ).model_dump(mode="json")
        if role == "final-review":
            return FinalReview(
                verdict=self.final_verdict,
                summary="Ready to merge.",
                scope_match=True,
                checks_green=True,
                unresolved_threads=0,
                owner_blocker=self.owner_blocker,
            ).model_dump(mode="json")
        if role == "ux-review":
            return UXReview(
                verdict=self.ux_review_verdict,
                summary=(
                    "The primary UI flow is usable."
                    if self.ux_review_verdict == ReviewVerdict.PASS
                    else "The UI flow needs a repair."
                ),
                contrast_passed=self.ux_review_verdict == ReviewVerdict.PASS,
                functionality_passed=self.ux_review_verdict == ReviewVerdict.PASS,
                cohesion_passed=self.ux_review_verdict == ReviewVerdict.PASS,
                flows_tested=("sign-in", "workspace navigation"),
            ).model_dump(mode="json")
        raise AssertionError(f"unexpected role: {role}")


class FailingUxReviewHarness(ReviewHarness):
    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("role") == "ux-review":
            raise RuntimeError("UX browser process crashed")
        return super().invoke(**kwargs)


class MemoryNotifier:
    def __init__(self) -> None:
        self.events: list[EventRecord] = []

    def send(self, event: EventRecord) -> None:
        self.events.append(event)


class FailOnceNotifier(MemoryNotifier):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def send(self, event: EventRecord) -> None:
        if not self.failed:
            self.failed = True
            raise NotificationError("Discord route unavailable")
        super().send(event)


class ExistingWorkboardWorkflow:
    def has_active_workflow(self, repo: object, decision: PlanDecision) -> bool:
        del repo, decision
        return True

    def enqueue(
        self,
        repo: object,
        decision: PlanDecision,
        action_id: str,
        *,
        workspace: object = None,
    ) -> object:
        del repo, decision, action_id, workspace
        raise AssertionError("an existing Workboard workflow must be reused")


class QueueingWorkboardWorkflow:
    def __init__(self, error: Exception | None = None, *, persist_after_error: bool = False) -> None:
        self.workspaces: list[object] = []
        self.error = error
        self.persist_after_error = persist_after_error
        self.persisted = False

    def has_active_workflow(self, repo: object, decision: PlanDecision) -> bool:
        del repo, decision
        return self.persisted

    def enqueue(
        self,
        repo: object,
        decision: PlanDecision,
        action_id: str,
        *,
        workspace: object = None,
    ) -> EnqueuedWorkflow:
        del repo, decision
        if self.error is not None:
            self.persisted = self.persist_after_error
            raise self.error
        self.workspaces.append(workspace)
        return EnqueuedWorkflow(
            workflow_id=action_id,
            board_id="captains-chair-example-project",
            root_card_id="root-card",
            stage_cards={"review": "review-card"},
        )


class ReviewWorktrees:
    def __init__(self, root: Path, error: Exception | None = None) -> None:
        self.root = root
        self.error = error
        self.calls: list[tuple[str, str, str]] = []
        self.removed: list[Path] = []
        self.discarded: list[Path] = []

    def checkout_existing(
        self,
        repo: object,
        work_id: str,
        remote_branch: str,
        *,
        lane: str = "repair",
    ) -> Worktree:
        del repo
        self.calls.append((work_id, remote_branch, lane))
        if self.error is not None:
            raise self.error
        return Worktree(
            path=self.root,
            branch=f"captains_chair/{lane}/{work_id}",
            base="origin/main",
            push_branch=remote_branch,
        )

    def remove(self, repo: object, worktree: Worktree) -> bool:
        del repo
        self.removed.append(worktree.path)
        return True

    def discard(self, repo: object, worktree: Worktree) -> bool:
        del repo
        self.discarded.append(worktree.path)
        return True


class UxCleanupFallbackWorktrees(ReviewWorktrees):
    def remove(self, repo: object, worktree: Worktree) -> bool:
        del repo, worktree
        raise RuntimeError("clean UX worktree removal failed")


def test_active_pr_continues_to_review_and_auto_merge_without_replanning(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Durable plan\n", encoding="utf-8")
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", tmp_path / "baseline.json", analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    decision = PlanDecision(
        action=ActionKind.UPDATE_PLAN,
        summary="Update durable plan",
        reason="The plan needs the current implementation path.",
    )
    state.save_active_work(
        repo.full_name,
        action_id="action-1",
        pr_number=35,
        branch="captains_chair/docs/plan",
        head_sha="head-1",
        status="pr_open",
        decision=decision.model_dump(mode="json"),
    )
    state.transition(repo.full_name, RunState.PR_OPEN)
    github = ActivePrGitHub()
    harness = ReviewHarness()
    notifier = MemoryNotifier()
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        harness,
        cast(Notifier, notifier),
        model_policy(),
    )

    result = engine.cycle(repo, shadow=False, execute=True)

    assert result.event.event_type == "PR_MERGED"
    assert result.event.evidence["merged_head_sha"] == "merged-main-head"
    assert result.event.evidence["pr_head_sha"] == "head-1"
    assert github.merged == [35]
    assert harness.roles == ["independent-review", "final-review"]
    assert state.active_work(repo.full_name) is None
    assert all(event.event_type != "ACTION_PROPOSED" for event in notifier.events)


def test_ux_review_failure_preserves_original_error_and_discards_disposable_worktree(
    tmp_path: Path,
) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Durable plan\n", encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", tmp_path / "baseline.json", analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement the frontend authorization flow",
        reason="The documented UI slice is ready for review.",
        target_issue=39,
    )
    state.save_active_work(
        repo.full_name,
        action_id="action-ux-failure",
        pr_number=35,
        branch="captains_chair/work/39",
        head_sha="head-1",
        status="pr_open",
        decision=decision.model_dump(mode="json"),
    )
    state.transition(repo.full_name, RunState.PR_OPEN)
    github = ActivePrGitHub()
    github.files = [{"path": "frontend/App.tsx"}]
    worktrees = ReviewWorktrees(tmp_path / "ux-failed")
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        FailingUxReviewHarness(),
        cast(Notifier, MemoryNotifier()),
        model_policy(),
    )
    engine.worktrees = cast(Any, worktrees)

    with pytest.raises(RuntimeError, match="UX browser process crashed"):
        engine.cycle(repo, shadow=False, execute=True)

    assert worktrees.discarded == [tmp_path / "ux-failed"]
    assert worktrees.removed == []


def test_ux_review_cleanup_fallback_is_visible_and_does_not_block_completion(
    tmp_path: Path,
) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Durable plan\n", encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", tmp_path / "baseline.json", analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement the frontend authorization flow",
        reason="The documented UI slice is ready for review.",
        target_issue=39,
    )
    state.save_active_work(
        repo.full_name,
        action_id="action-ux-cleanup",
        pr_number=35,
        branch="captains_chair/work/39",
        head_sha="head-1",
        status="pr_open",
        decision=decision.model_dump(mode="json"),
    )
    state.transition(repo.full_name, RunState.PR_OPEN)
    github = ActivePrGitHub()
    github.files = [{"path": "frontend/App.tsx"}]
    worktrees = UxCleanupFallbackWorktrees(tmp_path / "ux-cleanup")
    harness = ReviewHarness()
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        harness,
        cast(Notifier, MemoryNotifier()),
        model_policy(),
    )
    engine.worktrees = cast(Any, worktrees)

    result = engine.cycle(repo, shadow=False, execute=True)

    assert result.event.event_type == "COMPLETION_READY"
    assert "ux_worktree_cleanup_warning" in result.event.evidence
    assert worktrees.discarded == [tmp_path / "ux-cleanup"]
    assert harness.roles == ["independent-review", "ux-review", "final-review"]


def test_active_pr_with_workboard_owner_does_not_direct_review_or_merge(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Durable plan\n", encoding="utf-8")
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    artifact = tmp_path / "baseline.json"
    artifact.write_text("{}", encoding="utf-8")
    state.save_baseline(repo.full_name, "baseline", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement Etsy routes",
        reason="The implementation worker owns this PR.",
        target_issue=39,
    )
    state.save_active_work(
        repo.full_name,
        action_id="action-1",
        pr_number=35,
        branch="captains_chair/work/39",
        head_sha="head-1",
        status="pr_open",
        decision=decision.model_dump(mode="json"),
    )
    state.transition(repo.full_name, RunState.PR_OPEN)
    github = ActivePrGitHub()
    harness = ReviewHarness()
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        harness,
        cast(Notifier, MemoryNotifier()),
        model_policy(),
        orchestrator=cast(Any, ExistingWorkboardWorkflow()),
    )

    result = engine.cycle(repo, shadow=False, execute=True)

    assert result.event.event_type == "WORKFLOW_ALREADY_QUEUED"
    assert harness.roles == []
    assert github.merged == []
    assert state.current_state(repo.full_name) == RunState.REVIEWING


def test_active_pr_workboard_review_uses_isolated_current_head_workspace(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Durable plan\n", encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", tmp_path / "baseline.json", analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement the authorization slice",
        reason="The implementation worker owns this PR.",
        target_issue=39,
    )
    state.save_active_work(
        repo.full_name,
        action_id="action-1",
        pr_number=35,
        branch="captains_chair/work/39",
        head_sha="head-1",
        status="pr_open",
        decision=decision.model_dump(mode="json"),
    )
    state.transition(repo.full_name, RunState.PR_OPEN)
    queue = QueueingWorkboardWorkflow()
    worktrees = ReviewWorktrees(tmp_path / "review-worktree")
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, ActivePrGitHub()),
        ReviewHarness(),
        cast(Notifier, MemoryNotifier()),
        model_policy(),
        orchestrator=cast(Any, queue),
    )
    engine.worktrees = cast(Any, worktrees)

    result = engine.cycle(repo, shadow=False, execute=True)

    assert result.event.event_type == "WORKFLOW_QUEUED"
    assert worktrees.calls == [("pr-35-head-1", "captains_chair/docs/plan", "review")]
    assert len(queue.workspaces) == 1
    workspace = queue.workspaces[0]
    assert workspace is not None
    assert cast(Any, workspace).branch == "captains_chair/review/pr-35-head-1"
    assert cast(Any, workspace).push_branch == "captains_chair/docs/plan"


def test_active_pr_review_workspace_failure_preserves_pr_open_for_retry(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Durable plan\n", encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", tmp_path / "baseline.json", analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement the authorization slice",
        reason="The implementation worker owns this PR.",
        target_issue=39,
    )
    state.save_active_work(
        repo.full_name,
        action_id="action-1",
        pr_number=35,
        branch="captains_chair/work/39",
        head_sha="head-1",
        status="pr_open",
        decision=decision.model_dump(mode="json"),
    )
    state.transition(repo.full_name, RunState.PR_OPEN)
    queue = QueueingWorkboardWorkflow()
    worktrees = ReviewWorktrees(tmp_path / "review-worktree", RuntimeError("worktree unavailable"))
    notifier = MemoryNotifier()
    harness = ReviewHarness()
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, ActivePrGitHub()),
        harness,
        cast(Notifier, notifier),
        model_policy(),
        orchestrator=cast(Any, queue),
    )
    engine.worktrees = cast(Any, worktrees)

    result = engine.cycle(repo, shadow=False, execute=True)

    assert result.event.event_type == "WORKFLOW_QUEUE_FAILED"
    assert result.event.evidence["next_action"].startswith("Repair the isolated PR workspace")
    assert state.current_state(repo.full_name) == RunState.PR_OPEN
    assert queue.workspaces == []
    assert harness.roles == []
    assert not any(event.event_type == "ATTENTION_REQUIRED" for event in notifier.events)


@pytest.mark.parametrize("persist_after_error", (False, True))
def test_active_pr_queue_failure_handles_review_workspace_ownership(
    tmp_path: Path,
    persist_after_error: bool,
) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Durable plan\n", encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", tmp_path / "baseline.json", analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement the authorization slice",
        reason="The implementation worker owns this PR.",
        target_issue=39,
    )
    state.save_active_work(
        repo.full_name,
        action_id="action-1",
        pr_number=35,
        branch="captains_chair/work/39",
        head_sha="head-1",
        status="pr_open",
        decision=decision.model_dump(mode="json"),
    )
    state.transition(repo.full_name, RunState.PR_OPEN)
    queue = QueueingWorkboardWorkflow(
        RuntimeError("gateway unavailable"),
        persist_after_error=persist_after_error,
    )
    worktrees = ReviewWorktrees(tmp_path / "review-worktree")
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, ActivePrGitHub()),
        ReviewHarness(),
        cast(Notifier, MemoryNotifier()),
        model_policy(),
        orchestrator=cast(Any, queue),
    )
    engine.worktrees = cast(Any, worktrees)

    result = engine.cycle(repo, shadow=False, execute=True)

    assert result.event.event_type == "WORKFLOW_QUEUE_FAILED"
    assert worktrees.removed == ([] if persist_after_error else [tmp_path / "review-worktree"])
    assert state.current_state(repo.full_name) == RunState.PR_OPEN


def test_active_pr_waits_for_checks_without_spending_review_tokens(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Durable plan\n", encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", tmp_path / "baseline.json", analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement the authorization slice",
        reason="The implementation worker opened a PR.",
        target_issue=39,
    )
    state.save_active_work(
        repo.full_name,
        action_id="action-1",
        pr_number=35,
        branch="captains_chair/work/39",
        head_sha="head-1",
        status="pr_open",
        decision=decision.model_dump(mode="json"),
    )
    state.transition(repo.full_name, RunState.PR_OPEN)
    github = ActivePrGitHub(checks_green=False)
    harness = ReviewHarness()
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        harness,
        cast(Notifier, MemoryNotifier()),
        model_policy(),
    )

    first = engine.cycle(repo, shadow=False, execute=True)
    second = engine.cycle(repo, shadow=False, execute=True)

    assert first.event.event_type == "PR_CHECKS_WAITING"
    assert first.exit_code == 2
    assert second.event.event_id == first.event.event_id
    assert harness.roles == []
    assert github.merged == []
    assert state.current_state(repo.full_name) == RunState.PR_OPEN


def test_autonomous_final_review_escalates_only_explicit_goal_divergence(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Durable plan\n", encoding="utf-8")
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", tmp_path / "baseline.json", analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    decision = PlanDecision(
        action=ActionKind.UPDATE_PLAN,
        summary="Update durable plan",
        reason="The plan needs the current implementation path.",
    )
    state.save_active_work(
        repo.full_name,
        action_id="action-1",
        pr_number=35,
        branch="captains_chair/docs/plan",
        head_sha="head-1",
        status="pr_open",
        decision=decision.model_dump(mode="json"),
    )
    state.transition(repo.full_name, RunState.PR_OPEN)
    github = ActivePrGitHub()
    harness = ReviewHarness(owner_blocker="GOAL_DIVERGENCE: the requested change conflicts with the approved roadmap")
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        harness,
        cast(Notifier, MemoryNotifier()),
        model_policy(),
    )

    result = engine.cycle(repo, shadow=False, execute=True)

    assert result.event.event_type == "ATTENTION_REQUIRED"
    assert result.event.evidence["blocker_kind"] == "goal_divergence"
    assert state.current_state(repo.full_name) == RunState.BLOCKED
    assert github.merged == []


def test_active_pr_reuses_completion_wait_without_repeating_reviews(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Durable plan\n", encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", tmp_path / "baseline.json", analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    decision = PlanDecision(
        action=ActionKind.UPDATE_PLAN,
        summary="Update durable plan",
        reason="The plan needs the current implementation path.",
    )
    state.save_active_work(
        repo.full_name,
        action_id="action-1",
        pr_number=35,
        branch="captains_chair/docs/plan",
        head_sha="head-1",
        status="pr_open",
        decision=decision.model_dump(mode="json"),
    )
    state.transition(repo.full_name, RunState.PR_OPEN)
    github = ActivePrGitHub()
    harness = ReviewHarness(final_verdict=FinalVerdict.READY_FOR_OWNER)
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        harness,
        cast(Notifier, MemoryNotifier()),
        model_policy(),
    )

    first = engine.cycle(repo, shadow=False, execute=True)
    second = engine.cycle(repo, shadow=False, execute=True)

    assert first.event.event_type == "COMPLETION_READY"
    assert second.event.event_type == "COMPLETION_READY"
    assert second.exit_code == 0
    assert harness.roles == ["independent-review", "final-review"]

    github.head_sha = "head-2"
    third = engine.cycle(repo, shadow=False, execute=True)

    assert third.event.event_type == "COMPLETION_READY"
    assert harness.roles == [
        "independent-review",
        "final-review",
        "independent-review",
        "final-review",
    ]


def test_notification_failure_does_not_repeat_active_pr_review_work(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Durable plan\n", encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", tmp_path / "baseline.json", analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    decision = PlanDecision(
        action=ActionKind.UPDATE_PLAN,
        summary="Update durable plan",
        reason="The plan needs the current implementation path.",
    )
    state.save_active_work(
        repo.full_name,
        action_id="action-1",
        pr_number=35,
        branch="captains_chair/docs/plan",
        head_sha="head-1",
        status="pr_open",
        decision=decision.model_dump(mode="json"),
    )
    state.transition(repo.full_name, RunState.PR_OPEN)
    github = ActivePrGitHub()
    harness = ReviewHarness(final_verdict=FinalVerdict.READY_FOR_OWNER)
    notifier = FailOnceNotifier()
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        harness,
        cast(Notifier, notifier),
        model_policy(),
    )

    with pytest.raises(NotificationError, match="Discord route unavailable"):
        engine.cycle(repo, shadow=False, execute=True)

    second = engine.cycle(repo, shadow=False, execute=True)

    assert second.event.event_type == "COMPLETION_READY"
    assert harness.roles == ["independent-review", "final-review"]


def test_active_pr_review_findings_are_repaired_without_owner_attention(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Durable plan\n", encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", tmp_path / "baseline.json", analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement the authorization slice",
        reason="The documented work item is ready for review.",
        target_issue=39,
    )
    state.save_active_work(
        repo.full_name,
        action_id="action-1",
        pr_number=35,
        branch="captains_chair/work/39",
        head_sha="head-1",
        status="pr_open",
        decision=decision.model_dump(mode="json"),
    )
    state.transition(repo.full_name, RunState.PR_OPEN)
    github = ActivePrGitHub()
    harness = ReviewHarness(review_verdict=ReviewVerdict.REQUEST_CHANGES)
    notifier = MemoryNotifier()
    result = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        harness,
        cast(Notifier, notifier),
        model_policy(),
    ).cycle(repo, shadow=False, execute=True)

    assert result.event.event_type == "REVIEW_BLOCKED"
    assert state.current_state(repo.full_name) == RunState.REPAIRING
    assert github.merged == []
    assert not any(event.event_type == "ATTENTION_REQUIRED" for event in notifier.events)


def test_active_pr_unclassified_final_blocker_is_repairable(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Durable plan\n", encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", tmp_path / "baseline.json", analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement the authorization slice",
        reason="The documented work item is ready for final review.",
        target_issue=39,
    )
    state.save_active_work(
        repo.full_name,
        action_id="action-1",
        pr_number=35,
        branch="captains_chair/work/39",
        head_sha="head-1",
        status="pr_open",
        decision=decision.model_dump(mode="json"),
    )
    state.transition(repo.full_name, RunState.PR_OPEN)
    github = ActivePrGitHub()
    harness = ReviewHarness(owner_blocker="TECHNICAL: final evidence is incomplete")
    notifier = MemoryNotifier()
    result = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        harness,
        cast(Notifier, notifier),
        model_policy(),
    ).cycle(repo, shadow=False, execute=True)

    assert result.event.event_type == "FINAL_REVIEW_BLOCKED"
    assert state.current_state(repo.full_name) == RunState.REPAIRING
    assert github.merged == []
    assert not any(event.event_type == "ATTENTION_REQUIRED" for event in notifier.events)


@pytest.mark.parametrize(
    ("blocker", "event_type", "state"),
    (
        ("USER_SECRET: Azure credential is required", "ATTENTION_REQUIRED", RunState.BLOCKED),
        ("TECHNICAL: worker exited before proof", "EXECUTION_FAILED", RunState.DEGRADED),
    ),
)
def test_direct_worker_blockers_are_classified_without_false_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocker: str,
    event_type: str,
    state: RunState,
) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Durable plan\n", encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state_store = StateStore(config.state_dir / "state.db")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"ready"}}', encoding="utf-8")
    state_store.save_baseline(repo.full_name, "baseline", artifact, analyzed=True)
    state_store.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state_store.transition(repo.full_name, RunState.READY)
    github = ActivePrGitHub()
    notifier = MemoryNotifier()
    engine = ControlPlaneEngine(
        config,
        state_store,
        cast(GhGitHubProvider, github),
        ReviewHarness(),
        cast(Notifier, notifier),
        model_policy(),
    )
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement the blocked slice",
        reason="The documented work item is ready.",
        target_issue=39,
    )

    def blocked_plan(
        _repo: object,
        _run_id: str,
        _snapshot: RepositorySnapshot,
        **_kwargs: Any,
    ) -> tuple[PlanDecision, dict[str, str]]:
        return decision, {"model": "test-model"}

    monkeypatch.setattr(engine, "_plan", blocked_plan)

    def blocked_execute(*_args: Any, **_kwargs: Any) -> Any:
        raise WorkerBlockedError(blocker)

    monkeypatch.setattr(engine, "_execute", blocked_execute)

    result = engine.cycle(repo, shadow=False, execute=True)

    assert result.event.event_type == event_type
    assert result.event.evidence["blocker"] == blocker
    assert result.event.evidence["owner_required"] is (event_type == "ATTENTION_REQUIRED")
    assert state_store.current_state(repo.full_name) == state
    proposal = state_store.proposal(
        repo.full_name, str(result.event.evidence["proposal_action_id"])
    )
    assert proposal is not None
    assert proposal["status"] == "proposed"

    second = engine.cycle(repo, shadow=False, execute=True)
    assert second.event.event_type == (
        "ATTENTION_REQUIRED" if event_type == "ATTENTION_REQUIRED" else "STALLED"
    )
