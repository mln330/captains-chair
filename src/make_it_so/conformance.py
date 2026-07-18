from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from make_it_so.models import PlanDecision, RepoConfig
from make_it_so.orchestration import (
    QueueCard,
    QueueStatus,
    WorkflowOrchestrator,
    WorkQueueAdapter,
    WorkspaceRef,
    workflow_label,
)


class RuntimeConformanceError(AssertionError):
    """Raised when a runtime adapter violates the shared workflow contract."""


BlockCard = Callable[[str, str], QueueCard]
CompleteCard = Callable[[str, str, tuple[dict[str, object], ...]], QueueCard]


@dataclass(frozen=True)
class RuntimeConformanceReport:
    """Evidence returned after the shared runtime scenario passes."""

    workflow_id: str
    owner_blocked_card_id: str
    technical_retry_card_id: str
    mixed_owner_blocked_card_id: str
    mixed_technical_retry_card_id: str
    mixed_unrelated_card_id: str


def stage_card(
    adapter: WorkQueueAdapter,
    board_id: str,
    stage: str,
    *,
    retry: bool = False,
    workflow_id: str | None = None,
) -> QueueCard:
    matches = [
        card
        for card in adapter.list_cards(board_id)
        if f"stage:{stage}" in card.labels
        and (workflow_id is None or workflow_label(workflow_id) in card.labels)
        and (not retry or any(label.startswith("retry") for label in card.labels))
    ]
    if not matches:
        raise RuntimeConformanceError(f"no {stage} card exists on {board_id}")
    return matches[-1] if retry else matches[0]


def qa_cards(adapter: WorkQueueAdapter, board_id: str, workflow_id: str) -> list[QueueCard]:
    return [
        card
        for card in adapter.list_cards(board_id)
        if workflow_label(workflow_id) in card.labels and card.metadata.get("qaProfile")
    ]


def run_full_autonomous_workflow(
    orchestrator: WorkflowOrchestrator,
    adapter: WorkQueueAdapter,
    repo: RepoConfig,
    decision: PlanDecision,
    action_id: str,
    *,
    block_card: BlockCard,
    complete_card: CompleteCard | None = None,
    workspace: WorkspaceRef | None = None,
) -> str:
    """Run the complete implementation, repair, review, merge, and verify scenario.

    The scenario is intentionally runtime-neutral. A queue adapter integration can
    pass its own adapter and blocker operation without reimplementing the workflow
    assertions or weakening the proof requirements.
    """
    workflow = orchestrator.enqueue(repo, decision, action_id, workspace=workspace)
    _expect(orchestrator.enqueue(repo, decision, action_id) == workflow, "enqueue is not idempotent")
    complete = complete_card or _default_complete(adapter)

    first = orchestrator.reconcile(repo)
    implementation = stage_card(
        adapter, workflow.board_id, "implementation", workflow_id=workflow.workflow_id
    )
    promoted = set(first.dispatch["promoted"])
    _expect(implementation.id in promoted, "implementation was not promoted")
    _expect(
        not (set(workflow.stage_cards.values()) - {implementation.id}) & promoted,
        "a dependent card was promoted before implementation completed",
    )

    complete(
        implementation.id,
        "PR opened",
        (
            {
                "status": "passed",
                "note": "PR head bcdef12",
                "url": "https://github.com/example/project/pull/7",
            },
        ),
    )
    parallel = orchestrator.reconcile(repo)
    review_card = stage_card(adapter, workflow.board_id, "review", workflow_id=workflow.workflow_id)
    capability_cards = qa_cards(adapter, workflow.board_id, workflow.workflow_id)
    _expect(review_card.id in set(parallel.dispatch["promoted"]), "independent review was not promoted")
    _expect(bool(capability_cards), "no capability QA cards were materialized")
    _expect(
        all(card.status == QueueStatus.READY for card in capability_cards),
        "capability QA cards were not ready in parallel",
    )
    for qa_card in capability_cards:
        profile = str(qa_card.metadata["qaProfile"])
        complete(
            qa_card.id,
            f"{profile} passed",
            (
                {
                    "status": "passed",
                    "note": f"QA_PASSED:{profile}:bcdef12",
                    "model": "conformance-model",
                    "provider": "conformance",
                    "evidence": [
                        "accessibility checked",
                        "contrast checked",
                        "responsive behavior checked",
                        "flow checked",
                        "cohesion checked",
                    ],
                },
            ),
        )
    block_card(
        review_card.id,
        "TECHNICAL: missing assertion",
    )

    repair = orchestrator.reconcile(repo)
    _expect(len(repair.repairs_created) == 1, "technical review failure did not create one repair card")
    repair_card = stage_card(adapter, workflow.board_id, "repair", workflow_id=workflow.workflow_id)
    complete(
        repair_card.id,
        "Repair pushed",
        ({"status": "passed", "note": "New head bcdef12"},),
    )
    orchestrator.reconcile(repo)
    complete(
        stage_card(
            adapter, workflow.board_id, "review", retry=True, workflow_id=workflow.workflow_id
        ).id,
        "Review passed",
        ({"status": "passed", "note": "Current head bcdef12"},),
    )

    final_ready = orchestrator.reconcile(repo)
    final_card = stage_card(adapter, workflow.board_id, "final_review", workflow_id=workflow.workflow_id)
    _expect(final_card.id in final_ready.dispatch["promoted"], "final review was not promoted after all gates")
    complete(
        final_card.id,
        "Final review passed",
        ({"status": "passed", "note": "AUTO_MERGE_ALLOWED:bcdef12"},),
    )

    merge_ready = orchestrator.reconcile(repo)
    merge_card = stage_card(adapter, workflow.board_id, "merge", workflow_id=workflow.workflow_id)
    if merge_card.status != QueueStatus.DONE:
        _expect(
            merge_card.id in merge_ready.dispatch["promoted"],
            "merge was neither deterministically completed nor promoted after final proof",
        )
        complete(
            merge_card.id,
            "Merged",
            ({"status": "passed", "note": "Merge feed123"},),
        )
    else:
        deterministic_raw = cast(object, merge_ready.dispatch.get("deterministic_merge"))
        deterministic_status: object | None = None
        if isinstance(deterministic_raw, dict):
            deterministic = cast(dict[str, object], deterministic_raw)
            deterministic_status = deterministic.get("status")
        _expect(
            isinstance(deterministic_status, str) and deterministic_status == "completed",
            "completed merge lacks deterministic gate evidence",
        )

    verify_ready = orchestrator.reconcile(repo)
    verify_card = stage_card(adapter, workflow.board_id, "post_merge", workflow_id=workflow.workflow_id)
    _expect(
        verify_card.id in verify_ready.dispatch["promoted"]
        or verify_card.status in {QueueStatus.READY, QueueStatus.RUNNING},
        "post-merge verification was not promoted",
    )
    complete(
        verify_card.id,
        "Verified",
        ({"status": "passed", "note": "Main CI passed"},),
    )

    finished = orchestrator.reconcile(repo)
    _expect(finished.proof_retries == (), "successful workflow created an unexpected proof retry")
    _expect(
        all(
            card.status == QueueStatus.DONE
            for card in adapter.list_cards(workflow.board_id)
            if workflow_label(workflow.workflow_id) in card.labels
        ),
        "workflow did not finish with every card done",
    )
    _expect(
        stage_card(
            adapter, workflow.board_id, "implementation", workflow_id=workflow.workflow_id
        ).agent_id
        != stage_card(adapter, workflow.board_id, "review", workflow_id=workflow.workflow_id).agent_id,
        "coder and reviewer are not independent workers",
    )
    return workflow.workflow_id


def run_user_blocker_isolation(
    orchestrator: WorkflowOrchestrator,
    adapter: WorkQueueAdapter,
    repo: RepoConfig,
    decision: PlanDecision,
    *,
    block_card: BlockCard,
    scenario_prefix: str = "",
) -> str:
    """Prove an owner-blocked workflow does not stop unrelated ready work."""
    prefix = f"{scenario_prefix}-" if scenario_prefix else ""
    blocked_action_id = f"{prefix}blocked-workflow"
    unrelated_action_id = f"{prefix}unrelated-workflow"
    blocked = orchestrator.enqueue(repo, decision, blocked_action_id)
    unrelated = orchestrator.enqueue(repo, decision, unrelated_action_id)
    orchestrator.reconcile(repo)

    blocked_card_id = blocked.stage_cards[f"{blocked_action_id}:implementation"]
    unrelated_card_id = unrelated.stage_cards[f"{unrelated_action_id}:implementation"]
    block_card(blocked_card_id, "USER_SECRET: test credential is required")
    adapter.reclaim_card(
        unrelated_card_id,
        status=QueueStatus.TODO,
        reason="return unrelated work to the ready queue",
    )

    result = orchestrator.reconcile(repo)
    _expect(blocked_card_id in result.user_blockers, "owner blocker was not isolated")
    _expect(unrelated_card_id in result.dispatch["promoted"], "unrelated work was suppressed by owner blocker")
    return blocked_card_id


def run_technical_recovery_isolation(
    orchestrator: WorkflowOrchestrator,
    adapter: WorkQueueAdapter,
    repo: RepoConfig,
    decision: PlanDecision,
    *,
    block_card: BlockCard,
    scenario_prefix: str = "",
) -> str:
    """Prove a technical failure retries without paging the owner."""
    prefix = f"{scenario_prefix}-" if scenario_prefix else ""
    failed_action_id = f"{prefix}technical-workflow"
    unrelated_action_id = f"{prefix}unrelated-technical-workflow"
    failed = orchestrator.enqueue(repo, decision, failed_action_id)
    unrelated = orchestrator.enqueue(repo, decision, unrelated_action_id)
    orchestrator.reconcile(repo)

    failed_card_id = failed.stage_cards[f"{failed_action_id}:implementation"]
    unrelated_card_id = unrelated.stage_cards[f"{unrelated_action_id}:implementation"]
    block_card(failed_card_id, "TECHNICAL: worker process exited before proof")
    adapter.reclaim_card(
        unrelated_card_id,
        status=QueueStatus.TODO,
        reason="return unrelated work to the ready queue",
    )

    result = orchestrator.reconcile(repo)
    _expect(failed_card_id not in result.user_blockers, "technical failure was escalated to the owner")
    _expect(result.retried == (failed_card_id,), "technical failure was not retried")
    _expect(unrelated_card_id in result.dispatch["promoted"], "technical failure stopped unrelated work")
    return failed_card_id


def run_mixed_blocker_isolation(
    orchestrator: WorkflowOrchestrator,
    adapter: WorkQueueAdapter,
    repo: RepoConfig,
    decision: PlanDecision,
    *,
    block_card: BlockCard,
    scenario_prefix: str = "",
) -> tuple[str, str, str]:
    """Prove owner and technical blockers can coexist without stopping safe work."""
    prefix = f"{scenario_prefix}-" if scenario_prefix else ""
    owner_action_id = f"{prefix}mixed-owner-workflow"
    technical_action_id = f"{prefix}mixed-technical-workflow"
    unrelated_action_id = f"{prefix}mixed-unrelated-workflow"
    owner_work = orchestrator.enqueue(repo, decision, owner_action_id)
    technical_work = orchestrator.enqueue(repo, decision, technical_action_id)
    unrelated_work = orchestrator.enqueue(repo, decision, unrelated_action_id)
    orchestrator.reconcile(repo)

    owner_card_id = owner_work.stage_cards[f"{owner_action_id}:implementation"]
    technical_card_id = technical_work.stage_cards[f"{technical_action_id}:implementation"]
    unrelated_card_id = unrelated_work.stage_cards[f"{unrelated_action_id}:implementation"]
    block_card(owner_card_id, "USER_SECRET: test credential is required")
    block_card(technical_card_id, "TECHNICAL: worker process exited before proof")
    adapter.reclaim_card(
        unrelated_card_id,
        status=QueueStatus.TODO,
        reason="return unrelated work to the ready queue",
    )

    result = orchestrator.reconcile(repo)
    _expect(owner_card_id in result.user_blockers, "owner blocker was not isolated")
    _expect(technical_card_id not in result.user_blockers, "technical blocker was escalated to the owner")
    _expect(technical_card_id in result.retried, "technical failure was not retried")
    _expect(
        unrelated_card_id in result.dispatch["promoted"],
        "unrelated work was suppressed by mixed blockers",
    )
    return owner_card_id, technical_card_id, unrelated_card_id


def run_runtime_conformance(
    orchestrator: WorkflowOrchestrator,
    adapter: WorkQueueAdapter,
    repo: RepoConfig,
    decision: PlanDecision,
    *,
    action_id: str = "runtime-conformance",
    block_card: BlockCard,
    complete_card: CompleteCard | None = None,
    workspace: WorkspaceRef | None = None,
) -> RuntimeConformanceReport:
    """Run the shared adapter contract scenario and return auditable evidence."""
    workflow_id = run_full_autonomous_workflow(
        orchestrator,
        adapter,
        repo,
        decision,
        action_id,
        block_card=block_card,
        complete_card=complete_card,
        workspace=workspace,
    )
    technical_retry_card_id = run_technical_recovery_isolation(
        orchestrator,
        adapter,
        repo,
        decision,
        block_card=block_card,
        scenario_prefix=action_id,
    )
    owner_blocked_card_id = run_user_blocker_isolation(
        orchestrator,
        adapter,
        repo,
        decision,
        block_card=block_card,
        scenario_prefix=action_id,
    )
    (
        mixed_owner_blocked_card_id,
        mixed_technical_retry_card_id,
        mixed_unrelated_card_id,
    ) = run_mixed_blocker_isolation(
        orchestrator,
        adapter,
        repo,
        decision,
        block_card=block_card,
        scenario_prefix=action_id,
    )
    return RuntimeConformanceReport(
        workflow_id=workflow_id,
        owner_blocked_card_id=owner_blocked_card_id,
        technical_retry_card_id=technical_retry_card_id,
        mixed_owner_blocked_card_id=mixed_owner_blocked_card_id,
        mixed_technical_retry_card_id=mixed_technical_retry_card_id,
        mixed_unrelated_card_id=mixed_unrelated_card_id,
    )


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeConformanceError(message)


def _default_complete(adapter: WorkQueueAdapter) -> CompleteCard:
    def complete(
        card_id: str,
        summary: str,
        proof: tuple[dict[str, object], ...],
    ) -> QueueCard:
        return adapter.complete_card(card_id, summary=summary, proof=proof)

    return complete


__all__ = [
    "RuntimeConformanceError",
    "RuntimeConformanceReport",
    "run_full_autonomous_workflow",
    "run_mixed_blocker_isolation",
    "run_runtime_conformance",
    "run_technical_recovery_isolation",
    "run_user_blocker_isolation",
    "stage_card",
]
