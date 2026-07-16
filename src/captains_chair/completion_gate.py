from __future__ import annotations

import json
import re
from typing import Any, Protocol, cast

from captains_chair.config import load_project_manifest
from captains_chair.models import (
    ApplicationSurface,
    CompletionPolicy,
    FinalVerdict,
    PullRequestGate,
    QAProfile,
    RepoConfig,
)
from captains_chair.orchestration import (
    CompletionValidation,
    QARequirementContext,
    QueueCard,
    WorkStage,
)
from captains_chair.policy import (
    evaluate_control_plane_completion,
    evaluate_merge,
    evaluate_owner_completion,
)
from captains_chair.qa import select_qa


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
        stage = _stage(card)
        if stage in {WorkStage.TEST, WorkStage.UX_REVIEW} and card.metadata.get("qaProfile"):
            context = self.required_qa(repo, workflow_cards)
            if context is None:
                return CompletionValidation(False, "QA proof cannot be validated before a PR exists")
            profile_key = str(card.metadata.get("qaProfile") or "")
            profile = next(
                (item for item in context.selection.profiles if item.key == profile_key),
                None,
            )
            if profile is None:
                return CompletionValidation(
                    True,
                    f"QA profile {profile_key!r} is no longer required by current paths",
                )
            return _validate_qa_evidence(card, profile, context.head_sha)
        if stage != WorkStage.FINAL_REVIEW:
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
        if any(card.metadata.get("qaEvidenceVersion") for card in workflow_cards):
            qa_context = self.required_qa(repo, workflow_cards)
            if qa_context is None:
                return CompletionValidation(False, "required QA cannot be resolved from live GitHub state")
            for profile in qa_context.selection.profiles:
                candidates = [
                    item
                    for item in workflow_cards
                    if str(item.metadata.get("qaProfile") or "") == profile.key
                ]
                valid = next(
                    (
                        item
                        for item in reversed(candidates)
                        if _validate_qa_evidence(item, profile, qa_context.head_sha).allowed
                    ),
                    None,
                )
                if valid is None:
                    return CompletionValidation(
                        False,
                        f"required QA profile {profile.key!r} lacks current-head evidence",
                    )
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

    def required_qa(
        self,
        repo: RepoConfig,
        workflow_cards: list[QueueCard],
    ) -> QARequirementContext | None:
        pr_numbers = _pull_request_numbers(repo, workflow_cards)
        if len(pr_numbers) != 1:
            return None
        pr_number = next(iter(pr_numbers))
        pull_request = getattr(self.provider, "pull_request", None)
        pull_request_files = getattr(self.provider, "pull_request_files", None)
        if not callable(pull_request) or not callable(pull_request_files):
            return None
        pr = pull_request(repo, pr_number)
        if not isinstance(pr, dict):
            return None
        pr_value = cast(dict[str, Any], pr)
        head_sha = str(pr_value.get("headRefOid") or "").strip()
        if not head_sha:
            return None
        actual_value = pull_request_files(repo, pr_number)
        if not isinstance(actual_value, tuple):
            return None
        actual_items = cast(tuple[object, ...], actual_value)
        if any(not isinstance(item, str) for item in actual_items):
            return None
        actual_paths = cast(tuple[str, ...], actual_items)
        planned_paths = tuple(
            dict.fromkeys(
                str(path)
                for card in workflow_cards
                for value in (card.metadata.get("plannedChangedPaths"),)
                if isinstance(value, list)
                for path in cast(list[Any], value)
                if str(path).strip()
            )
        )
        manifest = load_project_manifest(repo.local_path, repo.project_manifest)
        selection = select_qa(repo, planned_paths, manifest, actual_paths)
        return QARequirementContext(
            pr_number=pr_number,
            pr_url=f"https://github.com/{repo.full_name}/pull/{pr_number}",
            head_sha=head_sha,
            planned_paths=planned_paths,
            actual_paths=actual_paths,
            selection=selection,
        )


def _validate_qa_evidence(
    card: QueueCard,
    profile: QAProfile,
    head_sha: str,
) -> CompletionValidation:
    proof = card.metadata.get("proof")
    if not isinstance(proof, list):
        return CompletionValidation(False, f"QA profile {profile.key!r} has no structured proof")
    latest = next(
        (
            cast(dict[str, Any], item)
            for item in reversed(cast(list[Any], proof))
            if isinstance(item, dict)
            and str(cast(dict[str, Any], item).get("status") or "").lower() == "passed"
        ),
        None,
    )
    if latest is None:
        return CompletionValidation(False, f"QA profile {profile.key!r} has no passed proof")
    marker = re.compile(
        rf"QA_PASSED:{re.escape(profile.key)}:([0-9a-f]{{7,64}})\b",
        re.IGNORECASE,
    )
    marker_head: str | None = None
    for field in ("label", "note"):
        match = marker.search(str(latest.get(field) or ""))
        if match:
            marker_head = match.group(1)
            break
    if marker_head is None or marker_head.lower() != head_sha.lower():
        return CompletionValidation(False, f"QA profile {profile.key!r} proof is stale for {head_sha}")
    if not str(latest.get("model") or "").strip() or not str(latest.get("provider") or "").strip():
        return CompletionValidation(False, f"QA profile {profile.key!r} proof lacks model provenance")
    evidence = latest.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return CompletionValidation(False, f"QA profile {profile.key!r} proof lacks evidence")
    if ApplicationSurface.WEB_UI in profile.surfaces:
        evidence_text = json.dumps(evidence, sort_keys=True).lower()
        missing = [
            item
            for item in ("accessibility", "contrast", "responsive", "flow", "cohesion")
            if item not in evidence_text
        ]
        if missing:
            return CompletionValidation(
                False,
                f"UI QA profile {profile.key!r} evidence is missing: {', '.join(missing)}",
            )
    return CompletionValidation(True, f"QA profile {profile.key!r} passed for {head_sha}")


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
