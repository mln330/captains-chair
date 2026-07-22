from __future__ import annotations

import json
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from make_it_so.command import CommandResult
from make_it_so.direct_orchestrator import DirectOrchestrator
from make_it_so.direct_workers import (
    CommandWorkerExecutor,
    WorkerExecutionResult,
    _codex_additional_writable_dirs,  # pyright: ignore[reportPrivateUsage]
    _codex_usage,  # pyright: ignore[reportPrivateUsage]
    _worker_output_schema,  # pyright: ignore[reportPrivateUsage]
    _worker_prompt,  # pyright: ignore[reportPrivateUsage]
)
from make_it_so.models import (
    ActionKind,
    CompletionPolicy,
    DirectOrchestratorConfig,
    OperationMode,
    PlanDecision,
    WorkerAssignments,
    WorkerModelAssignments,
)
from make_it_so.orchestration import (
    BlockerKind,
    QueueCard,
    QueueCardSpec,
    QueueStatus,
    WorkspaceRef,
    classify_blocker,
)
from make_it_so.runtime import build_work_queue_orchestrator
from tests.helpers import repo_config

WORKERS = WorkerAssignments(
    captain="captain",
    coder="coder",
    reviewer="reviewer",
    tester="tester",
    ux_reviewer="ux",
    final_reviewer="final",
    merger="merge",
    verifier="verify",
)


class StructuredWorkerRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.prompts: list[str] = []

    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, timeout
        argv = list(command)
        self.commands.append(argv)
        prompt = input_text or argv[argv.index("--message") + 1]
        self.prompts.append(prompt)
        payload = {
            "status": "completed",
            "summary": "Worker completed the assigned stage.",
            "proof": [
                {
                    "status": "passed",
                    "note": "Targeted checks passed. READY_FOR_OWNER:abcdef1",
                }
            ],
        }
        if len(argv) > 1 and argv[1] == "exec":
            output_path = Path(argv[argv.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(payload), encoding="utf-8")
            return CommandResult(
                0,
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 120,
                            "cached_input_tokens": 40,
                            "output_tokens": 12,
                            "reasoning_output_tokens": 3,
                        },
                    }
                ),
                "",
            )
        envelope = {"result": {"payloads": [{"text": json.dumps(payload)}]}}
        return CommandResult(0, json.dumps(envelope), "")


def _decision() -> PlanDecision:
    return PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement a direct worker lifecycle fixture",
        reason="The fixture proves board-free execution.",
        target_issue=2,
        acceptance_criteria=("Direct workers complete the workflow",),
    )


@pytest.mark.parametrize("runtime", ["openclaw", "codex"])
def test_direct_runtime_completes_workflow_without_workboard(
    tmp_path: Path,
    runtime: Literal["openclaw", "codex"],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = StructuredWorkerRunner()
    config = DirectOrchestratorConfig(
        database_path=tmp_path / f"{runtime}.db",
        worker_runtime=runtime,
        executable=runtime,
        max_dispatch_workers=10,
        require_live_completion_validation=False,
        workers=WORKERS,
    )
    orchestrator = build_work_queue_orchestrator(config, runner)
    repo = repo_config(
        workspace,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.OWNER_APPROVAL,
    )
    workflow = orchestrator.enqueue(
        repo,
        _decision(),
        f"{runtime}-direct-e2e",
        workspace=WorkspaceRef(kind="worktree", path=workspace, branch="fixture"),
    )

    for _ in range(8):
        orchestrator.reconcile(repo)
        cards = orchestrator.adapter.list_cards(workflow.board_id)
        if cards and all(card.status == QueueStatus.DONE for card in cards):
            break

    cards = orchestrator.adapter.list_cards(workflow.board_id)
    assert cards
    assert all(card.status == QueueStatus.DONE for card in cards)
    assert runner.commands
    assert all("Attempt ID / idempotency key:" in prompt for prompt in runner.prompts)
    assert all(f"Exact working directory: {workspace.resolve()}" in prompt for prompt in runner.prompts)
    assert all(
        "Do not call Workboard tools or lifecycle helper commands" in prompt for prompt in runner.prompts
    )
    assert all("returning the JSON object requested below" in prompt for prompt in runner.prompts)
    if runtime == "codex":
        assert all("workspace-write" in command for command in runner.commands)
        routed_models = [command[command.index("--model") + 1] for command in runner.commands]
        assert all(not model.startswith("codex/") for model in routed_models)
        assert "gpt-5.3-codex-spark" in routed_models
    else:
        assert all(command[1:3] == ["agent", "--agent"] for command in runner.commands)


def test_direct_process_routes_documented_models_by_stage(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = StructuredWorkerRunner()
    config = DirectOrchestratorConfig(
        database_path=tmp_path / "documented-models.db",
        worker_runtime="codex",
        executable="codex",
        max_dispatch_workers=10,
        require_live_completion_validation=False,
        workers=WORKERS,
        worker_models=WorkerModelAssignments(),
    )
    orchestrator = build_work_queue_orchestrator(config, runner)
    repo = repo_config(
        workspace,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.OWNER_APPROVAL,
    )
    workflow = orchestrator.enqueue(
        repo,
        _decision(),
        "documented-models-direct-e2e",
        workspace=WorkspaceRef(kind="worktree", path=workspace, branch="fixture"),
    )

    for _ in range(8):
        orchestrator.reconcile(repo)
        cards = orchestrator.adapter.list_cards(workflow.board_id)
        if cards and all(card.status == QueueStatus.DONE for card in cards):
            break

    observed: dict[str, str] = {}
    for command, prompt in zip(runner.commands, runner.prompts, strict=True):
        stage = next(line.split(": ", 1)[1] for line in prompt.splitlines() if line.startswith("Stage: "))
        observed[stage] = command[command.index("--model") + 1]

    assert observed == {
        "implementation": "gpt-5.3-codex-spark",
        "review": "gpt-5.6-terra",
        "test": "gpt-5.6-luna",
        "final_review": "gpt-5.6-sol",
    }


def test_merge_worker_prompt_allows_only_explicit_merge_stage_action(tmp_path: Path) -> None:
    card = QueueCard(
        id="merge-1",
        title="Merge the reviewed change",
        status=QueueStatus.READY,
        labels=("make_it_so", "stage:merge"),
        notes="Run the merge gate and merge the PR when allowed.",
    )

    prompt = _worker_prompt(card, attempt_id="attempt-1", workspace=tmp_path)

    assert "explicitly assigned merge-stage card" in prompt
    assert "Do not release, deploy, expose secrets, force-push, or delete branches." in prompt
    assert "Do not merge, release, deploy, expose secrets" not in prompt


def test_runtime_canary_prompt_forbids_repository_inspection(tmp_path: Path) -> None:
    card = QueueCard(
        id="canary-1",
        title="Runtime canary",
        status=QueueStatus.READY,
        labels=("runtime-canary",),
        notes="Return MAKE_IT_SO_CANARY_PROOF:spark immediately.",
    )

    prompt = _worker_prompt(card, attempt_id="attempt-1", workspace=tmp_path)

    assert "Do not inspect files, run commands, or mutate the workspace" in prompt
    assert "Inspect current repository state before mutating it" not in prompt


def test_codex_worker_captures_provider_usage_for_workboard_audit(tmp_path: Path) -> None:
    runner = StructuredWorkerRunner()
    executor = CommandWorkerExecutor("codex", "codex", runner)
    card = QueueCard(
        id="implementation-1",
        title="Implement the bounded change",
        status=QueueStatus.READY,
        labels=("stage:implementation",),
        notes="Implement and test the requested change.",
    )

    result = executor.execute(
        card,
        attempt_id="attempt-spark-1",
        workspace=tmp_path,
        model="codex/gpt-5.3-codex-spark",
        timeout_seconds=60,
    )

    assert result.telemetry is not None
    assert result.telemetry.runtime == "codex"
    assert result.telemetry.requested_model == "gpt-5.3-codex-spark"
    assert result.telemetry.attempt_id == "attempt-spark-1"
    assert result.telemetry.usage.input_tokens == 120
    assert result.telemetry.usage.cached_input_tokens == 40
    assert result.telemetry.usage.reasoning_tokens == 3
    assert result.telemetry.usage.output_tokens == 12


def test_codex_worker_uses_a_closed_proof_schema() -> None:
    schema = _worker_output_schema()
    proof = schema["properties"]["proof"]["items"]

    assert proof["additionalProperties"] is False
    assert proof["required"] == ["note", "status", "test_evidence", "url"]
    assert set(proof["properties"]) == {"note", "status", "url", "test_evidence"}
    assert proof["properties"]["test_evidence"]["anyOf"][1]["additionalProperties"] is False


def test_codex_usage_ignores_non_usage_events_and_records_reported_model() -> None:
    usage = _codex_usage(
        "\n".join(
            (
                "not-json",
                "[]",
                '{"type":"message"}',
                '{"type":"turn.completed","usage":"unavailable","model":"gpt-5.3-codex-spark"}',
            )
        ),
        prompt_bytes=12,
        response_bytes=7,
    )

    assert usage.reported_model == "gpt-5.3-codex-spark"
    assert usage.source == "unreported"
    assert usage.input_tokens is None


def test_direct_claims_are_atomic_under_overlapping_workers(tmp_path: Path) -> None:
    adapter = DirectOrchestrator(tmp_path / "direct.db")
    adapter.ensure_board("board", "Board", "Concurrent claim fixture", tmp_path)
    card = adapter.create_card(
        "board",
        QueueCardSpec(key="card", title="Card", notes="Claim exactly once"),
    )
    adapter.dispatch("board")

    def claim(index: int) -> str:
        try:
            return adapter.claim_card(
                card.id,
                owner_id=f"worker-{index}",
                token=f"token-{index}",
            ).metadata["claim"]["ownerId"]
        except PermissionError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (1, 2)))

    assert results.count("rejected") == 1
    assert len([value for value in results if value.startswith("worker-")]) == 1


def test_expired_leases_retry_then_block_and_cancellation_rejects_late_results(
    tmp_path: Path,
) -> None:
    adapter = DirectOrchestrator(tmp_path / "direct.db", lease_seconds=30)
    adapter.ensure_board("board", "Board", "Recovery fixture", tmp_path)
    card = adapter.create_card(
        "board",
        QueueCardSpec(
            key="card",
            title="Card",
            notes="Recover bounded failures",
            max_retries=1,
        ),
    )
    adapter.dispatch("board")
    first = adapter.claim_card(card.id, owner_id="worker-1", token="token-1")
    first_expiry = datetime.fromisoformat(first.metadata["claim"]["expiresAt"])
    assert adapter.recover_expired_claims("board", now=first_expiry + timedelta(seconds=1)) == (card.id,)
    assert adapter.list_cards("board")[0].status == QueueStatus.READY

    adapter.claim_card(card.id, owner_id="worker-2", token="token-2")
    cancelled = adapter.cancel_claimed_card(
        card.id,
        requested_by="captain",
        reason="Owner cancelled the superseded run.",
    )
    assert cancelled.status == QueueStatus.BLOCKED
    assert cancelled.metadata["workerProtocol"]["state"] == "cancelled"
    assert cancelled.metadata["workerProtocol"]["detail"].startswith("CANCELLED:")
    assert classify_blocker(cancelled.metadata["workerProtocol"]["detail"]) == BlockerKind.CANCELLATION
    with pytest.raises(PermissionError, match="no active claim"):
        adapter.complete_claimed_card(
            card.id,
            owner_id="worker-2",
            token="token-2",
            summary="late completion",
            proof=({"status": "passed"},),
        )

    adapter.reclaim_card(card.id, status=QueueStatus.READY, reason="retry cancellation fixture")
    third = adapter.claim_card(card.id, owner_id="worker-3", token="token-3")
    third_expiry = datetime.fromisoformat(third.metadata["claim"]["expiresAt"])
    assert third_expiry.tzinfo is not None
    adapter.recover_expired_claims("board", now=third_expiry + timedelta(seconds=1))
    exhausted = adapter.list_cards("board")[0]
    assert exhausted.status == QueueStatus.BLOCKED
    assert exhausted.metadata["failures"] == 2
    assert "token" not in exhausted.metadata["lastClaim"]


def test_expired_claim_cannot_complete_before_recovery_and_technical_block_counts_failure(
    tmp_path: Path,
) -> None:
    adapter = DirectOrchestrator(tmp_path / "direct.db")
    adapter.ensure_board("board", "Board", "Late completion fixture", tmp_path)
    card = adapter.create_card("board", QueueCardSpec(key="card", title="Card", notes="Reject stale proof"))
    adapter.dispatch("board")
    claimed = adapter.claim_card(card.id, owner_id="worker", token="token")
    metadata = dict(claimed.metadata)
    claim = dict(metadata["claim"])
    claim["expiresAt"] = (datetime.now().astimezone() - timedelta(seconds=1)).isoformat()
    metadata["claim"] = claim
    adapter._update(card.id, metadata=metadata)  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(PermissionError, match="lease expired"):
        adapter.complete_claimed_card(
            card.id,
            owner_id="worker",
            token="token",
            summary="late result",
            proof=({"status": "passed"},),
        )

    adapter.recover_expired_claims("board")
    adapter.claim_card(card.id, owner_id="worker-2", token="token-2")
    blocked = adapter.block_claimed_card(
        card.id,
        owner_id="worker-2",
        token="token-2",
        reason="TECHNICAL: command failed",
    )
    assert blocked.metadata["failures"] == 2


def test_openclaw_retry_uses_fresh_attempt_session_key(tmp_path: Path) -> None:
    runner = StructuredWorkerRunner()
    executor = CommandWorkerExecutor("openclaw", "openclaw", runner)
    adapter = DirectOrchestrator(tmp_path / "direct.db")
    adapter.ensure_board("board", "Board", "Fresh session fixture", tmp_path)
    card = adapter.create_card(
        "board",
        QueueCardSpec(
            key="card",
            title="Card",
            notes="Use fresh sessions",
            agent_id="coder",
        ),
    )

    executor.execute(
        card,
        attempt_id="attempt-one",
        workspace=tmp_path,
        model="codex/gpt-5.3-codex-spark",
        timeout_seconds=60,
    )
    executor.execute(
        card,
        attempt_id="attempt-two",
        workspace=tmp_path,
        model="codex/gpt-5.3-codex-spark",
        timeout_seconds=60,
    )

    session_keys = [command[command.index("--session-key") + 1] for command in runner.commands]
    assert session_keys[0] != session_keys[1]
    assert session_keys[0].endswith(":attempt-one")
    assert session_keys[1].endswith(":attempt-two")


def test_codex_worker_adds_linked_worktree_git_metadata_to_sandbox(tmp_path: Path) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    common_git = tmp_path / "repo" / ".git"
    worktree_admin = common_git / "worktrees" / "attempt"
    worktree_admin.mkdir(parents=True)
    (worktree_admin / "commondir").write_text("../..\n", encoding="utf-8")
    (workspace / ".git").write_text(
        f"gitdir: {worktree_admin.resolve()}\n",
        encoding="utf-8",
    )

    writable = _codex_additional_writable_dirs(workspace)

    assert writable == (worktree_admin.resolve(), common_git.resolve())

    runner = StructuredWorkerRunner()
    executor = CommandWorkerExecutor("codex", "codex", runner)
    executor.execute(
        QueueCard(
            id="implementation-linked-worktree",
            title="Implement the bounded change",
            status=QueueStatus.READY,
            labels=("stage:implementation",),
        ),
        attempt_id="attempt-linked-worktree",
        workspace=workspace,
        model="codex/gpt-5.3-codex-spark",
        timeout_seconds=60,
    )
    command = runner.commands[0]
    add_dirs = [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--add-dir"
    ]
    assert add_dirs == [str(worktree_admin.resolve()), str(common_git.resolve())]
    assert command[-1] == "-"
    assert all(command.index("--add-dir") < command.index("-") for _ in add_dirs)


def test_worker_execution_result_rejects_unproven_completion() -> None:
    with pytest.raises(ValueError, match="structured proof"):
        WorkerExecutionResult(status="completed", summary="No evidence")

    blocked = WorkerExecutionResult(
        status="blocked",
        summary="Cannot continue",
        reason="TECHNICAL: fixture failure",
    )
    assert blocked.status == "blocked"


def test_managed_direct_runtime_requires_an_explicit_executable(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="require executable"):
        DirectOrchestratorConfig(
            database_path=tmp_path / "direct.db",
            worker_runtime="codex",
            workers=WORKERS,
        )
