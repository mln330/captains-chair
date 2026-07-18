from pathlib import Path
from typing import cast

import pytest

from make_it_so.conformance import run_runtime_conformance
from make_it_so.direct_orchestrator import DirectOrchestrator
from make_it_so.models import (
    ActionKind,
    CompletionPolicy,
    DirectOrchestratorConfig,
    OperationMode,
    PlanDecision,
    WorkerAssignments,
)
from make_it_so.orchestration import QueueCardSpec, QueueStatus, WorkerLifecycleAdapter
from make_it_so.runtime import build_work_queue_orchestrator
from tests.helpers import repo_config


def _config(tmp_path: Path) -> DirectOrchestratorConfig:
    return DirectOrchestratorConfig(
        database_path=tmp_path / "direct.db",
        require_live_completion_validation=False,
        workers=WorkerAssignments(
            captain="captain",
            coder="coder",
            reviewer="reviewer",
            tester="tester",
            ux_reviewer="ux",
            final_reviewer="final",
            merger="merge",
            verifier="verify",
        ),
    )


def test_direct_orchestrator_is_durable_idempotent_and_claim_aware(tmp_path: Path) -> None:
    adapter = DirectOrchestrator(tmp_path / "direct.db")
    adapter.ensure_board("course-1", "Course", "Direct workflow", tmp_path)
    spec = QueueCardSpec(
        key="package-1",
        title="Package",
        notes="Implement it",
        metadata={"courseKey": "course-1", "workPackageKey": "package-1"},
    )

    created = adapter.create_card("course-1", spec)
    assert adapter.create_card("course-1", spec).id == created.id
    assert adapter.list_cards("course-1")[0].metadata["courseKey"] == "course-1"
    assert DirectOrchestrator(tmp_path / "direct.db").list_cards("course-1") == [created]

    assert adapter.dispatch("course-1")["promoted"] == [created.id]
    with pytest.raises(PermissionError, match="no active claim"):
        adapter.complete_claimed_card(
            created.id,
            owner_id="worker",
            token="secret",
            summary="not claimed",
            proof=(),
        )
    adapter.claim_card(created.id, owner_id="worker", token="secret")
    with pytest.raises(PermissionError, match="not ready"):
        adapter.claim_card(created.id, owner_id="other-worker", token="other-token")
    claimed = adapter.heartbeat_card(created.id, owner_id="worker", token="secret", note="started")
    assert claimed.status == QueueStatus.RUNNING
    with pytest.raises(PermissionError, match="claim credentials"):
        adapter.complete_claimed_card(
            created.id,
            owner_id="worker",
            token="wrong",
            summary="nope",
            proof=(),
        )
    completed = adapter.complete_claimed_card(
        created.id,
        owner_id="worker",
        token="secret",
        summary="done",
        proof=({"kind": "test", "status": "passed"},),
    )
    assert completed.status == QueueStatus.DONE
    assert DirectOrchestrator(tmp_path / "direct.db").list_cards("course-1")[0].status == QueueStatus.DONE


def test_direct_orchestrator_passes_shared_runtime_conformance(tmp_path: Path) -> None:
    config = _config(tmp_path)
    orchestrator = build_work_queue_orchestrator(config)
    assert isinstance(orchestrator.adapter, WorkerLifecycleAdapter)
    adapter = cast(DirectOrchestrator, orchestrator.adapter)
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement the direct-runtime package",
        reason="The approved course selected it.",
        target_issue=42,
        acceptance_criteria=("Scope is correct", "Checks pass"),
    )

    def block(card_id: str, reason: str):
        adapter.claim_card(card_id, owner_id="worker", token="token")
        return adapter.block_claimed_card(
            card_id,
            owner_id="worker",
            token="token",
            reason=reason,
        )

    def complete(card_id: str, summary: str, proof: tuple[dict[str, object], ...]):
        adapter.claim_card(card_id, owner_id="worker", token="token")
        return adapter.complete_claimed_card(
            card_id,
            owner_id="worker",
            token="token",
            summary=summary,
            proof=proof,
        )

    report = run_runtime_conformance(
        orchestrator,
        adapter,
        repo,
        decision,
        action_id="direct-conformance",
        block_card=block,
        complete_card=complete,
    )

    assert report.workflow_id == "direct-conformance"
    assert report.owner_blocked_card_id.startswith("direct-")
    assert orchestrator.adapter.diagnostics()["status"] == "healthy"


def test_direct_orchestrator_handles_reclaim_reassign_comments_and_dependencies(tmp_path: Path) -> None:
    adapter = DirectOrchestrator(tmp_path / "direct.db")
    adapter.ensure_board("course-1", "Course", "Direct workflow", tmp_path)
    parent = adapter.create_card(
        "course-1",
        QueueCardSpec(key="parent", title="Parent", notes="Parent work", status=QueueStatus.TODO),
    )
    child = adapter.create_card(
        "course-1",
        QueueCardSpec(
            key="child",
            title="Child",
            notes="Child work",
            status=QueueStatus.TODO,
            parents=(parent.id,),
            metadata={"failures": 2, "comments": "invalid"},
        ),
    )
    canary = adapter.create_card(
        "course-1",
        QueueCardSpec(
            key="canary",
            title="Canary",
            notes="Canary work",
            status=QueueStatus.READY,
            labels=("runtime-canary",),
        ),
    )

    first = adapter.dispatch("course-1")
    assert first["promoted"] == [parent.id]
    assert first["started"] == []
    claimed_canary = adapter.claim_card(
        canary.id, owner_id="canary-worker", token="canary-token"
    )
    assert claimed_canary.status == QueueStatus.RUNNING
    adapter.complete_card(parent.id, summary="parent complete", created_card_ids=(child.id,))
    second = adapter.dispatch("course-1")
    assert second["promoted"] == [child.id]

    commented = adapter.comment(child.id, "first comment")
    assert commented.metadata["comments"] == ["first comment"]
    reclaimed = adapter.reclaim_card(child.id, status=QueueStatus.BLOCKED, reason="worker timeout")
    assert reclaimed.metadata["reclaimReason"] == "worker timeout"
    reassigned = adapter.reassign_card(
        child.id,
        agent_id="replacement",
        status=QueueStatus.TODO,
        reset_failures=True,
        reason="repair routing",
    )
    assert reassigned.agent_id == "replacement"
    assert "failures" not in reassigned.metadata
    adapter.dispatch("course-1")
    adapter.claim_card(child.id, owner_id="replacement", token="token")
    adapter.heartbeat_card(child.id, owner_id="replacement", token="token", note="working")
    adapter.block_claimed_card(
        child.id,
        owner_id="replacement",
        token="token",
        reason="TECHNICAL: test blocker",
    )
    unblocked = adapter.unblock_card(child.id)
    assert unblocked.status == QueueStatus.TODO
    assert "workerProtocol" not in unblocked.metadata
