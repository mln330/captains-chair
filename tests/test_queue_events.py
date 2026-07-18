from pathlib import Path
from typing import Any, cast

import make_it_so.queue_events as queue_events
from make_it_so.notifications import render_event
from make_it_so.orchestration import QueueCard, QueueStatus
from make_it_so.queue_events import project_queue_events
from make_it_so.state import StateStore
from tests.helpers import repo_config


def test_completed_implementation_uses_worker_summary_proof_and_pr_link(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    state = StateStore(tmp_path / "state.db")
    card = QueueCard(
        id="implementation-1",
        title="Implementation: long planner title",
        status=QueueStatus.DONE,
        labels=("make_it_so", "stage:implementation"),
        agent_id="coder",
        source_url="https://github.com/example/project/issues/39",
        metadata={
            "automation": {"summary": "Implemented workspace-scoped Etsy routes and opened PR #40."},
            "proof": [
                {
                    "status": "passed",
                    "label": "Implementation proof",
                    "note": "Targeted tests 42/42 passed on head abcdef1.",
                    "url": "https://github.com/example/project/pull/40",
                }
            ],
        },
    )

    events = project_queue_events(state, repo, [card])
    message = render_event(events[0])

    assert events[0].summary == "Implemented workspace-scoped Etsy routes and opened PR #40."
    assert "Targeted tests 42/42 passed on head abcdef1." in message
    assert "https://github.com/example/project/pull/40" in message
    assert "Independent review, test, and applicable UX gates will run next." in message


def test_queue_projection_is_idempotent_and_only_user_blocker_needs_attention(
    tmp_path: Path,
) -> None:
    repo = repo_config(tmp_path)
    state = StateStore(tmp_path / "state.db")
    technical = QueueCard(
        id="technical",
        title="Technical block",
        status=QueueStatus.BLOCKED,
        labels=("stage:test",),
        metadata={"workerProtocol": {"detail": "TECHNICAL: test failed"}},
    )
    user = QueueCard(
        id="user",
        title="Access block",
        status=QueueStatus.BLOCKED,
        labels=("stage:implementation",),
        metadata={"workerProtocol": {"detail": "USER_SECRET: API key is required"}},
    )

    first = project_queue_events(state, repo, [technical, user])
    repeated = project_queue_events(state, repo, [technical, user])

    assert [event.event_type for event in first] == ["ATTENTION_REQUIRED"]
    assert "unrelated ready work continues" in first[0].reason
    assert render_event(first[0]).startswith("ACTION NEEDED\n")
    assert "Provide the requested decision" in render_event(first[0])
    assert "make_it_so orchestrate unblock --repo example/project --card user" in render_event(first[0])
    assert [event.event_type for event in repeated] == ["ATTENTION_REQUIRED"]
    assert repeated[0].evidence["attention_level"] == 2
    assert "second ping" in render_event(repeated[0])

    project_queue_events(state, repo, [technical, user])
    fourth = project_queue_events(state, repo, [technical, user])
    assert fourth[0].evidence["attention_level"] == 4


def test_owner_blocker_escalation_restarts_after_acknowledgement(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    state = StateStore(tmp_path / "state.db")
    user = QueueCard(
        id="user",
        title="Access block",
        status=QueueStatus.BLOCKED,
        labels=("stage:implementation",),
        metadata={"workerProtocol": {"detail": "EXTERNAL_ACCESS: repository access is required"}},
    )

    first = project_queue_events(state, repo, [user])
    assert first[0].evidence["attention_level"] == 1
    assert state.acknowledge_attention(repo.full_name, "queue:user:blocked", "ATTENTION_REQUIRED") == 1

    after_ack = project_queue_events(state, repo, [user])
    assert after_ack[0].evidence["attention_level"] == 1


def test_protocol_recovery_is_reported_once_as_requeued_work(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    state = StateStore(tmp_path / "state.db")
    review = QueueCard(
        id="implementation-1",
        title="Implementation",
        status=QueueStatus.REVIEW,
        labels=("make_it_so", "stage:implementation"),
        agent_id="coder",
    )
    ready = review.model_copy(
        update={
            "status": QueueStatus.READY,
            "metadata": {
                "comments": [
                    {
                        "body": (
                            "TECHNICAL: OpenClaw ended the worker run in review without "
                            "the mandatory MAKE_IT_SO completion proof; retry the same stage."
                        )
                    }
                ]
            },
        }
    )

    assert project_queue_events(state, repo, [review]) == []
    events = project_queue_events(state, repo, [ready])
    repeated = project_queue_events(state, repo, [ready])

    assert len(events) == 1
    assert events[0].event_type == "WORK_REQUEUED"
    assert "retry" in render_event(events[0]).lower()
    assert repeated == []


def test_protocol_retry_can_be_reported_after_dispatch(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    state = StateStore(tmp_path / "state.db")
    card = QueueCard(
        id="implementation-1",
        title="Implementation",
        status=QueueStatus.RUNNING,
        labels=("make_it_so", "stage:implementation"),
        agent_id="coder",
        source_url="https://github.com/example/project/issues/39",
        metadata={"automation": {"dispatchCount": 2}},
    )

    events = project_queue_events(state, repo, [card], protocol_retries=(card.id,))
    repeated = project_queue_events(state, repo, [card], protocol_retries=(card.id,))

    assert [event.event_type for event in events] == ["WORK_REQUEUED", "WORK_STARTED"]
    assert "automatically retried" in events[0].reason
    assert repeated == []


def test_technical_repair_and_control_plane_recovery_actions_are_reported_once(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    state = StateStore(tmp_path / "state.db")
    cards = [
        QueueCard(
            id="failed-1",
            title="Implementation",
            status=QueueStatus.READY,
            labels=("make_it_so", "stage:implementation"),
            agent_id="coder",
            source_url="https://github.com/example/project/issues/39",
            metadata={"failureCount": 1, "automation": {"dispatchCount": 2}},
        ),
        QueueCard(
            id="repair-1",
            title="Repair review findings",
            status=QueueStatus.READY,
            labels=("make_it_so", "stage:repair"),
            agent_id="coder",
            source_url="https://github.com/example/project/pull/40",
        ),
        QueueCard(
            id="recovery-1",
            title="Replan failed work",
            status=QueueStatus.READY,
            labels=("make_it_so", "stage:control_plane_action"),
            agent_id="make-it-so",
        ),
    ]

    events = project_queue_events(
        state,
        repo,
        cards,
        technical_retries=("failed-1",),
        repairs_created=("repair-1",),
        control_plane_recoveries=("recovery-1",),
    )
    repeated = project_queue_events(
        state,
        repo,
        cards,
        technical_retries=("failed-1",),
        repairs_created=("repair-1",),
        control_plane_recoveries=("recovery-1",),
    )

    assert [event.event_type for event in events] == [
        "TECHNICAL_RETRY",
        "REPAIR_QUEUED",
        "CONTROL_PLANE_RECOVERY_QUEUED",
    ]
    assert all(render_event(event).startswith("Captain HANDLING\n") for event in events)
    assert all("ACTION NEEDED" not in render_event(event) for event in events)
    assert "https://github.com/example/project/pull/40" in render_event(events[1])
    assert repeated == []


def test_queue_diagnostics_are_reported_once_with_card_link_and_next_action(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    state = StateStore(tmp_path / "state.db")
    diagnostics = {
        "diagnostics": [
            {
                "card": {
                    "id": "card-1",
                    "sourceUrl": "https://github.com/example/project/issues/39",
                },
                "diagnostics": [
                    {
                        "kind": "stranded_ready",
                        "severity": "warning",
                        "title": "Assigned card is ready but unclaimed",
                        "detail": "The assigned worker has not claimed this card.",
                    }
                ],
            },
            {
                "card": {
                    "id": "card-1",
                    "sourceUrl": "https://github.com/example/project/issues/39",
                },
                "diagnostics": [
                    {
                        "kind": "stranded_ready",
                        "severity": "warning",
                        "title": "Assigned card is ready but unclaimed",
                        "detail": "The assigned worker has not claimed this card.",
                    }
                ],
            },
        ]
    }

    events = project_queue_events(state, repo, [], diagnostics=diagnostics)
    repeated = project_queue_events(state, repo, [], diagnostics=diagnostics)

    assert len(events) == 1
    assert events[0].event_type == "QUEUE_STALLED"
    assert events[0].evidence["status_level"] == 1
    message = render_event(events[0])
    assert message.startswith("Captain HANDLING\n")
    assert "ACTION NEEDED" not in message
    assert "https://github.com/example/project/issues/39" in message
    assert "Reconcile the queue" in message
    assert len(repeated) == 1
    assert repeated[0].evidence["status_level"] == 2
    assert "Captain HANDLING" in render_event(repeated[0])


def test_queue_diagnostics_escalate_without_spamming_every_cycle(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    state = StateStore(tmp_path / "state.db")
    diagnostics = {
        "diagnostics": [
            {
                "kind": "missing_proof",
                "title": "Worker proof is missing",
                "detail": "The card is done without structured completion proof.",
            }
        ]
    }

    emitted = [
        project_queue_events(state, repo, [], diagnostics=diagnostics)
        for _ in range(5)
    ]

    assert [events[0].evidence["status_level"] for events in emitted if events] == [1, 2, 3, 4]
    assert "ACTION NEEDED" not in render_event(emitted[1][0])


def test_archived_queue_diagnostics_do_not_page_the_owner(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    state = StateStore(tmp_path / "state.db")

    events = project_queue_events(
        state,
        repo,
        [],
        diagnostics={
            "diagnostics": [
                {
                    "card": {
                        "id": "archived-card",
                        "metadata": {"archivedAt": "2026-07-13T08:00:00Z"},
                    },
                    "diagnostics": [
                        {
                            "kind": "missing_proof",
                            "title": "Historical worker proof is missing",
                            "detail": "This card is retained as evidence only.",
                        }
                    ],
                }
            ]
        },
    )

    assert events == []


def test_diagnostics_failure_is_reported_as_actionable_queue_health_event(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    state = StateStore(tmp_path / "state.db")

    events = project_queue_events(
        state,
        repo,
        [],
        diagnostics={"status": "degraded", "error": "gateway timeout"},
    )

    assert len(events) == 1
    assert events[0].event_type == "QUEUE_DEGRADED"
    assert events[0].reason == "gateway timeout"
    message = render_event(events[0])
    assert message.startswith("Captain HANDLING\n")
    assert "ACTION NEEDED" not in message
    assert "Check the configured queue runtime" in message


def test_worktree_cleanup_failure_is_technical_and_actionable(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    state = StateStore(tmp_path / "state.db")

    events = project_queue_events(
        state,
        repo,
        [],
        workspace_cleanup_failures=("workflow-123: dirty worktree",),
    )

    assert len(events) == 1
    assert events[0].event_type == "WORKSPACE_CLEANUP_FAILED"
    message = render_event(events[0])
    assert message.startswith("Captain HANDLING\n")
    assert "dirty worktree" in message
    assert "owner" not in message.lower()
    assert "GitHub branch and PR remain untouched" in message


def test_worker_recovery_warning_is_reported_without_owner_attention(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    state = StateStore(tmp_path / "state.db")

    events = project_queue_events(
        state,
        repo,
        [],
        recovery_warnings=("Could not inspect OpenClaw session agent:coder:1: gateway timeout",),
    )

    assert len(events) == 1
    assert events[0].event_type == "QUEUE_DEGRADED"
    message = render_event(events[0])
    assert message.startswith("Captain HANDLING\n")
    assert "gateway timeout" in message
    assert "unrelated ready work continues" in message


def test_queue_event_helpers_handle_runtime_specific_payload_edges() -> None:
    diagnostic_rows = cast(Any, queue_events._diagnostic_rows)  # pyright: ignore[reportPrivateUsage]
    diagnostic_action = cast(Any, queue_events._diagnostic_action)  # pyright: ignore[reportPrivateUsage]
    latest_comment = cast(Any, queue_events._latest_comment)  # pyright: ignore[reportPrivateUsage]
    card_block_reason = cast(Any, queue_events._card_block_reason)  # pyright: ignore[reportPrivateUsage]
    dispatch_count = cast(Any, queue_events._dispatch_count)  # pyright: ignore[reportPrivateUsage]
    failure_count = cast(Any, queue_events._failure_count)  # pyright: ignore[reportPrivateUsage]

    assert diagnostic_rows({"diagnostics": "not-a-list"}) == []
    assert diagnostic_rows({"diagnostics": [{"diagnostics": ["ignore", {"kind": "nested"}]}]}) == [
        {"kind": "nested"}
    ]
    assert diagnostic_action({"actions": ["retry"]}, "unknown") == "retry"
    assert "MAKE_IT_SO" in diagnostic_action({}, "missing_proof")
    assert "inspect" in diagnostic_action({}, "unknown").lower()

    card = QueueCard(id="edge", title="Edge", status=QueueStatus.BLOCKED, metadata={})
    assert card_block_reason(card) == ""
    assert latest_comment(card) == "The previous worker ended without structured completion proof."
    assert not cast(Any, queue_events._has_protocol_recovery_comment)(  # pyright: ignore[reportPrivateUsage]
        card
    )
    assert dispatch_count(card) == 0
    assert failure_count(card) == 0

    comment_card = card.model_copy(
        update={"metadata": {"comments": [{"body": "latest"}], "automation": {"dispatchCount": 3}}}
    )
    assert latest_comment(comment_card) == "latest"
    assert dispatch_count(comment_card) == 3
