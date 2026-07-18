from pathlib import Path
from typing import Any

from make_it_so.completion_gate import GitHubCompletionValidator
from make_it_so.models import (
    ActionKind,
    CompletionPolicy,
    OpenClawWorkboardConfig,
    OperationMode,
    PlanDecision,
    PullRequestGate,
    WorkerAssignments,
)
from make_it_so.orchestration import QueueStatus, WorkflowOrchestrator, build_workflow
from make_it_so.qa import select_qa
from tests.fakes import InMemoryWorkQueue
from tests.helpers import repo_config


def orchestration_config(*, live: bool = False) -> OpenClawWorkboardConfig:
    return OpenClawWorkboardConfig(
        require_live_completion_validation=live,
        workers=WorkerAssignments(
            captain="captain",
            coder="coder",
            reviewer="reviewer",
            tester="tester",
            ux_reviewer="ux-reviewer",
            final_reviewer="final-reviewer",
            merger="merger",
            verifier="verifier",
        ),
    )


class LiveQAProvider:
    def __init__(self, head_sha: str, paths: tuple[str, ...]) -> None:
        self.head_sha = head_sha
        self.paths = paths

    def pull_request(self, repo: object, number: int) -> dict[str, Any]:
        del repo, number
        return {"headRefOid": self.head_sha}

    def pull_request_files(self, repo: object, number: int) -> tuple[str, ...]:
        del repo, number
        return self.paths

    def gate(self, repo: object, number: int, review_head_sha: str | None) -> PullRequestGate:
        del repo
        return PullRequestGate(
            number=number,
            head_sha=self.head_sha,
            mergeable=True,
            merge_state="CLEAN",
            draft=False,
            checks_green=True,
            required_checks=(),
            unresolved_threads=0,
            review_head_sha=review_head_sha,
        )


def qa_proof(profile: str, head_sha: str, *, ui: bool = False) -> tuple[dict[str, Any], ...]:
    evidence = ["targeted behavior and failure modes checked"]
    if ui:
        evidence = [
            "accessibility checked",
            "contrast checked",
            "responsive layouts checked",
            "flow checked",
            "cohesion checked",
        ]
    return (
        {
            "status": "passed",
            "note": f"QA_PASSED:{profile}:{head_sha}",
            "model": "qa-model",
            "provider": "test-provider",
            "evidence": evidence,
        },
    )


def test_mixed_surfaces_dispatch_distinct_profiles_without_duplicate_generic_checks(
    tmp_path: Path,
) -> None:
    repo = repo_config(tmp_path).model_copy(update={"checks": ("pytest",)})
    paths = (
        "frontend/App.tsx",
        "src/cli.py",
        "api/openapi.yaml",
        "pipelines/orders.sql",
        "infra/main.tf",
    )
    selection = select_qa(repo, paths)
    workflow = build_workflow(
        repo,
        PlanDecision(
            action=ActionKind.IMPLEMENT,
            summary="Implement a mixed-surface feature",
            reason="The work package spans multiple public capabilities.",
            changed_paths=paths,
        ),
        "mixed-capabilities",
        orchestration_config(),
    )

    keys = [profile.key for profile in selection.profiles]
    assert len(keys) == len(set(keys))
    assert keys.count("deterministic-checks") == 1
    assert sum("pytest" in profile.checks for profile in selection.profiles) == 1
    qa_cards = [card for card in workflow.stages if card.metadata.get("qaProfile")]
    assert {str(card.metadata["qaProfile"]) for card in qa_cards} == set(keys)
    assert len(qa_cards) == len(keys)


def test_actual_unplanned_surface_materializes_a_new_qa_worker(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    provider = LiveQAProvider("abcdef1", ("frontend/App.tsx",))
    queue = InMemoryWorkQueue()
    orchestrator = WorkflowOrchestrator(
        queue,
        orchestration_config(live=True),
        completion_validator=GitHubCompletionValidator(provider),
    )
    workflow = orchestrator.enqueue(
        repo,
        PlanDecision(
            action=ActionKind.IMPLEMENT,
            summary="Implement a service change",
            reason="Planning expected only a service file.",
            target_issue=7,
            changed_paths=("src/service.py",),
        ),
        "actual-ui",
    )
    implementation = queue.cards[workflow.stage_cards["actual-ui:implementation"]]
    queue.complete_card(
        implementation.id,
        summary="PR opened",
        proof=(
            {
                "status": "passed",
                "url": "https://github.com/example/project/pull/12",
                "note": "head abcdef1",
            },
        ),
    )

    result = orchestrator.reconcile(repo, dispatch=False, dispatch_reason="test")

    assert len(result.qa_created) == 1
    created = queue.cards[result.qa_created[0]]
    assert created.metadata["qaProfile"] == "web-ui-qa"
    assert created.metadata["actualChangedPaths"] == ["frontend/App.tsx"]
    assert created.agent_id == "ux-reviewer"
    assert created.status == QueueStatus.READY


def test_stale_qa_is_retried_after_the_pr_head_changes(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    provider = LiveQAProvider("abcdef1", ("frontend/App.tsx",))
    queue = InMemoryWorkQueue()
    orchestrator = WorkflowOrchestrator(
        queue,
        orchestration_config(live=True),
        completion_validator=GitHubCompletionValidator(provider),
    )
    workflow = orchestrator.enqueue(
        repo,
        PlanDecision(
            action=ActionKind.REVIEW_PR,
            summary="Review the UI pull request",
            reason="The current PR needs all required gates.",
            target_pr=12,
            changed_paths=("frontend/App.tsx",),
        ),
        "stale-ui",
    )
    qa_id = workflow.stage_cards["stale-ui:qa:web-ui-qa"]
    queue.complete_card(
        qa_id,
        summary="UI QA passed",
        proof=qa_proof("web-ui-qa", "abcdef1", ui=True),
    )
    first = orchestrator.reconcile(repo, dispatch=False, dispatch_reason="test")
    assert qa_id not in first.proof_retries

    provider.head_sha = "bcdef12"
    second = orchestrator.reconcile(repo, dispatch=False, dispatch_reason="test")

    assert qa_id in second.proof_retries
    retry = next(card for card in queue.cards.values() if "retry" in card.title.lower())
    assert retry.metadata["qaProfile"] == "web-ui-qa"
    assert retry.agent_id == "ux-reviewer"


def test_final_review_rejects_missing_provenance_and_incomplete_ui_evidence(
    tmp_path: Path,
) -> None:
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    provider = LiveQAProvider("abcdef1", ("frontend/App.tsx",))
    queue = InMemoryWorkQueue()
    orchestrator = WorkflowOrchestrator(
        queue,
        orchestration_config(live=True),
        completion_validator=GitHubCompletionValidator(provider),
    )
    workflow = orchestrator.enqueue(
        repo,
        PlanDecision(
            action=ActionKind.REVIEW_PR,
            summary="Review the UI pull request",
            reason="The current PR needs all required gates.",
            target_pr=12,
            changed_paths=("frontend/App.tsx",),
        ),
        "ui-evidence",
    )
    qa_id = workflow.stage_cards["ui-evidence:qa:web-ui-qa"]
    queue.complete_card(
        qa_id,
        summary="Incomplete UI QA",
        proof=(
            {
                "status": "passed",
                "note": "QA_PASSED:web-ui-qa:abcdef1",
                "model": "",
                "provider": "",
                "evidence": ["responsive flow checked"],
            },
        ),
    )
    final_id = workflow.stage_cards["ui-evidence:final_review"]
    queue.complete_card(
        final_id,
        summary="Final review",
        proof=({"status": "passed", "note": "AUTO_MERGE_ALLOWED:abcdef1"},),
    )

    result = orchestrator.reconcile(repo, dispatch=False, dispatch_reason="test")

    assert qa_id in result.proof_retries
    assert final_id in result.proof_retries
