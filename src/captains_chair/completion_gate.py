from __future__ import annotations

import re
from typing import Any, Protocol, cast

from captains_chair.models import CompletionPolicy, FinalVerdict, PullRequestGate, RepoConfig
from captains_chair.orchestration import (
    CompletionValidation,
    QueueCard,
    WorkStage,
)
from captains_chair.policy import (
    evaluate_control_plane_completion,
    evaluate_merge,
    evaluate_owner_completion,
)


class CompletionGateProvider(Protocol):
    def gate(self, repo: RepoConfig, number: int, review_head_sha: str | None) -> PullRequestGate: ...


class GitHubCompletionValidator:
    """Validate final-review intent against live GitHub state."""

    def __init__(self, provider: CompletionGateProvider) -> None:
        self.provider = provider

    def validate(
        self,
        repo: RepoConfig,
        card: QueueCard,
        workflow_cards: list[QueueCard],
    ) -> CompletionValidation:
        if _stage(card) != WorkStage.FINAL_REVIEW:
            return CompletionValidation(False, "completion validation requires a final-review card")
        marker, verdict, reviewed_head = _final_review_evidence(repo, card)
        if marker is None or verdict is None or reviewed_head is None:
            return CompletionValidation(
                False,
                "final-review proof is missing the policy marker and reviewed PR head",
            )
        pr_numbers = _pull_request_numbers(repo, workflow_cards)
        if not pr_numbers:
            return CompletionValidation(
                False,
                "workflow proof does not contain a GitHub pull-request URL",
            )
        if len(pr_numbers) != 1:
            return CompletionValidation(
                False,
                "workflow proof contains inconsistent GitHub pull-request URLs",
            )
        pr_number = next(iter(pr_numbers))
        try:
            gate = self.provider.gate(repo, pr_number, reviewed_head)
        except Exception as exc:
            return CompletionValidation(False, f"GitHub completion gate unavailable: {str(exc)[:500]}")
        if repo.completion_policy == CompletionPolicy.OWNER_APPROVAL:
            result = evaluate_owner_completion(repo, verdict, gate)
        elif repo.completion_policy == CompletionPolicy.CONTROL_PLANE_COMPLETE:
            result = evaluate_control_plane_completion(repo, verdict, gate)
        else:
            result = evaluate_merge(repo, verdict, gate)
        return CompletionValidation(result.allowed, result.reason)


def _final_review_evidence(
    repo: RepoConfig,
    card: QueueCard,
) -> tuple[str | None, FinalVerdict | None, str | None]:
    marker = {
        CompletionPolicy.OWNER_APPROVAL: "READY_FOR_OWNER:",
        CompletionPolicy.CONTROL_PLANE_COMPLETE: "CONTROL_PLANE_COMPLETE:",
        CompletionPolicy.AUTO_MERGE: "AUTO_MERGE_ALLOWED:",
    }[repo.completion_policy]
    pattern = re.compile(rf"{re.escape(marker)}([0-9a-f]{{7,64}})\b", re.IGNORECASE)
    proof = card.metadata.get("proof")
    if not isinstance(proof, list):
        return None, None, None
    for raw_item in reversed(cast(list[Any], proof)):
        if not isinstance(raw_item, dict):
            continue
        item = cast(dict[str, Any], raw_item)
        if str(item.get("status") or "").lower() != "passed":
            continue
        for field in ("note", "label"):
            match = pattern.search(str(item.get(field) or ""))
            if match:
                verdict = {
                    CompletionPolicy.OWNER_APPROVAL: FinalVerdict.READY_FOR_OWNER,
                    CompletionPolicy.CONTROL_PLANE_COMPLETE: FinalVerdict.CONTROL_PLANE_COMPLETE,
                    CompletionPolicy.AUTO_MERGE: FinalVerdict.AUTO_MERGE_ALLOWED,
                }[repo.completion_policy]
                return marker, verdict, match.group(1)
        # A newer passed proof without the policy marker must not fall back to
        # an older authorization record.
        return None, None, None
    return None, None, None


def _pull_request_numbers(repo: RepoConfig, cards: list[QueueCard]) -> set[int]:
    pattern = re.compile(
        rf"https?://github\.com/{re.escape(repo.full_name)}/pull/(\d+)\b",
        re.IGNORECASE,
    )
    numbers: set[int] = set()
    for card in cards:
        source_match = pattern.search(card.source_url or "")
        if source_match:
            numbers.add(int(source_match.group(1)))
        proof = card.metadata.get("proof")
        if not isinstance(proof, list):
            continue
        for raw_item in cast(list[Any], proof):
            if not isinstance(raw_item, dict):
                continue
            item = cast(dict[str, Any], raw_item)
            if str(item.get("status") or "").lower() != "passed":
                continue
            for field in ("url", "note", "label", "command"):
                match = pattern.search(str(item.get(field) or ""))
                if match:
                    numbers.add(int(match.group(1)))
    return numbers


def _stage(card: QueueCard) -> WorkStage | None:
    for label in card.labels:
        if label.startswith("stage:"):
            try:
                return WorkStage(label.split(":", 1)[1])
            except ValueError:
                return None
    return None


__all__ = ["GitHubCompletionValidator"]
