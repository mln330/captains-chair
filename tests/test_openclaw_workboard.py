from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from captains_chair.command import CommandResult
from captains_chair.models import OpenClawWorkboardConfig, WorkerAssignments
from captains_chair.openclaw_workboard import (
    OpenClawWorkboardAdapter,
    OpenClawWorkboardError,
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
    assert card.workspace == WorkspaceRef(
        kind="worktree",
        path=tmp_path,
        branch="captains_chair/work/39",
        push_branch="feature/current-pr",
    )


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
        "captain": "openai/gpt-5.5",
        "coder": "openai/gpt-5.3-codex",
        "reviewer": "openai/gpt-5.5",
        "tester": "openai/gpt-5.3-codex",
        "ux": "openai/gpt-5.3-codex",
        "final": "openai/gpt-5.5",
        "merge": "openai/gpt-5.5",
        "verify": "openai/gpt-5.5",
    }

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        assert list(command)[1:4] == ["agents", "list", "--json"]
        return CommandResult(
            0,
            json.dumps([{"id": agent_id, "model": model} for agent_id, model in observed.items()]),
            "",
        )

    result = OpenClawWorkboardAdapter(config(), runner).validate_worker_models()

    assert result == {"status": "ok", "checked_agents": 8, "mismatches": []}
