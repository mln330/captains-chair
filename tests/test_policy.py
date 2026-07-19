from pathlib import Path

import pytest

from make_it_so.models import (
    ActionKind,
    ActionScope,
    CompletionPolicy,
    FinalVerdict,
    OperationMode,
    PlanDecision,
    PullRequestGate,
)
from make_it_so.policy import (
    evaluate_action,
    evaluate_control_plane_completion,
    evaluate_merge,
    evaluate_owner_completion,
)
from tests.helpers import repo_config


def decision(action: ActionKind = ActionKind.IMPLEMENT) -> PlanDecision:
    return PlanDecision(action=action, summary="Do the work", reason="It is next")


def gate(*, current_review: bool = True, green: bool = True) -> PullRequestGate:
    return PullRequestGate(
        number=1,
        head_sha="abc",
        mergeable=True,
        merge_state="CLEAN",
        draft=False,
        checks_green=green,
        required_checks=(),
        unresolved_threads=0,
        review_head_sha="abc" if current_review else "old",
    )


def test_advisory_and_supervised_actions_require_policy_permission(tmp_path: Path) -> None:
    advisory = repo_config(tmp_path, mode=OperationMode.ADVISORY)
    supervised = repo_config(tmp_path, mode=OperationMode.SUPERVISED)

    assert not evaluate_action(advisory, decision(), execute=True, shadow=False).allowed
    assert not evaluate_action(supervised, decision(), execute=True, shadow=False).allowed
    approved = evaluate_action(supervised, decision(), execute=True, shadow=False, approved=True)
    assert approved.allowed


@pytest.mark.parametrize(
    ("item", "execute", "shadow", "reason"),
    (
        (decision(ActionKind.REPORT_ONLY), True, False, "read-only action"),
        (decision(ActionKind.NO_ACTION), True, False, "read-only action"),
        (decision(), False, False, "live execution was not requested"),
        (decision(), True, True, "shadow mode records decisions but never mutates"),
        (
            PlanDecision(
                action=ActionKind.MAINTENANCE,
                summary="Repair control-plane state",
                reason="The control plane needs maintenance",
                scope=ActionScope.CONTROL_PLANE,
            ),
            True,
            False,
            "control-plane system maintenance",
        ),
    ),
)
def test_non_mutating_action_paths_are_deterministic(
    tmp_path: Path, item: PlanDecision, execute: bool, shadow: bool, reason: str
) -> None:
    result = evaluate_action(
        repo_config(tmp_path, mode=OperationMode.AUTONOMOUS), item, execute=execute, shadow=shadow
    )

    assert result.allowed is (reason == "read-only action")
    assert reason in result.reason


def test_disabled_mode_fails_closed_without_owner_attention(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.DISABLED)
    result = evaluate_action(repo, decision(), execute=True, shadow=False)

    assert not result.allowed
    assert not result.requires_owner
    assert result.reason == "repository Number 1 is disabled"


def test_disabled_mode_preserves_completion_policy_for_later_resume(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.DISABLED, completion=CompletionPolicy.AUTO_MERGE)

    assert repo.completion_policy == CompletionPolicy.AUTO_MERGE
    assert repo.allow_autonomous_merge


def test_high_risk_action_still_requires_owner_when_autonomous(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    result = evaluate_action(repo, decision(ActionKind.PRODUCTION_DEPLOY), execute=True, shadow=False)
    assert not result.allowed
    assert result.requires_owner


@pytest.mark.parametrize(
    "action",
    (
        ActionKind.UPDATE_PLAN,
        ActionKind.IMPLEMENT,
        ActionKind.REVIEW_PR,
        ActionKind.REPAIR_PR,
        ActionKind.CREATE_ISSUE,
        ActionKind.UPDATE_ISSUE,
        ActionKind.CLOSE_ISSUE,
        ActionKind.MAINTENANCE,
    ),
)
def test_autonomous_routine_actions_do_not_require_owner(
    tmp_path: Path, action: ActionKind
) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)

    result = evaluate_action(repo, decision(action), execute=True, shadow=False)

    assert result.allowed
    assert not result.requires_owner


def test_autonomous_issue_reconciliation_actions_do_not_require_owner(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    decisions = (
        PlanDecision(
            action=ActionKind.LABEL_ISSUE,
            summary="Label the active issue",
            reason="The issue is ready for implementation.",
            target_issue=12,
            issue_labels=("ready-for-dev",),
        ),
        PlanDecision(
            action=ActionKind.RETARGET_ISSUE,
            summary="Retarget the active issue",
            reason="The issue belongs to the active sprint.",
            target_issue=12,
            issue_milestone="Sprint 2",
            issue_assignees=("octocat",),
        ),
    )

    results = [evaluate_action(repo, item, execute=True, shadow=False) for item in decisions]

    assert all(result.allowed and not result.requires_owner for result in results)


@pytest.mark.parametrize(
    ("mode", "action"),
    tuple(
        (mode, action)
        for mode in (OperationMode.SUPERVISED, OperationMode.AUTONOMOUS)
        for action in (
            ActionKind.MERGE_PR,
            ActionKind.RELEASE,
            ActionKind.PRODUCTION_DEPLOY,
            ActionKind.SECRETS,
            ActionKind.BILLING,
            ActionKind.DESTRUCTIVE,
            ActionKind.FORCE_PUSH,
            ActionKind.DELETE_BRANCH,
        )
    ),
)
def test_explicit_approval_authorizes_sensitive_action(
    tmp_path: Path, mode: OperationMode, action: ActionKind
) -> None:
    repo = repo_config(tmp_path, mode=mode)

    result = evaluate_action(repo, decision(action), execute=True, shadow=False, approved=True)

    assert result.allowed
    assert not result.requires_owner


def test_sensitive_action_can_be_explicitly_whitelisted_for_autonomous_execution(
    tmp_path: Path,
) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS).model_copy(
        update={"approval_whitelist": frozenset({ActionKind.MERGE_PR})}
    )

    result = evaluate_action(repo, decision(ActionKind.MERGE_PR), execute=True, shadow=False)

    assert result.allowed
    assert not result.requires_owner


def test_autonomous_routine_action_honors_planner_approval_signal(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    guarded = PlanDecision(
        action=ActionKind.UPDATE_ISSUE,
        summary="Update the active work contract",
        reason="The issue is stale",
        target_issue=6,
        requires_owner_approval=True,
        owner_blocker="GOAL_DIVERGENCE: the issue no longer matches the approved goal",
    )

    result = evaluate_action(repo, guarded, execute=True, shadow=False)

    assert not result.allowed
    assert result.requires_owner


def test_autonomous_bare_planner_approval_request_does_not_page_owner(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    guarded = PlanDecision(
        action=ActionKind.UPDATE_ISSUE,
        summary="Update the active work contract",
        reason="The issue is stale",
        target_issue=6,
        requires_owner_approval=True,
    )

    result = evaluate_action(repo, guarded, execute=True, shadow=False)

    assert not result.allowed
    assert not result.requires_owner
    assert "explicit owner blocker" in result.reason


def test_plan_owner_blocker_requires_explicit_prefix() -> None:
    with pytest.raises(ValueError, match="owner_blocker must begin"):
        PlanDecision(
            action=ActionKind.UPDATE_PLAN,
            summary="Update the plan",
            reason="The goal changed",
            requires_owner_approval=True,
            owner_blocker="The owner should decide",
        )


def test_merge_action_requires_owner_unless_explicitly_whitelisted(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    guarded = decision(ActionKind.MERGE_PR).model_copy(update={"target_pr": 7})

    result = evaluate_action(repo, guarded, execute=True, shadow=False)

    assert not result.allowed
    assert result.requires_owner


def test_supervised_approval_satisfies_planner_approval_signal(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.SUPERVISED)
    guarded = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement the next slice",
        reason="It is ready",
        requires_owner_approval=True,
    )

    result = evaluate_action(repo, guarded, execute=True, shadow=False, approved=True)

    assert result.allowed


def test_preserved_migration_pr_cannot_be_repaired_or_reviewed(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS).model_copy(update={"preserved_prs": (34,)})
    for action in (ActionKind.REPAIR_PR, ActionKind.REVIEW_PR, ActionKind.MERGE_PR):
        guarded = PlanDecision(
            action=action,
            summary="Touch preserved PR",
            reason="Should be blocked",
            target_pr=34,
        )
        result = evaluate_action(repo, guarded, execute=True, shadow=False)
        assert not result.allowed
        assert result.requires_owner


def test_only_exact_auto_merge_verdict_can_merge(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS, completion=CompletionPolicy.AUTO_MERGE)
    assert not evaluate_merge(repo, FinalVerdict.READY_FOR_OWNER, gate()).allowed
    assert not evaluate_merge(repo, FinalVerdict.CONTROL_PLANE_COMPLETE, gate()).allowed
    assert evaluate_merge(repo, FinalVerdict.AUTO_MERGE_ALLOWED, gate()).allowed
    assert not evaluate_merge(repo, FinalVerdict.AUTO_MERGE_ALLOWED, gate(current_review=False)).allowed


@pytest.mark.parametrize(
    "repo_update, verdict, pr_gate",
    (
        ({"operation_mode": OperationMode.DISABLED}, FinalVerdict.AUTO_MERGE_ALLOWED, gate()),
        ({"completion_policy": CompletionPolicy.OWNER_APPROVAL}, FinalVerdict.AUTO_MERGE_ALLOWED, gate()),
        ({"operation_mode": OperationMode.SUPERVISED}, FinalVerdict.AUTO_MERGE_ALLOWED, gate()),
        ({"allow_autonomous_merge": False}, FinalVerdict.AUTO_MERGE_ALLOWED, gate()),
        ({}, FinalVerdict.AUTO_MERGE_ALLOWED, gate().model_copy(update={"draft": True})),
        ({}, FinalVerdict.AUTO_MERGE_ALLOWED, gate().model_copy(update={"mergeable": False})),
        ({}, FinalVerdict.AUTO_MERGE_ALLOWED, gate().model_copy(update={"merge_state": "BEHIND"})),
        ({}, FinalVerdict.AUTO_MERGE_ALLOWED, gate().model_copy(update={"unresolved_threads": 1})),
    ),
)
def test_merge_fails_closed_for_every_gate(
    tmp_path: Path,
    repo_update: dict[str, object],
    verdict: FinalVerdict,
    pr_gate: PullRequestGate,
) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS, completion=CompletionPolicy.AUTO_MERGE).model_copy(
        update=repo_update
    )

    result = evaluate_merge(repo, verdict, pr_gate)

    assert not result.allowed
    assert result.reason


def test_disabled_mode_blocks_all_completion_policies(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.DISABLED, completion=CompletionPolicy.OWNER_APPROVAL)
    assert not evaluate_control_plane_completion(repo, FinalVerdict.CONTROL_PLANE_COMPLETE, gate()).allowed


def test_control_plane_complete_is_distinct_from_merge(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS, completion=CompletionPolicy.CONTROL_PLANE_COMPLETE)
    assert evaluate_control_plane_completion(repo, FinalVerdict.CONTROL_PLANE_COMPLETE, gate()).allowed
    assert not evaluate_merge(repo, FinalVerdict.CONTROL_PLANE_COMPLETE, gate()).allowed
    assert not evaluate_control_plane_completion(repo, FinalVerdict.CONTROL_PLANE_COMPLETE, gate(green=False)).allowed


@pytest.mark.parametrize(
    "repo_update, verdict, pr_gate",
    (
        ({"completion_policy": CompletionPolicy.OWNER_APPROVAL}, FinalVerdict.CONTROL_PLANE_COMPLETE, gate()),
        ({}, FinalVerdict.READY_FOR_OWNER, gate()),
        ({}, FinalVerdict.CONTROL_PLANE_COMPLETE, gate().model_copy(update={"draft": True})),
        ({}, FinalVerdict.CONTROL_PLANE_COMPLETE, gate().model_copy(update={"mergeable": False})),
        ({}, FinalVerdict.CONTROL_PLANE_COMPLETE, gate().model_copy(update={"merge_state": "BEHIND"})),
        ({}, FinalVerdict.CONTROL_PLANE_COMPLETE, gate().model_copy(update={"unresolved_threads": 1})),
        ({}, FinalVerdict.CONTROL_PLANE_COMPLETE, gate(current_review=False)),
    ),
)
def test_control_plane_completion_fails_closed_for_every_gate(
    tmp_path: Path,
    repo_update: dict[str, object],
    verdict: FinalVerdict,
    pr_gate: PullRequestGate,
) -> None:
    repo = repo_config(
        tmp_path, mode=OperationMode.AUTONOMOUS, completion=CompletionPolicy.CONTROL_PLANE_COMPLETE
    ).model_copy(update=repo_update)

    result = evaluate_control_plane_completion(repo, verdict, pr_gate)

    assert not result.allowed
    assert result.reason


@pytest.mark.parametrize(
    "repo_update, verdict, pr_gate",
    (
        ({"completion_policy": CompletionPolicy.CONTROL_PLANE_COMPLETE}, FinalVerdict.READY_FOR_OWNER, gate()),
        ({}, FinalVerdict.READY_FOR_OWNER, gate().model_copy(update={"draft": True})),
        ({}, FinalVerdict.READY_FOR_OWNER, gate().model_copy(update={"mergeable": False})),
        ({}, FinalVerdict.READY_FOR_OWNER, gate().model_copy(update={"merge_state": "BEHIND"})),
        ({}, FinalVerdict.READY_FOR_OWNER, gate().model_copy(update={"checks_green": False})),
        ({}, FinalVerdict.READY_FOR_OWNER, gate().model_copy(update={"unresolved_threads": 1})),
        ({}, FinalVerdict.READY_FOR_OWNER, gate(current_review=False)),
    ),
)
def test_owner_completion_fails_closed_for_every_gate(
    tmp_path: Path,
    repo_update: dict[str, object],
    verdict: FinalVerdict,
    pr_gate: PullRequestGate,
) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS, completion=CompletionPolicy.OWNER_APPROVAL).model_copy(
        update=repo_update
    )

    result = evaluate_owner_completion(repo, verdict, pr_gate)

    assert not result.allowed
    assert result.reason
