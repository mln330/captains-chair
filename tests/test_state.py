from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from captains_chair.models import RunState
from captains_chair.state import ALLOWED_TRANSITIONS, LeaseBusyError, StateStore

_PATHS_TO_STATE: dict[RunState, tuple[RunState, ...]] = {
    RunState.UNBASELINED: (),
    RunState.BASELINE_REVIEW: (RunState.BASELINE_REVIEW,),
    RunState.READY: (RunState.BASELINE_REVIEW, RunState.READY),
    RunState.PLANNING: (RunState.BASELINE_REVIEW, RunState.READY, RunState.PLANNING),
    RunState.EXECUTING: (
        RunState.BASELINE_REVIEW,
        RunState.READY,
        RunState.PLANNING,
        RunState.EXECUTING,
    ),
    RunState.PR_OPEN: (RunState.BASELINE_REVIEW, RunState.READY, RunState.PR_OPEN),
    RunState.REVIEWING: (
        RunState.BASELINE_REVIEW,
        RunState.READY,
        RunState.PR_OPEN,
        RunState.REVIEWING,
    ),
    RunState.REPAIRING: (
        RunState.BASELINE_REVIEW,
        RunState.READY,
        RunState.PR_OPEN,
        RunState.REVIEWING,
        RunState.REPAIRING,
    ),
    RunState.COMPLETION_READY: (
        RunState.BASELINE_REVIEW,
        RunState.READY,
        RunState.PR_OPEN,
        RunState.REVIEWING,
        RunState.COMPLETION_READY,
    ),
    RunState.MERGED: (
        RunState.BASELINE_REVIEW,
        RunState.READY,
        RunState.PR_OPEN,
        RunState.REVIEWING,
        RunState.COMPLETION_READY,
        RunState.MERGED,
    ),
    RunState.POST_MERGE_VERIFICATION: (
        RunState.BASELINE_REVIEW,
        RunState.READY,
        RunState.PR_OPEN,
        RunState.REVIEWING,
        RunState.COMPLETION_READY,
        RunState.MERGED,
        RunState.POST_MERGE_VERIFICATION,
    ),
    RunState.BLOCKED: (RunState.BASELINE_REVIEW, RunState.READY, RunState.BLOCKED),
    RunState.DEGRADED: (RunState.BASELINE_REVIEW, RunState.READY, RunState.DEGRADED),
}


def test_state_transitions_and_approval_consumption(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.transition("example/project", RunState.BASELINE_REVIEW)
    state.transition("example/project", RunState.READY)
    assert state.current_state("example/project") == RunState.READY

    state.approve("example/project", "action-1", "owner")
    assert state.is_approved("example/project", "action-1")
    state.consume_approval("example/project", "action-1")
    assert not state.is_approved("example/project", "action-1")


def test_invalid_transition_and_overlapping_lease_fail(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    with pytest.raises(ValueError, match="invalid state transition"):
        state.transition("example/project", RunState.MERGED)

    with (
        state.lease("example/project", "first"),
        pytest.raises(LeaseBusyError),
        state.lease("example/project", "second"),
    ):
        pass


def test_dead_cli_lease_is_reclaimed_after_process_exit(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    with state._connect() as conn:
        conn.execute(
            "INSERT INTO leases(repo,owner,expires_at) VALUES(?,?,?)",
            (
                "example/project",
                "cli:reconcile:99999999:dead-process",
                (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            ),
        )

    with state.lease("example/project", "cli:reconcile:12345678:new-process"):
        pass


def test_every_declared_state_transition_is_reachable_and_valid(tmp_path: Path) -> None:
    for source, targets in ALLOWED_TRANSITIONS.items():
        for target in targets:
            repo = f"transition-{source.value}-{target.value}"
            state = StateStore(tmp_path / f"{source.value}-{target.value}.db")
            for step in _PATHS_TO_STATE[source]:
                state.transition(repo, step)

            state.transition(repo, target)

            assert state.current_state(repo) == target


def test_degraded_state_can_resume_bounded_execution_retry(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.transition("example/project", RunState.BASELINE_REVIEW)
    state.transition("example/project", RunState.READY)
    state.transition("example/project", RunState.DEGRADED)

    state.transition("example/project", RunState.EXECUTING)

    assert state.current_state("example/project") == RunState.EXECUTING


def test_notification_failure_preserves_event_provenance_and_degrades_repo(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.transition("example/project", RunState.BASELINE_REVIEW)
    state.transition("example/project", RunState.READY)
    event = state.record_event(
        repo="example/project",
        run_id="run-1",
        state=RunState.READY,
        event_type="ACTION_PROPOSED",
        summary="Implement issue 39",
        reason="The next documented slice is ready.",
        fingerprint="proposal-1",
        evidence={"links": ["https://github.com/example/project/issues/39"]},
    )

    failure = state.record_notification_failure(event, "Discord route unavailable")

    assert state.current_state("example/project") == RunState.DEGRADED
    assert failure.event_type == "NOTIFICATION_FAILED"
    assert failure.evidence["original_event"] == event.event_id
    assert failure.evidence["original_event_type"] == "ACTION_PROPOSED"
    assert failure.evidence["links"] == ["https://github.com/example/project/issues/39"]
    assert state.recent_events("example/project", 1)[0].event_id == failure.event_id


def test_latest_operational_event_is_not_hidden_by_notification_failure(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.transition("example/project", RunState.BASELINE_REVIEW)
    state.transition("example/project", RunState.READY)
    event = state.record_event(
        repo="example/project",
        run_id="run-1",
        state=RunState.READY,
        event_type="STALLED",
        summary="No evidence changed",
        reason="The previous decision remains current.",
        fingerprint="stalled-1",
    )

    state.record_notification_failure(event, "Discord route unavailable")

    operational = state.latest_operational_event("example/project")

    assert operational is not None
    assert operational.event_id == event.event_id
    assert operational.event_type == "STALLED"


def test_event_exists_supports_idempotent_queue_projection(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    assert not state.event_exists("example/project", "queue-action-1", "TECHNICAL_RETRY")

    state.record_event(
        repo="example/project",
        run_id="card-1",
        state=RunState.READY,
        event_type="TECHNICAL_RETRY",
        summary="Retry queued",
        reason="Technical failure is repairable.",
        fingerprint="queue-action-1",
    )

    assert state.event_exists("example/project", "queue-action-1", "TECHNICAL_RETRY")
    assert not state.event_exists("example/project", "queue-action-1", "REPAIR_QUEUED")


def test_proposal_is_durable_and_joined_to_exact_approval(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    decision = {"action": "update_plan", "summary": "Update plan", "reason": "Stale"}
    state.save_proposal("example/project", "action-1", "snapshot-1", decision)
    state.approve("example/project", "action-1", "owner")

    stored = state.approved_proposal("example/project")

    assert stored is not None
    assert stored["action_id"] == "action-1"
    assert stored["decision"] == decision
    state.set_proposal_status("example/project", "action-1", "executed")
    assert state.approved_proposal("example/project") is None


def test_state_reopens_with_active_work_after_process_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    first = StateStore(database)
    first.transition("example/project", RunState.BASELINE_REVIEW)
    first.transition("example/project", RunState.READY)
    first.transition("example/project", RunState.PR_OPEN)
    first.save_active_work(
        "example/project",
        action_id="action-1",
        pr_number=42,
        branch="captains_chair/work/42",
        head_sha="head-1",
        status="pr_open",
        decision={"action": "implement", "summary": "Implement issue 42"},
    )

    restarted = StateStore(database)

    assert restarted.current_state("example/project") == RunState.PR_OPEN
    active = restarted.active_work("example/project")
    assert active is not None
    assert active["action_id"] == "action-1"
    assert active["pr_number"] == 42
    assert active["decision"]["summary"] == "Implement issue 42"


def test_orchestration_card_sync_returns_only_status_transitions(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    ready = [{"id": "card-1", "status": "ready", "title": "Implement"}]

    first = state.sync_orchestration_cards("example/project", ready)
    unchanged = state.sync_orchestration_cards("example/project", ready)
    running = state.sync_orchestration_cards(
        "example/project", [{"id": "card-1", "status": "running", "title": "Implement"}]
    )

    assert first[0]["old_status"] is None
    assert unchanged == []
    assert running[0]["old_status"] == "ready"
    assert running[0]["new_status"] == "running"
