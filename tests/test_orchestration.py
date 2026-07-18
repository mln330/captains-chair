from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

import captains_chair.orchestration as orchestration
from captains_chair.models import (
    ActionKind,
    CompletionPolicy,
    DirectOrchestratorConfig,
    OpenClawWorkboardConfig,
    OperationMode,
    PlanDecision,
    WorkerAssignments,
)
from captains_chair.orchestration import (
    BlockerKind,
    QueueCard,
    QueueCardSpec,
    QueueStatus,
    WorkflowOrchestrator,
    WorkspaceRef,
    WorkStage,
    build_workflow,
    classify_blocker,
    worker_model_health,
    workflow_label,
)
from tests.fakes import InMemoryWorkQueue
from tests.helpers import repo_config

_block_reason = orchestration._block_reason  # pyright: ignore[reportPrivateUsage]
_card_stage = orchestration._card_stage  # pyright: ignore[reportPrivateUsage]
_completion_summary = orchestration._completion_summary  # pyright: ignore[reportPrivateUsage]
_failure_count = orchestration._failure_count  # pyright: ignore[reportPrivateUsage]
_has_passed_proof = orchestration._has_passed_proof  # pyright: ignore[reportPrivateUsage]
_has_valid_proof = orchestration._has_valid_proof  # pyright: ignore[reportPrivateUsage]
_passed_proof = orchestration._passed_proof  # pyright: ignore[reportPrivateUsage]
_retry_depth = orchestration._retry_depth  # pyright: ignore[reportPrivateUsage]
_retry_limit = orchestration._retry_limit  # pyright: ignore[reportPrivateUsage]
_is_retry_descendant = orchestration._is_retry_descendant  # pyright: ignore[reportPrivateUsage]


def worker_config() -> OpenClawWorkboardConfig:
    return OpenClawWorkboardConfig(
        require_live_completion_validation=False,
        workers=WorkerAssignments(
            captain="captains-chair",
            coder="github-coder",
            reviewer="github-reviewer",
            tester="github-tester",
            ux_reviewer="github-ux",
            final_reviewer="github-final",
            merger="github-merge",
            verifier="github-verify",
        )
    )


def implementation_decision() -> PlanDecision:
    return PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement workspace-scoped Etsy routes",
        reason="Issue 39 is the next unblocked roadmap item.",
        target_issue=39,
        acceptance_criteria=("Enforce workspace membership", "Keep CI green"),
    )


def test_qa_notes_use_planned_changed_paths_and_portable_worker_protocol(tmp_path: Path) -> None:
    repo = repo_config(tmp_path).model_copy(update={"surfaces": frozenset()})
    decision = implementation_decision().model_copy(
        update={"changed_paths": ("frontend/SearchResults.tsx",)}
    )

    workflow = build_workflow(repo, decision, "qa-paths", worker_config())
    test_card = next(card for card in workflow.stages if "stage:ux_review" in card.labels)

    assert "web_ui" in test_card.notes
    assert "configured orchestrator" in test_card.notes
    assert "Workboard complete" not in test_card.notes


def test_live_completion_validator_is_required_by_default(tmp_path: Path) -> None:
    del tmp_path
    strict_config = worker_config().model_copy(update={"require_live_completion_validation": True})

    with pytest.raises(ValueError, match="live completion validation is required"):
        WorkflowOrchestrator(InMemoryWorkQueue(), strict_config)


@pytest.mark.parametrize(
    ("reason", "expected"),
    (
        ("USER_SECRET: token is required", BlockerKind.USER_SECRET),
        ("GOAL_DIVERGENCE: roadmap conflict", BlockerKind.GOAL_DIVERGENCE),
        ("EXTERNAL_ACCESS: repository is unavailable", BlockerKind.EXTERNAL_ACCESS),
        ("HIGH_RISK_DECISION: release scope changed", BlockerKind.HIGH_RISK_DECISION),
        ("TECHNICAL: test failure", BlockerKind.TECHNICAL),
    ),
)
def test_blocker_classification_is_explicit_and_case_insensitive(
    reason: str, expected: BlockerKind
) -> None:
    assert classify_blocker(reason.lower()) == expected


def test_autonomous_workflow_is_role_separated_and_dependency_gated(tmp_path: Path) -> None:
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    workflow = build_workflow(
        repo,
        implementation_decision(),
        "action-1234567890",
        worker_config(),
        workspace=WorkspaceRef(kind="worktree", path=tmp_path / "work", branch="captains_chair/work/39"),
    )

    by_stage = {card.labels[-1]: card for card in workflow.stages}
    implementation = by_stage["stage:implementation"]
    review = by_stage["stage:review"]
    test = by_stage["stage:test"]
    final = by_stage["stage:final_review"]
    merge = by_stage["stage:merge"]
    verify = by_stage["stage:post_merge"]

    assert implementation.agent_id == "github-coder"
    assert review.agent_id == "github-reviewer"
    assert implementation.agent_id != review.agent_id
    assert implementation.workspace is not None
    assert review.workspace == implementation.workspace
    assert test.workspace == implementation.workspace
    assert review.parents == (implementation.key,)
    assert test.parents == (implementation.key,)
    assert set(final.parents) == {review.key, test.key}
    assert merge.parents == (final.key,)
    assert merge.agent_id is None
    assert merge.metadata["workerRole"] == "deterministic_merge"
    assert merge.metadata["expectedModel"] == "deterministic/no-model"
    assert implementation.metadata["expectedModel"] == "codex/gpt-5.3-codex-spark"
    assert verify.parents == (merge.key,)
    assert "Never merge your own work" in implementation.notes
    assert "USER_SECRET:" in final.notes
    assert "Configured completion policy: auto_merge." in final.notes
    assert "Required final-review marker: AUTO_MERGE_ALLOWED:<head-sha>." in final.notes
    assert all("OpenClaw" not in (card.notes or "") for card in workflow.stages)


def test_direct_orchestrator_keeps_declared_merge_worker(tmp_path: Path) -> None:
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    config = DirectOrchestratorConfig(
        database_path=tmp_path / "direct.db",
        workers=worker_config().workers,
    )

    workflow = build_workflow(repo, implementation_decision(), "direct-merge", config)
    merge = next(card for card in workflow.stages if "stage:merge" in card.labels)

    assert merge.agent_id == "github-merge"
    assert merge.metadata["workerRole"] == "merger"
    assert merge.metadata["expectedModel"] == "codex/gpt-5.6-terra"


def test_workflow_propagates_course_and_package_context(tmp_path: Path) -> None:
    decision = implementation_decision().model_copy(
        update={"course_key": "course-1", "work_package_key": "package-1"}
    )

    workflow = build_workflow(repo_config(tmp_path), decision, "context", worker_config())

    assert workflow.root.metadata == {
        "courseKey": "course-1",
        "workPackageKey": "package-1",
    }
    assert all(card.metadata["courseKey"] == "course-1" for card in workflow.stages)
    assert all(card.metadata["workPackageKey"] == "package-1" for card in workflow.stages)


def test_completed_workflow_cleans_shared_workspace_without_touching_branch_metadata(
    tmp_path: Path,
) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS, completion=CompletionPolicy.OWNER_APPROVAL)
    workspace = WorkspaceRef(
        kind="worktree", path=tmp_path / "managed-worktree", branch="captains_chair/work/39"
    )
    queue = InMemoryWorkQueue()
    cleaned: list[tuple[str, str | None]] = []

    def cleanup(clean_repo: Any, clean_workspace: WorkspaceRef) -> bool:
        cleaned.append((clean_repo.full_name, clean_workspace.branch))
        return True

    orchestrator = WorkflowOrchestrator(queue, worker_config(), workspace_cleanup=cleanup)
    workflow = orchestrator.enqueue(repo, implementation_decision(), "cleanup-action", workspace=workspace)
    for card_id in workflow.stage_cards.values():
        note = (
            "READY_FOR_OWNER:abcdef1"
            if "stage:final_review" in queue.cards[card_id].labels
            else "current-head proof"
        )
        queue.complete_card(
            card_id,
            summary="Passed",
            proof=({"status": "passed", "note": note},),
        )

    result = orchestrator.reconcile(repo, dispatch=False, dispatch_reason="test")

    assert workspace.path is not None
    assert result.cleaned_workspaces == (str(workspace.path.resolve()),)
    assert result.workspace_cleanup_failures == ()
    assert cleaned == [(repo.full_name, workspace.branch)]
    assert result.dispatch["status"] == "dispatch_suppressed"


def test_incomplete_or_inconsistent_workflow_never_cleans_workspace(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS, completion=CompletionPolicy.OWNER_APPROVAL)
    workspace = WorkspaceRef(kind="worktree", path=tmp_path / "managed-worktree", branch="captains_chair/work/39")
    queue = InMemoryWorkQueue()
    cleaned: list[str] = []
    orchestrator = WorkflowOrchestrator(
        queue,
        worker_config(),
        workspace_cleanup=lambda _repo, value: cleaned.append(str(value.path)) or True,
    )
    workflow = orchestrator.enqueue(repo, implementation_decision(), "partial-action", workspace=workspace)
    queue.complete_card(
        workflow.stage_cards["partial-action:implementation"],
        summary="Only implementation passed",
        proof=({"status": "passed"},),
    )

    incomplete = orchestrator.reconcile(repo, dispatch=False, dispatch_reason="test")
    assert incomplete.cleaned_workspaces == ()
    assert cleaned == []

    for card_id in workflow.stage_cards.values():
        note = (
            "READY_FOR_OWNER:abcdef1"
            if "stage:final_review" in queue.cards[card_id].labels
            else "passed"
        )
        queue.complete_card(card_id, summary="Passed", proof=({"status": "passed", "note": note},))
    review_id = workflow.stage_cards["partial-action:review"]
    queue.cards[review_id] = queue.cards[review_id].model_copy(
        update={
            "workspace": WorkspaceRef(
                kind="worktree", path=tmp_path / "different-worktree", branch="captains_chair/work/other"
            )
        }
    )

    inconsistent = orchestrator.reconcile(repo, dispatch=False, dispatch_reason="test")
    assert inconsistent.cleaned_workspaces == ()
    assert len(inconsistent.workspace_cleanup_failures) == 1
    assert "inconsistent workspace references" in inconsistent.workspace_cleanup_failures[0]
    assert cleaned == []


def test_workspace_cleanup_failure_does_not_stop_unrelated_dispatch(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS, completion=CompletionPolicy.OWNER_APPROVAL)
    workspace = WorkspaceRef(kind="worktree", path=tmp_path / "managed-worktree", branch="captains_chair/work/39")
    queue = InMemoryWorkQueue()

    def cleanup(_repo: Any, _workspace: WorkspaceRef) -> bool:
        raise RuntimeError("dirty worktree")

    orchestrator = WorkflowOrchestrator(queue, worker_config(), workspace_cleanup=cleanup)
    workflow = orchestrator.enqueue(repo, implementation_decision(), "failure-action", workspace=workspace)
    for card_id in workflow.stage_cards.values():
        note = (
            "READY_FOR_OWNER:abcdef1"
            if "stage:final_review" in queue.cards[card_id].labels
            else "passed"
        )
        queue.complete_card(card_id, summary="Passed", proof=({"status": "passed", "note": note},))
    queue.cards["unrelated-card"] = QueueCard(
        id="unrelated-card",
        title="Unrelated ready work",
        status=QueueStatus.READY,
        labels=("captains_chair", "stage:implementation"),
        agent_id="github-coder",
    )

    result = orchestrator.reconcile(repo)

    assert result.workspace_cleanup_failures == (f"{workflow_label(workflow.workflow_id)[9:]}: dirty worktree",)
    assert queue.dispatches == 1


def test_recovery_adapter_failure_does_not_stop_unrelated_dispatch(tmp_path: Path) -> None:
    class BrokenRecoveryQueue(InMemoryWorkQueue):
        def recover_ended_workers(self, board_id: str, cards: list[QueueCard]) -> tuple[str, ...]:
            del board_id, cards
            raise RuntimeError("session gateway unavailable")

    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS, completion=CompletionPolicy.OWNER_APPROVAL)
    queue = BrokenRecoveryQueue()
    orchestrator = WorkflowOrchestrator(queue, worker_config())
    workflow = orchestrator.enqueue(repo, implementation_decision(), "recovery-fault")

    result = orchestrator.reconcile(repo)

    assert result.recovery_warnings == ("Worker recovery adapter failed: session gateway unavailable",)
    assert queue.dispatches == 1
    assert queue.cards[workflow.stage_cards["recovery-fault:implementation"]].status == QueueStatus.READY


def test_owner_completion_policy_does_not_create_merge_or_post_merge_workers(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, completion=CompletionPolicy.OWNER_APPROVAL)
    workflow = build_workflow(repo, implementation_decision(), "owner-action", worker_config())

    labels = {card.labels[-1] for card in workflow.stages}
    assert "stage:final_review" in labels
    assert "stage:merge" not in labels
    assert "stage:post_merge" not in labels


def test_orchestration_card_helpers_fail_closed_and_preserve_latest_evidence(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, completion=CompletionPolicy.OWNER_APPROVAL)
    protocol = QueueCard(
        id="protocol",
        title="Protocol",
        status=QueueStatus.BLOCKED,
        labels=("stage:review",),
        metadata={"workerProtocol": {"detail": "protocol detail"}},
    )
    logs = protocol.model_copy(
        update={"metadata": {"workerLogs": [{"message": "old"}, {"message": "latest"}]}}
    )
    attempts = protocol.model_copy(
        update={"metadata": {"attempts": [{"error": "provider error"}]}}
    )
    bare = protocol.model_copy(update={"metadata": {}})

    assert _card_stage(protocol) == WorkStage.REVIEW
    assert _card_stage(protocol.model_copy(update={"labels": ("stage:unknown",)})) is None
    assert _card_stage(protocol.model_copy(update={"labels": ()})) is None
    assert _block_reason(protocol) == "protocol detail"
    assert _block_reason(logs) == "latest"
    assert _block_reason(attempts) == "provider error"
    assert _block_reason(bare).startswith("TECHNICAL:")
    assert _failure_count(protocol) == 1
    assert _failure_count(protocol.model_copy(update={"metadata": {"failureCount": 0}})) == 0
    assert _failure_count(protocol.model_copy(update={"metadata": {"attempts": []}})) == 1
    assert _retry_limit(protocol, 2) == 2
    assert _retry_limit(
        protocol.model_copy(update={"metadata": {"automation": {"maxRetries": 4}}}), 2
    ) == 4

    passed = protocol.model_copy(
        update={"metadata": {"proof": [{"status": "failed"}, {"status": "PASSED", "note": "proof"}]}}
    )
    assert _has_passed_proof(passed)
    assert _passed_proof(passed)[0]["note"] == "proof"
    assert _has_valid_proof(repo, passed)
    assert not _has_passed_proof(bare)
    assert not _has_valid_proof(repo, bare)
    assert _completion_summary(passed) == "Recovered completed review card from runtime review status."


def test_passed_proof_handoff_uses_only_latest_record() -> None:
    card = QueueCard(
        id="proof-1",
        title="Proof",
        status=QueueStatus.DONE,
        metadata={
            "proof": [
                {"status": "passed", "note": "old"},
                {"status": "passed", "note": "latest"},
            ]
        },
    )

    assert _passed_proof(card) == ({"status": "passed", "note": "latest"},)


def test_final_completion_proof_requires_current_head_marker_for_each_policy(tmp_path: Path) -> None:
    card = QueueCard(
        id="final",
        title="Final",
        status=QueueStatus.DONE,
        labels=("stage:final_review",),
        metadata={"proof": [{"status": "passed", "note": "READY_FOR_OWNER: abcdef1"}]},
    )
    for policy, marker in (
        (CompletionPolicy.OWNER_APPROVAL, "READY_FOR_OWNER:"),
        (CompletionPolicy.CONTROL_PLANE_COMPLETE, "CONTROL_PLANE_COMPLETE:"),
        (CompletionPolicy.AUTO_MERGE, "AUTO_MERGE_ALLOWED:"),
    ):
        repo = repo_config(
            tmp_path,
            mode=OperationMode.AUTONOMOUS if policy == CompletionPolicy.AUTO_MERGE else OperationMode.ADVISORY,
            completion=policy,
        )
        valid = card.model_copy(
            update={"metadata": {"proof": [{"status": "passed", "label": f"{marker}abcdef1"}]}}
        )
        assert _has_valid_proof(repo, valid)
    invalid = card.model_copy(
        update={"metadata": {"proof": [{"status": "passed", "note": "READY_FOR_OWNER: not-a-sha"}]}}
    )
    assert not _has_valid_proof(repo_config(tmp_path), invalid)


def test_worker_health_reports_adapter_capability_failures() -> None:
    class NoHealth:
        pass

    class BrokenHealth:
        def validate_worker_models(self) -> dict[str, str]:
            raise RuntimeError("health unavailable")

    class WrongHealth:
        def validate_worker_models(self) -> list[str]:
            return []

    class Healthy:
        def validate_worker_models(self) -> dict[str, str]:
            return {"status": "ok"}

    health = cast(Any, worker_model_health)
    assert health(NoHealth()) == {"status": "not_supported"}
    assert health(BrokenHealth()) == {
        "status": "degraded",
        "error": "health unavailable",
    }
    assert health(WrongHealth())["status"] == "degraded"
    assert health(Healthy()) == {"status": "ok"}


def test_retry_depth_handles_missing_and_cyclic_parent_labels() -> None:
    root = QueueCard(id="root", title="Root", status=QueueStatus.BLOCKED, labels=("stage:review",))
    child = QueueCard(
        id="child",
        title="Child",
        status=QueueStatus.BLOCKED,
        labels=("stage:review", "retry-for:root"),
    )
    missing = QueueCard(
        id="missing",
        title="Missing",
        status=QueueStatus.BLOCKED,
        labels=("stage:review", "retry-for:does-not-exist"),
    )
    cycle = root.model_copy(update={"labels": ("stage:review", "retry-for:child")})

    assert _retry_depth(root, [root]) == 0
    assert _retry_depth(child, [root, child]) == 1
    assert _retry_depth(missing, [missing]) == 1
    assert _retry_depth(cycle, [cycle, child]) == 2


def test_repair_and_merge_actions_use_review_appropriate_worker_topology(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS, completion=CompletionPolicy.AUTO_MERGE)
    repair = build_workflow(
        repo,
        PlanDecision(
            action=ActionKind.REPAIR_PR,
            summary="Repair the current pull request",
            reason="The independent review found a repairable defect.",
            target_pr=42,
        ),
        "repair-action",
        worker_config(),
    )
    merge = build_workflow(
        repo,
        PlanDecision(
            action=ActionKind.MERGE_PR,
            summary="Complete the reviewed pull request",
            reason="The owner approved the exact merge action.",
            target_pr=42,
        ),
        "merge-action",
        worker_config(),
    )

    repair_stages = [card.labels[-1] for card in repair.stages]
    merge_stages = [card.labels[-1] for card in merge.stages]
    assert repair_stages[0] == "stage:repair"
    assert repair.stages[0].agent_id == "github-coder"
    assert repair.stages[1].parents == (repair.stages[0].key,)
    assert merge_stages[0] == "stage:review"
    assert "stage:implementation" not in merge_stages
    assert "stage:merge" in merge_stages


@pytest.mark.parametrize(
    ("completion", "marker"),
    (
        (CompletionPolicy.OWNER_APPROVAL, "READY_FOR_OWNER:abcdef1"),
        (CompletionPolicy.CONTROL_PLANE_COMPLETE, "CONTROL_PLANE_COMPLETE:abcdef1"),
        (CompletionPolicy.AUTO_MERGE, "AUTO_MERGE_ALLOWED:abcdef1"),
    ),
)
def test_final_review_completion_requires_policy_marker(
    tmp_path: Path, completion: CompletionPolicy, marker: str
) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS, completion=completion)
    queue = InMemoryWorkQueue()
    workflow = WorkflowOrchestrator(queue, worker_config()).enqueue(
        repo, implementation_decision(), f"marker-{completion.value}"
    )
    final_id = workflow.stage_cards[f"marker-{completion.value}:final_review"]
    queue.cards[final_id] = queue.cards[final_id].model_copy(
        update={
            "status": QueueStatus.REVIEW,
            "metadata": {"proof": [{"status": "passed", "note": marker}]},
        }
    )

    result = WorkflowOrchestrator(queue, worker_config()).reconcile(
        repo, dispatch=False, dispatch_reason="test"
    )

    assert result.protocol_retries == ()
    assert queue.cards[final_id].status == QueueStatus.DONE


def test_final_review_generic_passed_proof_is_retried_instead_of_completed(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    queue = InMemoryWorkQueue()
    workflow = WorkflowOrchestrator(queue, worker_config()).enqueue(
        repo, implementation_decision(), "missing-marker"
    )
    final_id = workflow.stage_cards["missing-marker:final_review"]
    queue.cards[final_id] = queue.cards[final_id].model_copy(
        update={
            "status": QueueStatus.REVIEW,
            "metadata": {
                "proof": [
                    {"status": "passed", "note": "old AUTO_MERGE_ALLOWED:abcdef1"},
                    {"status": "passed", "note": "all gates passed"},
                ]
            },
        }
    )

    result = WorkflowOrchestrator(queue, worker_config()).reconcile(
        repo, dispatch=False, dispatch_reason="test"
    )

    assert len(result.protocol_retries) == 1
    assert queue.cards[final_id].status == QueueStatus.REVIEW


def test_partial_enqueue_cleans_allocated_workspace(tmp_path: Path) -> None:
    class FailingQueue(InMemoryWorkQueue):
        def ensure_board(self, board_id: str, name: str, description: str, workspace: Path) -> None:
            del board_id, name, description, workspace
            raise RuntimeError("gateway disconnected before materialization")

    repo = repo_config(tmp_path)
    workspace = WorkspaceRef(kind="worktree", path=tmp_path / "managed-worktree", branch="captains_chair/work/39")
    cleaned: list[str] = []
    orchestrator = WorkflowOrchestrator(
        FailingQueue(),
        worker_config(),
        workspace_cleanup=lambda _repo, value: cleaned.append(str(value.path)) or True,
    )

    with pytest.raises(RuntimeError, match="gateway disconnected"):
        orchestrator.enqueue(repo, implementation_decision(), "partial-enqueue", workspace=workspace)

    assert cleaned == [str(workspace.path)]


def test_partial_enqueue_preserves_workspace_after_root_card_exists(tmp_path: Path) -> None:
    class FailingQueue(InMemoryWorkQueue):
        def create_card(self, board_id: str, spec: QueueCardSpec) -> QueueCard:
            if len(self.cards) >= 2:
                raise RuntimeError("gateway disconnected while creating stage card")
            return super().create_card(board_id, spec)

    repo = repo_config(tmp_path)
    workspace = WorkspaceRef(kind="worktree", path=tmp_path / "managed-worktree", branch="captains_chair/work/39")
    cleaned: list[str] = []
    queue = FailingQueue()
    orchestrator = WorkflowOrchestrator(
        queue,
        worker_config(),
        workspace_cleanup=lambda _repo, value: cleaned.append(str(value.path)) or True,
    )

    with pytest.raises(RuntimeError, match="gateway disconnected"):
        orchestrator.enqueue(repo, implementation_decision(), "root-materialized", workspace=workspace)

    assert cleaned == []
    assert len(queue.cards) == 2
    assert any(card.workspace == workspace for card in queue.cards.values())


def test_issue_mutation_uses_control_plane_worker_and_verifier(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    decisions = (
        PlanDecision(
            action=ActionKind.UPDATE_ISSUE,
            summary="Clarify issue 7",
            reason="Acceptance criteria are ambiguous.",
            target_issue=7,
        ),
        PlanDecision(
            action=ActionKind.LABEL_ISSUE,
            summary="Label issue 7",
            reason="The issue is ready for implementation.",
            target_issue=7,
            issue_labels=("ready-for-dev",),
        ),
        PlanDecision(
            action=ActionKind.RETARGET_ISSUE,
            summary="Retarget issue 7",
            reason="The issue moved into the active sprint.",
            target_issue=7,
            issue_milestone="Sprint 2",
        ),
    )

    for index, decision in enumerate(decisions):
        workflow = build_workflow(repo, decision, f"issue-action-{index}", worker_config())
        assert [card.agent_id for card in workflow.stages] == ["captains-chair", "github-verify"]
        assert workflow.stages[1].parents == (workflow.stages[0].key,)


def test_only_explicit_user_blocker_tags_require_intervention() -> None:
    assert classify_blocker("USER_SECRET: Azure token required") == BlockerKind.USER_SECRET
    assert classify_blocker("goal_divergence: choose a different product") == BlockerKind.GOAL_DIVERGENCE
    assert classify_blocker("EXTERNAL_ACCESS: GitHub permission missing") == BlockerKind.EXTERNAL_ACCESS
    assert classify_blocker("HIGH_RISK_DECISION: production migration") == BlockerKind.HIGH_RISK_DECISION
    assert classify_blocker("TECHNICAL: compiler failed") == BlockerKind.TECHNICAL
    assert classify_blocker("CI failed") == BlockerKind.TECHNICAL


class MemoryQueue:
    def __init__(self) -> None:
        self.cards: dict[str, QueueCard] = {}
        self.specs: list[QueueCardSpec] = []
        self.completed: list[tuple[str, tuple[str, ...]]] = []
        self.board: str | None = None
        self.reclaimed: list[str] = []
        self.reassigned: list[tuple[str, str]] = []
        self.unblocked: list[str] = []
        self.dispatch_count = 0

    def ensure_board(self, board_id: str, name: str, description: str, workspace: Path) -> None:
        del name, description, workspace
        self.board = board_id

    def list_cards(self, board_id: str) -> list[QueueCard]:
        del board_id
        return list(self.cards.values())

    def create_card(self, board_id: str, spec: QueueCardSpec) -> QueueCard:
        del board_id
        self.specs.append(spec)
        existing = self.cards.get(spec.key)
        if existing:
            return existing
        card = QueueCard(
            id=f"card-{len(self.cards) + 1}",
            title=spec.title,
            notes=spec.notes,
            status=spec.status,
            priority=spec.priority,
            labels=spec.labels,
            agent_id=spec.agent_id,
            source_url=spec.source_url,
            workspace=spec.workspace,
        )
        self.cards[spec.key] = card
        return card

    def complete_card(
        self,
        card_id: str,
        *,
        summary: str,
        proof: tuple[dict[str, Any], ...] = (),
        created_card_ids: tuple[str, ...] = (),
    ) -> QueueCard:
        del summary, proof
        self.completed.append((card_id, created_card_ids))
        return next(card for card in self.cards.values() if card.id == card_id).model_copy(
            update={"status": QueueStatus.DONE}
        )

    def unblock_card(self, card_id: str) -> QueueCard:
        self.unblocked.append(card_id)
        return next(card for card in self.cards.values() if card.id == card_id)

    def reclaim_card(self, card_id: str, *, status: QueueStatus, reason: str) -> QueueCard:
        del status, reason
        self.reclaimed.append(card_id)
        return next(card for card in self.cards.values() if card.id == card_id)

    def reassign_card(
        self,
        card_id: str,
        *,
        agent_id: str,
        status: QueueStatus,
        reset_failures: bool,
        reason: str,
    ) -> QueueCard:
        del status, reset_failures, reason
        self.reassigned.append((card_id, agent_id))
        return next(card for card in self.cards.values() if card.id == card_id)

    def comment(self, card_id: str, body: str) -> QueueCard:
        del body
        return next(card for card in self.cards.values() if card.id == card_id)

    def dispatch(self, board_id: str) -> dict[str, Any]:
        self.dispatch_count += 1
        return {"boardId": board_id, "count": 0}

    def diagnostics(self) -> dict[str, Any]:
        return {"count": 0}


def test_enqueue_resolves_dependency_keys_to_card_ids_and_is_idempotent(tmp_path: Path) -> None:
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    queue = MemoryQueue()
    orchestrator = WorkflowOrchestrator(queue, worker_config())

    first = orchestrator.enqueue(repo, implementation_decision(), "stable-action")
    second = orchestrator.enqueue(repo, implementation_decision(), "stable-action")

    assert first.root_card_id == second.root_card_id
    assert first.stage_cards == second.stage_cards
    assert queue.board == "captains-chair-example-project"
    implementation_spec = next(spec for spec in queue.specs if spec.key.endswith(":implementation"))
    review_spec = next(
        spec
        for spec in queue.specs
        if spec.key.endswith(":review") and not spec.key.endswith(":final_review")
    )
    assert implementation_spec.parents == (first.root_card_id,)
    assert review_spec.parents == (first.stage_cards["stable-action:implementation"],)
    implementation_id = first.stage_cards["stable-action:implementation"]
    assert queue.completed[0][1] == (implementation_id,)


def test_merge_and_post_merge_cards_do_not_inherit_a_pr_branch_workspace(tmp_path: Path) -> None:
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    workspace = WorkspaceRef(
        kind="worktree",
        path=tmp_path / "pr-worktree",
        branch="captains_chair/review/pr-35-head-1",
        push_branch="feature/current-pr",
    )

    workflow = build_workflow(
        repo,
        implementation_decision(),
        "workspace-stage-boundary",
        worker_config(),
        workspace=workspace,
    )

    implementation = next(item for item in workflow.stages if item.key.endswith(":implementation"))
    final_review = next(item for item in workflow.stages if item.key.endswith(":final_review"))
    merge = next(item for item in workflow.stages if item.key.endswith(":merge"))
    post_merge = next(item for item in workflow.stages if item.key.endswith(":post_merge"))
    assert implementation.workspace == workspace
    assert final_review.workspace == workspace
    assert merge.workspace is None
    assert post_merge.workspace is None
    assert "Workspace contract:" in (implementation.notes or "")
    assert "Workspace contract:" in (final_review.notes or "")
    assert "Workspace contract:" not in (merge.notes or "")
    assert "Workspace contract:" not in (post_merge.notes or "")


def test_review_only_workflow_registers_only_root_level_parallel_children(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    decision = PlanDecision(
        action=ActionKind.REVIEW_PR,
        summary="Review current PR",
        reason="The PR needs independent gates.",
        target_pr=12,
    )
    queue = MemoryQueue()
    orchestrator = WorkflowOrchestrator(queue, worker_config())

    workflow = orchestrator.enqueue(repo, decision, "review-action")

    assert set(queue.completed[0][1]) == {
        workflow.stage_cards["review-action:review"],
        workflow.stage_cards["review-action:qa:custom-qa"],
    }


def test_reconcile_creates_coder_repair_for_blocked_review_and_keeps_dispatching(
    tmp_path: Path,
) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    blocked = QueueCard(
        id="review-1",
        title="Independent review",
        status=QueueStatus.BLOCKED,
        labels=("captains_chair", "stage:review"),
        agent_id="github-reviewer",
        source_url="https://github.com/example/project/pull/4",
        metadata={"workerProtocol": {"detail": "TECHNICAL: authorization test is missing"}},
    )
    queue.cards["blocked-review"] = blocked
    orchestrator = WorkflowOrchestrator(queue, worker_config())

    result = orchestrator.reconcile(repo)

    assert len(result.repairs_created) == 1
    repair_spec = queue.specs[-1]
    assert repair_spec.agent_id == "github-coder"
    assert "repair-for:review-1" in repair_spec.labels
    assert queue.dispatch_count == 1


def test_reconcile_can_suppress_worker_dispatch_after_recovery(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    queue.cards["ready"] = QueueCard(
        id="ready-1",
        title="Implementation",
        status=QueueStatus.READY,
        labels=("captains_chair", "stage:implementation"),
        agent_id="github-coder",
    )

    result = WorkflowOrchestrator(queue, worker_config()).reconcile(
        repo,
        dispatch=False,
        dispatch_reason="daily usage budget reached",
    )

    assert result.dispatch == {
        "status": "dispatch_suppressed",
        "reason": "daily usage budget reached",
        "promoted": [],
        "count": 0,
    }
    assert queue.dispatch_count == 0


def test_repair_label_is_compact_for_uuid_card(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    card_id = "a8917312-58e2-45c5-ab57-e4f340e29e76"
    workspace = WorkspaceRef(kind="worktree", path=tmp_path / "work", branch="captains_chair/work/39")
    queue.cards["blocked-review"] = QueueCard(
        id=card_id,
        title="Independent review",
        status=QueueStatus.BLOCKED,
        labels=("captains_chair", "stage:review"),
        workspace=workspace,
        metadata={"workerProtocol": {"detail": "TECHNICAL: issue found"}},
    )

    WorkflowOrchestrator(queue, worker_config()).reconcile(repo)

    repair = queue.specs[-1]
    assert all(len(label) <= 40 for label in repair.labels)
    assert any(label.startswith("repair:") for label in repair.labels)
    assert repair.workspace == workspace


def test_reconcile_leaves_real_user_blocker_but_dispatches_unrelated_work(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    queue.cards["blocked"] = QueueCard(
        id="blocked-1",
        title="Production credential",
        status=QueueStatus.BLOCKED,
        labels=("captains_chair", "stage:implementation"),
        metadata={"workerProtocol": {"detail": "USER_SECRET: Azure credential is required"}},
    )
    queue.cards["ready"] = QueueCard(
        id="ready-1",
        title="Unrelated documentation",
        status=QueueStatus.READY,
        labels=("captains_chair", "stage:implementation"),
    )
    orchestrator = WorkflowOrchestrator(queue, worker_config())

    result = orchestrator.reconcile(repo)

    assert result.user_blockers == ("blocked-1",)
    assert queue.reclaimed == []
    assert queue.dispatch_count == 1


def test_reconcile_routes_exhausted_technical_failure_to_control_plane_recovery(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    queue.cards["failed"] = QueueCard(
        id="coder-1",
        title="Implementation",
        status=QueueStatus.BLOCKED,
        labels=("captains_chair", "stage:implementation"),
        metadata={
            "failureCount": 3,
            "automation": {"maxRetries": 2},
            "workerProtocol": {"detail": "TECHNICAL: build still fails"},
        },
    )
    orchestrator = WorkflowOrchestrator(queue, worker_config())

    result = orchestrator.reconcile(repo)

    assert len(result.control_plane_recoveries) == 1
    recovery = queue.specs[-1]
    assert recovery.agent_id == "captains-chair"
    assert any(label.startswith("control-plane-recovery") for label in recovery.labels)
    assert queue.reassigned == []


def test_control_plane_action_without_completion_proof_is_retried_instead_of_counted_done(
    tmp_path: Path,
) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    queue.cards["recovery"] = QueueCard(
        id="recovery-1",
        title="Captain recovery",
        status=QueueStatus.DONE,
        labels=("captains_chair", "stage:control_plane_action", "control-plane-recovery-for:failed-1"),
        agent_id="captains-chair",
        metadata={},
    )
    orchestrator = WorkflowOrchestrator(queue, worker_config())

    result = orchestrator.reconcile(repo, dispatch=False, dispatch_reason="test")

    assert result.proof_retries == ("recovery-1",)
    assert len(result.protocol_retries) == 1
    retry = queue.specs[-1]
    assert retry.agent_id == "captains-chair"
    assert "stage:control_plane_action" in retry.labels
    assert "retry-for:recovery-1" in retry.labels
    assert result.cleaned_workspaces == ()


def test_control_plane_recovery_uses_a_fresh_card_for_uuid_worker_failure(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    failed_id = "7a20c13b-441d-4e99-b383-37aff1195c15"
    queue.cards["failed"] = QueueCard(
        id=failed_id,
        title="Repair findings",
        status=QueueStatus.BLOCKED,
        labels=("captains_chair", "stage:repair"),
        metadata={
            "failureCount": 3,
            "automation": {"maxRetries": 2},
            "workerProtocol": {"detail": "TECHNICAL: repair keeps failing"},
        },
    )

    result = WorkflowOrchestrator(queue, worker_config()).reconcile(repo)

    assert len(result.control_plane_recoveries) == 1
    recovery = queue.specs[-1]
    assert all(len(label) <= 40 for label in recovery.labels)
    assert recovery.agent_id == "captains-chair"
    assert recovery.key.endswith(":1")


def test_completed_repair_unblocks_original_independent_gate(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    queue.cards["review"] = QueueCard(
        id="review-1",
        title="Review",
        status=QueueStatus.BLOCKED,
        labels=("captains_chair", "stage:review"),
        metadata={"workerProtocol": {"detail": "TECHNICAL: fix requested"}},
    )
    queue.cards["repair"] = QueueCard(
        id="repair-1",
        title="Repair",
        status=QueueStatus.DONE,
        labels=("captains_chair", "stage:repair", "repair-for:review-1"),
        metadata={"proof": [{"status": "passed", "note": "new head abcdef1"}]},
    )
    orchestrator = WorkflowOrchestrator(queue, worker_config())

    result = orchestrator.reconcile(repo)

    assert result.unblocked == ()
    assert queue.unblocked == []
    assert result.protocol_retries == ("card-3",)
    assert queue.specs[-1].agent_id == "github-reviewer"
    assert "retry-for:review-1" in queue.specs[-1].labels


def test_fresh_gate_retry_proof_completes_blocked_original(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    queue.cards["review"] = QueueCard(
        id="review-1",
        title="Review",
        status=QueueStatus.BLOCKED,
        labels=("captains_chair", "stage:review"),
        metadata={"workerProtocol": {"detail": "TECHNICAL: fix requested"}},
    )
    queue.cards["repair"] = QueueCard(
        id="repair-1",
        title="Repair",
        status=QueueStatus.DONE,
        labels=("captains_chair", "stage:repair", "repair-for:review-1"),
        metadata={"proof": [{"status": "passed", "note": "new head abcdef1"}]},
    )
    queue.cards["retry"] = QueueCard(
        id="retry-1",
        title="Fresh review",
        status=QueueStatus.DONE,
        labels=("captains_chair", "stage:review", "retry-for:review-1"),
        metadata={"proof": [{"status": "passed", "note": "reviewed abcdef1"}]},
    )

    WorkflowOrchestrator(queue, worker_config()).reconcile(repo)

    assert queue.completed == [("review-1", ())]


def test_done_worker_card_without_passed_proof_is_requeued_before_dispatch(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    queue.cards["review"] = QueueCard(
        id="review-1",
        title="Review",
        status=QueueStatus.DONE,
        labels=("captains_chair", "stage:review"),
        agent_id="github-reviewer",
        metadata={"proof": []},
    )
    orchestrator = WorkflowOrchestrator(queue, worker_config())

    result = orchestrator.reconcile(repo)

    assert result.proof_retries == ("review-1",)
    assert len(result.protocol_retries) == 1
    retry = queue.specs[-1]
    assert "retry-for:review-1" in retry.labels
    assert retry.agent_id == "github-reviewer"
    assert queue.reassigned == []
    assert queue.dispatch_count == 1


def test_done_worker_card_with_passed_proof_is_not_requeued(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    queue.cards["review"] = QueueCard(
        id="review-1",
        title="Review",
        status=QueueStatus.DONE,
        labels=("captains_chair", "stage:review"),
        agent_id="github-reviewer",
        metadata={"proof": [{"status": "passed", "note": "current head abcdef1"}]},
    )
    orchestrator = WorkflowOrchestrator(queue, worker_config())

    result = orchestrator.reconcile(repo)

    assert result.proof_retries == ()
    assert queue.reassigned == []


def test_openclaw_review_lifecycle_without_proof_is_requeued(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    queue.cards["review"] = QueueCard(
        id="review-1",
        title="Implementation",
        status=QueueStatus.REVIEW,
        labels=("captains_chair", "stage:implementation"),
        agent_id="github-coder",
        metadata={"automation": {"summary": "PR opened"}},
    )
    orchestrator = WorkflowOrchestrator(queue, worker_config())

    result = orchestrator.reconcile(repo)

    assert len(result.protocol_retries) == 1
    retry = queue.specs[-1]
    assert "retry-for:review-1" in retry.labels
    assert retry.agent_id == "github-coder"
    assert queue.reclaimed == []
    assert queue.dispatch_count == 1


def test_fresh_retry_proof_completes_original_stage_card(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    queue.cards["original"] = QueueCard(
        id="review-1",
        title="Implementation",
        status=QueueStatus.REVIEW,
        labels=("captains_chair", "stage:implementation"),
        agent_id="github-coder",
        metadata={"automation": {"summary": "PR opened"}},
    )
    queue.cards["retry"] = QueueCard(
        id="retry-1",
        title="Retry implementation",
        status=QueueStatus.DONE,
        labels=("captains_chair", "stage:implementation", "retry-for:review-1"),
        agent_id="github-coder",
        metadata={"proof": [{"status": "passed", "note": "PR head abcdef1"}]},
    )
    orchestrator = WorkflowOrchestrator(queue, worker_config())

    result = orchestrator.reconcile(repo)

    assert result.protocol_retries == ()
    assert queue.completed == [("review-1", ())]


def test_valid_final_retry_reopens_blocked_merge_card(tmp_path: Path) -> None:
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    queue = MemoryQueue()
    queue.cards["final"] = QueueCard(
        id="final-1",
        title="Final review",
        status=QueueStatus.DONE,
        labels=("captains_chair", "workflow:flow-1", "stage:final_review"),
        metadata={
            "proof": [{"status": "passed", "note": "AUTO_MERGE_ALLOWED:abcdef1"}]
        },
    )
    queue.cards["merge"] = QueueCard(
        id="merge-1",
        title="Merge",
        status=QueueStatus.BLOCKED,
        labels=("captains_chair", "workflow:flow-1", "stage:merge"),
        metadata={
            "links": [{"type": "parent", "targetCardId": "final-1"}],
            "workerProtocol": {"detail": "TECHNICAL: stale final review handoff"},
            "failureCount": 1,
        },
    )

    result = WorkflowOrchestrator(queue, worker_config()).reconcile(repo)

    assert result.protocol_retries == ("card-3",)
    assert queue.reclaimed == []
    assert "final-1" in queue.specs[-1].notes


def test_stale_final_block_retries_review_instead_of_creating_coder_repair(
    tmp_path: Path,
) -> None:
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    queue = MemoryQueue()
    queue.cards["review"] = QueueCard(
        id="review-1",
        title="Independent review",
        status=QueueStatus.DONE,
        labels=("captains_chair", "workflow:flow-1", "stage:review"),
        metadata={"proof": [{"status": "passed", "note": "reviewed abcdef1"}]},
    )
    queue.cards["qa"] = QueueCard(
        id="qa-1",
        title="CLI QA",
        status=QueueStatus.DONE,
        labels=("captains_chair", "workflow:flow-1", "stage:test"),
        metadata={"proof": [{"status": "passed", "note": "QA_PASSED:cli:abcdef1"}]},
    )
    queue.cards["final"] = QueueCard(
        id="final-1",
        title="Final review",
        status=QueueStatus.BLOCKED,
        labels=("captains_chair", "workflow:flow-1", "stage:final_review"),
        metadata={
            "links": [
                {"type": "parent", "targetCardId": "review-1"},
                {"type": "parent", "targetCardId": "qa-1"},
            ],
            "failureCount": 1,
        },
    )
    queue.cards["repair"] = QueueCard(
        id="repair-1",
        title="Repair findings from Final review",
        status=QueueStatus.READY,
        labels=("captains_chair", "workflow:flow-1", "stage:repair", "repair:final-1"),
    )

    result = WorkflowOrchestrator(queue, worker_config()).reconcile(
        repo,
        dispatch=False,
        dispatch_reason="test",
    )

    assert len(result.protocol_retries) == 1
    assert result.repairs_created == ()
    assert queue.reclaimed == ["repair-1"]
    retry = queue.specs[-1]
    assert retry.agent_id == "github-final"
    assert "stage:final_review" in retry.labels
    assert "retry-for:final-1" in retry.labels


def test_nested_final_retry_promotes_blocked_original_for_downstream_merge(
    tmp_path: Path,
) -> None:
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    queue = MemoryQueue()
    queue.cards["final"] = QueueCard(
        id="final-1",
        title="Final review",
        status=QueueStatus.BLOCKED,
        labels=("captains_chair", "workflow:flow-1", "stage:final_review"),
        metadata={
            "workerProtocol": {"detail": "TECHNICAL: stale final review handoff"},
        },
    )
    queue.cards["first-retry"] = QueueCard(
        id="first-retry-1",
        title="Retry final review",
        status=QueueStatus.BLOCKED,
        labels=(
            "captains_chair",
            "workflow:flow-1",
            "stage:final_review",
            "retry-for:final-1",
        ),
        metadata={"workerProtocol": {"detail": "TECHNICAL: worker timeout"}},
    )
    queue.cards["nested-retry"] = QueueCard(
        id="nested-retry-1",
        title="Nested retry final review",
        status=QueueStatus.DONE,
        labels=(
            "captains_chair",
            "workflow:flow-1",
            "stage:final_review",
            "retry-for:first-retry-1",
        ),
        metadata={
            "proof": [
                {"status": "passed", "note": "AUTO_MERGE_ALLOWED:abcdef1"}
            ]
        },
    )
    queue.cards["merge"] = QueueCard(
        id="merge-1",
        title="Merge",
        status=QueueStatus.TODO,
        labels=("captains_chair", "workflow:flow-1", "stage:merge"),
        metadata={"links": [{"type": "parent", "targetCardId": "final-1"}]},
    )

    WorkflowOrchestrator(queue, worker_config()).reconcile(repo, dispatch=False)

    assert ("final-1", ()) in queue.completed
    assert queue.specs == []


def test_recovered_control_plane_card_is_cancelled_without_dispatch(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    queue.cards["target"] = QueueCard(
        id="target-1",
        title="Review",
        status=QueueStatus.DONE,
        labels=("captains_chair", "stage:review"),
        metadata={"proof": [{"status": "passed", "note": "reviewed abcdef1"}]},
    )
    queue.cards["recovery"] = QueueCard(
        id="recovery-1",
        title="Recovery",
        status=QueueStatus.READY,
        labels=(
            "captains_chair",
            "stage:control_plane_action",
            "control-plane-recovery:target-1",
        ),
    )

    WorkflowOrchestrator(queue, worker_config()).reconcile(repo)

    assert queue.reclaimed == ["recovery-1"]


def test_exhausted_merge_ready_card_resets_before_dispatch(tmp_path: Path) -> None:
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    queue = MemoryQueue()
    queue.cards["final"] = QueueCard(
        id="final-1",
        title="Final review",
        status=QueueStatus.DONE,
        labels=("captains_chair", "workflow:flow-1", "stage:final_review"),
        metadata={
            "proof": [{"status": "passed", "note": "AUTO_MERGE_ALLOWED:abcdef1"}]
        },
    )
    queue.cards["merge"] = QueueCard(
        id="merge-1",
        title="Merge",
        status=QueueStatus.READY,
        labels=("captains_chair", "workflow:flow-1", "stage:merge"),
        metadata={
            "links": [{"type": "parent", "targetCardId": "final-1"}],
            "failureCount": 3,
            "automation": {"maxRetries": 2},
        },
    )

    result = WorkflowOrchestrator(queue, worker_config()).reconcile(repo)

    assert result.protocol_retries == ("card-3",)
    assert queue.reassigned == []
    assert "final-1" in queue.specs[-1].notes


def test_repair_for_cancelled_target_is_not_dispatched(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    queue.cards["target"] = QueueCard(
        id="target-1",
        title="Final review",
        status=QueueStatus.BLOCKED,
        labels=("captains_chair", "stage:final_review"),
        metadata={"comments": [{"body": "CANCELLED: superseded"}]},
    )
    queue.cards["repair"] = QueueCard(
        id="repair-1",
        title="Repair",
        status=QueueStatus.READY,
        labels=("captains_chair", "stage:repair", "repair:target-1"),
    )

    WorkflowOrchestrator(queue, worker_config()).reconcile(repo)

    assert queue.reclaimed == ["repair-1"]


def test_nested_retry_proof_completes_original_without_another_retry(tmp_path: Path) -> None:
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    queue = MemoryQueue()
    queue.cards["original"] = QueueCard(
        id="review-1",
        title="Implementation",
        status=QueueStatus.DONE,
        labels=("captains_chair", "stage:final_review"),
        agent_id="github-reviewer",
        metadata={"proof": []},
    )
    queue.cards["first-retry"] = QueueCard(
        id="retry-1",
        title="Retry review",
        status=QueueStatus.READY,
        labels=("captains_chair", "stage:final_review", "retry-for:review-1"),
        agent_id="github-reviewer",
    )
    queue.cards["nested-retry"] = QueueCard(
        id="retry-2",
        title="Nested retry review",
        status=QueueStatus.DONE,
        labels=("captains_chair", "stage:final_review", "retry-for:retry-1"),
        agent_id="github-reviewer",
        metadata={
            "proof": [
                {
                    "status": "passed",
                    "note": "reviewed abcdef1 AUTO_MERGE_ALLOWED:abcdef1",
                }
            ]
        },
    )
    queue.cards["newer-retry"] = QueueCard(
        id="retry-3",
        title="Newer nested retry review",
        status=QueueStatus.READY,
        labels=("captains_chair", "stage:final_review", "retry-for:retry-2"),
        agent_id="github-reviewer",
    )

    assert _is_retry_descendant(queue.cards["nested-retry"], "review-1", list(queue.cards.values()))
    result = WorkflowOrchestrator(queue, worker_config()).reconcile(repo)

    assert result.protocol_retries == ()
    assert queue.completed == [("review-1", ())]
    assert "retry-3" in queue.reclaimed


def test_fresh_retry_uses_workboard_safe_compact_label_for_uuid_card(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    original_id = "6ea52955-ab1c-44a2-b84e-7b3a927604a7"
    queue.cards["original"] = QueueCard(
        id=original_id,
        title="Implementation",
        status=QueueStatus.REVIEW,
        labels=("captains_chair", "stage:implementation"),
        agent_id="github-coder",
    )

    WorkflowOrchestrator(queue, worker_config()).reconcile(repo)

    retry = queue.specs[-1]
    assert all(len(label) <= 40 for label in retry.labels)
    assert len(next(label for label in retry.labels if label.startswith("retry:"))) <= 40


def test_fresh_retry_preserves_the_isolated_workspace(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    workspace = WorkspaceRef(kind="worktree", path=tmp_path / "work", branch="captains_chair/work/39")
    queue.cards["original"] = QueueCard(
        id="review-1",
        title="Implementation",
        status=QueueStatus.REVIEW,
        labels=("captains_chair", "stage:implementation"),
        agent_id="github-coder",
        workspace=workspace,
    )

    WorkflowOrchestrator(queue, worker_config()).reconcile(repo)

    assert queue.specs[-1].workspace == workspace


def test_archived_cards_are_not_recovered_or_retried(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    queue.cards["archived"] = QueueCard(
        id="old-1",
        title="Superseded implementation",
        status=QueueStatus.DONE,
        labels=("captains_chair", "stage:implementation"),
        metadata={"archivedAt": "2026-07-12T00:00:00Z"},
    )

    result = WorkflowOrchestrator(queue, worker_config()).reconcile(repo)

    assert result.proof_retries == ()
    assert result.protocol_retries == ()
    assert queue.specs == []


def test_retry_copies_only_parent_links(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    queue.cards["original"] = QueueCard(
        id="review-1",
        title="Implementation",
        status=QueueStatus.REVIEW,
        labels=("captains_chair", "stage:implementation"),
        agent_id="github-coder",
        metadata={
            "links": [
                {"type": "parent", "targetCardId": "root-1"},
                {"type": "child", "targetCardId": "review-child"},
            ]
        },
    )

    WorkflowOrchestrator(queue, worker_config()).reconcile(repo)

    assert queue.specs[-1].parents == ("root-1",)


def test_archived_retry_does_not_satisfy_live_retry_lookup(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    queue.cards["original"] = QueueCard(
        id="review-1",
        title="Implementation",
        status=QueueStatus.REVIEW,
        labels=("captains_chair", "stage:implementation"),
        agent_id="github-coder",
    )
    queue.cards["archived-retry"] = QueueCard(
        id="retry-old",
        title="Archived retry",
        status=QueueStatus.TODO,
        labels=("captains_chair", "stage:implementation", "retry-for:review-1"),
        metadata={"archivedAt": "2026-07-12T00:00:00Z"},
    )

    WorkflowOrchestrator(queue, worker_config()).reconcile(repo)

    assert queue.specs[-1].key == "captains_chair:retry:review-1:2"


def test_proofless_retry_chain_routes_to_fresh_control_plane_recovery(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    queue.cards["original"] = QueueCard(
        id="review-1",
        title="Implementation",
        status=QueueStatus.DONE,
        labels=("captains_chair", "stage:implementation"),
        metadata={"proof": []},
    )
    queue.cards["retry-1"] = QueueCard(
        id="retry-1",
        title="Retry one",
        status=QueueStatus.DONE,
        labels=("captains_chair", "stage:implementation", "retry-for:review-1"),
        metadata={"proof": []},
    )
    queue.cards["retry-2"] = QueueCard(
        id="retry-2",
        title="Retry two",
        status=QueueStatus.REVIEW,
        labels=(
            "captains_chair",
            "stage:implementation",
            "retry-for:review-1",
            "retry-for:retry-1",
        ),
        metadata={"proof": []},
    )

    result = WorkflowOrchestrator(queue, worker_config()).reconcile(repo)

    assert len(result.control_plane_recoveries) == 1
    recovery = queue.specs[-1]
    assert recovery.agent_id == "captains-chair"
    assert recovery.key == "captains_chair:control-plane-recovery:retry-2:1"


def test_openclaw_review_lifecycle_with_proof_is_normalized_to_done(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    queue.cards["review"] = QueueCard(
        id="review-1",
        title="Independent review",
        status=QueueStatus.REVIEW,
        labels=("captains_chair", "stage:review"),
        agent_id="github-reviewer",
        metadata={"proof": [{"status": "passed", "note": "head abcdef1"}]},
    )
    orchestrator = WorkflowOrchestrator(queue, worker_config())

    result = orchestrator.reconcile(repo)

    assert result.protocol_retries == ()
    assert queue.completed == [("review-1", ())]


def test_has_active_workflow_matches_issue_or_pr_and_ignores_completed_groups(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    queue.cards["active"] = QueueCard(
        id="active-1",
        title="Review",
        status=QueueStatus.RUNNING,
        labels=("captains_chair", "workflow:action-1", "stage:review"),
        source_url="https://github.com/other/project/issues/39",
    )
    orchestrator = WorkflowOrchestrator(queue, worker_config())
    decision = implementation_decision()

    assert orchestrator.has_active_workflow(repo, decision) is False

    repo = repo.model_copy(update={"full_name": "other/project"})
    assert orchestrator.has_active_workflow(repo, decision) is True

    queue.cards["active"] = queue.cards["active"].model_copy(update={"status": QueueStatus.DONE})
    assert orchestrator.has_active_workflow(repo, decision) is False


def test_active_workflow_count_preserves_capacity_for_owner_blocked_work(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    queue = MemoryQueue()
    queue.cards.update(
        {
            "technical": QueueCard(
                id="technical-1",
                title="Technical workflow",
                status=QueueStatus.RUNNING,
                labels=("captains_chair", "workflow:technical", "stage:implementation"),
            ),
            "owner": QueueCard(
                id="owner-1",
                title="Owner-blocked workflow",
                status=QueueStatus.BLOCKED,
                labels=("captains_chair", "workflow:owner", "stage:implementation"),
                metadata={"workerProtocol": {"detail": "USER_SECRET: Azure token required"}},
            ),
            "done": QueueCard(
                id="done-1",
                title="Completed workflow",
                status=QueueStatus.DONE,
                labels=("captains_chair", "workflow:done", "stage:implementation"),
            ),
        }
    )
    orchestrator = WorkflowOrchestrator(queue, worker_config())

    assert orchestrator.active_workflow_count(repo) == 1

    queue.cards["owner-running"] = QueueCard(
        id="owner-running-1",
        title="Owner workflow still has active work",
        status=QueueStatus.RUNNING,
        labels=("captains_chair", "workflow:owner", "stage:review"),
    )
    assert orchestrator.active_workflow_count(repo) == 2
