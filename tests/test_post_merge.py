from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from captains_chair.command import CommandResult
from captains_chair.engine import ControlPlaneEngine, classify_post_merge_runs
from captains_chair.github import GhGitHubProvider, RepositorySnapshot
from captains_chair.harness import HarnessAdapter
from captains_chair.models import EventRecord, OperationMode, RunState
from captains_chair.notifications import Notifier
from captains_chair.state import StateStore
from tests.helpers import app_config, model_policy, repo_config


class PostMergeGitHub:
    def __init__(self, workflow_runs: list[dict[str, Any]]) -> None:
        self.workflow_runs = workflow_runs
        self.default_branch_calls = 0

    def snapshot(self, repo: object) -> RepositorySnapshot:
        del repo
        return RepositorySnapshot({}, [], [], ["main"], self.workflow_runs)

    def default_branch_sha(self, repo: object) -> str:
        del repo
        self.default_branch_calls += 1
        return "unexpected-current-head"


class NullNotifier:
    def send(self, event: EventRecord) -> None:
        del event


def no_git_runner(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: int = 60,
) -> CommandResult:
    del command, cwd, input_text, timeout
    return CommandResult(1, "", "not a git checkout")


def _engine(
    tmp_path: Path,
    workflow_runs: list[dict[str, Any]],
    *,
    include_pr_head: bool = True,
    include_merge_event: bool = True,
) -> tuple[ControlPlaneEngine, StateStore, PostMergeGitHub]:
    plan = tmp_path / "ISSUES_EXECUTION_PLAN.md"
    plan.write_text("# Durable plan\n", encoding="utf-8")
    artifact = tmp_path / "baseline.json"
    artifact.write_text('{"analysis":{"summary":"merged slice"}}', encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", artifact, analyzed=True)
    for target in (
        RunState.BASELINE_REVIEW,
        RunState.READY,
        RunState.PR_OPEN,
        RunState.REVIEWING,
        RunState.COMPLETION_READY,
        RunState.MERGED,
        RunState.POST_MERGE_VERIFICATION,
    ):
        state.transition(repo.full_name, target)
    if include_merge_event:
        evidence: dict[str, Any] = {"merged_head_sha": "main-head"}
        if include_pr_head:
            evidence["pr_head_sha"] = "pr-head"
        state.record_event(
            repo=repo.full_name,
            run_id="merge-run",
            state=RunState.POST_MERGE_VERIFICATION,
            event_type="PR_MERGED",
            summary="Merged",
            reason="Merge gates passed.",
            fingerprint="merge-fingerprint",
            evidence=evidence,
        )
    github = PostMergeGitHub(workflow_runs)
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        cast(HarnessAdapter, object()),
        cast(Notifier, NullNotifier()),
        model_policy(),
        runner=no_git_runner,
    )
    return engine, state, github


def run(name: str, status: str, conclusion: str, sha: str = "head") -> dict[str, object]:
    return {
        "workflowName": name,
        "status": status,
        "conclusion": conclusion,
        "headSha": sha,
        "url": f"https://example.test/{name}",
    }


def test_post_merge_waits_for_current_head_ci() -> None:
    outcome, _, _ = classify_post_merge_runs([], "head", False)
    assert outcome == "waiting"
    outcome, _, _ = classify_post_merge_runs([run("CI", "in_progress", "")], "head", False)
    assert outcome == "waiting"


def test_post_merge_ci_failure_is_degraded() -> None:
    outcome, reason, _ = classify_post_merge_runs([run("CI", "completed", "failure")], "head", False)
    assert outcome == "failed"
    assert "CI" in reason


def test_deploy_failure_is_separate_unless_configured_as_gate() -> None:
    runs = [run("CI", "completed", "success"), run("Deploy to Azure", "completed", "failure")]
    outcome, reason, _ = classify_post_merge_runs(runs, "head", False)
    assert outcome == "passed"
    assert "release blocker" in reason
    gated, _, _ = classify_post_merge_runs(runs, "head", True)
    assert gated == "failed"


def test_engine_post_merge_wait_is_idempotent_until_current_head_ci_finishes(tmp_path: Path) -> None:
    engine, state, _ = _engine(tmp_path, [])

    first = engine.watch(repo_config(tmp_path, mode=OperationMode.AUTONOMOUS), execute=True)
    second = engine.watch(repo_config(tmp_path, mode=OperationMode.AUTONOMOUS), execute=True)

    assert first is not None
    assert first.event.event_type == "POST_MERGE_WAITING"
    assert first.exit_code == 2
    assert second is not None
    assert second.event.event_id == first.event.event_id
    assert state.current_state("example/project") == RunState.POST_MERGE_VERIFICATION


def test_engine_post_merge_failure_degrades_without_selecting_new_work(tmp_path: Path) -> None:
    engine, state, _ = _engine(
        tmp_path,
        [
            {
                "workflowName": "CI",
                "status": "completed",
                "conclusion": "failure",
                "headSha": "main-head",
                "url": "https://example.test/ci",
            }
        ],
    )

    result = engine.watch(repo_config(tmp_path, mode=OperationMode.AUTONOMOUS), execute=True)

    assert result is not None
    assert result.event.event_type == "POST_MERGE_FAILED"
    assert result.exit_code == 2
    assert state.current_state("example/project") == RunState.DEGRADED


def test_engine_post_merge_success_reopens_planning_after_main_ci_passes(tmp_path: Path) -> None:
    engine, state, github = _engine(
        tmp_path,
        [
            {
                "workflowName": "CI",
                "status": "completed",
                "conclusion": "success",
                "headSha": "main-head",
                "url": "https://example.test/ci",
            }
        ],
        include_pr_head=False,
    )

    result = engine.watch(repo_config(tmp_path, mode=OperationMode.AUTONOMOUS), execute=True)

    assert result is not None
    assert result.event.event_type == "POST_MERGE_VERIFIED"
    assert result.exit_code == 0
    assert state.current_state("example/project") == RunState.READY
    assert github.default_branch_calls == 0


def test_engine_post_merge_missing_merge_evidence_fails_closed(tmp_path: Path) -> None:
    engine, state, _ = _engine(tmp_path, [], include_merge_event=False)

    result = engine.watch(repo_config(tmp_path, mode=OperationMode.AUTONOMOUS), execute=True)

    assert result is not None
    assert result.event.event_type == "POST_MERGE_EVIDENCE_MISSING"
    assert result.exit_code == 2
    assert state.current_state("example/project") == RunState.DEGRADED
