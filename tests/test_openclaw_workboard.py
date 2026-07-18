from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from captains_chair.command import CommandResult
from captains_chair.direct_workers import WorkerExecutionError, WorkerExecutionResult
from captains_chair.models import OpenClawWorkboardConfig, WorkerAssignments
from captains_chair.openclaw_workboard import (
    OpenClawWorkboardAdapter,
    OpenClawWorkboardError,
    _managed_completion_proof,
    decode_openclaw_json,
)
from captains_chair.orchestration import QueueCard, QueueCardSpec, QueueStatus, WorkspaceRef


def config() -> OpenClawWorkboardConfig:
    return OpenClawWorkboardConfig(
        executable="openclaw",
        workers=WorkerAssignments(
            captain="captain",
            coder="coder",
            reviewer="reviewer",
            tester="tester",
            ux_reviewer="ux",
            final_reviewer="final",
            merger="merge",
            verifier="verify",
        ),
    )


def test_noisy_openclaw_output_decodes_first_json_value() -> None:
    value = decode_openclaw_json('migration warning\n{"cards":[]}\ntrailing note')
    assert value == {"cards": []}


def test_managed_completion_proof_preserves_policy_marker_from_summary() -> None:
    proof = _managed_completion_proof(
        ({"status": "passed", "note": "python -m pytest -q"},),
        "Final review passed. AUTO_MERGE_ALLOWED:749a3a45de43fc2d6eeaf1cb2d2a91b549fd04b3",
    )

    assert proof[0]["note"].endswith(
        "AUTO_MERGE_ALLOWED:749a3a45de43fc2d6eeaf1cb2d2a91b549fd04b3"
    )


@pytest.mark.parametrize(
    "output",
    (
        '{"type":"session.ended","status":"completed"}',
        '{"event":"session_terminated"}',
        '{"status":"completed"}',
        '{"status":"failed"}',
        '{"state":"aborted"}',
        "session ended: worker exited without proof",
        "session crashed: worker exited without proof",
        "gateway closed (1006 abnormal closure)",
    ),
)
def test_recovery_recognizes_terminal_session_output(output: str) -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        if "sessions" in command:
            return CommandResult(0, output, "")
        return CommandResult(
            0,
            json.dumps({"card": {"id": "card-1", "title": "Implementation", "status": "review"}}),
            "",
        )

    card = QueueCard(
        id="card-1",
        title="Implementation",
        status=QueueStatus.RUNNING,
        labels=("stage:implementation",),
        metadata={"attempts": [{"sessionKey": "agent:coder:captains_chair:1"}]},
    )

    recovered = OpenClawWorkboardAdapter(config(), runner).recover_ended_workers("board", [card])

    assert recovered == ("card-1",)


def test_recovery_treats_missing_session_as_ended() -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        if "sessions" in command:
            return CommandResult(1, "", "session not found")
        return CommandResult(
            0,
            json.dumps({"card": {"id": "card-1", "title": "Implementation", "status": "review"}}),
            "",
        )

    card = QueueCard(
        id="card-1",
        title="Implementation",
        status=QueueStatus.RUNNING,
        labels=("stage:implementation",),
        metadata={"attempts": [{"sessionKey": "agent:coder:captains_chair:1"}]},
    )

    recovered = OpenClawWorkboardAdapter(config(), runner).recover_ended_workers("board", [card])

    assert recovered == ("card-1",)


def test_create_card_uses_gateway_rpc_and_preserves_worker_metadata(tmp_path: Path) -> None:
    commands: list[Sequence[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(command)
        return CommandResult(
            0,
            json.dumps(
                {
                    "card": {
                        "id": "card-1",
                        "title": "Implement issue",
                        "status": "todo",
                        "priority": "high",
                        "labels": ["captains_chair"],
                        "agentId": "coder",
                        "workspace": {
                            "kind": "worktree",
                            "path": str(tmp_path),
                            "branch": "captains_chair/work/39",
                            "pushBranch": "feature/current-pr",
                        },
                    }
                }
            ),
            "",
        )

    adapter = OpenClawWorkboardAdapter(config(), runner)
    card = adapter.create_card(
        "printhub",
        QueueCardSpec(
            key="captains_chair:issue:39:implementation",
            title="Implement issue",
            notes="Use the isolated worktree.",
            status=QueueStatus.TODO,
            priority="high",
            labels=("captains_chair",),
            agent_id="coder",
            parents=("parent-card",),
            source_url="https://github.com/example/repo/issues/39",
            workspace=WorkspaceRef(
                kind="worktree",
                path=tmp_path,
                branch="captains_chair/work/39",
                push_branch="feature/current-pr",
            ),
            metadata={"courseKey": "course-1", "workPackageKey": "package-1"},
        ),
    )

    assert card.id == "card-1"
    command = list(commands[0])
    assert command[2:4] == ["call", "workboard.cards.create"]
    params = json.loads(command[command.index("--params") + 1])
    assert params["idempotencyKey"] == "captains_chair:issue:39:implementation"
    assert params["parents"] == ["parent-card"]
    assert params["agentId"] == "coder"
    assert params["workspace"]["kind"] == "worktree"
    assert params["workspace"]["pushBranch"] == "feature/current-pr"
    assert params["metadata"] == {"courseKey": "course-1", "workPackageKey": "package-1"}
    assert card.workspace == WorkspaceRef(
        kind="worktree",
        path=tmp_path,
        branch="captains_chair/work/39",
        push_branch="feature/current-pr",
    )


def test_create_card_bounds_labels_for_openclaw_limit(tmp_path: Path) -> None:
    commands: list[Sequence[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(command)
        return CommandResult(
            0,
            json.dumps(
                {
                    "card": {
                        "id": "card-1",
                        "title": "Implement issue",
                        "status": "todo",
                        "labels": ["captains_chair"],
                    }
                }
            ),
            "",
        )

    OpenClawWorkboardAdapter(config(), runner).create_card(
        "board",
        QueueCardSpec(
            key="key",
            title="Implement issue",
            notes="Use the isolated worktree.",
            labels=("repo:mln330/captains-chair-e2e-smoke-20260716-0958", " stage:implementation "),
            workspace=WorkspaceRef(kind="dir", path=tmp_path),
        ),
    )

    params = json.loads(list(commands[0])[list(commands[0]).index("--params") + 1])
    assert all(len(label) <= 40 for label in params["labels"])
    assert params["labels"] == ["repo:mln330/captains-chair-e2e-smoke-...", "stage:implementation"]


def test_card_normalizes_metadata_workspace_push_branch() -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return CommandResult(
            0,
            json.dumps(
                {
                    "cards": [
                        {
                            "id": "card-1",
                            "title": "Repair PR",
                            "status": "todo",
                            "metadata": {
                                "workspace": {
                                    "kind": "worktree",
                                    "path": "/tmp/repair",
                                    "branch": "captains_chair/repair/pr-41",
                                    "pushBranch": "feature/current-pr",
                                }
                            },
                        }
                    ]
                }
            ),
            "",
        )

    card = OpenClawWorkboardAdapter(config(), runner).list_cards("printhub")[0]

    assert card.workspace == WorkspaceRef(
        kind="worktree",
        path=Path("/tmp/repair"),
        branch="captains_chair/repair/pr-41",
        push_branch="feature/current-pr",
    )


def test_create_card_bounds_long_planner_title_to_workboard_limit() -> None:
    commands: list[Sequence[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(command)
        return CommandResult(
            0,
            json.dumps({"card": {"id": "card-1", "title": "bounded", "status": "todo"}}),
            "",
        )

    adapter = OpenClawWorkboardAdapter(config(), runner)
    adapter.create_card(
        "printhub",
        QueueCardSpec(key="long-title", title="x" * 240, notes="Planner detail stays here."),
    )

    params = json.loads(list(commands[0])[list(commands[0]).index("--params") + 1])
    assert len(params["title"]) == 180
    assert params["title"].endswith("...")


def test_gateway_failure_is_not_reported_as_success() -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return CommandResult(1, "", "permission denied")

    adapter = OpenClawWorkboardAdapter(config(), runner)
    with pytest.raises(OpenClawWorkboardError, match="permission denied"):
        adapter.list_cards("printhub")


def test_gateway_failure_includes_structured_stdout_when_stderr_has_warning() -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return CommandResult(1, '{"error":{"message":"invalid dependency"}}', "migration warning")

    adapter = OpenClawWorkboardAdapter(config(), runner)
    with pytest.raises(OpenClawWorkboardError, match="invalid dependency"):
        adapter.list_cards("printhub")


def test_claimed_completion_passes_owner_token_and_structured_proof() -> None:
    commands: list[Sequence[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(command)
        return CommandResult(
            0,
            json.dumps(
                {
                    "card": {
                        "id": "card-1",
                        "title": "Verify",
                        "status": "done",
                        "labels": [],
                    }
                }
            ),
            "",
        )

    adapter = OpenClawWorkboardAdapter(config(), runner)
    card = adapter.complete_claimed_card(
        "card-1",
        owner_id="verify",
        token="claim-token",
        summary="Verified",
        proof=({"status": "passed", "label": "hostname", "note": "TARSOpenClaw"},),
    )

    params = json.loads(list(commands[0])[list(commands[0]).index("--params") + 1])
    assert card.status == QueueStatus.DONE
    assert params["ownerId"] == "verify"
    assert params["token"] == "claim-token"
    assert params["proof"]["status"] == "passed"


@pytest.mark.parametrize(
    ("operation", "expected_method", "expected_fields"),
    (
        (
            "heartbeat",
            "workboard.cards.heartbeat",
            {"ownerId": "tester", "token": "claim-token", "note": "still working"},
        ),
        (
            "block",
            "workboard.cards.block",
            {"ownerId": "tester", "token": "claim-token", "reason": "TECHNICAL: test failed"},
        ),
    ),
)
def test_claimed_worker_lifecycle_uses_typed_runtime_boundary(
    operation: str,
    expected_method: str,
    expected_fields: dict[str, str],
) -> None:
    commands: list[Sequence[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(command)
        return CommandResult(
            0,
            json.dumps({"card": {"id": "card-1", "title": "Worker", "status": "running"}}),
            "",
        )

    adapter = OpenClawWorkboardAdapter(config(), runner)
    if operation == "heartbeat":
        adapter.heartbeat_card(
            "card-1", owner_id="tester", token="claim-token", note="still working"
        )
    else:
        adapter.block_claimed_card(
            "card-1", owner_id="tester", token="claim-token", reason="TECHNICAL: test failed"
        )

    command = list(commands[0])
    assert command[command.index("call") + 1] == expected_method
    params = json.loads(command[command.index("--params") + 1])
    assert params["id"] == "card-1"
    for field, value in expected_fields.items():
        assert params[field] == value


def test_completion_rejects_multiple_proof_records_before_rpc() -> None:
    commands: list[Sequence[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(command)
        return CommandResult(0, "{}", "")

    adapter = OpenClawWorkboardAdapter(config(), runner)
    with pytest.raises(OpenClawWorkboardError, match="exactly one"):
        adapter.complete_card(
            "card-1",
            summary="Too many proof records",
            proof=({"status": "passed"}, {"status": "passed"}),
        )
    assert commands == []


def test_malformed_success_output_fails_closed() -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return CommandResult(0, "not json", "")

    adapter = OpenClawWorkboardAdapter(config(), runner)
    with pytest.raises(OpenClawWorkboardError, match="valid JSON"):
        adapter.diagnostics()


def test_diagnostics_for_board_filters_global_workboard_results() -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return CommandResult(
            0,
            json.dumps(
                {
                    "diagnostics": [
                        {
                            "card": {
                                "id": "old-card",
                                "metadata": {"automation": {"boardId": "old-board"}},
                            },
                            "diagnostics": [{"kind": "stale"}],
                        },
                        {
                            "card": {
                                "id": "current-card",
                                "metadata": {"automation": {"boardId": "current-board"}},
                            },
                            "diagnostics": [{"kind": "current"}],
                        },
                    ]
                }
            ),
            "",
        )

    result = OpenClawWorkboardAdapter(config(), runner).diagnostics_for_board("current-board")
    assert [entry["card"]["id"] for entry in result["diagnostics"]] == ["current-card"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("id", "", "non-empty id"),
        ("labels", "not-an-array", "labels must be an array"),
        ("metadata", [], "metadata must be an object"),
        ("workspace", "not-an-object", "workspace must be an object"),
    ),
)
def test_malformed_card_payload_fails_closed(
    field: str,
    value: object,
    message: str,
) -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        card: dict[str, object] = {
            "id": "card-1",
            "title": "Malformed card",
            "status": "todo",
            field: value,
        }
        return CommandResult(0, json.dumps({"cards": [card]}), "")

    adapter = OpenClawWorkboardAdapter(config(), runner)
    with pytest.raises(OpenClawWorkboardError, match=message):
        adapter.list_cards("printhub")


@pytest.mark.parametrize(
    ("cards", "message"),
    (
        ([{"id": "card-1", "title": "Valid", "status": "todo"}, "not-a-card"], "non-object card"),
        (
            [
                {"id": "card-1", "title": "First", "status": "todo"},
                {"id": "card-1", "title": "Duplicate", "status": "todo"},
            ],
            "duplicate card ids",
        ),
    ),
)
def test_card_list_integrity_fail_closed(cards: list[object], message: str) -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return CommandResult(0, json.dumps({"cards": cards}), "")

    adapter = OpenClawWorkboardAdapter(config(), runner)
    with pytest.raises(OpenClawWorkboardError, match=message):
        adapter.list_cards("printhub")


def test_recover_ended_worker_reclaims_running_card_for_fresh_retry() -> None:
    commands: list[Sequence[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(command)
        if "sessions" in command:
            return CommandResult(0, "05:00 model.completed\n", "05:00 session.ended success\n")
        if "workboard.cards.reclaim" in command:
            return CommandResult(
                0,
                json.dumps({"card": {"id": "card-1", "title": "Test", "status": "review"}}),
                "",
            )
        raise AssertionError(f"unexpected command: {command}")

    adapter = OpenClawWorkboardAdapter(config(), runner)
    card = QueueCard(
        id="card-1",
        title="Test",
        status=QueueStatus.RUNNING,
        metadata={
            "attempts": [{"sessionKey": "agent:tester:subagent:card-1", "status": "running"}]
        },
    )

    assert adapter.recover_ended_workers("printhub", [card]) == ("card-1",)
    assert any("workboard.cards.reclaim" in command for command in commands)


def test_recovery_reconstructs_managed_session_from_claim_owner() -> None:
    commands: list[Sequence[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(command)
        if "sessions" in command:
            return CommandResult(0, "05:00 model.completed\n05:00 session.ended success\n", "")
        if "workboard.cards.reclaim" in command:
            return CommandResult(
                0,
                json.dumps({"card": {"id": "card-1", "title": "Test", "status": "review"}}),
                "",
            )
        raise AssertionError(f"unexpected command: {command}")

    card = QueueCard(
        id="card-1",
        title="Test",
        status=QueueStatus.RUNNING,
        agent_id="github-coder",
        metadata={"claim": {"ownerId": "captains-chair-managed:managed:card-1:attempt-1"}},
    )

    assert OpenClawWorkboardAdapter(config(), runner).recover_ended_workers("board", [card]) == ("card-1",)
    session_command = next(command for command in commands if "sessions" in command)
    assert "agent:github-coder:captains-chair:worker:card-1:managed:card-1:attempt-1" in session_command


def test_recover_expired_claim_without_session_lookup() -> None:
    commands: list[Sequence[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(command)
        if "workboard.cards.reclaim" in command:
            return CommandResult(
                0,
                json.dumps({"card": {"id": "card-1", "title": "Test", "status": "review"}}),
                "",
            )
        raise AssertionError(f"unexpected command: {command}")

    adapter = OpenClawWorkboardAdapter(config(), runner)
    card = QueueCard(
        id="card-1",
        title="Test",
        status=QueueStatus.RUNNING,
        metadata={
            "claim": {"ownerId": "tester", "expiresAt": 0},
            "attempts": [{"sessionKey": "agent:tester:subagent:card-1", "status": "running"}],
        },
    )

    assert adapter.recover_ended_workers("printhub", [card]) == ("card-1",)
    assert not any("sessions" in command for command in commands)
    assert any("workboard.cards.reclaim" in command for command in commands)


def test_recover_stopped_attempt_without_session_lookup() -> None:
    commands: list[Sequence[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(command)
        if "workboard.cards.reclaim" in command:
            return CommandResult(
                0,
                json.dumps({"card": {"id": "card-1", "title": "Test", "status": "review"}}),
                "",
            )
        raise AssertionError(f"unexpected command: {command}")

    card = QueueCard(
        id="card-1",
        title="Test",
        status=QueueStatus.RUNNING,
        metadata={
            "attempts": [
                {
                    "sessionKey": "agent:tester:subagent:workboard-board-card-1",
                    "status": "stopped",
                    "error": "gateway closed (1006 abnormal closure)",
                }
            ]
        },
    )

    assert OpenClawWorkboardAdapter(config(), runner).recover_ended_workers("board", [card]) == (
        "card-1",
    )
    assert not any("sessions" in command for command in commands)


def test_worker_model_health_fails_closed_when_agent_inventory_fails() -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return CommandResult(1, "", "agent inventory unavailable")

    adapter = OpenClawWorkboardAdapter(config(), runner)

    with pytest.raises(OpenClawWorkboardError, match="model health check failed"):
        adapter.validate_worker_models()


def test_worker_model_health_accepts_codex_route_reported_by_openai_provider() -> None:
    observed = {
        "captain": "openai/gpt-5.6-terra",
        "coder": "openai/gpt-5.3-codex-spark",
        "reviewer": "openai/gpt-5.6-terra",
        "tester": "openai/gpt-5.6-luna",
        "ux": "openai/gpt-5.6-terra",
        "final": "openai/gpt-5.6-sol",
        "merge": "openai/gpt-5.6-terra",
        "verify": "openai/gpt-5.6-terra",
    }

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        if list(command)[1:4] == ["agents", "list", "--json"]:
            return CommandResult(
                0,
                json.dumps(
                    [{"id": agent_id, "model": model} for agent_id, model in observed.items()]
                ),
                "",
            )
        if list(command)[3] == "tools":
            return CommandResult(
                0,
                json.dumps(
                    {
                        "allow": [
                            "workboard_block",
                            "workboard_comment",
                            "workboard_complete",
                            "workboard_heartbeat",
                            "workboard_proof",
                            "workboard_read",
                            "workboard_worker_log",
                        ]
                    }
                ),
                "",
            )
        return CommandResult(
            0,
            json.dumps({"maxConcurrent": 1}),
            "",
        )

    result = OpenClawWorkboardAdapter(config(), runner).validate_worker_models()

    assert result["status"] == "ok"
    assert result["checked_agents"] == 8
    assert result["mismatches"] == []
    assert result["missing_worker_tools"] == []
    assert result["max_concurrent_subagents"]["valid"] is True


def test_worker_health_fails_closed_on_missing_tools_and_unsafe_concurrency() -> None:
    expected = config().worker_models
    workers = config().workers
    observed = {
        workers.captain: expected.captain,
        workers.coder: expected.coder,
        workers.reviewer: expected.reviewer,
        workers.tester: expected.tester,
        workers.ux_reviewer: expected.ux_reviewer,
        workers.final_reviewer: expected.final_reviewer,
        workers.merger: expected.merger,
        workers.verifier: expected.verifier,
    }

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        if list(command)[1:4] == ["agents", "list", "--json"]:
            return CommandResult(
                0,
                json.dumps(
                    [{"id": agent_id, "model": model} for agent_id, model in observed.items()]
                ),
                "",
            )
        if list(command)[3] == "tools":
            return CommandResult(0, json.dumps({"allow": ["group:fs"]}), "")
        return CommandResult(0, json.dumps({"maxConcurrent": 8}), "")

    result = OpenClawWorkboardAdapter(config(), runner).validate_worker_models()

    assert result["status"] == "degraded"
    assert "workboard_complete" in result["missing_worker_tools"]
    assert result["max_concurrent_subagents"] == {
        "expected_max": 1,
        "observed": 8,
        "valid": False,
    }


def test_worker_health_accepts_unrestricted_tool_policy() -> None:
    expected = config().worker_models
    workers = config().workers
    observed = {
        workers.captain: expected.captain,
        workers.coder: expected.coder,
        workers.reviewer: expected.reviewer,
        workers.tester: expected.tester,
        workers.ux_reviewer: expected.ux_reviewer,
        workers.final_reviewer: expected.final_reviewer,
        workers.merger: expected.merger,
        workers.verifier: expected.verifier,
    }

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        if list(command)[1:4] == ["agents", "list", "--json"]:
            return CommandResult(
                0,
                json.dumps(
                    [{"id": agent_id, "model": model} for agent_id, model in observed.items()]
                ),
                "",
            )
        if list(command)[3] == "tools":
            return CommandResult(0, json.dumps({}), "")
        return CommandResult(0, json.dumps({"maxConcurrent": 1}), "")

    result = OpenClawWorkboardAdapter(config(), runner).validate_worker_models()

    assert result["status"] == "ok"
    assert result["missing_worker_tools"] == []


def test_worker_health_rejects_bool_concurrency() -> None:
    expected = config().worker_models
    workers = config().workers
    observed = {
        workers.captain: expected.captain,
        workers.coder: expected.coder,
        workers.reviewer: expected.reviewer,
        workers.tester: expected.tester,
        workers.ux_reviewer: expected.ux_reviewer,
        workers.final_reviewer: expected.final_reviewer,
        workers.merger: expected.merger,
        workers.verifier: expected.verifier,
    }

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        if list(command)[1:4] == ["agents", "list", "--json"]:
            return CommandResult(
                0,
                json.dumps(
                    [{"id": agent_id, "model": model} for agent_id, model in observed.items()]
                ),
                "",
            )
        if list(command)[3] == "tools":
            return CommandResult(0, json.dumps({}), "")
        return CommandResult(0, json.dumps({"maxConcurrent": True}), "")

    result = OpenClawWorkboardAdapter(config(), runner).validate_worker_models()

    assert result["status"] == "degraded"
    assert result["max_concurrent_subagents"]["valid"] is False


def test_worker_health_fails_closed_when_runtime_config_read_fails() -> None:
    expected = config().worker_models
    workers = config().workers
    observed = {
        workers.captain: expected.captain,
        workers.coder: expected.coder,
        workers.reviewer: expected.reviewer,
        workers.tester: expected.tester,
        workers.ux_reviewer: expected.ux_reviewer,
        workers.final_reviewer: expected.final_reviewer,
        workers.merger: expected.merger,
        workers.verifier: expected.verifier,
    }

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        if list(command)[1:4] == ["agents", "list", "--json"]:
            return CommandResult(
                0,
                json.dumps(
                    [{"id": agent_id, "model": model} for agent_id, model in observed.items()]
                ),
                "",
            )
        return CommandResult(1, "", "config unavailable")

    with pytest.raises(OpenClawWorkboardError, match="runtime safety check failed"):
        OpenClawWorkboardAdapter(config(), runner).validate_worker_models()


def test_worker_health_fails_closed_when_runtime_config_is_not_object() -> None:
    expected = config().worker_models
    workers = config().workers
    observed = {
        workers.captain: expected.captain,
        workers.coder: expected.coder,
        workers.reviewer: expected.reviewer,
        workers.tester: expected.tester,
        workers.ux_reviewer: expected.ux_reviewer,
        workers.final_reviewer: expected.final_reviewer,
        workers.merger: expected.merger,
        workers.verifier: expected.verifier,
    }

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        if list(command)[1:4] == ["agents", "list", "--json"]:
            return CommandResult(
                0,
                json.dumps(
                    [{"id": agent_id, "model": model} for agent_id, model in observed.items()]
                ),
                "",
            )
        return CommandResult(0, "[]", "")

    with pytest.raises(OpenClawWorkboardError, match="did not return an object"):
        OpenClawWorkboardAdapter(config(), runner).validate_worker_models()


def test_managed_dispatch_completes_one_ready_card(monkeypatch: pytest.MonkeyPatch) -> None:
    cards = {
        "card-1": _managed_card("card-1", status="ready", agent_id="tester"),
        "card-2": _managed_card("card-2", status="ready", agent_id="reviewer"),
    }
    calls: list[str] = []
    _patch_worker_executor(
        monkeypatch,
        WorkerExecutionResult(
            status="completed",
            summary="completed",
            proof=({"status": "passed", "note": "ok"},),
        ),
    )

    result = OpenClawWorkboardAdapter(config(), _managed_runner(cards, calls)).dispatch("board")

    assert result["strategy"] == "managed_single"
    assert result["started"] == ["card-1"]
    assert result["completed"] == ["card-1"]
    assert cards["card-1"]["status"] == "done"
    assert cards["card-2"]["status"] == "ready"
    assert "workboard.cards.dispatch" not in calls


def test_managed_dispatch_collapses_multiple_worker_proof_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cards = {"card-1": _managed_card("card-1", status="ready", agent_id="tester")}
    _patch_worker_executor(
        monkeypatch,
        WorkerExecutionResult(
            status="completed",
            summary="completed",
            proof=(
                {"status": "passed", "note": "primary"},
                {"status": "passed", "note": "secondary"},
            ),
        ),
    )

    result = OpenClawWorkboardAdapter(config(), _managed_runner(cards, [])).dispatch("board")

    assert result["completed"] == ["card-1"]
    proof = cards["card-1"]["metadata"]["proof"]
    assert len(proof) == 1
    assert proof[0]["note"] == "primary"
    assert [item["note"] for item in proof[0]["evidence"]] == ["primary", "secondary"]


def test_managed_dispatch_canonicalizes_canary_style_proof_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cards = {"card-1": _managed_card("card-1", status="ready", agent_id="tester")}
    _patch_worker_executor(
        monkeypatch,
        WorkerExecutionResult(
            status="completed",
            summary="completed",
            proof=(
                {"command": "pwd", "output": "ok"},
                {"proof_note": "CAPTAINS_CHAIR_CANARY_PROOF:smoke"},
            ),
        ),
    )

    result = OpenClawWorkboardAdapter(config(), _managed_runner(cards, [])).dispatch("board")

    assert result["completed"] == ["card-1"]
    proof = cards["card-1"]["metadata"]["proof"][0]
    assert proof["status"] == "passed"
    assert proof["note"] == "CAPTAINS_CHAIR_CANARY_PROOF:smoke"


def test_managed_dispatch_promotes_dependency_ready_card(monkeypatch: pytest.MonkeyPatch) -> None:
    cards = {
        "parent": _managed_card("parent", status="done", agent_id="captain"),
        "child": _managed_card("child", status="todo", agent_id="tester", parents=("parent",)),
    }
    _patch_worker_executor(
        monkeypatch,
        WorkerExecutionResult(
            status="completed",
            summary="completed child",
            proof=({"status": "passed", "note": "child"},),
        ),
    )

    result = OpenClawWorkboardAdapter(config(), _managed_runner(cards, [])).dispatch("board")

    assert result["promoted"] == ["child"]
    assert result["completed"] == ["child"]
    assert cards["child"]["status"] == "done"


@pytest.mark.parametrize(
    ("allowed", "expected_status"),
    ((True, "done"), (False, "blocked")),
)
def test_merge_card_uses_deterministic_gate_without_model_worker(
    allowed: bool,
    expected_status: str,
) -> None:
    head = "6fc76b212ac2011b01eb91b6ad008f9d9c2c6267"
    cards: dict[str, dict[str, Any]] = {
        "implementation": {
            **_managed_card("implementation", status="done", agent_id="coder"),
            "notes": "Repository: mln330/canary",
            "labels": ["workflow:current"],
            "metadata": {
                "proof": [
                    {
                        "status": "passed",
                        "url": "https://github.com/mln330/canary/pull/1",
                    }
                ]
            },
        },
        "final": {
            **_managed_card("final", status="done", agent_id="final"),
            "notes": "Repository: mln330/canary",
            "labels": ["workflow:current", "stage:final_review"],
            "metadata": {
                "proof": [
                    {
                        "status": "passed",
                        "note": (
                            f"AUTO_MERGE_ALLOWED:{head}"
                            if allowed
                            else f"READY_FOR_OWNER:{head}"
                        ),
                    }
                ]
            },
        },
        "merge": {
            **_managed_card("merge", status="todo", agent_id="", parents=("final",)),
            "notes": "Repository: mln330/canary",
            "labels": ["workflow:current", "stage:merge"],
        },
        "older-implementation": {
            **_managed_card("older-implementation", status="done", agent_id="coder"),
            "notes": "Repository: mln330/canary",
            "labels": ["workflow:older"],
            "metadata": {
                "proof": [
                    {
                        "status": "passed",
                        "url": "https://github.com/mln330/canary/pull/2",
                    }
                ]
            },
        },
    }
    calls: list[str] = []
    gateway_runner = _managed_runner(cards, calls)
    merge_commands: list[Sequence[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        if command and command[0] == "captains_chair":
            merge_commands.append(command)
            payload = {
                "allowed": allowed,
                "merged": allowed,
                "reason": "all merge gates passed" if allowed else "missing AUTO_MERGE_ALLOWED proof",
                "current_head": head,
            }
            return CommandResult(0 if allowed else 2, json.dumps(payload), "")
        return gateway_runner(
            command,
            cwd=cwd,
            input_text=input_text,
            timeout=timeout,
        )

    result = OpenClawWorkboardAdapter(config(), runner).dispatch("board")

    assert cards["merge"]["status"] == expected_status
    assert len(merge_commands) == 1
    assert "merge-gate" in merge_commands[0]
    assert "--merge" in merge_commands[0]
    assert merge_commands[0][merge_commands[0].index("--pr") + 1] == "1"
    assert result["deterministic_merge"]["status"] == ("completed" if allowed else "blocked")
    if allowed:
        proof = cards["merge"]["metadata"]["proof"][0]
        assert proof["model"] == "deterministic/no-model"
        assert "Model: deterministic/no-model; Provider: captains-chair" in proof["note"]
    else:
        assert "missing AUTO_MERGE_ALLOWED" in cards["merge"]["metadata"]["workerProtocol"][
            "detail"
        ]


def test_deterministic_merge_command_failure_blocks_claimed_card() -> None:
    cards: dict[str, dict[str, Any]] = {
        "final": {
            **_managed_card("final", status="done", agent_id="final"),
            "notes": "Repository: mln330/canary",
            "labels": ["workflow:current", "stage:final_review"],
            "metadata": {
                "proof": [
                    {
                        "status": "passed",
                        "note": "AUTO_MERGE_ALLOWED:6fc76b2",
                        "url": "https://github.com/mln330/canary/pull/1",
                    }
                ]
            },
        },
        "merge": {
            **_managed_card("merge", status="todo", agent_id="", parents=("final",)),
            "notes": "Repository: mln330/canary",
            "labels": ["workflow:current", "stage:merge"],
        },
    }
    gateway_runner = _managed_runner(cards, [])

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        if command and command[0] == "captains_chair":
            raise OSError("merge executable is unavailable")
        return gateway_runner(command, cwd=cwd, input_text=input_text, timeout=timeout)

    result = OpenClawWorkboardAdapter(config(), runner).dispatch("board")

    assert result["deterministic_merge"]["status"] == "blocked"
    assert cards["merge"]["status"] == "blocked"
    assert "merge executable is unavailable" in cards["merge"]["metadata"]["workerProtocol"][
        "detail"
    ]


def test_managed_dispatch_idles_when_dependencies_are_not_done() -> None:
    cards = {
        "parent": _managed_card("parent", status="blocked", agent_id="captain"),
        "child": _managed_card("child", status="todo", agent_id="tester", parents=("parent",)),
    }

    result = OpenClawWorkboardAdapter(config(), _managed_runner(cards, [])).dispatch("board")

    assert result == {
        "status": "idle",
        "strategy": "managed_single",
        "promoted": [],
        "started": [],
        "completed": [],
        "blocked": [],
        "count": 0,
    }


def test_managed_dispatch_blocks_when_agent_model_is_missing() -> None:
    cards = {"card-1": _managed_card("card-1", status="ready", agent_id="unknown-agent")}

    result = OpenClawWorkboardAdapter(config(), _managed_runner(cards, [])).dispatch("board")

    assert result["blocked"] == ["card-1"]
    assert cards["card-1"]["status"] == "blocked"
    assert "no OpenClaw worker model" in cards["card-1"]["metadata"]["workerProtocol"]["detail"]


def test_managed_dispatch_blocks_when_worker_returns_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cards = {"card-1": _managed_card("card-1", status="ready", agent_id="tester")}
    _patch_worker_executor(
        monkeypatch,
        WorkerExecutionResult(
            status="blocked",
            summary="blocked",
            reason="TECHNICAL: expected fake worker block",
        ),
    )

    result = OpenClawWorkboardAdapter(config(), _managed_runner(cards, [])).dispatch("board")

    assert result["blocked"] == ["card-1"]
    assert cards["card-1"]["metadata"]["workerProtocol"]["detail"] == "TECHNICAL: expected fake worker block"


def test_managed_dispatch_blocks_when_worker_execution_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cards = {"card-1": _managed_card("card-1", status="ready", agent_id="tester")}
    _patch_worker_executor(monkeypatch, WorkerExecutionError("fake worker crash"))

    result = OpenClawWorkboardAdapter(config(), _managed_runner(cards, [])).dispatch("board")

    assert result["blocked"] == ["card-1"]
    assert "fake worker crash" in cards["card-1"]["metadata"]["workerProtocol"]["detail"]


def _managed_card(
    card_id: str,
    *,
    status: str,
    agent_id: str,
    parents: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "id": card_id,
        "title": card_id,
        "status": status,
        "priority": "normal",
        "labels": [],
        "agentId": agent_id,
        "metadata": {
            "automation": {"maxRuntimeSeconds": 60},
            "links": [{"type": "parent", "targetCardId": parent} for parent in parents],
        },
    }


def _managed_runner(
    cards: dict[str, dict[str, Any]],
    calls: list[str],
) -> Any:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        method = list(command)[3]
        calls.append(method)
        params = json.loads(list(command)[list(command).index("--params") + 1])
        if method == "workboard.cards.list":
            return CommandResult(0, json.dumps({"cards": list(cards.values())}), "")
        card = cards[params["id"]]
        if method == "workboard.cards.reclaim":
            card["status"] = params["status"]
        elif method == "workboard.cards.claim":
            card["status"] = "running"
            card["metadata"]["claim"] = {
                "ownerId": params["ownerId"],
                "token": params["token"],
                "attemptId": params.get("attemptId", "attempt"),
            }
        elif method == "workboard.cards.complete":
            card["status"] = "done"
            card["metadata"]["proof"] = [params["proof"]]
        elif method == "workboard.cards.block":
            card["status"] = "blocked"
            card["metadata"]["workerProtocol"] = {
                "state": "blocked",
                "detail": params["reason"],
            }
        elif method == "workboard.cards.heartbeat":
            pass
        else:
            raise AssertionError(f"unexpected method: {method}")
        return CommandResult(0, json.dumps({"card": card}), "")

    return runner


def _patch_worker_executor(
    monkeypatch: pytest.MonkeyPatch,
    outcome: WorkerExecutionResult | WorkerExecutionError,
) -> None:
    class FakeExecutor:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def execute(self, *args: object, **kwargs: object) -> WorkerExecutionResult:
            if isinstance(outcome, WorkerExecutionError):
                raise outcome
            return outcome

    monkeypatch.setattr("captains_chair.openclaw_workboard.CommandWorkerExecutor", FakeExecutor)
