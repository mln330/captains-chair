from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest
from pydantic import BaseModel

from captains_chair.command import CommandResult, run_command
from captains_chair.conformance import run_full_autonomous_workflow
from captains_chair.engine import ControlPlaneEngine
from captains_chair.github import GhGitHubProvider, RepositorySnapshot
from captains_chair.harness import HarnessAdapter
from captains_chair.models import (
    ActionKind,
    CompletionPolicy,
    EventRecord,
    HarnessConfig,
    ModelTarget,
    OperationMode,
    PlanDecision,
    RunState,
    WorkerResult,
)
from captains_chair.orchestration import EnqueuedWorkflow, WorkflowOrchestrator
from captains_chair.state import StateStore
from captains_chair.worktrees import Worktree
from tests.fakes import InMemoryWorkQueue, worker_policy
from tests.helpers import app_config, model_policy, repo_config

OutputModel = TypeVar("OutputModel", bound=BaseModel)


class PlanningHarness(HarnessAdapter):
    def __init__(self) -> None:
        super().__init__(HarnessConfig(kind="codex", executable="codex"))
        self.roles: list[str] = []

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
        return PlanDecision(
            action=ActionKind.IMPLEMENT,
            summary="Implement the next documented slice",
            reason="The baseline identifies issue 39 as dependency-ready.",
            target_issue=39,
            acceptance_criteria=("Scope matches issue 39", "Required checks pass"),
        ).model_dump(mode="json")


class FailingImplementationHarness(PlanningHarness):
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
        if role == "planner":
            planned = PlanDecision.model_validate(
                super().invoke(
                    prompt=prompt,
                    model=model,
                    role=role,
                    output_model=output_model,
                    cwd=cwd,
                    writable=writable,
                    session_id=session_id,
                )
            )
            return planned.model_copy(update={"work_item_id": "39"}).model_dump(mode="json")
        raise RuntimeError("worker process crashed")


class SuccessfulImplementationHarness(PlanningHarness):
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
        if role == "planner":
            planned = PlanDecision.model_validate(
                super().invoke(
                    prompt=prompt,
                    model=model,
                    role=role,
                    output_model=output_model,
                    cwd=cwd,
                    writable=writable,
                    session_id=session_id,
                )
            )
            return planned.model_copy(update={"work_item_id": "39"}).model_dump(mode="json")
        self.roles.append(role)
        return WorkerResult(summary="Implementation completed", changed_files=()).model_dump(mode="json")


class IssueThenImplementationHarness(PlanningHarness):
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
        if len(self.roles) == 1:
            return PlanDecision(
                action=ActionKind.CREATE_ISSUE,
                summary="Create the next documented gap as a GitHub issue",
                reason="The baseline identified a bounded gap without an existing issue.",
                issue_title="Implement the next documented slice",
                issue_body="Acceptance criteria from the durable plan.",
            ).model_dump(mode="json")
        return PlanDecision(
            action=ActionKind.IMPLEMENT,
            summary="Implement the newly created issue",
            reason="The issue now exists and is the next dependency-ready work item.",
            target_issue=77,
            acceptance_criteria=("Scope matches issue 77", "Required checks pass"),
        ).model_dump(mode="json")


class RepairPlanningHarness(HarnessAdapter):
    def __init__(self) -> None:
        super().__init__(HarnessConfig(kind="codex", executable="codex"))

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
        del prompt, model, role, output_model, cwd, writable, session_id
        return PlanDecision(
            action=ActionKind.REPAIR_PR,
            summary="Repair the current pull request",
            reason="The latest independent review found a repairable defect.",
            target_pr=42,
            acceptance_criteria=("Address the review finding", "Keep checks green"),
        ).model_dump(mode="json")


class SnapshotGitHub:
    def snapshot(self, repo: object) -> RepositorySnapshot:
        del repo
        return RepositorySnapshot(
            repo={"nameWithOwner": "example/project"},
            issues=[{"number": 39, "title": "Implement the next slice", "state": "OPEN"}],
            pull_requests=[],
            branches=["main"],
            workflow_runs=[],
        )

    def pull_request(self, repo: object, number: int) -> dict[str, Any]:
        del repo
        if number != 42:
            raise AssertionError(f"unexpected PR {number}")
        return {
            "number": number,
            "headRefName": "feature/current-pr",
            "headRefOid": "abcdef123456",
            "baseRefName": "main",
            "url": "https://github.com/example/project/pull/42",
        }


class DirectImplementationGitHub(SnapshotGitHub):
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def create_pull_request(
        self,
        repo: object,
        *,
        branch: str,
        title: str,
        body: str,
        draft: bool,
    ) -> dict[str, Any]:
        del repo
        pr = {
            "number": 99,
            "url": "https://github.com/example/project/pull/99",
            "headRefName": branch,
            "headRefOid": "head-99",
            "title": title,
            "body": body,
            "isDraft": draft,
        }
        self.created.append(pr)
        return pr


class IssueWorkflowGitHub(SnapshotGitHub):
    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []

    def snapshot(self, repo: object) -> RepositorySnapshot:
        snapshot = super().snapshot(repo)
        created_issues = [
            {
                "number": 77,
                "title": title,
                "state": "OPEN",
                "body": body,
            }
            for title, body in self.created
        ]
        return RepositorySnapshot(
            snapshot.repo,
            [*snapshot.issues, *created_issues],
            snapshot.pull_requests,
            snapshot.branches,
            snapshot.workflow_runs,
        )

    def create_issue(self, repo: object, title: str, body: str) -> dict[str, Any]:
        del repo
        self.created.append((title, body))
        return {
            "number": 77,
            "title": title,
            "body": body,
            "url": "https://github.com/example/project/issues/77",
        }


class MemoryNotifier:
    def __init__(self) -> None:
        self.events: list[EventRecord] = []

    def send(self, event: EventRecord) -> None:
        self.events.append(event)


def no_op_git_runner(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: int = 60,
) -> CommandResult:
    del command, cwd, input_text, timeout
    return CommandResult(0, "main\n", "")


def direct_implementation_runner(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: int = 60,
) -> CommandResult:
    del cwd, input_text, timeout
    if "show" in command:
        return CommandResult(0, "# Durable plan\n", "")
    if "branch" in command and "--show-current" in command:
        return CommandResult(0, "captains_chair/work/39\n", "")
    if "rev-list" in command:
        return CommandResult(0, "1\n", "")
    return CommandResult(0, "", "")


def local_planning_runner(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: int = 60,
) -> CommandResult:
    del cwd, input_text, timeout
    if "status" in command:
        return CommandResult(0, "", "")
    if "branch" in command:
        return CommandResult(0, "main\n", "")
    if "fetch" in command:
        return CommandResult(1, "", "not a git checkout")
    return CommandResult(0, "", "")


class FakeRepairWorktrees:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: list[tuple[str, str]] = []

    def checkout_existing(self, repo: object, work_id: str, remote_branch: str) -> Worktree:
        del repo
        self.calls.append((work_id, remote_branch))
        return Worktree(
            path=self.path,
            branch="captains_chair/repair/pr-42",
            base="origin/main",
            push_branch=remote_branch,
        )


class FailingOrchestrator:
    def active_workflow_count(self, repo: object) -> int:
        del repo
        return 0

    def has_active_workflow(self, repo: object, decision: PlanDecision) -> bool:
        del repo, decision
        return False

    def enqueue(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("Workboard gateway unavailable")


class RecoveringOrchestrator(FailingOrchestrator):
    def __init__(self) -> None:
        self.enqueue_attempts = 0

    def enqueue(self, *args: object, **kwargs: object) -> EnqueuedWorkflow:
        del args, kwargs
        self.enqueue_attempts += 1
        if self.enqueue_attempts == 1:
            raise RuntimeError("Workboard gateway unavailable")
        return EnqueuedWorkflow(
            workflow_id="recovered-workflow",
            board_id="captains-chair-example-project",
            root_card_id="root-1",
            stage_cards={"implementation": "card-1"},
        )


class QueueWorktrees:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.removed: list[Path] = []

    def create(self, repo: object, work_id: str, *, lane: str) -> Worktree:
        del repo
        return Worktree(
            path=self.path,
            branch=f"captains_chair/{lane}/{work_id}",
            base="origin/main",
            push_branch=f"captains_chair/{lane}/{work_id}",
        )

    def remove(self, repo: object, worktree: Worktree) -> bool:
        del repo
        self.removed.append(worktree.path)
        return True


class DiscardingWorktrees(QueueWorktrees):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.discarded: list[Path] = []

    def discard(self, repo: object, worktree: Worktree) -> bool:
        del repo
        self.discarded.append(worktree.path)
        return True


class CleanupFallbackWorktrees(DiscardingWorktrees):
    def remove(self, repo: object, worktree: Worktree) -> bool:
        del repo, worktree
        raise RuntimeError("clean removal failed")


def _git(cwd: Path | None, *args: str) -> str:
    result = run_command(["git", *args], cwd=cwd, timeout=120)
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _git_commit(cwd: Path, message: str) -> None:
    _git(
        cwd,
        "-c",
        "user.name=CAPTAINS_CHAIR Test",
        "-c",
        "user.email=captains_chair@example.test",
        "commit",
        "-m",
        message,
    )


def test_engine_queues_full_workflow_from_a_real_isolated_git_worktree(tmp_path: Path) -> None:
    bare = tmp_path / "origin.git"
    repo_root = tmp_path / "repo"
    _git(None, "init", "--bare", str(bare))
    _git(None, "init", "--initial-branch=main", str(repo_root))
    _git(repo_root, "config", "user.name", "CAPTAINS_CHAIR Test")
    _git(repo_root, "config", "user.email", "captains_chair@example.test")
    (repo_root / "README.md").write_text("# Disposable project\n", encoding="utf-8")
    (repo_root / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    _git(repo_root, "add", "README.md", "ISSUES_EXECUTION_PLAN.md")
    _git_commit(repo_root, "Seed disposable project")
    _git(repo_root, "remote", "add", "origin", str(bare))
    _git(repo_root, "push", "--set-upstream", "origin", "main")

    repo = repo_config(
        repo_root,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    config = app_config(tmp_path, repo)
    artifact = tmp_path / "baseline.json"
    artifact.write_text(
        '{"analysis":{"summary":"issue 39 is dependency-ready"},"checks":[]}',
        encoding="utf-8",
    )
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    queue = InMemoryWorkQueue()
    orchestrator = WorkflowOrchestrator(queue, worker_policy())
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        PlanningHarness(),
        MemoryNotifier(),
        model_policy(),
        orchestrator=orchestrator,
        runner=run_command,
    )

    result = engine.cycle(repo, shadow=False, execute=True)

    assert result.event.event_type == "WORKFLOW_QUEUED"
    action_id = str(result.event.evidence["action_id"])
    implementation = queue.cards[queue.keys[f"{action_id}:implementation"]]
    assert implementation.workspace is not None
    assert implementation.workspace.path is not None
    assert implementation.workspace.path.is_dir()
    assert implementation.workspace.path != repo_root
    assert implementation.workspace.branch == "captains_chair/work/39"
    assert _git(repo_root, "branch", "--show-current") == "main"
    assert _git(repo_root, "status", "--porcelain") == ""
    assert not (repo_root / "feature.txt").exists()

    decision = PlanDecision.model_validate(result.event.evidence["decision"])
    run_full_autonomous_workflow(
        orchestrator,
        queue,
        repo,
        decision,
        action_id,
        block_card=queue.block,
    )

    worktree = Worktree(
        path=implementation.workspace.path,
        branch=implementation.workspace.branch or "",
        base="origin/main",
        push_branch=implementation.workspace.push_branch or implementation.workspace.branch or "",
    )
    engine.worktrees.remove(repo, worktree)
    assert not worktree.path.exists()
    assert _git(repo_root, "status", "--porcelain") == ""


def test_engine_planning_hands_off_to_shared_autonomous_workflow(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Durable plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text(
        '{"analysis":{"summary":"issue 39 is dependency-ready"},"checks":[]}',
        encoding="utf-8",
    )
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    harness = PlanningHarness()
    queue = InMemoryWorkQueue()
    orchestrator = WorkflowOrchestrator(queue, worker_policy())
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        harness,
        MemoryNotifier(),
        model_policy(),
        orchestrator=orchestrator,
        runner=no_op_git_runner,
    )

    result = engine.cycle(repo, shadow=False, execute=True)

    assert result.event.event_type == "WORKFLOW_QUEUED"
    assert result.event.evidence["board_id"] == "captains-chair-example-project"
    assert harness.roles == ["planner"]
    decision = PlanDecision.model_validate(result.event.evidence["decision"])
    action_id = str(result.event.evidence["action_id"])

    run_full_autonomous_workflow(
        orchestrator,
        queue,
        repo,
        decision,
        action_id,
        block_card=queue.block,
    )


def test_direct_worker_failure_discards_isolated_worktree_for_automatic_retry(
    tmp_path: Path,
) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Durable plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text(
        '{"analysis":{"summary":"issue 39 is dependency-ready"},"checks":[]}',
        encoding="utf-8",
    )
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    worktrees = DiscardingWorktrees(tmp_path / "failed-worktree")
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        FailingImplementationHarness(),
        MemoryNotifier(),
        model_policy(),
        runner=no_op_git_runner,
    )
    engine.worktrees = cast(Any, worktrees)

    result = engine.cycle(repo, shadow=False, execute=True)

    assert result.event.event_type == "EXECUTION_FAILED"
    assert worktrees.discarded == [tmp_path / "failed-worktree"]
    assert worktrees.removed == []


@pytest.mark.parametrize("worktree_type", [DiscardingWorktrees, CleanupFallbackWorktrees])
def test_direct_success_keeps_pr_open_when_local_cleanup_needs_force_discard(
    tmp_path: Path,
    worktree_type: type[DiscardingWorktrees],
) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Durable plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text(
        '{"analysis":{"summary":"issue 39 is dependency-ready"},"checks":[]}',
        encoding="utf-8",
    )
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    worktrees = worktree_type(tmp_path / "successful-worktree")
    github = DirectImplementationGitHub()
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        SuccessfulImplementationHarness(),
        MemoryNotifier(),
        model_policy(),
        runner=direct_implementation_runner,
    )
    engine.worktrees = cast(Any, worktrees)

    result = engine.cycle(repo, shadow=False, execute=True)

    assert result.event.event_type == "PR_OPENED", result.event.model_dump(mode="json")
    assert github.created[0]["number"] == 99
    assert result.event.evidence.get("worktree_cleanup_warning") in {
        None,
        "clean removal failed; force-discarded local worktree: clean removal failed",
    }
    assert worktrees.discarded == (
        [tmp_path / "successful-worktree"]
        if worktree_type is CleanupFallbackWorktrees
        else []
    )


def test_engine_creates_issue_then_queues_and_completes_autonomous_workflow(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Durable plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text(
        '{"analysis":{"summary":"a bounded undocumented gap"},"checks":[]}',
        encoding="utf-8",
    )
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "fingerprint", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    github = IssueWorkflowGitHub()
    queue = InMemoryWorkQueue()
    orchestrator = WorkflowOrchestrator(queue, worker_policy())
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        IssueThenImplementationHarness(),
        MemoryNotifier(),
        model_policy(),
        orchestrator=orchestrator,
        runner=no_op_git_runner,
    )

    created = engine.cycle(repo, shadow=False, execute=True)
    assert created.event.event_type == "ISSUE_CREATED"
    assert github.created == [
        ("Implement the next documented slice", "Acceptance criteria from the durable plan.")
    ]

    queued = engine.cycle(repo, shadow=False, execute=True)
    assert queued.event.event_type == "WORKFLOW_QUEUED"
    decision = PlanDecision.model_validate(queued.event.evidence["decision"])
    assert decision.target_issue == 77

    run_full_autonomous_workflow(
        orchestrator,
        queue,
        repo,
        decision,
        str(queued.event.evidence["action_id"]),
        block_card=queue.block,
    )


def test_engine_queues_planner_selected_repair_with_existing_pr_workspace(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Durable plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text(
        '{"analysis":{"summary":"repair is required"},"checks":[]}',
        encoding="utf-8",
    )
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    queue = InMemoryWorkQueue()
    orchestrator = WorkflowOrchestrator(queue, worker_policy())
    github = SnapshotGitHub()
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        RepairPlanningHarness(),
        MemoryNotifier(),
        model_policy(),
        orchestrator=orchestrator,
        runner=no_op_git_runner,
    )
    worktrees = FakeRepairWorktrees(tmp_path / "repair-worktree")
    engine.worktrees = cast(Any, worktrees)

    result = engine.cycle(repo, shadow=False, execute=True)

    assert result.event.event_type == "WORKFLOW_QUEUED"
    assert len(worktrees.calls) == 1
    assert worktrees.calls[0][0].startswith("pr-42-")
    assert worktrees.calls[0][1] == "feature/current-pr"
    action_id = str(result.event.evidence["action_id"])
    repair_card = queue.cards[queue.keys[f"{action_id}:repair"]]
    assert repair_card.workspace is not None
    assert repair_card.workspace.branch == "captains_chair/repair/pr-42"
    assert repair_card.workspace.push_branch == "feature/current-pr"
    assert "push implementation changes to `feature/current-pr`" in (repair_card.notes or "")


def test_workboard_queue_failure_is_audited_and_does_not_replan_unchanged_evidence(
    tmp_path: Path,
) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Durable plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"issue 39 is ready"},"checks":[]}', encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    harness = PlanningHarness()
    worktrees = QueueWorktrees(tmp_path / "queued-worktree")
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        harness,
        MemoryNotifier(),
        model_policy(),
        orchestrator=cast(Any, FailingOrchestrator()),
        runner=no_op_git_runner,
    )
    engine.worktrees = cast(Any, worktrees)

    first = engine.cycle(repo, shadow=False, execute=True)
    second = engine.cycle(repo, shadow=False, execute=True)

    assert first.event.event_type == "WORKFLOW_QUEUE_FAILED"
    assert first.exit_code == 3
    assert second.event.event_type == "STALLED"
    assert second.exit_code == 2
    assert harness.roles == ["planner"]
    assert worktrees.removed == [tmp_path / "queued-worktree"]


def test_workboard_queue_failure_recovers_after_planning_evidence_changes(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "ISSUES_EXECUTION_PLAN.md"
    plan_path.write_text("# Durable plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"issue 39 is ready"},"checks":[]}', encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    harness = PlanningHarness()
    worktrees = QueueWorktrees(tmp_path / "queued-worktree")
    orchestrator = RecoveringOrchestrator()
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        harness,
        MemoryNotifier(),
        model_policy(),
        orchestrator=cast(Any, orchestrator),
        runner=local_planning_runner,
    )
    engine.worktrees = cast(Any, worktrees)

    first = engine.cycle(repo, shadow=False, execute=True)
    unchanged = engine.cycle(repo, shadow=False, execute=True)
    plan_path.write_text("# Durable plan\n\n## Newly confirmed dependency\n", encoding="utf-8")
    recovered = engine.cycle(repo, shadow=False, execute=True)

    assert first.event.event_type == "WORKFLOW_QUEUE_FAILED"
    assert unchanged.event.event_type == "STALLED"
    assert recovered.event.event_type == "WORKFLOW_QUEUED"
    assert harness.roles == ["planner", "planner"]
    assert orchestrator.enqueue_attempts == 2
    assert worktrees.removed == [tmp_path / "queued-worktree"]
