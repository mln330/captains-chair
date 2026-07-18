from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import make_it_so.completion_gate as completion_gate
import make_it_so.scheduler as scheduler
import make_it_so.worktrees as worktrees
from make_it_so.canary import build_canary_spec, evaluate_canary_card, summarize_canary_card
from make_it_so.command import CommandResult
from make_it_so.direct_orchestrator import DirectOrchestrator
from make_it_so.models import (
    CompletionPolicy,
    NotificationConfig,
    OperationMode,
    RepoConfig,
)
from make_it_so.orchestration import QueueCard, QueueCardSpec, QueueStatus, WorkStage
from make_it_so.scheduler import OpenClawScheduler, ScheduleSpec
from make_it_so.worktrees import Worktree, WorktreeManager
from tests.helpers import repo_config


def _repo(tmp_path: Path) -> RepoConfig:
    return RepoConfig(
        full_name="example/project",
        local_path=tmp_path / "repo",
        planning_doc="ISSUES_EXECUTION_PLAN.md",
        notification=NotificationConfig(kind="stdout"),
    )


def _runner(result: CommandResult) -> Any:
    def run(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return result

    return run


def test_worktree_manager_fail_closed_edges(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.local_path.mkdir()
    manager = WorktreeManager(tmp_path / "worktrees", _runner(CommandResult(0, "", "")))

    with pytest.raises(ValueError, match="unsafe branch"):
        manager.create(repo, "item", lane="../")
    with pytest.raises(ValueError, match="unsafe remote branch"):
        manager.checkout_existing(repo, "item", "bad branch")
    with pytest.raises(ValueError, match="identifier"):
        worktrees._safe_component("...")  # pyright: ignore[reportPrivateUsage]
    assert not worktrees._safe_branch("/main")  # pyright: ignore[reportPrivateUsage]
    assert not worktrees._safe_branch("main/")  # pyright: ignore[reportPrivateUsage]
    assert not worktrees._safe_branch("main//child")  # pyright: ignore[reportPrivateUsage]

    managed = manager.root / "example-project" / "item"
    managed.mkdir(parents=True)
    with pytest.raises(FileExistsError):
        manager.checkout_existing(repo, "item", "feature/current")
    with pytest.raises(ValueError, match="outside"):
        manager.remove_path(repo, tmp_path / "outside")
    assert manager.remove_path(repo, manager.root / "absent") is False
    with pytest.raises(ValueError, match="outside"):
        manager.discard_path(repo, tmp_path / "outside")
    assert manager.discard_path(repo, manager.root / "absent") is False
    with pytest.raises(ValueError, match="outside"):
        manager.remove_disposable(
            repo,
            Worktree(
                path=tmp_path / "outside",
                branch="make_it_so/repair/ux-item",
                base="origin/main",
                push_branch="make_it_so/repair/ux-item",
            ),
        )


def test_scheduler_rejects_malformed_openclaw_payloads_and_handles_cron_shape(tmp_path: Path) -> None:
    value = ScheduleSpec(name="job", argv=("python", "-m", "make_it_so"), cwd=tmp_path)

    def malformed(command: Sequence[str], **kwargs: Any) -> CommandResult:
        del kwargs
        if list(command)[2] == "list":
            return CommandResult(0, "[]", "")
        return CommandResult(0, json.dumps([]), "")

    with pytest.raises(RuntimeError, match="unreadable schedule list"):
        OpenClawScheduler("openclaw", malformed).install(value)

    def malformed_list(command: Sequence[str], **kwargs: Any) -> CommandResult:
        del command, kwargs
        return CommandResult(0, json.dumps({"jobs": "wrong"}), "")

    with pytest.raises(RuntimeError, match="unreadable schedule list"):
        OpenClawScheduler("openclaw", malformed_list).install(value)

    cron_job = {
        "payload": {"kind": "command", "argv": list(value.argv), "cwd": str(value.cwd)},
        "schedule": {"kind": "cron", "expr": value.cron},
    }
    assert scheduler._schedule_matches(cron_job, value)  # pyright: ignore[reportPrivateUsage]
    assert not scheduler._schedule_matches({**cron_job, "schedule": {"kind": "unknown"}}, value)  # pyright: ignore[reportPrivateUsage]

    def missing_id(command: Sequence[str], **kwargs: Any) -> CommandResult:
        del kwargs
        if list(command)[2] == "list":
            return CommandResult(0, json.dumps({"jobs": [{"name": value.name}]}), "")
        raise AssertionError(command)

    with pytest.raises(RuntimeError, match="has no ID"):
        OpenClawScheduler("openclaw", missing_id).install(value)


def test_canary_completion_and_direct_orchestrator_edge_contracts(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    with pytest.raises(ValueError, match="canary_id"):
        build_canary_spec(repo, canary_id="bad id", worker_id="worker", max_runtime_seconds=10, max_retries=1)

    card = QueueCard(id="canary", title="Canary", status=QueueStatus.TODO, metadata={})
    assert evaluate_canary_card(card, canary_id="one").status == "pending"
    assert evaluate_canary_card(card.model_copy(update={"status": QueueStatus.BLOCKED}), canary_id="one").status == "failed"
    done = card.model_copy(update={"status": QueueStatus.DONE, "metadata": {"proof": "wrong"}})
    assert evaluate_canary_card(done, canary_id="one").status == "failed"
    done = card.model_copy(update={"status": QueueStatus.DONE, "metadata": {"proof": [{"status": "failed"}]}})
    assert evaluate_canary_card(done, canary_id="one").status == "failed"
    summary = summarize_canary_card(done.model_copy(update={"metadata": {"proof": ["ignore", {"status": "passed", "label": "ok"}]}}))
    assert summary["proof"] == [{"status": "passed", "note": "ok", "url": None}]

    adapter = DirectOrchestrator(tmp_path / "direct.db")
    adapter.ensure_board("course", "Course", "description", tmp_path)
    created = adapter.create_card("course", QueueCardSpec(key="item", title="Item", notes="Item work"))
    with pytest.raises(KeyError, match="unknown direct card"):
        adapter._card("missing")  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(KeyError, match="unknown direct card"):
        adapter._update("missing", status=QueueStatus.DONE)  # pyright: ignore[reportPrivateUsage]

    adapter.dispatch("course")
    adapter.claim_card(created.id, owner_id="worker", token="token")
    claimed = adapter.heartbeat_card(created.id, owner_id="worker", token="token", note="started")
    with pytest.raises(PermissionError, match="claim credentials"):
        adapter.complete_claimed_card(
            claimed.id, owner_id="worker", token="wrong", summary="bad", proof=()
        )
    adapter._update(created.id, metadata={"claim": "malformed"})  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(ValueError, match="invalid claim"):
        adapter.complete_claimed_card(
            created.id, owner_id="worker", token="token", summary="bad", proof=()
        )


def test_completion_gate_private_helpers_ignore_stale_or_malformed_proof() -> None:
    repo = repo_config(
        Path("."),
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    card = QueueCard(
        id="final",
        title="Final",
        status=QueueStatus.DONE,
        labels=("stage:final_review",),
        metadata={
            "proof": [
                "ignore",
                {"status": "failed", "note": "AUTO_MERGE_ALLOWED:abcdef1"},
                {"status": "passed", "note": "missing marker"},
            ]
        },
    )
    final_review_evidence = completion_gate._final_review_evidence  # pyright: ignore[reportPrivateUsage]
    pull_request_numbers = completion_gate._pull_request_numbers  # pyright: ignore[reportPrivateUsage]
    stage = completion_gate._stage  # pyright: ignore[reportPrivateUsage]
    marker, verdict, head = final_review_evidence(repo, card)
    assert marker is None and verdict is None and head is None
    assert stage(card) == WorkStage.FINAL_REVIEW
    assert stage(card.model_copy(update={"labels": ()})) is None
    assert pull_request_numbers(repo, [card.model_copy(update={"metadata": {"proof": "wrong"}})]) == set()
