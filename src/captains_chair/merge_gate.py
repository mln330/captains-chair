from __future__ import annotations

import re
from typing import Any, cast

from captains_chair.models import FinalVerdict, PullRequestGate, RepoConfig
from captains_chair.orchestration import QueueCard, QueueStatus
from captains_chair.policy import PolicyResult, evaluate_merge

_AUTO_MERGE_PROOF = re.compile(r"(?:^|\s)AUTO_MERGE_ALLOWED:([0-9a-fA-F]{7,64})(?:\s|$)")


def final_review_head(card: QueueCard) -> str | None:
    """Return the reviewed head only for a completed, passed final-review proof."""
    if card.status != QueueStatus.DONE or "stage:final_review" not in card.labels:
        return None
    proof_value = card.metadata.get("proof")
    if not isinstance(proof_value, list):
        return None
    for value in reversed(cast(list[Any], proof_value)):
        if not isinstance(value, dict):
            continue
        proof = cast(dict[str, Any], value)
        if str(proof.get("status") or "").lower() != "passed":
            continue
        for field in ("note", "label"):
            match = _AUTO_MERGE_PROOF.search(str(proof.get(field) or ""))
            if match:
                return match.group(1).lower()
        # A newer passed record without merge authorization invalidates older
        # proof rather than allowing stale evidence to authorize a merge.
        return None
    return None


def evaluate_workboard_merge(
    repo: RepoConfig,
    card: QueueCard,
    gate: PullRequestGate,
) -> PolicyResult:
    reviewed_head = final_review_head(card)
    if reviewed_head is None:
        return PolicyResult(
            False,
            False,
            "final Workboard card lacks passed AUTO_MERGE_ALLOWED:<head-sha> proof",
        )
    anchored_gate = gate.model_copy(update={"review_head_sha": reviewed_head})
    return evaluate_merge(repo, FinalVerdict.AUTO_MERGE_ALLOWED, anchored_gate)
