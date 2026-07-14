from __future__ import annotations

from dataclasses import dataclass

from captains_chair.models import (
    ALWAYS_APPROVAL_ACTIONS,
    ActionKind,
    ActionScope,
    CompletionPolicy,
    FinalVerdict,
    OperationMode,
    PlanDecision,
    PullRequestGate,
    RepoConfig,
)

READ_ONLY_ACTIONS = frozenset({ActionKind.REPORT_ONLY, ActionKind.NO_ACTION})


@dataclass(frozen=True)
class PolicyResult:
    allowed: bool
    requires_owner: bool
    reason: str


def evaluate_action(
    repo: RepoConfig,
    decision: PlanDecision,
    *,
    execute: bool,
    shadow: bool,
    approved: bool = False,
) -> PolicyResult:
    if repo.operation_mode == OperationMode.DISABLED:
        return PolicyResult(False, False, "repository Captain is disabled")
    if decision.target_pr in repo.preserved_prs and decision.action in {
        ActionKind.REPAIR_PR,
        ActionKind.REVIEW_PR,
        ActionKind.MERGE_PR,
    }:
        return PolicyResult(False, True, "the target PR is preserved migration evidence")
    if shadow:
        return PolicyResult(False, False, "shadow mode records decisions but never mutates")
    if decision.scope == ActionScope.CONTROL_PLANE:
        return PolicyResult(
            False, False, "control-plane system maintenance cannot be executed against the managed repository"
        )
    if decision.action in READ_ONLY_ACTIONS:
        return PolicyResult(True, False, "read-only action")
    if not execute:
        return PolicyResult(False, False, "live execution was not requested")
    if repo.operation_mode == OperationMode.ADVISORY:
        return PolicyResult(False, True, "repository is advisory")
    if (
        decision.action in ALWAYS_APPROVAL_ACTIONS
        and decision.action not in repo.approval_whitelist
        and not approved
    ):
        return PolicyResult(False, True, f"{decision.action.value} always requires owner approval")
    if repo.operation_mode == OperationMode.SUPERVISED and not approved:
        return PolicyResult(False, True, "repository is supervised")
    if decision.requires_owner_approval and not approved:
        if decision.owner_blocker is None:
            return PolicyResult(
                False,
                False,
                "planner requested owner approval without an explicit owner blocker; autonomous replanning is required",
            )
        return PolicyResult(False, True, f"planner supplied owner blocker: {decision.owner_blocker}")
    if repo.operation_mode == OperationMode.SUPERVISED:
        return PolicyResult(True, False, "the exact supervised action was approved")
    return PolicyResult(True, False, "allowed by deterministic autonomous policy")


def evaluate_merge(repo: RepoConfig, verdict: FinalVerdict, gate: PullRequestGate) -> PolicyResult:
    if repo.operation_mode == OperationMode.DISABLED:
        return PolicyResult(False, False, "repository Captain is disabled")
    if repo.completion_policy != CompletionPolicy.AUTO_MERGE:
        return PolicyResult(False, True, f"completion policy is {repo.completion_policy.value}")
    if repo.operation_mode != OperationMode.AUTONOMOUS or not repo.allow_autonomous_merge:
        return PolicyResult(False, True, "autonomous merge is not enabled")
    if verdict != FinalVerdict.AUTO_MERGE_ALLOWED:
        return PolicyResult(False, True, f"final verdict is {verdict.value}, not AUTO_MERGE_ALLOWED")
    if gate.draft:
        return PolicyResult(False, False, "pull request is still draft")
    if not gate.mergeable or gate.merge_state.upper() != "CLEAN":
        return PolicyResult(False, False, f"merge state is {gate.merge_state}")
    if not gate.checks_green:
        return PolicyResult(False, False, "required checks are not green")
    if gate.unresolved_threads:
        return PolicyResult(False, False, "unresolved review threads remain")
    if gate.review_head_sha != gate.head_sha:
        return PolicyResult(False, False, "final review is not anchored to the current PR head")
    return PolicyResult(True, False, "all autonomous merge gates passed")


def evaluate_control_plane_completion(repo: RepoConfig, verdict: FinalVerdict, gate: PullRequestGate) -> PolicyResult:
    if repo.operation_mode == OperationMode.DISABLED:
        return PolicyResult(False, False, "repository Captain is disabled")
    if repo.completion_policy != CompletionPolicy.CONTROL_PLANE_COMPLETE:
        return PolicyResult(False, True, f"completion policy is {repo.completion_policy.value}")
    if verdict not in {FinalVerdict.CONTROL_PLANE_COMPLETE, FinalVerdict.AUTO_MERGE_ALLOWED}:
        return PolicyResult(False, True, f"final verdict is {verdict.value}, not CONTROL_PLANE_COMPLETE")
    if gate.draft:
        return PolicyResult(False, False, "pull request is still draft")
    if not gate.mergeable or gate.merge_state.upper() != "CLEAN":
        return PolicyResult(False, False, f"merge state is {gate.merge_state}")
    if not gate.checks_green:
        return PolicyResult(False, False, "required checks are not green")
    if gate.unresolved_threads:
        return PolicyResult(False, False, "unresolved review threads remain")
    if gate.review_head_sha != gate.head_sha:
        return PolicyResult(False, False, "final review is not anchored to the current PR head")
    return PolicyResult(True, False, "Captain completion gates passed; merge remains an owner action")


def evaluate_owner_completion(
    repo: RepoConfig,
    verdict: FinalVerdict,
    gate: PullRequestGate,
) -> PolicyResult:
    if repo.operation_mode == OperationMode.DISABLED:
        return PolicyResult(False, False, "repository Captain is disabled")
    if repo.completion_policy != CompletionPolicy.OWNER_APPROVAL:
        return PolicyResult(False, True, f"completion policy is {repo.completion_policy.value}")
    if verdict != FinalVerdict.READY_FOR_OWNER:
        return PolicyResult(False, True, f"final verdict is {verdict.value}, not READY_FOR_OWNER")
    if gate.draft:
        return PolicyResult(False, False, "pull request is still draft")
    if not gate.mergeable or gate.merge_state.upper() != "CLEAN":
        return PolicyResult(False, False, f"merge state is {gate.merge_state}")
    if not gate.checks_green:
        return PolicyResult(False, False, "required checks are not green")
    if gate.unresolved_threads:
        return PolicyResult(False, False, "unresolved review threads remain")
    if gate.review_head_sha != gate.head_sha:
        return PolicyResult(False, False, "final review is not anchored to the current PR head")
    return PolicyResult(True, False, "owner approval gates passed; merge remains an owner action")
