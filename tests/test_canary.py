from pathlib import Path

import pytest

from captains_chair.canary import (
    build_canary_spec,
    canary_board_id,
    canary_proof_marker,
    evaluate_canary_card,
)
from captains_chair.orchestration import QueueCard, QueueStatus
from tests.helpers import repo_config


def _card(status: QueueStatus, proof: list[dict[str, object]] | None = None) -> QueueCard:
    return QueueCard(
        id="card-1",
        title="Runtime canary",
        status=status,
        metadata={"proof": proof} if proof is not None else {},
    )


def test_canary_spec_is_dedicated_and_forbids_repository_work(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    spec = build_canary_spec(
        repo,
        canary_id="smoke-1",
        worker_id="github-tester",
        max_runtime_seconds=60,
        max_retries=1,
    )

    assert canary_board_id(repo) == "captains_chair-canary-example-project"
    assert spec.key == "captains_chair:canary:example/project:smoke-1"
    assert spec.agent_id == "github-tester"
    assert "Do not inspect, edit, commit, push, or merge" in spec.notes
    assert "portable worker-protocol helper is the lifecycle interface" in spec.notes
    assert "Do not search OpenClaw internals or call native Workboard tools" in spec.notes
    assert canary_proof_marker("smoke-1") in spec.notes


@pytest.mark.parametrize(
    ("status", "proof", "expected", "reason"),
    (
        (QueueStatus.READY, None, "pending", "card is ready"),
        (QueueStatus.BLOCKED, None, "failed", "card is blocked"),
        (
            QueueStatus.DONE,
            [{"status": "passed", "note": "tests passed"}],
            "failed",
            "does not contain CAPTAINS_CHAIR_CANARY_PROOF:smoke",
        ),
        (
            QueueStatus.DONE,
            [{"status": "passed", "note": "CAPTAINS_CHAIR_CANARY_PROOF:smoke"}],
            "passed",
            "required passed canary proof",
        ),
    ),
)
def test_canary_evaluation_requires_explicit_passed_marker(
    status: QueueStatus,
    proof: list[dict[str, object]] | None,
    expected: str,
    reason: str,
) -> None:
    result = evaluate_canary_card(_card(status, proof), canary_id="smoke")
    assert result.status == expected
    assert reason in result.reason


def test_canary_id_is_validated(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="canary_id"):
        build_canary_spec(
            repo_config(tmp_path),
            canary_id="contains spaces",
            worker_id="tester",
            max_runtime_seconds=60,
            max_retries=1,
        )
