from __future__ import annotations

from pathlib import Path

import pytest

from captains_chair.conformance import (
    RuntimeConformanceError,
    run_full_autonomous_workflow,
    run_mixed_blocker_isolation,
    run_technical_recovery_isolation,
    run_user_blocker_isolation,
)
from captains_chair.models import (
    ActionKind,
    CompletionPolicy,
    OperationMode,
    PlanDecision,
)
from captains_chair.orchestration import QueueCard, QueueCardSpec, QueueStatus, WorkflowOrchestrator
from tests.fakes import InMemoryWorkQueue, PersistentWorkQueue, worker_policy
from tests.helpers import repo_config


class BrokenDependencyQueue(InMemoryWorkQueue):
    """Deliberately violate dependency promotion to test the shared contract."""

    def dispatch(self, board_id: str) -> dict[str, object]:
        del board_id
        self.dispatches += 1
        promoted: list[str] = []
        for card_id, card in list(self.cards.items()):
            if card.status != QueueStatus.TODO:
                continue
            self._update(card_id, status=QueueStatus.READY)
            promoted.append(card_id)
        return {"promoted": promoted, "count": len(promoted)}


class CrashAfterPersistingCards(PersistentWorkQueue):
    """Persist a partial materialization, then simulate a gateway crash once."""

    def __init__(self, path: Path, *, crash_after: int) -> None:
        self._remaining = crash_after
        super().__init__(path)

    def create_card(self, board_id: str, spec: QueueCardSpec) -> QueueCard:
        card = super().create_card(board_id, spec)
        self._remaining -= 1
        if self._remaining == 0:
            raise RuntimeError("gateway crashed after persisting card")
        return card


def implementation() -> PlanDecision:
    return PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement a disposable vertical slice",
        reason="It is the next dependency-ready work item.",
        target_issue=39,
        acceptance_criteria=("Scope is correct", "Checks pass"),
    )


def test_disposable_runtime_drives_autonomous_workflow_through_repair_and_merge(
    tmp_path: Path,
) -> None:
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    queue = InMemoryWorkQueue()
    orchestrator = WorkflowOrchestrator(queue, worker_policy())
    run_full_autonomous_workflow(
        orchestrator,
        queue,
        repo,
        implementation(),
        "workflow-e2e",
        block_card=queue.block,
    )


def test_shared_conformance_rejects_early_dependency_promotion(tmp_path: Path) -> None:
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    queue = BrokenDependencyQueue()
    orchestrator = WorkflowOrchestrator(queue, worker_policy())

    with pytest.raises(RuntimeConformanceError, match="dependent card"):
        run_full_autonomous_workflow(
            orchestrator,
            queue,
            repo,
            implementation(),
            "broken-dependency-workflow",
            block_card=queue.block,
        )


def test_persistent_queue_replays_partial_enqueue_without_duplicate_cards(tmp_path: Path) -> None:
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    queue_path = tmp_path / "workboard.json"
    crashed = CrashAfterPersistingCards(queue_path, crash_after=3)
    first = WorkflowOrchestrator(crashed, worker_policy())

    with pytest.raises(RuntimeError, match="after persisting card"):
        first.enqueue(repo, implementation(), "crash-replay")

    restarted_queue = PersistentWorkQueue(queue_path)
    restarted = WorkflowOrchestrator(restarted_queue, worker_policy())
    replay = restarted.enqueue(repo, implementation(), "crash-replay")

    expected_cards = len(replay.stage_cards) + 1
    assert len(restarted_queue.cards) == expected_cards
    assert replay.workflow_id == "crash-replay"
    assert len({card.id for card in restarted_queue.cards.values()}) == expected_cards
    assert replay.root_card_id == restarted_queue.keys["crash-replay:root"]


def test_user_blocker_does_not_prevent_unrelated_workflow_dispatch(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    queue = InMemoryWorkQueue()
    orchestrator = WorkflowOrchestrator(queue, worker_policy())
    run_user_blocker_isolation(
        orchestrator,
        queue,
        repo,
        implementation(),
        block_card=queue.block,
    )


def test_technical_recovery_does_not_prevent_unrelated_workflow_dispatch(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    queue = InMemoryWorkQueue()
    orchestrator = WorkflowOrchestrator(queue, worker_policy())
    run_technical_recovery_isolation(
        orchestrator,
        queue,
        repo,
        implementation(),
        block_card=queue.block,
    )


def test_mixed_owner_and_technical_blockers_keep_unrelated_work_moving(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    queue = InMemoryWorkQueue()
    orchestrator = WorkflowOrchestrator(queue, worker_policy())

    owner_card, technical_card, unrelated_card = run_mixed_blocker_isolation(
        orchestrator,
        queue,
        repo,
        implementation(),
        block_card=queue.block,
    )

    assert queue.cards[owner_card].status == QueueStatus.BLOCKED
    assert queue.cards[technical_card].status == QueueStatus.READY
    assert queue.cards[unrelated_card].status == QueueStatus.READY


def test_fresh_orchestrator_reconciles_persisted_queue_state_without_duplicates(
    tmp_path: Path,
) -> None:
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    queue_path = tmp_path / "workboard.json"
    first_queue = PersistentWorkQueue(queue_path)
    first = WorkflowOrchestrator(first_queue, worker_policy())
    workflow = first.enqueue(repo, implementation(), "restart-workflow")
    first.reconcile(repo)

    implementation_id = workflow.stage_cards["restart-workflow:implementation"]
    first_queue.complete_card(
        implementation_id,
        summary="Implementation completed before the orchestrator restarted.",
        proof=(
            {
                "status": "passed",
                "label": "implementation proof",
                "note": "targeted checks passed on head abc1234",
            },
        ),
    )

    restarted_queue = PersistentWorkQueue(queue_path)
    restarted = WorkflowOrchestrator(restarted_queue, worker_policy())
    result = restarted.reconcile(repo)
    replay = restarted.enqueue(repo, implementation(), "restart-workflow")

    assert result.dispatch["promoted"]
    assert replay.stage_cards == workflow.stage_cards
    assert len(restarted_queue.cards) == len(workflow.stage_cards) + 1
    ready_stages = {
        label.split(":", 1)[1]
        for card in restarted_queue.cards.values()
        if card.status == QueueStatus.READY
        for label in card.labels
        if label.startswith("stage:")
    }
    assert ready_stages == {"review", "test"}
