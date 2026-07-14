import hashlib
import json
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest
from pydantic import BaseModel

from captains_chair.command import CommandResult
from captains_chair.engine import ControlPlaneEngine, ModelCallSuppressedError
from captains_chair.github import GhGitHubProvider, RepositorySnapshot
from captains_chair.harness import HarnessAdapter
from captains_chair.models import (
    ActionKind,
    ActionScope,
    EventRecord,
    HarnessConfig,
    ModelTarget,
    OperationMode,
    PlanDecision,
    RepoConfig,
    RunState,
    UsageConfig,
    UsageRate,
)
from captains_chair.notifications import NotificationError, Notifier
from captains_chair.orchestration import EnqueuedWorkflow, WorkflowOrchestrator
from captains_chair.state import StateStore
from captains_chair.worktrees import Worktree
from tests.helpers import app_config, model_policy, repo_config

OutputModel = TypeVar("OutputModel", bound=BaseModel)


class CapturingHarness(HarnessAdapter):
    prompt: str = ""
    calls: int = 0

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
        del model, role, output_model, cwd, writable, session_id
        self.prompt = prompt
        self.calls += 1
        return PlanDecision(
            action=ActionKind.NO_ACTION,
            summary="Nothing to do",
            reason="Baseline was read",
        ).model_dump(mode="json")


class RewordingHarness(CapturingHarness):
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
        del model, role, output_model, cwd, writable, session_id
        self.prompt = prompt
        self.calls += 1
        return PlanDecision(
            action=ActionKind.MAINTENANCE,
            scope=ActionScope.CONTROL_PLANE,
            summary=f"Maintenance wording {self.calls}",
            reason=f"Same root cause, phrasing {self.calls}",
        ).model_dump(mode="json")


class FailingPlannerHarness(CapturingHarness):
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
        self.calls += 1
        raise RuntimeError("provider unavailable")


class ApprovalFlagHarness(CapturingHarness):
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
        del model, role, output_model, cwd, writable, session_id
        self.prompt = prompt
        self.calls += 1
        return PlanDecision(
            action=ActionKind.UPDATE_ISSUE,
            summary="Retarget issue #6",
            reason="The work contract is stale",
            target_issue=6,
            issue_title="Implement the first slice",
            issue_body="Focused implementation scope",
            requires_owner_approval=True,
            owner_blocker="GOAL_DIVERGENCE: the work contract no longer matches the approved goal",
        ).model_dump(mode="json")


class BareThenRoutineHarness(CapturingHarness):
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
        del model, role, output_model, cwd, writable, session_id
        self.prompt = prompt
        self.calls += 1
        if self.calls == 1:
            return PlanDecision(
                action=ActionKind.UPDATE_ISSUE,
                summary="Retarget issue #6",
                reason="The work contract is stale",
                target_issue=6,
                requires_owner_approval=True,
            ).model_dump(mode="json")
        return PlanDecision(
            action=ActionKind.UPDATE_ISSUE,
            summary="Retarget issue #6",
            reason="The work contract is ready for the routine update",
            target_issue=6,
            issue_title="Implement the first slice",
            issue_body="Focused implementation scope",
        ).model_dump(mode="json")


class QueueHarness(CapturingHarness):
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
        del model, role, output_model, cwd, writable, session_id
        self.prompt = prompt
        self.calls += 1
        return PlanDecision(
            action=ActionKind.UPDATE_ISSUE,
            summary="Clarify issue 39",
            reason="Keep the next worker contract current.",
            target_issue=39,
            issue_body="Updated acceptance criteria",
        ).model_dump(mode="json")


class ImplementationQueueHarness(CapturingHarness):
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
        self.calls += 1
        return PlanDecision(
            action=ActionKind.IMPLEMENT,
            summary="Implement issue 39",
            reason="The issue is the next dependency-ready work item.",
            target_issue=39,
            acceptance_criteria=("Scope is correct", "Checks pass"),
        ).model_dump(mode="json")


class IssueActionHarness(CapturingHarness):
    def __init__(self, decision: PlanDecision) -> None:
        self.decision = decision

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
        self.calls += 1
        return self.decision.model_dump(mode="json")


class CapturingOrchestrator:
    def __init__(self, active_count: int = 0, capacity_error: str | None = None) -> None:
        self.decisions: list[PlanDecision] = []
        self.active_count = active_count
        self.capacity_error = capacity_error

    def enqueue(
        self,
        repo: object,
        decision: PlanDecision,
        action_id: str,
        *,
        workspace: object = None,
    ) -> EnqueuedWorkflow:
        del repo, workspace
        self.decisions.append(decision)
        return EnqueuedWorkflow(
            workflow_id=action_id,
            board_id="captains-chair-example-project",
            root_card_id="root-1",
            stage_cards={"captain": "card-1"},
        )

    def has_active_workflow(self, repo: object, decision: PlanDecision) -> bool:
        del repo, decision
        return False

    def active_workflow_count(self, repo: object) -> int:
        del repo
        if self.capacity_error:
            raise RuntimeError(self.capacity_error)
        return self.active_count


class StubWorktrees:
    def __init__(self, root: Path) -> None:
        self.root = root

    def create(self, repo: RepoConfig, work_id: str, lane: str = "work") -> Worktree:
        del repo
        path = self.root / work_id
        path.mkdir(parents=True, exist_ok=True)
        branch = f"captains_chair/{lane}/{work_id}"
        return Worktree(path=path, branch=branch, base="origin/main", push_branch=branch)


class SnapshotGitHub:
    def snapshot(self, repo: object) -> RepositorySnapshot:
        del repo
        return RepositorySnapshot({}, [], [], ["main"], [])


class ChangingSnapshotGitHub(SnapshotGitHub):
    def __init__(self) -> None:
        self.calls = 0

    def snapshot(self, repo: object) -> RepositorySnapshot:
        del repo
        self.calls += 1
        files = [] if self.calls == 1 else [{"path": "src/new_file.py", "status": "modified"}]
        return RepositorySnapshot({}, [], [], ["main"], files)


class IssueGitHub(SnapshotGitHub):
    def __init__(self) -> None:
        self.updated: list[int] = []

    def update_issue(
        self, repo: object, number: int, title: str | None, body: str | None
    ) -> None:
        del repo, title, body
        self.updated.append(number)


class FailingIssueGitHub(IssueGitHub):
    def update_issue(
        self, repo: object, number: int, title: str | None, body: str | None
    ) -> None:
        del repo, number, title, body
        raise RuntimeError("temporary GitHub write failure")


class FlakyIssueGitHub(IssueGitHub):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def update_issue(
        self, repo: object, number: int, title: str | None, body: str | None
    ) -> None:
        del repo, title, body
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary GitHub write failure")
        self.updated.append(number)


class FailingCreateIssueGitHub(IssueGitHub):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def create_issue(self, repo: object, title: str, body: str) -> dict[str, Any]:
        del repo, title, body
        self.calls += 1
        raise RuntimeError("ambiguous GitHub create failure")


class IssueMutationGitHub(IssueGitHub):
    def __init__(self) -> None:
        super().__init__()
        self.created: list[tuple[str, str]] = []
        self.labeled: list[tuple[int, tuple[str, ...]]] = []
        self.retargeted: list[tuple[int, str | None, tuple[str, ...]]] = []
        self.closed: list[tuple[int, str]] = []

    def create_issue(self, repo: object, title: str, body: str) -> dict[str, Any]:
        del repo
        self.created.append((title, body))
        return {"number": 77, "url": "https://github.test/example/project/issues/77"}

    def close_issue(self, repo: object, number: int, reason: str) -> None:
        del repo
        self.closed.append((number, reason))

    def label_issue(self, repo: object, number: int, labels: tuple[str, ...]) -> None:
        del repo
        self.labeled.append((number, labels))

    def retarget_issue(
        self,
        repo: object,
        number: int,
        milestone: str | None,
        assignees: tuple[str, ...],
    ) -> None:
        del repo
        self.retargeted.append((number, milestone, assignees))


class FlakyIssueMutationGitHub(IssueMutationGitHub):
    def __init__(self, operation: str) -> None:
        super().__init__()
        self.operation = operation
        self.attempts: dict[str, int] = {}

    def _maybe_fail(self, operation: str) -> None:
        self.attempts[operation] = self.attempts.get(operation, 0) + 1
        if operation == self.operation and self.attempts[operation] == 1:
            raise RuntimeError("temporary GitHub write failure")

    def update_issue(
        self, repo: object, number: int, title: str | None, body: str | None
    ) -> None:
        self._maybe_fail("update")
        super().update_issue(repo, number, title, body)

    def close_issue(self, repo: object, number: int, reason: str) -> None:
        self._maybe_fail("close")
        super().close_issue(repo, number, reason)

    def label_issue(self, repo: object, number: int, labels: tuple[str, ...]) -> None:
        self._maybe_fail("label")
        super().label_issue(repo, number, labels)

    def retarget_issue(
        self,
        repo: object,
        number: int,
        milestone: str | None,
        assignees: tuple[str, ...],
    ) -> None:
        self._maybe_fail("retarget")
        super().retarget_issue(repo, number, milestone, assignees)


class NullNotifier:
    def send(self, event: EventRecord) -> None:
        del event


class FailOnceNotifier:
    def __init__(self) -> None:
        self.failed = False

    def send(self, event: EventRecord) -> None:
        del event
        if not self.failed:
            self.failed = True
            raise NotificationError("Discord route unavailable")


def test_direct_model_call_honors_configured_daily_budget_before_provider_call(
    tmp_path: Path,
) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo).model_copy(
        update={"usage": UsageConfig(daily_budget_credits=0)}
    )
    state = StateStore(config.state_dir / "state.db")
    harness = CapturingHarness(HarnessConfig(kind="codex", executable="codex"))
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        harness,
        NullNotifier(),
        model_policy(),
    )

    with pytest.raises(ModelCallSuppressedError, match="daily usage budget"):
        engine.run_model(
            repo,
            "budget-check",
            "planner",
            "Return a plan.",
            models=model_policy().planner,
            output_model=PlanDecision,
            cwd=tmp_path,
            writable=False,
        )

    assert harness.calls == 0


def test_direct_model_call_uses_account_wide_budget_across_repositories(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo).model_copy(
        update={
            "usage": UsageConfig(
                rates={"gpt-5.5": UsageRate(input_credits_per_million=10)},
                daily_budget_credits=10,
            )
        }
    )
    state = StateStore(config.state_dir / "state.db")
    state.record_model_call(
        "other/project",
        "other-run",
        "planner",
        "gpt-5.5",
        [{"input_tokens": 1_000_000, "output_tokens": 0, "total_tokens": 1_000_000}],
    )
    harness = CapturingHarness(HarnessConfig(kind="codex", executable="codex"))
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        harness,
        NullNotifier(),
        model_policy(),
    )

    with pytest.raises(ModelCallSuppressedError, match="daily usage budget"):
        engine.run_model(
            repo,
            "cross-repo-budget-check",
            "planner",
            "Return a plan.",
            models=model_policy().planner,
            output_model=PlanDecision,
            cwd=tmp_path,
            writable=False,
        )

    assert harness.calls == 0


def test_direct_model_call_honors_unknown_telemetry_guard_without_daily_budget(
    tmp_path: Path,
) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo).model_copy(update={"usage": UsageConfig(block_on_unknown=True)})
    state = StateStore(config.state_dir / "state.db")
    state.record_external_usage(
        {
            "source": "openclaw-session",
            "external_id": "agent:captains-chair:unknown-session",
            "repo": repo.full_name,
            "role": "captain",
            "model": "gpt-5.5",
        }
    )
    harness = CapturingHarness(HarnessConfig(kind="codex", executable="codex"))
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        harness,
        NullNotifier(),
        model_policy(),
    )

    with pytest.raises(ModelCallSuppressedError, match="usage telemetry is incomplete"):
        engine.run_model(
            repo,
            "unknown-telemetry-check",
            "planner",
            "Return a plan.",
            models=model_policy().planner,
            output_model=PlanDecision,
            cwd=tmp_path,
            writable=False,
        )

    assert harness.calls == 0


def test_direct_model_call_suppresses_when_runtime_usage_sync_fails(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo).model_copy(update={"usage": UsageConfig(block_on_unknown=True)})
    state = StateStore(config.state_dir / "state.db")
    harness = CapturingHarness(HarnessConfig(kind="openclaw", executable="openclaw"))

    def failed_sync(_: StateStore, __: object) -> dict[str, Any]:
        raise RuntimeError("session endpoint unavailable")

    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        harness,
        NullNotifier(),
        model_policy(),
        usage_sync=failed_sync,
    )

    with pytest.raises(ModelCallSuppressedError, match="usage telemetry sync failed"):
        engine.run_model(
            repo,
            "sync-failure-check",
            "planner",
            "Return a plan.",
            models=model_policy().planner,
            output_model=PlanDecision,
            cwd=tmp_path,
            writable=False,
        )

    assert harness.calls == 0


def test_direct_model_call_suppresses_when_runtime_usage_sync_is_degraded(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo).model_copy(update={"usage": UsageConfig(block_on_unknown=True)})
    state = StateStore(config.state_dir / "state.db")
    harness = CapturingHarness(HarnessConfig(kind="openclaw", executable="openclaw"))

    def degraded_sync(_: StateStore, __: RepoConfig) -> dict[str, Any]:
        return {"status": "degraded", "error": "usage response was incomplete"}

    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        harness,
        NullNotifier(),
        model_policy(),
        usage_sync=degraded_sync,
    )

    with pytest.raises(ModelCallSuppressedError, match="usage telemetry sync was degraded"):
        engine.run_model(
            repo,
            "degraded-sync-check",
            "planner",
            "Return a plan.",
            models=model_policy().planner,
            output_model=PlanDecision,
            cwd=tmp_path,
            writable=False,
        )

    assert harness.calls == 0


def test_cycle_reports_usage_suppression_as_a_non_owner_blocker(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo).model_copy(
        update={"usage": UsageConfig(daily_budget_credits=0)}
    )
    state = StateStore(config.state_dir / "state.db")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"ready"}}', encoding="utf-8")
    state.save_baseline(repo.full_name, "baseline", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    harness = CapturingHarness(HarnessConfig(kind="codex", executable="codex"))
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        harness,
        NullNotifier(),
        model_policy(),
    )

    result = engine.cycle(repo, shadow=False, execute=True)

    assert result.exit_code == 2
    assert result.event.event_type == "MODEL_CALL_SUPPRESSED"
    assert result.event.state == RunState.DEGRADED
    assert result.event.evidence["role"] == "planner"
    assert harness.calls == 0


def _issue_action_engine(
    tmp_path: Path, decision: PlanDecision
) -> tuple[ControlPlaneEngine, StateStore, IssueMutationGitHub]:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"issue action is ready"}}', encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "fingerprint", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    github = IssueMutationGitHub()
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        IssueActionHarness(decision),
        cast(Notifier, NullNotifier()),
        model_policy(),
    )
    return engine, state, github


def test_planner_receives_baseline_analysis_not_only_artifact_pointer(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text(
        json.dumps(
            {
                "analysis": {"summary": "material implementation gap"},
                "checks": [{"command": "pytest", "returncode": 0}],
                "source_inventory": {"counts": {"source_files": 10}},
                "evidence_exclusions": [],
            }
        ),
        encoding="utf-8",
    )
    repo = repo_config(tmp_path, mode=OperationMode.ADVISORY)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "fingerprint", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    harness = CapturingHarness(HarnessConfig(kind="codex", executable="codex"))
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        harness,
        cast(Notifier, NullNotifier()),
        model_policy(),
    )

    engine.cycle(repo, shadow=True, execute=False)

    assert "material implementation gap" in harness.prompt
    assert '"returncode": 0' in harness.prompt


def test_planning_document_prefers_fetched_default_branch_when_checkout_is_untrusted(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Stale local plan\n", encoding="utf-8")
    repo = repo_config(tmp_path)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")

    def runner(command: object, **_: object) -> CommandResult:
        args = [str(item) for item in cast(list[object], command)]
        if "show" in args:
            return CommandResult(0, "# Current remote plan\n", "")
        return CommandResult(0, "", "")

    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        CapturingHarness(HarnessConfig(kind="codex", executable="codex")),
        cast(Notifier, NullNotifier()),
        model_policy(),
        runner=runner,
    )

    document = cast(Any, engine)._planning_document(repo, default_branch_synced=False)

    assert document.source == "origin/main"
    assert document.text == "# Current remote plan\n"
    assert document.text != (tmp_path / repo.planning_doc).read_text(encoding="utf-8")


def test_planning_document_fails_closed_when_git_context_cannot_be_read(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Local plan\n", encoding="utf-8")
    repo = repo_config(tmp_path)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")

    def runner(command: object, **_: object) -> CommandResult:
        del command
        return CommandResult(1, "", "origin unavailable")

    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        CapturingHarness(HarnessConfig(kind="codex", executable="codex")),
        cast(Notifier, NullNotifier()),
        model_policy(),
        runner=runner,
    )

    with pytest.raises(RuntimeError, match="could not read origin/main"):
        cast(Any, engine)._planning_document(repo, default_branch_synced=False)


def test_autonomous_engine_creates_issue_from_planner_action(tmp_path: Path) -> None:
    decision = PlanDecision(
        action=ActionKind.CREATE_ISSUE,
        summary="Create the next documented issue",
        reason="The baseline identified a bounded gap.",
        issue_title="Implement the next slice",
        issue_body="Acceptance criteria from the durable plan.",
    )
    engine, state, github = _issue_action_engine(tmp_path, decision)

    result = engine.cycle(
        repo_config(tmp_path, mode=OperationMode.AUTONOMOUS), shadow=False, execute=True
    )

    assert result.event.event_type == "ISSUE_CREATED"
    assert result.event.evidence["links"] == ["https://github.test/example/project/issues/77"]
    assert github.created == [("Implement the next slice", "Acceptance criteria from the durable plan.")]
    assert state.current_state("example/project") == RunState.READY


def test_autonomous_engine_closes_issue_from_planner_action(tmp_path: Path) -> None:
    decision = PlanDecision(
        action=ActionKind.CLOSE_ISSUE,
        summary="Close the superseded issue",
        reason="The documented work is already complete elsewhere.",
        target_issue=39,
    )
    engine, state, github = _issue_action_engine(tmp_path, decision)

    result = engine.cycle(
        repo_config(tmp_path, mode=OperationMode.AUTONOMOUS), shadow=False, execute=True
    )

    assert result.event.event_type == "ISSUE_CLOSED"
    assert result.event.evidence["issue"] == 39
    assert github.closed == [(39, "The documented work is already complete elsewhere.")]
    assert state.current_state("example/project") == RunState.READY


def test_autonomous_engine_labels_issue_from_planner_action(tmp_path: Path) -> None:
    decision = PlanDecision(
        action=ActionKind.LABEL_ISSUE,
        summary="Mark the issue as ready for implementation",
        reason="The issue has enough acceptance criteria for the coding queue.",
        target_issue=39,
        issue_labels=("ready-for-dev", "captains_chair"),
    )
    engine, state, github = _issue_action_engine(tmp_path, decision)

    result = engine.cycle(
        repo_config(tmp_path, mode=OperationMode.AUTONOMOUS), shadow=False, execute=True
    )

    assert result.event.event_type == "ISSUE_LABELED"
    assert result.event.evidence["issue"] == 39
    assert result.event.evidence["labels"] == ["ready-for-dev", "captains_chair"]
    assert github.labeled == [(39, ("ready-for-dev", "captains_chair"))]
    assert state.current_state("example/project") == RunState.READY


def test_autonomous_engine_retargets_issue_from_planner_action(tmp_path: Path) -> None:
    decision = PlanDecision(
        action=ActionKind.RETARGET_ISSUE,
        summary="Move the issue into the active sprint",
        reason="The dependency is now unblocked and has an owner.",
        target_issue=39,
        issue_milestone="Sprint 2",
        issue_assignees=("octocat",),
    )
    engine, state, github = _issue_action_engine(tmp_path, decision)

    result = engine.cycle(
        repo_config(tmp_path, mode=OperationMode.AUTONOMOUS), shadow=False, execute=True
    )

    assert result.event.event_type == "ISSUE_RETARGETED"
    assert result.event.evidence["issue"] == 39
    assert result.event.evidence["milestone"] == "Sprint 2"
    assert result.event.evidence["assignees"] == ["octocat"]
    assert github.retargeted == [(39, "Sprint 2", ("octocat",))]
    assert state.current_state("example/project") == RunState.READY


def test_autonomous_engine_executes_issue_reconciliation_before_workflow_queue(
    tmp_path: Path,
) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"issue 39 is next"}}', encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "fingerprint", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    harness = QueueHarness(HarnessConfig(kind="codex", executable="codex"))
    github = IssueGitHub()
    queue = CapturingOrchestrator()
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        harness,
        cast(Notifier, NullNotifier()),
        model_policy(),
        orchestrator=cast(WorkflowOrchestrator, queue),
    )

    result = engine.cycle(repo, shadow=False, execute=True)

    assert result.event.event_type == "ISSUE_UPDATED"
    assert result.event.evidence["issue"] == 39
    assert queue.decisions == []
    assert github.updated == [39]


def test_workboard_capacity_wait_suppresses_replanning_until_capacity_changes(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"issue 39 is next"}}', encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "fingerprint", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    harness = ImplementationQueueHarness(HarnessConfig(kind="codex", executable="codex"))
    queue = CapturingOrchestrator(active_count=1)
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, IssueGitHub()),
        harness,
        cast(Notifier, NullNotifier()),
        model_policy(),
        orchestrator=cast(WorkflowOrchestrator, queue),
    )
    engine.worktrees = cast(Any, StubWorktrees(tmp_path / "worktrees"))

    first = engine.cycle(repo, shadow=False, execute=True)
    second = engine.cycle(repo, shadow=False, execute=True)

    assert first.event.event_type == "WORKFLOW_CAPACITY_WAIT"
    assert second.event.event_id == first.event.event_id
    assert harness.calls == 1
    assert queue.decisions == []

    queue.active_count = 0
    third = engine.cycle(repo, shadow=False, execute=True)

    assert third.event.event_type == "WORKFLOW_QUEUED"
    assert harness.calls == 2
    assert len(queue.decisions) == 1

    queue.capacity_error = "Workboard gateway timeout"
    failed = engine.cycle(repo, shadow=False, execute=True)

    assert failed.event.event_type == "WORKBOARD_CAPACITY_UNKNOWN"
    assert failed.exit_code == 2
    assert harness.calls == 2


def test_unchanged_stall_alerts_once_then_suppresses_model_calls(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"known gap"}}', encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.ADVISORY)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "fingerprint", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    harness = CapturingHarness(HarnessConfig(kind="codex", executable="codex"))
    notifier = NullNotifier()
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        harness,
        cast(Notifier, notifier),
        model_policy(),
    )

    first = engine.cycle(repo, shadow=True, execute=False)
    second = engine.cycle(repo, shadow=True, execute=False)
    third = engine.cycle(repo, shadow=True, execute=False)
    forced = engine.cycle(repo, shadow=True, execute=False, force_replan=True)

    assert first.event.event_type == "ACTION_PROPOSED"
    assert second.event.event_type == "STALLED"
    assert third.event.event_id == second.event.event_id
    assert forced.event.event_type == "ACTION_PROPOSED"
    assert harness.calls == 2


def test_semantically_identical_rewording_still_stalls(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"known gap"}}', encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.ADVISORY)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "fingerprint", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    harness = RewordingHarness(HarnessConfig(kind="codex", executable="codex"))
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        harness,
        cast(Notifier, NullNotifier()),
        model_policy(),
    )

    first = engine.cycle(repo, shadow=True, execute=False)
    second = engine.cycle(repo, shadow=True, execute=False)
    third = engine.cycle(repo, shadow=True, execute=False)

    assert first.event.event_type == "CONTROL_PLANE_MAINTENANCE_REQUIRED"
    assert second.event.event_type == "STALLED"
    assert third.event.event_id == second.event.event_id
    assert harness.calls == 1


def test_changed_repository_evidence_allows_fresh_planning(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"known gap"}}', encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.ADVISORY)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "fingerprint", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    harness = CapturingHarness(HarnessConfig(kind="codex", executable="codex"))
    github = ChangingSnapshotGitHub()
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        harness,
        cast(Notifier, NullNotifier()),
        model_policy(),
    )

    first = engine.cycle(repo, shadow=True, execute=False)
    second = engine.cycle(repo, shadow=True, execute=False)

    assert first.event.event_type == "ACTION_PROPOSED"
    assert second.event.event_type == "ACTION_PROPOSED"
    assert harness.calls == 2


def test_unchanged_execution_failure_suppresses_repeated_planning(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"known gap"}}', encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "fingerprint", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    harness = QueueHarness(HarnessConfig(kind="codex", executable="codex"))
    github = FailingIssueGitHub()
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        harness,
        cast(Notifier, NullNotifier()),
        model_policy(),
    )

    first = engine.cycle(repo, shadow=False, execute=True)
    second = engine.cycle(repo, shadow=False, execute=True)
    third = engine.cycle(repo, shadow=False, execute=True)
    forced = engine.cycle(repo, shadow=False, execute=True, force_replan=True)

    assert first.event.event_type == "EXECUTION_FAILED"
    assert second.event.event_type == "EXECUTION_FAILED"
    assert second.event.evidence["execution_attempt"] == 2
    assert third.event.event_type == "STALLED"
    assert third.exit_code == 2
    assert forced.event.event_type == "EXECUTION_FAILED"
    assert harness.calls == 2


def test_notification_failure_does_not_repeat_planner_call(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"known gap"}}', encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.ADVISORY)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "fingerprint", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    harness = CapturingHarness(HarnessConfig(kind="codex", executable="codex"))
    notifier = FailOnceNotifier()
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        harness,
        cast(Notifier, notifier),
        model_policy(),
    )

    with pytest.raises(NotificationError, match="Discord route unavailable"):
        engine.cycle(repo, shadow=True, execute=False)

    second = engine.cycle(repo, shadow=True, execute=False)

    assert second.event.event_type == "STALLED"
    assert harness.calls == 1


def test_autonomous_direct_issue_retry_reuses_decision_without_replanning(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"known gap"}}', encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "fingerprint", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    harness = QueueHarness(HarnessConfig(kind="codex", executable="codex"))
    github = FlakyIssueGitHub()
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        harness,
        cast(Notifier, NullNotifier()),
        model_policy(),
    )

    first = engine.cycle(repo, shadow=False, execute=True)
    second = engine.cycle(repo, shadow=False, execute=True)

    assert first.event.event_type == "EXECUTION_FAILED"
    assert second.event.event_type == "ISSUE_UPDATED"
    assert second.event.evidence["retry_of"] == first.event.event_id
    assert second.event.evidence["execution_attempt"] == 2
    assert github.updated == [39]
    assert harness.calls == 1


@pytest.mark.parametrize(
    ("operation", "decision", "event_type"),
    (
        (
            "close",
            PlanDecision(
                action=ActionKind.CLOSE_ISSUE,
                summary="Close the stale issue",
                reason="The work is complete elsewhere.",
                target_issue=39,
            ),
            "ISSUE_CLOSED",
        ),
        (
            "label",
            PlanDecision(
                action=ActionKind.LABEL_ISSUE,
                summary="Label the ready issue",
                reason="The acceptance criteria are complete.",
                target_issue=39,
                issue_labels=("ready-for-dev",),
            ),
            "ISSUE_LABELED",
        ),
        (
            "retarget",
            PlanDecision(
                action=ActionKind.RETARGET_ISSUE,
                summary="Retarget the active issue",
                reason="The dependency is now assigned.",
                target_issue=39,
                issue_milestone="Sprint 2",
                issue_assignees=("octocat",),
            ),
            "ISSUE_RETARGETED",
        ),
    ),
)
def test_autonomous_direct_issue_retry_covers_each_idempotent_mutation(
    tmp_path: Path,
    operation: str,
    decision: PlanDecision,
    event_type: str,
) -> None:
    engine, _, _ = _issue_action_engine(tmp_path, decision)
    github = FlakyIssueMutationGitHub(operation)
    engine.github = cast(GhGitHubProvider, github)
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)

    first = engine.cycle(repo, shadow=False, execute=True)
    second = engine.cycle(repo, shadow=False, execute=True)

    assert first.event.event_type == "EXECUTION_FAILED"
    assert second.event.event_type == event_type
    assert second.event.evidence["retry_of"] == first.event.event_id
    assert second.event.evidence["execution_attempt"] == 2
    assert github.attempts[operation] == 2


def test_issue_creation_failure_is_not_automatically_replayed(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"known gap"}}', encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "fingerprint", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    decision = PlanDecision(
        action=ActionKind.CREATE_ISSUE,
        summary="Create the next issue",
        reason="The documented gap needs a tracked work item.",
        issue_title="Next issue",
        issue_body="Acceptance criteria",
    )
    harness = IssueActionHarness(decision)
    github = FailingCreateIssueGitHub()
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        harness,
        cast(Notifier, NullNotifier()),
        model_policy(),
    )

    first = engine.cycle(repo, shadow=False, execute=True)
    second = engine.cycle(repo, shadow=False, execute=True)

    assert first.event.event_type == "EXECUTION_FAILED"
    assert second.event.event_type == "STALLED"
    assert github.calls == 1
    assert harness.calls == 1


def test_failed_planner_attempt_is_recorded_and_unchanged_retry_is_suppressed(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"known gap"}}', encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "fingerprint", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    harness = FailingPlannerHarness(HarnessConfig(kind="codex", executable="codex"))
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        harness,
        cast(Notifier, NullNotifier()),
        model_policy(),
    )

    first = engine.cycle(repo, shadow=False, execute=True)
    second = engine.cycle(repo, shadow=False, execute=True)
    forced = engine.cycle(repo, shadow=False, execute=True, force_replan=True)

    assert first.event.event_type == "PLANNING_FAILED"
    assert first.exit_code == 3
    assert second.event.event_type == "STALLED"
    assert second.exit_code == 2
    assert forced.event.event_type == "PLANNING_FAILED"
    assert harness.calls == 2
    summary = state.usage_summary(repo=repo.full_name)
    assert summary["direct_calls"]["calls"] == 2
    assert summary["direct_calls"]["unknown_calls"] == 2


def test_supervised_approval_executes_stored_decision_without_replanning(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"known gap"}}', encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.SUPERVISED)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "fingerprint", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    harness = CapturingHarness(HarnessConfig(kind="codex", executable="codex"))
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        harness,
        cast(Notifier, NullNotifier()),
        model_policy(),
    )

    proposed = engine.cycle(repo, shadow=True, execute=False)
    action_id = str(proposed.event.evidence["action_id"])
    state.approve(repo.full_name, action_id, "owner")
    executed = engine.cycle(repo, shadow=False, execute=True)

    assert executed.event.event_type == "STATUS_REPORTED"
    assert harness.calls == 1
    stored = state.proposal(repo.full_name, action_id)
    assert stored is not None
    assert stored["status"] == "executed"


def test_supervised_approved_implementation_uses_workboard_workers(
    tmp_path: Path,
) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"known gap"}}', encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.SUPERVISED)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "fingerprint", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    harness = ImplementationQueueHarness(HarnessConfig(kind="codex", executable="codex"))
    queue = CapturingOrchestrator()
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        harness,
        cast(Notifier, NullNotifier()),
        model_policy(),
        orchestrator=cast(WorkflowOrchestrator, queue),
    )
    engine.worktrees = cast(Any, StubWorktrees(tmp_path / "worktrees"))

    proposed = engine.cycle(repo, shadow=True, execute=False)
    action_id = str(proposed.event.evidence["action_id"])
    state.approve(repo.full_name, action_id, "owner")
    queued = engine.cycle(repo, shadow=False, execute=True)

    assert proposed.event.event_type in {"ACTION_PROPOSED", "APPROVAL_REQUIRED"}
    assert queued.event.event_type == "WORKFLOW_QUEUED"
    assert queued.event.evidence["approved_action_id"] == action_id
    assert len(queue.decisions) == 1
    assert queue.decisions[0].action == ActionKind.IMPLEMENT
    assert harness.calls == 1
    stored = state.proposal(repo.full_name, action_id)
    assert stored is not None
    assert stored["status"] == "queued"
    assert state.approved_proposal(repo.full_name) is None


def test_autonomous_approved_implementation_resume_uses_workboard_workers(
    tmp_path: Path,
) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"known gap"}}', encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "fingerprint", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement issue 39",
        reason="The issue is the next dependency-ready work item.",
        target_issue=39,
        acceptance_criteria=("Scope is correct", "Checks pass"),
        requires_owner_approval=True,
        owner_blocker="GOAL_DIVERGENCE: implementation scope needs owner confirmation",
    )
    snapshot = RepositorySnapshot({}, [], [], ["main"], [])
    snapshot_fingerprint = hashlib.sha256(
        json.dumps(snapshot.as_dict(), sort_keys=True, default=str).encode()
    ).hexdigest()
    action_id = "action-39"
    state.save_proposal(
        repo.full_name,
        action_id,
        snapshot_fingerprint,
        decision.model_dump(mode="json"),
    )
    state.transition(repo.full_name, RunState.BLOCKED)
    state.record_event(
        repo=repo.full_name,
        run_id="blocked-run",
        state=RunState.BLOCKED,
        event_type="APPROVAL_REQUIRED",
        summary=decision.summary,
        reason="planner marked this action as requiring owner approval",
        fingerprint="approval-fingerprint",
        evidence={
            "action_id": action_id,
            "decision": decision.model_dump(mode="json"),
            "model": "test-model",
        },
    )
    queue = CapturingOrchestrator()
    harness = ImplementationQueueHarness(HarnessConfig(kind="codex", executable="codex"))
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        harness,
        cast(Notifier, NullNotifier()),
        model_policy(),
        orchestrator=cast(WorkflowOrchestrator, queue),
    )
    engine.worktrees = cast(Any, StubWorktrees(tmp_path / "worktrees"))

    blocked = engine.cycle(repo, shadow=False, execute=True)

    assert blocked.event.event_type == "ATTENTION_REQUIRED"
    assert queue.decisions == []
    assert harness.calls == 0

    state.approve(repo.full_name, action_id, "owner")
    resumed = engine.cycle(repo, shadow=False, execute=True)

    assert resumed.event.event_type == "WORKFLOW_QUEUED"
    assert resumed.event.evidence["approved_action_id"] == action_id
    assert len(queue.decisions) == 1
    assert queue.decisions[0].action == ActionKind.IMPLEMENT
    assert harness.calls == 0
    stored = state.proposal(repo.full_name, action_id)
    assert stored is not None
    assert stored["status"] == "queued"


def test_owner_blocker_is_preserved_when_default_branch_planning_context_is_unavailable(
    tmp_path: Path,
) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Local plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"known gap"}}', encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "fingerprint", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    state.transition(repo.full_name, RunState.BLOCKED)
    state.record_event(
        repo=repo.full_name,
        run_id="owner-blocked-run",
        state=RunState.BLOCKED,
        event_type="ATTENTION_REQUIRED",
        summary="A decision is required for the current scope.",
        reason="The implementation diverges from the approved goal.",
        fingerprint="owner-blocker",
        evidence={
            "owner_required": True,
            "blocker": "GOAL_DIVERGENCE: implementation scope needs owner confirmation",
            "next_action": "Confirm the implementation scope.",
        },
    )
    harness = CapturingHarness(HarnessConfig(kind="codex", executable="codex"))

    def runner(command: object, **_: object) -> CommandResult:
        args = [str(item) for item in cast(list[object], command)]
        if "status" in args or "branch" in args:
            return CommandResult(0, "" if "status" in args else "main\n", "")
        return CommandResult(1, "", "origin unavailable")

    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, SnapshotGitHub()),
        harness,
        cast(Notifier, NullNotifier()),
        model_policy(),
        runner=runner,
    )

    result = engine.cycle(repo, shadow=False, execute=True)

    assert result.event.event_type == "ATTENTION_REQUIRED"
    assert result.event.evidence["blocker"].startswith("GOAL_DIVERGENCE:")
    assert harness.calls == 0


def test_autonomous_cycle_requires_and_then_consumes_owner_approval(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"known gap"}}', encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "fingerprint", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    decision = PlanDecision(
        action=ActionKind.UPDATE_ISSUE,
        summary="Retarget issue #6",
        reason="The work contract is stale",
        target_issue=6,
        issue_title="Implement the first slice",
        issue_body="Focused implementation scope",
        requires_owner_approval=True,
        owner_blocker="GOAL_DIVERGENCE: the stored proposal no longer matches the approved goal",
    )
    snapshot = RepositorySnapshot({}, [], [], ["main"], [])
    snapshot_fingerprint = hashlib.sha256(
        json.dumps(snapshot.as_dict(), sort_keys=True, default=str).encode()
    ).hexdigest()
    action_id = "action-6"
    state.save_proposal(
        repo.full_name,
        action_id,
        snapshot_fingerprint,
        decision.model_dump(mode="json"),
    )
    state.transition(repo.full_name, RunState.BLOCKED)
    state.record_event(
        repo=repo.full_name,
        run_id="blocked-run",
        state=RunState.BLOCKED,
        event_type="APPROVAL_REQUIRED",
        summary=decision.summary,
        reason="planner marked this action as requiring owner approval",
        fingerprint="soft-block",
        evidence={
            "action_id": action_id,
            "decision": decision.model_dump(mode="json"),
            "model": "test-model",
        },
    )
    github = IssueGitHub()
    harness = ApprovalFlagHarness(HarnessConfig(kind="codex", executable="codex"))
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        harness,
        cast(Notifier, NullNotifier()),
        model_policy(),
    )

    blocked = engine.cycle(repo, shadow=False, execute=True)

    assert blocked.event.event_type == "ATTENTION_REQUIRED"
    assert github.updated == []
    assert harness.calls == 0
    stored = state.proposal(repo.full_name, action_id)
    assert stored is not None
    assert stored["status"] == "proposed"

    state.approve(repo.full_name, action_id, "owner")
    result = engine.cycle(repo, shadow=False, execute=True)

    assert result.event.event_type == "ISSUE_UPDATED"
    assert github.updated == [6]
    assert harness.calls == 0
    stored = state.proposal(repo.full_name, action_id)
    assert stored is not None
    assert stored["status"] == "executed"


def test_autonomous_bare_approval_flag_replans_without_owner_page(tmp_path: Path) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"known gap"}}', encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "fingerprint", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    github = IssueGitHub()
    harness = BareThenRoutineHarness(HarnessConfig(kind="codex", executable="codex"))
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        harness,
        cast(Notifier, NullNotifier()),
        model_policy(),
    )

    first = engine.cycle(repo, shadow=False, execute=True)
    second = engine.cycle(repo, shadow=False, execute=True)

    assert first.event.event_type == "PLANNER_APPROVAL_INVALID"
    assert first.event.state == RunState.DEGRADED
    assert "explicit owner blocker" in first.event.reason
    assert second.event.event_type == "ISSUE_UPDATED"
    assert github.updated == [6]
    assert harness.calls == 2
