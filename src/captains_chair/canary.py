from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, cast

from captains_chair.models import RepoConfig
from captains_chair.orchestration import QueueCard, QueueCardSpec, QueueStatus

CANARY_PROOF_PREFIX = "CAPTAINS_CHAIR_CANARY_PROOF:"
CanaryStatus = Literal["pending", "passed", "failed"]


@dataclass(frozen=True)
class CanaryResult:
    status: CanaryStatus
    reason: str


def canary_board_id(repo: RepoConfig) -> str:
    """Return a dedicated Workboard board name that cannot mix with project cards."""
    return f"captains_chair-canary-{repo.full_name.replace('/', '-').lower()}"


def canary_proof_marker(canary_id: str) -> str:
    return f"{CANARY_PROOF_PREFIX}{canary_id}"


def build_canary_spec(
    repo: RepoConfig,
    *,
    canary_id: str,
    worker_id: str,
    max_runtime_seconds: int,
    max_retries: int,
) -> QueueCardSpec:
    """Build a no-repository-mutation card for runtime validation."""
    normalized_id = canary_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", normalized_id):
        raise ValueError("canary_id must contain only letters, numbers, dots, underscores, or hyphens")
    marker = canary_proof_marker(normalized_id)
    return QueueCardSpec(
        key=f"captains_chair:canary:{repo.full_name.lower()}:{normalized_id}",
        title=f"CAPTAINS_CHAIR runtime canary: {repo.full_name}",
        notes=(
            f"Repository: {repo.full_name}\n"
            f"Canary ID: {normalized_id}\n\n"
            "This is a runtime-only canary. Do not inspect, edit, commit, push, or merge repository code. "
            "Follow the lifecycle helper commands in your AGENTS.md exactly: send at least one heartbeat, "
            "then complete the already-claimed card with passed proof. Do not search OpenClaw internals or "
            "call native Workboard tools; the portable worker-protocol helper is the lifecycle interface. "
            f"The proof note must contain exactly `{marker}`. If the runtime cannot complete the card, "
            "block it with a TECHNICAL: reason."
        ),
        status=QueueStatus.TODO,
        priority="low",
        labels=("captains_chair", "runtime-canary", f"repo:{repo.full_name.lower()}", f"canary:{normalized_id}"),
        agent_id=worker_id,
        source_url=f"https://github.com/{repo.full_name}",
        max_runtime_seconds=max_runtime_seconds,
        max_retries=max_retries,
    )


def evaluate_canary_card(card: QueueCard, *, canary_id: str) -> CanaryResult:
    """Evaluate only the durable card state and proof required by the canary contract."""
    if card.status == QueueStatus.BLOCKED:
        return CanaryResult("failed", "the canary card is blocked")
    if card.status != QueueStatus.DONE:
        return CanaryResult("pending", f"the canary card is {card.status.value}")
    marker = canary_proof_marker(canary_id.strip())
    proof = card.metadata.get("proof")
    if not isinstance(proof, list):
        return CanaryResult("failed", "the card is done without structured proof")
    for raw_item in cast(list[Any], proof):
        if not isinstance(raw_item, dict):
            continue
        item = cast(dict[str, Any], raw_item)
        if str(item.get("status") or "").lower() != "passed":
            continue
        if any(marker in str(item.get(field) or "") for field in ("note", "label", "command")):
            return CanaryResult("passed", "the worker submitted the required passed canary proof")
    return CanaryResult("failed", f"done proof does not contain {marker}")


def summarize_canary_card(card: QueueCard) -> dict[str, Any]:
    """Return concise operator-facing card data without echoing notes or runtime logs."""
    proof = card.metadata.get("proof")
    proof_summary: list[dict[str, Any]] = []
    if isinstance(proof, list):
        for raw_item in cast(list[Any], proof):
            if not isinstance(raw_item, dict):
                continue
            item = cast(dict[str, Any], raw_item)
            proof_summary.append(
                {
                    "status": item.get("status"),
                    "note": str(item.get("note") or item.get("label") or "")[:500],
                    "url": item.get("url"),
                }
            )
    return {
        "id": card.id,
        "title": card.title,
        "status": card.status.value,
        "agent_id": card.agent_id,
        "source_url": card.source_url,
        "proof": proof_summary,
    }


__all__ = [
    "CANARY_PROOF_PREFIX",
    "CanaryResult",
    "build_canary_spec",
    "canary_board_id",
    "canary_proof_marker",
    "evaluate_canary_card",
    "summarize_canary_card",
]
