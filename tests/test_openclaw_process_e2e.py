from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from captains_chair.canary import (
    build_canary_spec,
    canary_board_id,
    evaluate_canary_card,
)
from captains_chair.command import CommandResult, run_command
from captains_chair.conformance import run_runtime_conformance
from captains_chair.models import (
    ActionKind,
    CompletionPolicy,
    OpenClawWorkboardConfig,
    OperationMode,
    PlanDecision,
    WorkerAssignments,
)
from captains_chair.openclaw_workboard import OpenClawWorkboardAdapter
from captains_chair.orchestration import QueueStatus, WorkflowOrchestrator, WorkspaceRef
from tests.helpers import repo_config

FAKE_OPENCLAW = r'''
from __future__ import annotations

import json
import sys
from pathlib import Path


state_path = Path(__file__).with_suffix(".state.json")
if state_path.is_file():
    state = json.loads(state_path.read_text(encoding="utf-8"))
else:
    state = {"cards": {}, "next_id": 1}


def save() -> None:
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def emit(value: object) -> None:
    print("fake openclaw warning")
    print(json.dumps(value))


args = sys.argv[1:]
if args[:3] == ["agents", "list", "--json"]:
    emit(
        [
            {"id": "captain", "model": "openai/gpt-5.5"},
            {"id": "coder", "model": "openai/gpt-5.3-codex-spark"},
            {"id": "reviewer", "model": "openai/gpt-5.5"},
            {"id": "tester", "model": "openai/gpt-5.3-codex-spark"},
            {"id": "ux", "model": "openai/gpt-5.3-codex-spark"},
            {"id": "final", "model": "openai/gpt-5.5"},
            {"id": "merge", "model": "openai/gpt-5.5"},
            {"id": "verify", "model": "openai/gpt-5.5"},
        ]
    )
    raise SystemExit(0)

if args[:4] == ["config", "get", "tools", "--json"]:
    emit({"allow": ["workboard_block", "workboard_comment", "workboard_complete", "workboard_heartbeat", "workboard_proof", "workboard_read", "workboard_worker_log"]})
    raise SystemExit(0)

if args[:4] == ["config", "get", "agents.defaults.subagents", "--json"]:
    emit({"maxConcurrent": 1})
    raise SystemExit(0)

if args and args[0] == "agent":
    message = args[args.index("--message") + 1]
    marker = "CAPTAINS_CHAIR_CANARY_PROOF:"
    suffix = "process-e2e"
    if marker in message:
        suffix = message.split(marker, 1)[1].split("`", 1)[0].split()[0]
    emit(
        {
            "result": {
                "payloads": [
                    {
                        "text": json.dumps(
                            {
                                "status": "completed",
                                "summary": "fake managed OpenClaw canary completed",
                                "proof": [
                                    {
                                        "status": "passed",
                                        "label": "runtime canary",
                                        "note": marker + suffix,
                                    }
                                ],
                            }
                        )
                    }
                ]
            }
        }
    )
    raise SystemExit(0)

if args[:2] != ["gateway", "call"]:
    print("unsupported fake OpenClaw command", file=sys.stderr)
    raise SystemExit(2)

method = args[2]
params = json.loads(args[args.index("--params") + 1])
cards = state["cards"]

if method == "workboard.boards.upsert":
    emit({"board": {"id": params["id"]}})
elif method == "workboard.cards.create":
    card_id = f"card-{state['next_id']}"
    state["next_id"] += 1
    card = {
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
        },
    }
    if params.get("workspace"):
        card["workspace"] = params["workspace"]
    cards[card_id] = card
    save()
    emit({"card": card})
elif method == "workboard.cards.list":
    emit({"cards": list(cards.values())})
elif method == "workboard.cards.reclaim":
    card = cards[params["id"]]
    card["status"] = params["status"]
    save()
    emit({"card": card})
elif method == "workboard.cards.claim":
    card = cards[params["id"]]
    card["status"] = "running"
    card.setdefault("metadata", {})["claim"] = {
        "ownerId": params["ownerId"],
        "token": params["token"],
        "attemptId": params.get("attemptId", "attempt"),
    }
    save()
    emit({"card": card, "token": params["token"]})
elif method == "workboard.cards.complete":
    card = cards[params["id"]]
    card["status"] = "done"
    if params.get("proof"):
        card["metadata"]["proof"] = [params["proof"]]
    card["metadata"]["automation"]["summary"] = params.get("summary", "")
    save()
    emit({"card": card})
elif method == "workboard.cards.block":
    card = cards[params["id"]]
    card["status"] = "blocked"
    card.setdefault("metadata", {})["workerProtocol"] = {
        "state": "blocked",
        "detail": params["reason"],
    }
    save()
    emit({"card": card})
elif method == "workboard.cards.heartbeat":
    emit({"card": cards[params["id"]]})
else:
    print(f"unsupported fake Workboard method: {method}", file=sys.stderr)
    raise SystemExit(2)
'''


FAKE_OPENCLAW_WORKBOARD = r'''
from __future__ import annotations

import json
import sys
from pathlib import Path


state_path = Path(__file__).with_suffix(".workboard.json")
if state_path.is_file():
    state = json.loads(state_path.read_text(encoding="utf-8"))
else:
    state = {"cards": {}, "idempotency": {}, "next_id": 1}


def save() -> None:
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def emit(value: object) -> None:
    print("fake OpenClaw process diagnostic")
    print(json.dumps(value))


args = sys.argv[1:]
if args[:3] == ["agents", "list", "--json"]:
    emit(
        [
            {"id": "captain", "model": "codex/gpt-5.5"},
            {"id": "coder", "model": "codex/gpt-5.3-codex-spark"},
            {"id": "reviewer", "model": "codex/gpt-5.5"},
            {"id": "tester", "model": "codex/gpt-5.3-codex-spark"},
            {"id": "ux", "model": "codex/gpt-5.3-codex-spark"},
            {"id": "final", "model": "codex/gpt-5.5"},
            {"id": "merge", "model": "codex/gpt-5.5"},
            {"id": "verify", "model": "codex/gpt-5.5"},
        ]
    )
    raise SystemExit(0)

if args[:4] == ["config", "get", "tools", "--json"]:
    emit({"allow": ["workboard_block", "workboard_comment", "workboard_complete", "workboard_heartbeat", "workboard_proof", "workboard_read", "workboard_worker_log"]})
    raise SystemExit(0)

if args[:4] == ["config", "get", "agents.defaults.subagents", "--json"]:
    emit({"maxConcurrent": 1})
    raise SystemExit(0)

if args[:2] != ["gateway", "call"]:
    print("unsupported fake OpenClaw command", file=sys.stderr)
    raise SystemExit(2)

method = args[2]
params = json.loads(args[args.index("--params") + 1])
cards = state["cards"]


def card_payload(card_id: str) -> dict[str, object]:
    return {"card": cards[card_id]}


if method == "workboard.boards.upsert":
    emit({"board": {"id": params["id"]}})
elif method == "workboard.cards.create":
    key = str(params["idempotencyKey"])
    if key in state["idempotency"]:
        emit(card_payload(state["idempotency"][key]))
        raise SystemExit(0)
    card_id = f"card-{state['next_id']}"
    state["next_id"] += 1
    metadata = {
        **params.get("metadata", {}),
        "automation": {"maxRetries": params.get("maxRetries", 2)},
        "links": [
            {"type": "parent", "targetCardId": parent}
            for parent in params.get("parents", [])
        ],
    }
    card = {
        "id": card_id,
        "title": params["title"],
        "notes": params.get("notes"),
        "status": params.get("status", "todo"),
        "priority": params.get("priority", "normal"),
        "labels": params.get("labels", []),
        "agentId": params.get("agentId"),
        "sourceUrl": params.get("sourceUrl"),
        "metadata": metadata,
    }
    if params.get("workspace"):
        card["workspace"] = params["workspace"]
    cards[card_id] = card
    state["idempotency"][key] = card_id
    save()
    emit({"card": card})
elif method == "workboard.cards.list":
    emit({"cards": list(cards.values())})
elif method == "workboard.cards.claim":
    card = cards[params["id"]]
    card["status"] = "running"
    card.setdefault("metadata", {})["claim"] = {
        "ownerId": params["ownerId"],
        "token": params["token"],
        "attemptId": params.get("attemptId", "attempt"),
    }
    save()
    emit({"card": card, "token": params["token"]})
elif method == "workboard.cards.complete":
    card = cards[params["id"]]
    card["status"] = "done"
    metadata = card.setdefault("metadata", {})
    if params.get("proof"):
        metadata["proof"] = [params["proof"]]
    metadata.setdefault("automation", {})["summary"] = params.get("summary", "")
    save()
    emit({"card": card})
elif method == "workboard.cards.block":
    card = cards[params["id"]]
    card["status"] = "blocked"
    metadata = card.setdefault("metadata", {})
    metadata["workerProtocol"] = {"state": "blocked", "detail": params["reason"]}
    metadata["failureCount"] = int(metadata.get("failureCount", 0)) + 1
    save()
    emit({"card": card})
elif method == "workboard.cards.unblock":
    card = cards[params["id"]]
    card["status"] = "todo"
    save()
    emit({"card": card})
elif method == "workboard.cards.reclaim":
    card = cards[params["id"]]
    card["status"] = params["status"]
    save()
    emit({"card": card})
elif method == "workboard.cards.reassign":
    card = cards[params["id"]]
    card["status"] = params["status"]
    card["agentId"] = params["agentId"]
    if params.get("resetFailures"):
        card.setdefault("metadata", {})["failureCount"] = 0
    save()
    emit({"card": card})
elif method == "workboard.cards.heartbeat":
    emit(card_payload(params["id"]))
elif method == "workboard.cards.comment":
    emit(card_payload(params["id"]))
elif method == "workboard.cards.dispatch":
    promoted = []
    for card in cards.values():
        if card["status"] != "todo":
            continue
        parents = [
            str(link["targetCardId"])
            for link in card.get("metadata", {}).get("links", [])
            if link.get("type") == "parent"
        ]
        if all(cards[parent]["status"] == "done" for parent in parents):
            card["status"] = "ready"
            promoted.append(card["id"])
    save()
    emit({"promoted": promoted, "count": len(promoted)})
elif method == "workboard.cards.diagnostics.refresh":
    emit({"status": "ok", "cards": len(cards)})
else:
    print(f"unsupported fake Workboard method: {method}", file=sys.stderr)
    raise SystemExit(2)
'''


def test_openclaw_adapter_crosses_real_process_boundary_for_canary(tmp_path: Path) -> None:
    executable = tmp_path / "fake_openclaw.py"
    executable.write_text(FAKE_OPENCLAW, encoding="utf-8")
    config = OpenClawWorkboardConfig(
        executable=sys.executable,
        dispatch_timeout_seconds=10,
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

    def process_runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text
        return run_command([*command[:1], str(executable), *command[1:]], timeout=timeout)

    adapter = OpenClawWorkboardAdapter(config, process_runner)
    repo = repo_config(tmp_path)
    board_id = canary_board_id(repo)
    canary_id = "process-e2e"

    assert adapter.validate_worker_models()["status"] == "ok"
    adapter.ensure_board(board_id, "Process canary", "Disposable process test", tmp_path)
    card = adapter.create_card(
        board_id,
        build_canary_spec(
            repo,
            canary_id=canary_id,
            worker_id="tester",
            max_runtime_seconds=60,
            max_retries=1,
        ),
    )
    assert evaluate_canary_card(card, canary_id=canary_id).status == "pending"
    dispatch = adapter.dispatch(board_id)
    assert dispatch["started"] == [card.id]
    assert dispatch["completed"] == [card.id]
    completed = next(item for item in adapter.list_cards(board_id) if item.id == card.id)
    assert evaluate_canary_card(completed, canary_id=canary_id).status == "passed"


def test_openclaw_adapter_crosses_real_process_boundary_for_full_conformance(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake_openclaw_workboard.py"
    executable.write_text(FAKE_OPENCLAW_WORKBOARD, encoding="utf-8")
    config = OpenClawWorkboardConfig(
        executable=sys.executable,
        dispatch_timeout_seconds=10,
        dispatch_strategy="workboard",
        require_live_completion_validation=False,
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

    def process_runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text
        return run_command([*command[:1], str(executable), *command[1:]], timeout=timeout)

    adapter = OpenClawWorkboardAdapter(config, process_runner)
    orchestrator = WorkflowOrchestrator(adapter, config)
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS, completion=CompletionPolicy.AUTO_MERGE).model_copy(
        update={"orchestration_board": "captains-chair-process-e2e"}
    )
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement the process-backed conformance slice",
        reason="The disposable runtime test selected it.",
        target_issue=39,
        acceptance_criteria=("The workflow remains isolated", "All gates pass"),
    )
    workspace = WorkspaceRef(
        kind="worktree",
        path=tmp_path / "process-worktree",
        branch="captains_chair/process-work",
        push_branch="captains_chair/process-work",
    )

    report = run_runtime_conformance(
        orchestrator,
        adapter,
        repo,
        decision,
        action_id="process-conformance",
        block_card=lambda card_id, reason: adapter.block_claimed_card(
            card_id,
            owner_id="worker",
            token="process-token",
            reason=reason,
        ),
        complete_card=lambda card_id, summary, proof: adapter.complete_claimed_card(
            card_id,
            owner_id="worker",
            token="process-token",
            summary=summary,
            proof=proof,
        ),
        workspace=workspace,
    )

    assert report.workflow_id == "process-conformance"
    assert report.technical_retry_card_id.startswith("card-")
    assert report.owner_blocked_card_id.startswith("card-")
    assert report.mixed_unrelated_card_id.startswith("card-")
    cards = adapter.list_cards(repo.orchestration_board or "")
    workflow_cards = [
        card for card in cards if "workflow:process-conformance" in card.labels
    ]
    assert workflow_cards
    assert all(
        card.workspace == workspace
        for card in workflow_cards
        if "stage:orchestration" not in card.labels
        and "stage:merge" not in card.labels
        and "stage:post_merge" not in card.labels
        and card.status == QueueStatus.DONE
    )
