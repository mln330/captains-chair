from pathlib import Path

from make_it_so.merge_gate import evaluate_workboard_merge, final_review_head
from make_it_so.models import CompletionPolicy, OperationMode, PullRequestGate
from make_it_so.orchestration import QueueCard, QueueStatus
from tests.helpers import repo_config


def final_card(*, status: str = "passed", head: str = "abcdef123456") -> QueueCard:
    return QueueCard(
        id="final-1",
        title="Final review",
        status=QueueStatus.DONE,
        labels=("make_it_so", "stage:final_review"),
        metadata={
            "proof": [
                {
                    "status": status,
                    "label": "Number 1 final review",
                    "note": f"All final gates passed. AUTO_MERGE_ALLOWED:{head}",
                }
            ]
        },
    )


def gate(*, head: str = "abcdef123456", green: bool = True) -> PullRequestGate:
    return PullRequestGate(
        number=7,
        head_sha=head,
        mergeable=True,
        merge_state="CLEAN",
        draft=False,
        checks_green=green,
        required_checks=(),
        unresolved_threads=0,
    )


def autonomous_repo(tmp_path: Path):
    return repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )


def test_final_review_head_requires_done_final_card_and_passed_proof() -> None:
    assert final_review_head(final_card()) == "abcdef123456"
    assert final_review_head(final_card(status="failed")) is None
    assert final_review_head(final_card().model_copy(update={"status": QueueStatus.BLOCKED})) is None
    assert final_review_head(final_card().model_copy(update={"labels": ("stage:review",)})) is None


def test_final_review_head_uses_latest_passed_proof() -> None:
    card = final_card().model_copy(
        update={
            "metadata": {
                "proof": [
                    {"status": "passed", "note": "old AUTO_MERGE_ALLOWED:dead111"},
                    {"status": "passed", "note": "latest AUTO_MERGE_ALLOWED:bead123"},
                ]
            }
        }
    )

    assert final_review_head(card) == "bead123"


def test_final_review_head_does_not_reuse_old_marker_after_new_pass_without_marker() -> None:
    card = final_card().model_copy(
        update={
            "metadata": {
                "proof": [
                    {"status": "passed", "note": "old AUTO_MERGE_ALLOWED:dead111"},
                    {"status": "passed", "note": "new review completed, marker omitted"},
                ]
            }
        }
    )

    assert final_review_head(card) is None


def test_final_review_head_ignores_malformed_proof_shapes() -> None:
    malformed = final_card().model_copy(update={"metadata": {"proof": "not-a-list"}})
    assert final_review_head(malformed) is None
    mixed = final_card().model_copy(
        update={
            "metadata": {
                "proof": [
                    {"status": "passed", "note": "AUTO_MERGE_ALLOWED:abcdef1"},
                    "not-a-proof-object",
                ]
            }
        }
    )
    assert final_review_head(mixed) == "abcdef1"


def test_workboard_merge_gate_rejects_stale_final_review(tmp_path: Path) -> None:
    result = evaluate_workboard_merge(
        autonomous_repo(tmp_path),
        final_card(head="deadbeef"),
        gate(head="cafebabe"),
    )
    assert not result.allowed
    assert "current PR head" in result.reason


def test_workboard_merge_gate_rejects_missing_proof_and_failed_checks(tmp_path: Path) -> None:
    no_proof = final_card().model_copy(update={"metadata": {"proof": []}})
    assert not evaluate_workboard_merge(autonomous_repo(tmp_path), no_proof, gate()).allowed
    assert not evaluate_workboard_merge(autonomous_repo(tmp_path), final_card(), gate(green=False)).allowed


def test_workboard_merge_gate_allows_exact_current_head_proof(tmp_path: Path) -> None:
    result = evaluate_workboard_merge(autonomous_repo(tmp_path), final_card(), gate())
    assert result.allowed
    assert not result.requires_owner
