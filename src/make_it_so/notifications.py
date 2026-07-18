from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Protocol, cast, runtime_checkable

from make_it_so.command import CommandRunner, run_command
from make_it_so.models import EventRecord, NotificationConfig
from make_it_so.plugins import EntryPointProvider, load_entrypoint_plugins


class NotificationError(RuntimeError):
    pass


@runtime_checkable
class NotifierAdapter(Protocol):
    def send(self, event: EventRecord) -> None: ...


# Compatibility name for existing engine and adapter packages.
Notifier = NotifierAdapter


class NotifierAdapterContractError(RuntimeError):
    """Raised when an installed notifier does not implement the send contract."""


NOTIFIER_ADAPTER_ENTRYPOINT_GROUP = "make_it_so.notifier_adapters"
NotifierAdapterBuilder = Callable[[NotificationConfig, CommandRunner], object]


_OWNER_ATTENTION_EVENTS = frozenset({"APPROVAL_REQUIRED", "APPROVAL_STALE"})
_TECHNICAL_STATUS_EVENTS = frozenset(
    {
        "ACTION_BLOCKED",
        "BASELINE_REQUIRED",
        "EXECUTION_FAILED",
        "WORKFLOW_QUEUE_FAILED",
        "TECHNICAL_RETRY",
        "REPAIR_QUEUED",
        "CONTROL_PLANE_RECOVERY_QUEUED",
        "CONTROL_PLANE_MAINTENANCE_REQUIRED",
        "POST_MERGE_FAILED",
        "NOTIFICATION_FAILED",
        "QUEUE_DEGRADED",
        "QUEUE_STALLED",
        "PLANNING_CONTEXT_UNAVAILABLE",
        "WORKSPACE_CLEANUP_FAILED",
        "STALLED",
    }
)
_OWNER_BLOCKER_PREFIXES = (
    "USER_SECRET:",
    "GOAL_DIVERGENCE:",
    "EXTERNAL_ACCESS:",
    "HIGH_RISK_DECISION:",
)


def requires_owner_attention(
    event_type: str, evidence: Mapping[str, Any] | None = None
) -> bool:
    """Return whether an event needs an owner decision rather than Captain recovery."""
    if event_type in _OWNER_ATTENTION_EVENTS:
        return True
    if event_type == "COMPLETION_READY":
        return bool(evidence and evidence.get("owner_required") is True)
    if event_type == "ATTENTION_REQUIRED":
        if evidence and evidence.get("owner_required") is True:
            return True
        original_event = str((evidence or {}).get("original_event") or "")
        if original_event in _OWNER_ATTENTION_EVENTS:
            return True
        blocker = str(
            (evidence or {}).get("blocker")
            or (evidence or {}).get("owner_blocker")
            or ""
        )
        return blocker.strip().upper().startswith(_OWNER_BLOCKER_PREFIXES)
    return False


def render_event(event: EventRecord) -> str:
    repo_name = event.repo.split("/", 1)[-1]
    next_action = event.evidence.get("next_action")
    action_id = event.evidence.get("action_id")
    links = event.evidence.get("links")
    link_text = ""
    if isinstance(links, list) and links:
        link_list = cast(list[Any], links)
        link_text = " " + " ".join(str(link) for link in link_list[:3])
    if requires_owner_attention(event.event_type, event.evidence):
        lines = [
            _attention_prefix(event),
            f"{repo_name}: {_one_line(event.summary)}",
            f"Why: {_one_line(event.reason)}",
        ]
        if next_action:
            lines.append(f"Next: {_one_line(str(next_action))}")
        if action_id:
            lines.append(f"Approve: `make_it_so approve --repo {event.repo} --action-id {action_id}`")
        if link_text:
            lines.append(f"Links:{link_text}")
        return "\n".join(lines)

    if event.event_type in _TECHNICAL_STATUS_EVENTS:
        lines = [
            "Captain HANDLING",
            f"{repo_name}: {_event_label(event.event_type)}",
            f"What happened: {_one_line(event.summary)}",
            f"Why: {_one_line(event.reason)}",
        ]
        if next_action:
            lines.append(f"Next: {_one_line(str(next_action))}")
        if link_text:
            lines.append(f"Links:{link_text}")
        return "\n".join(lines)[:1800]

    lines = [
        f"{repo_name} | {_event_label(event.event_type)}",
        f"{_summary_prefix(event.event_type)}: {_one_line(event.summary)}",
    ]
    proof = _proof_line(event)
    if proof:
        lines.append(f"Proof: {proof}")
    if next_action:
        lines.append(f"Next: {_one_line(str(next_action))}")
    if link_text:
        lines.append(f"Links:{link_text}")
    return "\n".join(lines)[:1800]


def _event_label(event_type: str) -> str:
    labels = {
        "ACTION_PROPOSED": "Plan ready",
        "ISSUE_CREATED": "Issue created",
        "ISSUE_UPDATED": "Issue updated",
        "ISSUE_CLOSED": "Issue closed",
        "PR_OPENED": "PR opened",
        "PR_REPAIRED": "PR repaired",
        "PR_MERGED": "PR merged",
        "CONTROL_PLANE_COMPLETED": "Captain review complete",
        "COMPLETION_READY": "Completion ready",
        "POST_MERGE_VERIFIED": "Post-merge verified",
        "REVIEW_BLOCKED": "Review found changes",
        "FINAL_REVIEW_BLOCKED": "Final review found changes",
        "STATUS_REPORTED": "Status",
        "WORK_STARTED": "Work started",
        "WORKFLOW_QUEUE_FAILED": "Workflow queue failed",
        "WORK_REQUEUED": "Work requeued",
        "TECHNICAL_RETRY": "Technical retry",
        "REPAIR_QUEUED": "Repair queued",
        "CONTROL_PLANE_RECOVERY_QUEUED": "Captain recovery queued",
        "WORKFLOW_ALREADY_QUEUED": "Workflow already active",
        "WORK_COMPLETED": "Implementation complete",
        "WORKSPACE_CLEANUP_FAILED": "Worktree cleanup failed",
    }
    return labels.get(event_type, event_type.replace("_", " ").title())


def _summary_prefix(event_type: str) -> str:
    if event_type in {"REVIEW_BLOCKED", "FINAL_REVIEW_BLOCKED"}:
        return "Review"
    if event_type in {"PR_CHECKS_WAITING", "POST_MERGE_WAITING", "REVIEW_WAITING"}:
        return "Status"
    return "Done"


def _proof_line(event: EventRecord) -> str:
    details: list[str] = []
    issue = event.evidence.get("issue")
    if issue:
        details.append(f"issue #{issue}")
    pr = event.evidence.get("pr")
    if isinstance(pr, dict):
        pr_object = cast(dict[str, Any], pr)
        if pr_object.get("number"):
            details.append(f"PR #{pr_object['number']}")
    elif isinstance(pr, int):
        details.append(f"PR #{pr}")
    checks = event.evidence.get("checks")
    if isinstance(checks, list) and checks:
        check_list = cast(list[Any], checks)
        passed = 0
        for item in check_list:
            if isinstance(item, dict) and cast(dict[str, Any], item).get("returncode") == 0:
                passed += 1
        details.append(f"checks {passed}/{len(check_list)} passed")
    merged_head = event.evidence.get("merged_head_sha")
    if merged_head:
        details.append(f"head {str(merged_head)[:8]}")
    model = event.evidence.get("model")
    if model:
        details.append(f"model {model}")
    proof_label = event.evidence.get("proof_label")
    proof_note = event.evidence.get("proof_note")
    if proof_label or proof_note:
        proof_text = ": ".join(str(value) for value in (proof_label, proof_note) if value)
        details.append(_one_line(proof_text)[:220])
    return " | ".join(details)


def _attention_prefix(event: EventRecord) -> str:
    level = int(event.evidence.get("attention_level") or 1)
    if level <= 1:
        return "ACTION NEEDED"
    if level == 2:
        return "ACTION NEEDED, second ping. Tiny flag is now waving."
    if level == 3:
        return "ACTION NEEDED, third ping. Clipboard stare has intensified."
    return "ACTION NEEDED, polite flare launched. The Captain is still blocked."


def _one_line(value: str) -> str:
    collapsed = " ".join(value.split())
    return collapsed[:260] + ("..." if len(collapsed) > 260 else "")


class StdoutNotifier:
    def send(self, event: EventRecord) -> None:
        print(render_event(event))


class OpenClawDiscordNotifier:
    def __init__(self, config: NotificationConfig, runner: CommandRunner = run_command) -> None:
        self.config = config
        self.runner = runner

    def send(self, event: EventRecord) -> None:
        assert self.config.executable and self.config.route
        result = self.runner(
            [
                self.config.executable,
                "message",
                "send",
                "--channel",
                "discord",
                "--target",
                self.config.route,
                "--message",
                render_event(event),
                "--json",
            ],
            timeout=90,
        )
        if result.returncode:
            raise NotificationError((result.stderr or result.stdout).strip()[:2000])


class DiscordWebhookNotifier:
    def __init__(self, config: NotificationConfig) -> None:
        self.config = config

    def send(self, event: EventRecord) -> None:
        assert self.config.webhook_env
        url = os.environ.get(self.config.webhook_env)
        if not url:
            raise NotificationError(f"environment variable {self.config.webhook_env} is not set")
        request = urllib.request.Request(
            url,
            data=json.dumps({"content": render_event(event)}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status >= 300:
                    raise NotificationError(f"Discord webhook returned HTTP {response.status}")
        except OSError as exc:
            raise NotificationError(f"Discord webhook failed: {exc}") from exc


class NotifierAdapterRegistry:
    """Explicit notifier registry for portable runtime and delivery plugins."""

    def __init__(self) -> None:
        self._builders: dict[str, NotifierAdapterBuilder] = {}
        self._loaded_plugins: set[str] = set()

    def register(
        self,
        kind: str,
        builder: NotifierAdapterBuilder,
        *,
        replace: bool = False,
    ) -> None:
        normalized = kind.strip()
        if not normalized:
            raise ValueError("notifier adapter kind must not be empty")
        if normalized in self._builders and not replace:
            raise ValueError(f"notifier adapter is already registered: {normalized}")
        self._builders[normalized] = builder

    def discover(self, *, provider: EntryPointProvider | None = None) -> tuple[str, ...]:
        if provider is None:
            return load_entrypoint_plugins(
                self,
                group=NOTIFIER_ADAPTER_ENTRYPOINT_GROUP,
                loaded=self._loaded_plugins,
            )
        return load_entrypoint_plugins(
            self,
            group=NOTIFIER_ADAPTER_ENTRYPOINT_GROUP,
            provider=provider,
            loaded=self._loaded_plugins,
        )

    def build(self, config: NotificationConfig, runner: CommandRunner) -> Notifier:
        builder = self._builders.get(config.kind)
        if builder is None:
            raise NotificationError(
                f"notification kind {config.kind} has no installed adapter; "
                "register a Notifier with NotifierAdapterRegistry"
            )
        adapter = builder(config, runner)
        if not isinstance(adapter, Notifier):
            raise NotifierAdapterContractError(
                "notifier adapter must implement send(event)"
            )
        return adapter


def _build_stdout_notifier(config: NotificationConfig, runner: CommandRunner) -> Notifier:
    del config, runner
    return StdoutNotifier()


def _build_openclaw_discord_notifier(
    config: NotificationConfig, runner: CommandRunner
) -> Notifier:
    return OpenClawDiscordNotifier(config, runner)


def _build_discord_webhook_notifier(config: NotificationConfig, runner: CommandRunner) -> Notifier:
    del runner
    return DiscordWebhookNotifier(config)


DEFAULT_NOTIFIER_ADAPTERS = NotifierAdapterRegistry()
DEFAULT_NOTIFIER_ADAPTERS.register("stdout", _build_stdout_notifier)
DEFAULT_NOTIFIER_ADAPTERS.register("openclaw_discord", _build_openclaw_discord_notifier)
DEFAULT_NOTIFIER_ADAPTERS.register("discord_webhook", _build_discord_webhook_notifier)


def register_notifier_adapter(
    kind: str,
    builder: NotifierAdapterBuilder,
    *,
    replace: bool = False,
) -> None:
    """Register a production notifier adapter in the process-wide registry."""
    DEFAULT_NOTIFIER_ADAPTERS.register(kind, builder, replace=replace)


def build_notifier(
    config: NotificationConfig,
    runner: CommandRunner = run_command,
    *,
    registry: NotifierAdapterRegistry = DEFAULT_NOTIFIER_ADAPTERS,
) -> Notifier:
    if registry is DEFAULT_NOTIFIER_ADAPTERS:
        registry.discover()
    return registry.build(config, runner)
