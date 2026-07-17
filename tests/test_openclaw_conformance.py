from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

import captains_chair.cli as cli
from captains_chair.canary import (
    build_canary_spec,
    canary_board_id,
    canary_proof_marker,
    evaluate_canary_card,
)
from captains_chair.command import CommandResult
from captains_chair.completion_gate import GitHubCompletionValidator
from captains_chair.conformance import (
    RuntimeConformanceError,
    run_mixed_blocker_isolation,
    run_runtime_conformance,
    run_technical_recovery_isolation,
    run_user_blocker_isolation,
    stage_card,
)
from captains_chair.models import (
    ActionKind,
    CompletionPolicy,
    OpenClawWorkboardConfig,
    OperationMode,
    PlanDecision,
    PullRequestGate,
    WorkerAssignments,
    WorkerModelAssignments,
)
from captains_chair.openclaw_workboard import OpenClawWorkboardAdapter
from captains_chair.orchestration import WorkflowOrchestrator
from tests.helpers import app_config, repo_config


def test_stage_card_reports_missing_runtime_evidence() -> None:
    class EmptyAdapter:
        def list_cards(self, board_id: str) -> list[object]:
            del board_id
            return []

    with pytest.raises(RuntimeConformanceError, match="no implementation card"):
        stage_card(EmptyAdapter(), "board", "implementation")  # type: ignore[arg-type]


class FakeGateway:
    """A JSON-RPC Workboard double used to exercise the real OpenClaw adapter."""

    def __init__(self) -> None:
        self.cards: dict[str, dict[str, Any]] = {}
        self.idempotency: dict[str, str] = {}
        self.ended_sessions: set[str] = set()
        self.session_inspection_error: str | None = None
        self.coder_model = WorkerModelAssignments().coder
        self.next_id = 1

    def runner(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        values = list(command)
        if "sessions" in values:
            session_key = values[values.index("--session-key") + 1]
            if self.session_inspection_error:
                return CommandResult(1, "", self.session_inspection_error)
            output = "session.ended" if session_key in self.ended_sessions else "session.progress"
            return CommandResult(0, output, "")
        if values[1:4] == ["agents", "list", "--json"]:
            return CommandResult(
                0,
                json.dumps(
                    [
                        {"id": "captains-chair", "model": WorkerModelAssignments().captain},
                        {"id": "github-coder", "model": self.coder_model},
                        {"id": "github-reviewer", "model": WorkerModelAssignments().reviewer},
                        {"id": "github-tester", "model": WorkerModelAssignments().tester},
                        {"id": "github-ux", "model": WorkerModelAssignments().ux_reviewer},
                        {"id": "github-final", "model": WorkerModelAssignments().final_reviewer},
                        {"id": "github-merge", "model": WorkerModelAssignments().merger},
                        {"id": "github-verify", "model": WorkerModelAssignments().verifier},
                    ]
                ),
                "",
            )
        if values[1:5] == ["config", "get", "tools", "--json"]:
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
        if values[1:5] == [
            "config",
            "get",
            "agents.defaults.subagents",
            "--json",
        ]:
            return CommandResult(0, json.dumps({"maxConcurrent": 1}), "")
        method = values[3]
        params = json.loads(values[values.index("--params") + 1])
        handler = getattr(self, f"_{method.replace('.', '_')}", None)
        if handler is None:
            return CommandResult(0, json.dumps({}), "")
        return CommandResult(0, json.dumps(handler(params)), "")

    def _workboard_boards_upsert(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"board": {"id": params["id"]}}

    def _workboard_cards_create(self, params: dict[str, Any]) -> dict[str, Any]:
        key = str(params["idempotencyKey"])
        if key in self.idempotency:
            return {"card": self.cards[self.idempotency[key]]}
        card_id = f"card-{self.next_id}"
        self.next_id += 1
        parents = [str(item) for item in params.get("parents", [])]
        raw = {
            "id": card_id,
            "title": params["title"],
            "notes": params.get("notes"),
            "status": params.get("status", "todo"),
            "priority": params.get("priority", "normal"),
            "labels": params.get("labels", []),
            "agentId": params.get("agentId"),
            "sourceUrl": params.get("sourceUrl"),
            "metadata": {
                **params.get("metadata", {}),
                "automation": {"maxRetries": params.get("maxRetries", 2)},
                "links": [
                    {"type": "parent", "targetCardId": parent}
                    for parent in parents
                ],
            },
        }
        self.cards[card_id] = raw
        self.idempotency[key] = card_id
        return {"card": raw}

    def _workboard_cards_list(self, params: dict[str, Any]) -> dict[str, Any]:
        del params
        return {"cards": list(self.cards.values())}

    def _workboard_cards_complete(self, params: dict[str, Any]) -> dict[str, Any]:
        card = self.cards[params["id"]]
        card["status"] = "done"
        card["metadata"] = {
            **card.get("metadata", {}),
            "proof": [params["proof"]] if params.get("proof") else [],
            "automation": {
                **card.get("metadata", {}).get("automation", {}),
                "summary": params.get("summary", ""),
            },
        }
        return {"card": card}

    def _workboard_cards_block(self, params: dict[str, Any]) -> dict[str, Any]:
        card = self.cards[params["id"]]
        card["status"] = "blocked"
        card["metadata"] = {
            **card.get("metadata", {}),
            "workerProtocol": {"state": "blocked", "detail": params["reason"]},
        }
        return {"card": card}

    def _workboard_cards_reclaim(self, params: dict[str, Any]) -> dict[str, Any]:
        card = self.cards[params["id"]]
        card["status"] = params["status"]
        return {"card": card}

    def _workboard_cards_reassign(self, params: dict[str, Any]) -> dict[str, Any]:
        card = self.cards[params["id"]]
        card["status"] = params["status"]
        card["agentId"] = params["agentId"]
        return {"card": card}

    def _workboard_cards_unblock(self, params: dict[str, Any]) -> dict[str, Any]:
        card = self.cards[params["id"]]
        card["status"] = "todo"
        return {"card": card}

    def _workboard_cards_comment(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"card": self.cards[params["id"]]}

    def _workboard_cards_dispatch(self, params: dict[str, Any]) -> dict[str, Any]:
        del params
        promoted: list[str] = []
        started: list[str] = []
        for card in self.cards.values():
            if card["status"] == "ready" and "runtime-canary" in card.get("labels", []):
                card["status"] = "running"
                started.append(card["id"])
                continue
            if card["status"] != "todo":
                continue
            parents = [
                str(link["targetCardId"])
                for link in card.get("metadata", {}).get("links", [])
                if link.get("type") == "parent"
            ]
            if all(self.cards[parent]["status"] == "done" for parent in parents):
                card["status"] = "ready"
                promoted.append(card["id"])
        return {
            "promoted": promoted,
            "started": started,
            "count": len(promoted) + len(started),
        }

    def _workboard_cards_diagnostics_refresh(self, params: dict[str, Any]) -> dict[str, Any]:
        del params
        return {"cards": len(self.cards)}


def _config(*, require_live: bool = False) -> OpenClawWorkboardConfig:
    return OpenClawWorkboardConfig(
        max_retries=1,
        dispatch_strategy="workboard",
        require_live_completion_validation=require_live,
        workers=WorkerAssignments(
            captain="captains-chair",
            coder="github-coder",
            reviewer="github-reviewer",
            tester="github-tester",
            ux_reviewer="github-ux",
            final_reviewer="github-final",
            merger="github-merge",
            verifier="github-verify",
        ),
    )


def _decision() -> PlanDecision:
    return PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement a gateway-backed slice",
        reason="The disposable conformance fixture selects it.",
        target_issue=39,
        acceptance_criteria=("Scope is correct", "Checks pass"),
    )


class FakeCompletionGitHub:
    """Live-PR gate double used by the OpenClaw end-to-end workflow fixture."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str | None]] = []

    def gate(
        self,
        repo: object,
        number: int,
        review_head_sha: str | None,
    ) -> PullRequestGate:
        del repo
        self.calls.append((number, review_head_sha))
        return PullRequestGate(
            number=number,
            head_sha=review_head_sha or "bcdef12",
            mergeable=True,
            merge_state="CLEAN",
            draft=False,
            checks_green=True,
            required_checks=(),
            unresolved_threads=0,
            review_head_sha=review_head_sha,
        )

    def pull_request(self, repo: object, number: int) -> dict[str, object]:
        del repo, number
        return {"headRefOid": "bcdef12"}

    def pull_request_files(self, repo: object, number: int) -> tuple[str, ...]:
        del repo, number
        return ()


def _stage(adapter: OpenClawWorkboardAdapter, board: str, name: str) -> str:
    return next(card.id for card in adapter.list_cards(board) if f"stage:{name}" in card.labels)


def test_real_openclaw_adapter_drives_full_autonomous_workflow(tmp_path: Path) -> None:
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    ).model_copy(update={"orchestration_board": "captains-chair-example-project"})
    gateway = FakeGateway()
    github = FakeCompletionGitHub()
    runtime_config = _config(require_live=True)
    adapter = OpenClawWorkboardAdapter(runtime_config, gateway.runner)
    orchestrator = WorkflowOrchestrator(
        adapter,
        runtime_config,
        completion_validator=GitHubCompletionValidator(github),
    )
    report = run_runtime_conformance(
        orchestrator,
        adapter,
        repo,
        _decision(),
        action_id="gateway-e2e",
        block_card=lambda card_id, reason: adapter.block_claimed_card(
            card_id,
            owner_id="github-reviewer",
            token="review-token",
            reason=reason,
        ),
        complete_card=lambda card_id, summary, proof: adapter.complete_claimed_card(
            card_id,
            owner_id="test-worker",
            token="test-token",
            summary=summary,
            proof=proof,
        ),
    )
    assert report.workflow_id == "gateway-e2e"
    assert report.owner_blocked_card_id.startswith("card-")
    assert report.technical_retry_card_id.startswith("card-")
    assert github.calls
    assert set(github.calls) == {(7, "bcdef12")}


def test_openclaw_user_blocker_does_not_stop_unrelated_work(tmp_path: Path) -> None:
    repo = repo_config(tmp_path).model_copy(update={"orchestration_board": "captains-chair-example-project"})
    gateway = FakeGateway()
    adapter = OpenClawWorkboardAdapter(_config(), gateway.runner)
    orchestrator = WorkflowOrchestrator(adapter, _config())

    run_user_blocker_isolation(
        orchestrator,
        adapter,
        repo,
        _decision(),
        block_card=lambda card_id, reason: adapter.block_claimed_card(
            card_id,
            owner_id="github-coder",
            token="coder-token",
            reason=reason,
        ),
    )


def test_openclaw_technical_recovery_does_not_stop_unrelated_work(tmp_path: Path) -> None:
    repo = repo_config(tmp_path).model_copy(update={"orchestration_board": "captains-chair-example-project"})
    gateway = FakeGateway()
    adapter = OpenClawWorkboardAdapter(_config(), gateway.runner)
    orchestrator = WorkflowOrchestrator(adapter, _config())

    run_technical_recovery_isolation(
        orchestrator,
        adapter,
        repo,
        _decision(),
        block_card=lambda card_id, reason: adapter.block_claimed_card(
            card_id,
            owner_id="github-coder",
            token="coder-token",
            reason=reason,
        ),
    )


def test_openclaw_mixed_blockers_keep_unrelated_work_moving(tmp_path: Path) -> None:
    repo = repo_config(tmp_path).model_copy(update={"orchestration_board": "captains-chair-example-project"})
    gateway = FakeGateway()
    adapter = OpenClawWorkboardAdapter(_config(), gateway.runner)
    orchestrator = WorkflowOrchestrator(adapter, _config())

    owner_card, technical_card, unrelated_card = run_mixed_blocker_isolation(
        orchestrator,
        adapter,
        repo,
        _decision(),
        block_card=lambda card_id, reason: adapter.block_claimed_card(
            card_id,
            owner_id="github-coder",
            token="coder-token",
            reason=reason,
        ),
    )

    board_id = repo.orchestration_board
    assert board_id is not None
    cards = {card.id: card for card in adapter.list_cards(board_id)}
    assert cards[owner_card].status.value == "blocked"
    assert cards[technical_card].status.value == "ready"
    assert cards[unrelated_card].status.value == "ready"


def test_new_adapter_instance_recovers_ended_worker_session(tmp_path: Path) -> None:
    repo = repo_config(tmp_path).model_copy(update={"orchestration_board": "captains-chair-example-project"})
    gateway = FakeGateway()
    first = OpenClawWorkboardAdapter(_config(), gateway.runner)
    orchestrator = WorkflowOrchestrator(first, _config())
    workflow = orchestrator.enqueue(repo, _decision(), "restart-e2e")
    implementation_id = _stage(first, workflow.board_id, "implementation")
    raw = gateway.cards[implementation_id]
    raw["status"] = "running"
    raw["metadata"]["attempts"] = [{"sessionKey": "agent:github-coder:subagent:implementation"}]
    gateway.ended_sessions.add("agent:github-coder:subagent:implementation")

    restarted = OpenClawWorkboardAdapter(_config(), gateway.runner)
    result = WorkflowOrchestrator(restarted, _config()).reconcile(repo)

    assert result.protocol_retries
    assert gateway.cards[implementation_id]["status"] == "review"


def test_openclaw_recovery_warning_does_not_stop_unrelated_dispatch(tmp_path: Path) -> None:
    repo = repo_config(tmp_path).model_copy(update={"orchestration_board": "captains-chair-example-project"})
    gateway = FakeGateway()
    adapter = OpenClawWorkboardAdapter(_config(), gateway.runner)
    orchestrator = WorkflowOrchestrator(adapter, _config())

    first = orchestrator.enqueue(repo, _decision(), "recovery-warning")
    implementation_id = _stage(adapter, first.board_id, "implementation")
    gateway.cards[implementation_id]["status"] = "running"
    gateway.cards[implementation_id]["metadata"]["attempts"] = [
        {"sessionKey": "agent:github-coder:subagent:recovery-warning"}
    ]
    gateway.session_inspection_error = "gateway timeout"
    orchestrator.enqueue(repo, _decision(), "unrelated-ready-work")

    result = orchestrator.reconcile(repo)

    assert result.recovery_warnings == (
        "Could not inspect OpenClaw session agent:github-coder:subagent:recovery-warning: gateway timeout",
    )
    assert result.dispatch["count"] == 1
    cards = {card.id: card for card in adapter.list_cards(first.board_id)}
    assert cards[implementation_id].status.value == "running"
    assert any(
        card.status.value == "ready"
        and card.id != implementation_id
        and "stage:implementation" in card.labels
        for card in cards.values()
    )


def test_openclaw_model_mismatch_suppresses_new_worker_dispatch(tmp_path: Path) -> None:
    repo = repo_config(tmp_path).model_copy(update={"orchestration_board": "captains-chair-example-project"})
    gateway = FakeGateway()
    gateway.coder_model = "ollama/glm-5.2:cloud"
    adapter = OpenClawWorkboardAdapter(_config(), gateway.runner)
    orchestrator = WorkflowOrchestrator(adapter, _config())

    orchestrator.enqueue(repo, _decision(), "model-health-e2e")
    result = orchestrator.reconcile(repo)

    assert result.dispatch["status"] == "dispatch_suppressed"
    assert result.dispatch["model_health"]["status"] == "degraded"
    assert result.dispatch["model_health"]["mismatches"][0]["agent_id"] == "github-coder"
    assert not result.dispatch["promoted"]


def test_real_openclaw_adapter_runs_runtime_canary_to_durable_pass(tmp_path: Path) -> None:
    repo = repo_config(tmp_path).model_copy(
        update={"orchestration_board": "captains-chair-example-project"}
    )
    gateway = FakeGateway()
    adapter = OpenClawWorkboardAdapter(_config(), gateway.runner)
    board_id = canary_board_id(repo)
    canary_id = "openclaw-rpc"

    adapter.ensure_board(
        board_id,
        f"{repo.full_name} runtime canary",
        "Disposable CAPTAINS_CHAIR runtime validation cards; no repository changes are allowed.",
        repo.local_path,
    )
    spec = build_canary_spec(
        repo,
        canary_id=canary_id,
        worker_id="github-tester",
        max_runtime_seconds=60,
        max_retries=1,
    )
    card = adapter.create_card(board_id, spec)
    assert evaluate_canary_card(card, canary_id=canary_id).status == "pending"

    dispatch = adapter.dispatch(board_id)
    assert dispatch["promoted"] == [card.id]
    ready = next(item for item in adapter.list_cards(board_id) if item.id == card.id)
    assert ready.status.value == "ready"

    completed = adapter.complete_card(
        card.id,
        summary="OpenClaw RPC canary completed",
        proof=({"status": "passed", "note": canary_proof_marker(canary_id)},),
    )
    assert evaluate_canary_card(completed, canary_id=canary_id).status == "passed"
    assert gateway.cards[card.id]["status"] == "done"


def test_openclaw_cli_canary_uses_real_adapter_without_model_calls(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    repo = repo_config(tmp_path).model_copy(
        update={
            "orchestrator": "workers",
            "orchestration_board": "captains-chair-example-project",
            "operation_mode": OperationMode.SUPERVISED,
        }
    )
    runtime_config = _config()
    config = app_config(tmp_path, repo_config(tmp_path)).model_copy(
        update={"orchestrators": {"workers": runtime_config}, "repos": (repo,)}
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")

    gateway = FakeGateway()
    adapter = OpenClawWorkboardAdapter(runtime_config, gateway.runner)
    orchestrator = SimpleNamespace(adapter=adapter)

    def fake_orchestrator(config: Any, repo_name: str) -> Any:
        del config, repo_name
        return orchestrator

    monkeypatch.setattr(cli, "_orchestrator", fake_orchestrator)

    def allow_usage_guard(
        config: Any,
        repo_name: str,
        state: Any,
        *,
        orchestrator_executable: str | None,
    ) -> tuple[dict[str, Any], None]:
        del config, repo_name, state, orchestrator_executable
        return {"allowed": True, "reason": "test"}, None

    monkeypatch.setattr(cli, "_usage_guard", allow_usage_guard)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "orchestrate",
                "canary",
                "--repo",
                repo.full_name,
                "--canary-id",
                "cli-smoke",
                "--run",
            ]
        )
        == 0
    )
    dispatched = json.loads(capsys.readouterr().out)
    card_id = dispatched["card"]["id"]
    assert dispatched["status"] == "dispatched"
    assert dispatched["dispatch"]["started"] == [card_id]

    adapter.complete_card(
        card_id,
        summary="CLI canary completed",
        proof=({"status": "passed", "note": canary_proof_marker("cli-smoke")},),
    )
    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "orchestrate",
                "canary",
                "--repo",
                repo.full_name,
                "--canary-id",
                "cli-smoke",
                "--check",
                "--card",
                card_id,
            ]
        )
        == 0
    )
    checked = json.loads(capsys.readouterr().out)
    assert checked["status"] == "passed"
    assert gateway.cards[card_id]["status"] == "done"
