from collections.abc import Sequence
from pathlib import Path

import pytest

from make_it_so.command import CommandResult, CommandRunner
from make_it_so.models import EventRecord, NotificationConfig, RunState
from make_it_so.notifications import (
    NotificationError,
    Notifier,
    NotifierAdapterContractError,
    NotifierAdapterRegistry,
    build_notifier,
    render_event,
)


def notification_event() -> EventRecord:
    return EventRecord(
        event_id="delivery-event",
        repo="example/project",
        run_id="delivery-run",
        state=RunState.READY,
        event_type="ACTION_PROPOSED",
        summary="A bounded action is ready.",
        reason="The evidence is current.",
        fingerprint="delivery-fingerprint",
        evidence={"next_action": "Review the proposal."},
    )


def test_completion_summary_includes_done_proof_next_and_link() -> None:
    event = EventRecord(
        event_id="event-1",
        repo="NewmanZone/PrintHub",
        run_id="run-1",
        state=RunState.PR_OPEN,
        event_type="PR_OPENED",
        summary="Implement authenticated current-user bootstrap.",
        reason="The worker completed the selected scope.",
        fingerprint="fingerprint",
        evidence={
            "next_action": "Run independent review.",
            "links": ["https://github.com/NewmanZone/PrintHub/pull/36"],
            "pr": {"number": 36},
            "checks": [{"returncode": 0}, {"returncode": 0}],
            "model": "gpt-5.6-sol",
        },
    )

    message = render_event(event)

    assert "PrintHub | PR opened" in message
    assert "Done: Implement authenticated current-user bootstrap." in message
    assert "Proof: PR #36 | checks 2/2 passed | model gpt-5.6-sol" in message
    assert "Next: Run independent review." in message
    assert "https://github.com/NewmanZone/PrintHub/pull/36" in message


def test_autonomous_review_findings_are_progress_not_owner_attention() -> None:
    event = EventRecord(
        event_id="event-2",
        repo="NewmanZone/PrintHub",
        run_id="run-2",
        state=RunState.REPAIRING,
        event_type="REVIEW_BLOCKED",
        summary="JWT-path test proof is missing.",
        reason="Independent review found blocking changes.",
        fingerprint="review-fingerprint",
        evidence={
            "next_action": "Queue an autonomous repair.",
            "links": ["https://github.com/NewmanZone/PrintHub/pull/36"],
        },
    )

    message = render_event(event)

    assert "ACTION NEEDED" not in message
    assert "PrintHub | Review found changes" in message
    assert "Review: JWT-path test proof is missing." in message
    assert "Next: Queue an autonomous repair." in message


def test_technical_failure_is_control_plane_status_not_owner_attention() -> None:
    event = EventRecord(
        event_id="event-3",
        repo="NewmanZone/PrintHub",
        run_id="run-3",
        state=RunState.DEGRADED,
        event_type="EXECUTION_FAILED",
        summary="The worker exited before producing completion proof.",
        reason="The OpenClaw worker handoff was incomplete.",
        fingerprint="technical-fingerprint",
        evidence={"next_action": "Reclaim the card and retry automatically."},
    )

    message = render_event(event)

    assert message.startswith("Number 1 HANDLING\n")
    assert "ACTION NEEDED" not in message
    assert "Reclaim the card and retry automatically." in message


def test_notification_failure_is_control_plane_health_not_owner_attention() -> None:
    event = EventRecord(
        event_id="event-notification-failure",
        repo="NewmanZone/PrintHub",
        run_id="run-notification-failure",
        state=RunState.DEGRADED,
        event_type="NOTIFICATION_FAILED",
        summary="Workboard diagnostics are unavailable",
        reason="Discord route unavailable",
        fingerprint="notification:event-1",
        evidence={"next_action": "Check the configured notification route."},
    )

    message = render_event(event)

    assert message.startswith("Number 1 HANDLING\n")
    assert "ACTION NEEDED" not in message
    assert "Check the configured notification route." in message


def test_untrusted_planning_context_is_control_plane_status_not_owner_attention() -> None:
    event = EventRecord(
        event_id="event-planning-context",
        repo="NewmanZone/PrintHub",
        run_id="run-planning-context",
        state=RunState.DEGRADED,
        event_type="PLANNING_CONTEXT_UNAVAILABLE",
        summary="The Number 1 could not obtain a trustworthy default-branch planning document.",
        reason="origin/main could not be read.",
        fingerprint="planning-context-failure",
        evidence={"next_action": "Restore the repository remote and rerun the cycle."},
    )

    message = render_event(event)

    assert message.startswith("Number 1 HANDLING\n")
    assert "ACTION NEEDED" not in message
    assert "Restore the repository remote" in message


def test_completion_ready_only_pages_when_merge_policy_requires_owner() -> None:
    base = EventRecord(
        event_id="event-4",
        repo="NewmanZone/PrintHub",
        run_id="run-4",
        state=RunState.COMPLETION_READY,
        event_type="COMPLETION_READY",
        summary="All Number 1 gates passed.",
        reason="The configured completion policy requires an owner decision.",
        fingerprint="completion-fingerprint",
        evidence={"next_action": "Choose the configured completion action."},
    )

    autonomous = base.model_copy(
        update={"evidence": {**base.evidence, "owner_required": False}}
    )
    owner_decision = base.model_copy(
        update={"evidence": {**base.evidence, "owner_required": True}}
    )

    assert "ACTION NEEDED" not in render_event(autonomous)
    assert "ACTION NEEDED" in render_event(owner_decision)


def test_approval_reminder_preserves_owner_attention_provenance() -> None:
    event = EventRecord(
        event_id="event-5",
        repo="NewmanZone/PrintHub",
        run_id="run-5",
        state=RunState.BLOCKED,
        event_type="ATTENTION_REQUIRED",
        summary="Approve the documented implementation slice.",
        reason="The original approval request is still unresolved.",
        fingerprint="approval-fingerprint",
        evidence={
            "original_event": "APPROVAL_REQUIRED",
            "attention_level": 2,
            "action_id": "action-123",
            "next_action": "Approve the exact action to resume.",
        },
    )

    message = render_event(event)

    assert message.startswith("ACTION NEEDED, second ping.")
    assert "Approve: `make_it_so approve --repo NewmanZone/PrintHub --action-id action-123`" in message


def test_lowercase_owner_blocker_still_pages_owner() -> None:
    event = EventRecord(
        event_id="event-lowercase-blocker",
        repo="NewmanZone/PrintHub",
        run_id="run-lowercase-blocker",
        state=RunState.BLOCKED,
        event_type="ATTENTION_REQUIRED",
        summary="A credential is required.",
        reason="The worker reported a user blocker.",
        fingerprint="lowercase-blocker",
        evidence={"blocker": "user_secret: Azure credential required"},
    )

    assert render_event(event).startswith("ACTION NEEDED")


@pytest.mark.parametrize(
    ("level", "prefix"),
    ((3, "third ping"), (4, "polite flare")),
)
def test_escalation_ladder_keeps_humor_at_higher_attention_levels(level: int, prefix: str) -> None:
    event = notification_event().model_copy(
        update={
            "state": RunState.BLOCKED,
            "event_type": "ATTENTION_REQUIRED",
            "evidence": {
                "attention_level": level,
                "owner_required": True,
                "next_action": "Review the proposal.",
            },
        }
    )
    assert prefix in render_event(event)


def test_openclaw_discord_notifier_sends_and_surfaces_gateway_failure() -> None:
    calls: list[list[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        calls.append(list(command))
        return CommandResult(0, "", "")

    from make_it_so.notifications import OpenClawDiscordNotifier

    notifier = OpenClawDiscordNotifier(
        NotificationConfig(kind="openclaw_discord", route="channel-1", executable="openclaw"),
        runner,
    )
    notifier.send(notification_event())
    assert calls[0][0:4] == ["openclaw", "message", "send", "--channel"]
    assert "channel-1" in calls[0]

    def failing_runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return CommandResult(1, "", "gateway unavailable")

    failing = OpenClawDiscordNotifier(
        NotificationConfig(kind="openclaw_discord", route="channel-1", executable="openclaw"),
        failing_runner,
    )
    with pytest.raises(NotificationError, match="gateway unavailable"):
        failing.send(notification_event())


def test_discord_webhook_notifier_handles_missing_success_http_and_os_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from make_it_so.notifications import DiscordWebhookNotifier

    config = NotificationConfig(kind="discord_webhook", webhook_env="MAKE_IT_SO_TEST_WEBHOOK")
    notifier = DiscordWebhookNotifier(config)
    monkeypatch.delenv("MAKE_IT_SO_TEST_WEBHOOK", raising=False)
    with pytest.raises(NotificationError, match="is not set"):
        notifier.send(notification_event())

    monkeypatch.setenv("MAKE_IT_SO_TEST_WEBHOOK", "https://discord.test/webhook")

    class Response:
        def __init__(self, status: int) -> None:
            self.status = status

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            del args

    def success(request: object, timeout: int) -> Response:
        del request, timeout
        return Response(204)

    monkeypatch.setattr("make_it_so.notifications.urllib.request.urlopen", success)
    notifier.send(notification_event())

    def unavailable_http(request: object, timeout: int) -> Response:
        del request, timeout
        return Response(503)

    monkeypatch.setattr("make_it_so.notifications.urllib.request.urlopen", unavailable_http)
    with pytest.raises(NotificationError, match="HTTP 503"):
        notifier.send(notification_event())

    def unavailable(request: object, timeout: int) -> None:
        del request, timeout
        raise OSError("network unavailable")

    monkeypatch.setattr("make_it_so.notifications.urllib.request.urlopen", unavailable)
    with pytest.raises(NotificationError, match="network unavailable"):
        notifier.send(notification_event())


class MemoryNotifier:
    def send(self, event: EventRecord) -> None:
        del event


def test_custom_notifier_kind_registers_without_changing_core() -> None:
    registry = NotifierAdapterRegistry()
    marker = MemoryNotifier()

    def build_custom(config: NotificationConfig, runner: CommandRunner) -> Notifier:
        del config, runner
        return marker

    registry.register("hermes_discord", build_custom)

    assert (
        build_notifier(
            NotificationConfig(kind="hermes_discord", settings={"channel": "captain"}),
            registry=registry,
        )
        is marker
    )


def test_unknown_notifier_kind_fails_closed() -> None:
    with pytest.raises(NotificationError, match="no installed adapter"):
        build_notifier(NotificationConfig(kind="unconfigured_runtime"))


def test_notifier_registry_rejects_invalid_adapter() -> None:
    registry = NotifierAdapterRegistry()

    def build_invalid(config: NotificationConfig, runner: CommandRunner) -> object:
        del config, runner
        return object()

    registry.register("invalid", build_invalid)

    with pytest.raises(NotifierAdapterContractError, match="implement send"):
        build_notifier(NotificationConfig(kind="invalid"), registry=registry)
