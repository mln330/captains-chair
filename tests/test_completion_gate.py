from __future__ import annotations

from pathlib import Path

import pytest

from captains_chair.completion_gate import GitHubCompletionValidator
from captains_chair.models import (
    ActionKind,
    CompletionPolicy,
    OpenClawWorkboardConfig,
    OperationMode,
    PlanDecision,
    PullRequestGate,
    WorkerAssignments,
)
from captains_chair.orchestration import (
    CompletionValidation,
    QueueCard,
    QueueStatus,
    WorkflowOrchestrator,
    WorkspaceRef,
)
from tests.fakes import InMemoryWorkQueue
from tests.helpers import repo_config


class FakeGitHub:
    def __init__(self, gate: PullRequestGate) -> None:
        self.result = gate
        self.calls: list[tuple[int, str | None]] = []

    def gate(self, repo: object, number: int, review_head_sha: str | None) -> PullRequestGate:
        del repo
        self.calls.append((number, review_head_sha))
        return self.result


def final_card(note: str, *, source_url: str | None = None) -> QueueCard:
    return QueueCard(
        id="final-card",
        title="Final review",
        status=QueueStatus.DONE,
        labels=("captains_chair", "stage:final_review"),
        source_url=source_url,
        metadata={"proof": [{"status": "passed", "note": note}]},
    )


def green_gate(
    *,
    head_sha: str = "abcdef1",
    review_head_sha: str | None = "abcdef1",
    checks_green: bool = True,
    unresolved_threads: int = 0,
    merge_state: str = "CLEAN",
    mergeable: bool = True,
    draft: bool = False,
) -> PullRequestGate:
    return PullRequestGate(
        number=42,
        head_sha=head_sha,
        mergeable=mergeable,
        merge_state=merge_state,
        draft=draft,
        checks_green=checks_green,
        required_checks=(),
        unresolved_threads=unresolved_threads,
        review_head_sha=review_head_sha,
    )


@pytest.mark.parametrize(
    ("completion", "mode", "allow_merge", "marker"),
    (
        (
            CompletionPolicy.OWNER_APPROVAL,
            OperationMode.SUPERVISED,
            False,
            "READY_FOR_OWNER:abcdef1",
        ),
        (CompletionPolicy.CONTROL_PLANE_COMPLETE, OperationMode.AUTONOMOUS, False, "CONTROL_PLANE_COMPLETE:abcdef1"),
        (
            CompletionPolicy.AUTO_MERGE,
            OperationMode.AUTONOMOUS,
            True,
            "AUTO_MERGE_ALLOWED:abcdef1",
        ),
    ),
)
def test_live_github_completion_gates_accept_policy_specific_proof(
    tmp_path: Path,
    completion: CompletionPolicy,
    mode: OperationMode,
    allow_merge: bool,
    marker: str,
) -> None:
    repo = repo_config(tmp_path, mode=mode, completion=completion).model_copy(
        update={"allow_autonomous_merge": allow_merge}
    )
    provider = FakeGitHub(green_gate())
    validator = GitHubCompletionValidator(provider)
    final = final_card(marker, source_url="https://github.com/example/project/pull/42")

    result = validator.validate(repo, final, [final])

    assert result.allowed is True
    assert provider.calls == [(42, "abcdef1")]


def test_live_completion_uses_latest_passed_review_proof(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS, completion=CompletionPolicy.AUTO_MERGE)
    provider = FakeGitHub(green_gate(head_sha="bead123", review_head_sha="bead123"))
    validator = GitHubCompletionValidator(provider)
    final = final_card(
        "old review AUTO_MERGE_ALLOWED:dead111",
        source_url="https://github.com/example/project/pull/42",
    ).model_copy(
        update={
            "metadata": {
                "proof": [
                    {"status": "passed", "note": "old review AUTO_MERGE_ALLOWED:dead111"},
                    {"status": "passed", "note": "latest review AUTO_MERGE_ALLOWED:bead123"},
                ]
            }
        }
    )

    result = validator.validate(repo, final, [final])

    assert result.allowed is True
    assert provider.calls == [(42, "bead123")]


def test_live_completion_does_not_reuse_old_marker_after_new_pass_without_marker(
    tmp_path: Path,
) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS, completion=CompletionPolicy.AUTO_MERGE)
    provider = FakeGitHub(green_gate())
    validator = GitHubCompletionValidator(provider)
    final = final_card(
        "old review AUTO_MERGE_ALLOWED:abcdef1",
        source_url="https://github.com/example/project/pull/42",
    ).model_copy(
        update={
            "metadata": {
                "proof": [
                    {"status": "passed", "note": "old review AUTO_MERGE_ALLOWED:abcdef1"},
                    {"status": "passed", "note": "new review completed, marker omitted"},
                ]
            }
        }
    )

    result = validator.validate(repo, final, [final])

    assert result.allowed is False
    assert provider.calls == []


@pytest.mark.parametrize(
    "gate",
    (
        green_gate(head_sha="abcdef2"),
        green_gate(checks_green=False),
        green_gate(unresolved_threads=1),
        green_gate(merge_state="DIRTY"),
    ),
)
def test_live_github_completion_gates_reject_stale_or_unhealthy_pr(
    tmp_path: Path,
    gate: PullRequestGate,
) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS, completion=CompletionPolicy.AUTO_MERGE)
    provider = FakeGitHub(gate)
    validator = GitHubCompletionValidator(provider)
    final = final_card(
        "AUTO_MERGE_ALLOWED:abcdef1",
        source_url="https://github.com/example/project/pull/42",
    )

    result = validator.validate(repo, final, [final])

    assert result.allowed is False
    assert result.reason


def test_pr_link_can_come_from_implementation_proof(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.SUPERVISED)
    provider = FakeGitHub(green_gate())
    validator = GitHubCompletionValidator(provider)
    implementation = QueueCard(
        id="implementation-card",
        title="Implementation",
        status=QueueStatus.DONE,
        labels=("captains_chair", "stage:implementation"),
        metadata={
            "proof": [
                {
                    "status": "passed",
                    "url": "https://github.com/example/project/pull/42",
                }
            ]
        },
    )
    final = final_card("READY_FOR_OWNER:abcdef1", source_url="https://github.com/example/project/issues/39")

    result = validator.validate(repo, final, [implementation, final])

    assert result.allowed is True
    assert provider.calls == [(42, "abcdef1")]


def test_mixed_workflow_pull_request_links_fail_closed(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.SUPERVISED)
    provider = FakeGitHub(green_gate())
    validator = GitHubCompletionValidator(provider)
    implementation = QueueCard(
        id="implementation-card",
        title="Implementation",
        status=QueueStatus.DONE,
        labels=("captains_chair", "stage:implementation"),
        metadata={
            "proof": [
                {
                    "status": "passed",
                    "url": "https://github.com/example/project/pull/42",
                }
            ]
        },
    )
    stale_repair = QueueCard(
        id="repair-card",
        title="Repair",
        status=QueueStatus.DONE,
        labels=("captains_chair", "stage:repair"),
        metadata={
            "proof": [
                {
                    "status": "passed",
                    "note": "Repair was pushed to https://github.com/example/project/pull/41",
                }
            ]
        },
    )
    final = final_card("READY_FOR_OWNER:abcdef1")

    result = validator.validate(repo, final, [implementation, stale_repair, final])

    assert result.allowed is False
    assert "inconsistent" in result.reason
    assert provider.calls == []


def test_missing_pr_link_fails_closed(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.SUPERVISED)
    validator = GitHubCompletionValidator(FakeGitHub(green_gate()))

    result = validator.validate(repo, final_card("READY_FOR_OWNER:abcdef1"), [])

    assert result.allowed is False
    assert "pull-request URL" in result.reason


@pytest.mark.parametrize(
    ("card", "workflow_cards", "message"),
    (
        (
            QueueCard(
                id="implementation-card",
                title="Implementation",
                status=QueueStatus.DONE,
                labels=("stage:implementation",),
            ),
            [],
            "final-review card",
        ),
        (
            QueueCard(
                id="final-card",
                title="Final review",
                status=QueueStatus.DONE,
                labels=("stage:final_review",),
                metadata={"proof": [{"status": "failed", "note": "AUTO_MERGE_ALLOWED:abcdef1"}]},
            ),
            [],
            "proof is missing",
        ),
    ),
)
def test_completion_validator_rejects_invalid_final_review_cards(
    tmp_path: Path,
    card: QueueCard,
    workflow_cards: list[QueueCard],
    message: str,
) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS, completion=CompletionPolicy.AUTO_MERGE)
    result = GitHubCompletionValidator(FakeGitHub(green_gate())).validate(repo, card, workflow_cards)
    assert result.allowed is False
    assert message in result.reason


def test_completion_validator_rejects_non_list_proof_and_unknown_stage(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS, completion=CompletionPolicy.AUTO_MERGE)
    non_list = final_card("AUTO_MERGE_ALLOWED:abcdef1").model_copy(
        update={"metadata": {"proof": "not-a-list"}}
    )
    result = GitHubCompletionValidator(FakeGitHub(green_gate())).validate(
        repo,
        non_list,
        [non_list.model_copy(update={"source_url": "https://github.com/example/project/pull/42"})],
    )
    assert result.allowed is False
    assert "proof is missing" in result.reason

    unknown_stage = final_card("AUTO_MERGE_ALLOWED:abcdef1").model_copy(
        update={"labels": ("stage:not-a-real-stage",)}
    )
    result = GitHubCompletionValidator(FakeGitHub(green_gate())).validate(repo, unknown_stage, [])
    assert result.allowed is False
    assert "final-review card" in result.reason


def test_completion_validator_rejects_multiple_prs_and_provider_failures(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.SUPERVISED)
    final = final_card("READY_FOR_OWNER:abcdef1")
    first = QueueCard(
        id="first",
        title="First PR",
        status=QueueStatus.DONE,
        labels=("stage:implementation",),
        source_url="https://github.com/example/project/pull/41",
    )
    second = QueueCard(
        id="second",
        title="Second PR",
        status=QueueStatus.DONE,
        labels=("stage:repair",),
        source_url="https://github.com/example/project/pull/42",
    )
    result = GitHubCompletionValidator(FakeGitHub(green_gate())).validate(repo, final, [first, second, final])
    assert result.allowed is False
    assert "inconsistent" in result.reason

    class FailingProvider:
        def gate(self, repo: object, number: int, review_head_sha: str | None) -> PullRequestGate:
            del repo, number, review_head_sha
            raise RuntimeError("GitHub API is unavailable")

    repo = repo_config(tmp_path / "provider", mode=OperationMode.SUPERVISED)
    result = GitHubCompletionValidator(FailingProvider()).validate(
        repo,
        final,
        [final.model_copy(update={"source_url": "https://github.com/example/project/pull/42"})],
    )
    assert result.allowed is False
    assert "unavailable" in result.reason


class FixedCompletionValidator:
    def __init__(self, allowed: bool, reason: str = "test completion result") -> None:
        self.allowed = allowed
        self.reason = reason
        self.calls = 0

    def validate(
        self,
        repo: object,
        card: QueueCard,
        workflow_cards: list[QueueCard],
    ) -> CompletionValidation:
        del repo, card, workflow_cards
        self.calls += 1
        return CompletionValidation(self.allowed, self.reason)


def test_rejected_live_completion_creates_retry_and_does_not_cleanup_workspace(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS, completion=CompletionPolicy.AUTO_MERGE)
    config = OpenClawWorkboardConfig(
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
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement the next slice",
        reason="The roadmap item is ready.",
        target_issue=39,
    )
    queue = InMemoryWorkQueue()
    cleaned: list[str] = []
    validator = FixedCompletionValidator(False, "current PR head is stale")
    orchestrator = WorkflowOrchestrator(
        queue,
        config,
        workspace_cleanup=lambda _repo, workspace: cleaned.append(str(workspace.path)) or True,
        completion_validator=validator,
    )
    workspace = WorkspaceRef(kind="worktree", path=tmp_path / "managed-worktree", branch="captains_chair/work/39")
    workflow = orchestrator.enqueue(repo, decision, "live-gate-action", workspace=workspace)
    for card_id in workflow.stage_cards.values():
        note = (
            "AUTO_MERGE_ALLOWED:abcdef1"
            if "stage:final_review" in queue.cards[card_id].labels
            else "passed"
        )
        queue.complete_card(card_id, summary="Passed", proof=({"status": "passed", "note": note},))

    result = orchestrator.reconcile(repo, dispatch=False, dispatch_reason="test")

    assert workflow.stage_cards["live-gate-action:final_review"] in result.proof_retries
    assert result.cleaned_workspaces == ()
    assert cleaned == []
    assert validator.calls == 2
    retry_cards = [card for card in queue.cards.values() if "retry" in card.title.lower()]
    assert retry_cards
    assert "current PR head is stale" in (retry_cards[0].notes or "")


def test_live_completion_is_revalidated_after_github_evidence_changes(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS, completion=CompletionPolicy.AUTO_MERGE)
    config = OpenClawWorkboardConfig(
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
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement the next slice",
        reason="The roadmap item is ready.",
        target_issue=39,
    )
    queue = InMemoryWorkQueue()
    validator = FixedCompletionValidator(True)
    orchestrator = WorkflowOrchestrator(queue, config, completion_validator=validator)
    workflow = orchestrator.enqueue(repo, decision, "revalidate-live-gate")
    for card_id in workflow.stage_cards.values():
        note = (
            "AUTO_MERGE_ALLOWED:abcdef1"
            if "stage:final_review" in queue.cards[card_id].labels
            else "passed"
        )
        queue.complete_card(card_id, summary="Passed", proof=({"status": "passed", "note": note},))

    first = orchestrator.reconcile(repo, dispatch=False, dispatch_reason="test")
    assert first.proof_retries == ()
    assert validator.calls == 2

    validator.allowed = False
    validator.reason = "current PR head changed after final review"
    second = orchestrator.reconcile(repo, dispatch=False, dispatch_reason="test")

    final_card_id = workflow.stage_cards["revalidate-live-gate:final_review"]
    assert final_card_id in second.proof_retries
    assert validator.calls == 4
