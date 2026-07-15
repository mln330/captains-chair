from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from captains_chair.adapters import CallbackUsageTelemetryAdapter, UsageTelemetryAdapter
from captains_chair.baseline import DeepBaselineCollector
from captains_chair.canary import (
    build_canary_spec,
    canary_board_id,
    evaluate_canary_card,
    summarize_canary_card,
)
from captains_chair.command import run_command
from captains_chair.completion_gate import GitHubCompletionValidator
from captains_chair.config import load_config, write_json_schema
from captains_chair.courses import CourseStore, planning_session
from captains_chair.engine import ControlPlaneEngine, ModelCallSuppressedError
from captains_chair.github import GhGitHubProvider
from captains_chair.harness import build_harness
from captains_chair.merge_gate import evaluate_workboard_merge, final_review_head
from captains_chair.models import (
    ActionKind,
    AppConfig,
    DirectOrchestratorConfig,
    EventRecord,
    HarnessHealth,
    OpenClawWorkboardConfig,
    OperationMode,
    PlanDecision,
    RepoConfig,
    RunState,
    WorkerAssignments,
)
from captains_chair.notifications import NotificationError, Notifier, build_notifier, render_event
from captains_chair.openclaw_runtime import OpenClawRuntimeInstaller
from captains_chair.openclaw_usage import DEFAULT_SESSION_LIMIT, sync_openclaw_sessions
from captains_chair.openclaw_workboard import OpenClawWorkboardAdapter
from captains_chair.orchestration import (
    QueueCard,
    QueueCardSpec,
    QueueStatus,
    ReconcileResult,
    WorkerLifecycleAdapter,
    WorkflowOrchestrator,
    WorkQueueAdapter,
    WorkspaceRef,
    worker_model_health,
)
from captains_chair.queue_events import project_queue_events
from captains_chair.runtime import (
    RuntimeAdapterContractError,
    RuntimeAdapterUnavailable,
    build_work_queue_orchestrator,
)
from captains_chair.scheduler import (
    ScheduleSpec,
    build_scheduler,
)
from captains_chair.state import LeaseBusyError, StateStore
from captains_chair.usage import build_usage_report, dispatch_budget, usage_summary_text
from captains_chair.worktrees import WorktreeManager

CONTINUATION_EVENTS = frozenset(
    {
        "ISSUE_UPDATED",
        "ISSUE_CREATED",
        "ISSUE_CLOSED",
        "REVIEW_BLOCKED",
        "FINAL_REVIEW_BLOCKED",
        "UX_REVIEW_BLOCKED",
        "PR_REPAIRED",
        "PR_MERGED",
        "POST_MERGE_VERIFIED",
    }
)
WATCH_WAITING_EVENTS = frozenset({"PR_CHECKS_WAITING", "POST_MERGE_WAITING", "REVIEW_WAITING"})
OWNER_RESUME_PREFIXES = (
    "USER_SECRET:",
    "GOAL_DIVERGENCE:",
    "EXTERNAL_ACCESS:",
    "HIGH_RISK_DECISION:",
    "OWNER_PAUSED:",
)


def _control_plane_mutation_block(
    repo: RepoConfig,
    *,
    operation: str,
    mutation: str,
    next_action: str,
) -> dict[str, str] | None:
    """Return a user-facing stop response for modes that forbid this mutation."""
    if repo.operation_mode not in {OperationMode.DISABLED, OperationMode.ADVISORY}:
        return None
    mode = repo.operation_mode.value
    return {
        "status": mode,
        "repo": repo.full_name,
        "operation": operation,
        "reason": f"repository Captain is {mode}; {mutation}",
        "next_action": next_action,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="captains-chair", description="Captain's Chair control plane")
    parser.add_argument("--config", type=Path, default=Path("~/.config/captains-chair/config.yaml").expanduser())
    sub = parser.add_subparsers(dest="command", required=True)

    schema = sub.add_parser("schema", help="write the strict configuration JSON Schema")
    schema.add_argument("--output", type=Path, default=Path("schemas/config.schema.json"))

    sub.add_parser("doctor", help="validate configuration, repositories, GitHub, and harness executables")

    model_check = sub.add_parser("model-check", help="validate one configured model through its harness")
    model_check.add_argument("--repo", required=True)
    model_check.add_argument("--harness", required=True)
    model_check.add_argument(
        "--role",
        choices=("planner", "coder", "tester", "reviewer", "ux_reviewer", "final_reviewer"),
        default="planner",
        help="configured model role to probe (default: planner)",
    )

    baseline = sub.add_parser("baseline", help="collect a deep repository baseline")
    baseline.add_argument("--repo", required=True)
    baseline.add_argument("--harness", required=True)
    baseline.add_argument("--analyze", action=argparse.BooleanOptionalAction, default=True)
    baseline.add_argument("--run-checks", action=argparse.BooleanOptionalAction, default=True)
    baseline.add_argument("--send", action="store_true")

    cycle = sub.add_parser("cycle", help="run one bounded Captain cycle")
    cycle.add_argument("--repo", required=True)
    cycle.add_argument("--harness", required=True)
    mode = cycle.add_mutually_exclusive_group()
    mode.add_argument("--shadow", action="store_true", default=True)
    mode.add_argument("--live", action="store_true")
    cycle.add_argument(
        "--force-replan",
        action="store_true",
        help="bypass one unchanged-stall suppression check; intended for operator recovery",
    )
    cycle.add_argument(
        "--continue-run",
        action="store_true",
        help="continue safe immediate transitions for a bounded time budget",
    )
    cycle.add_argument(
        "--watch",
        action="store_true",
        help="only advance active PR or post-merge work; never select new work",
    )

    canary = sub.add_parser("shadow-canary", help="run repeated non-mutating canary cycles")
    canary.add_argument("--repo", required=True)
    canary.add_argument("--harness", required=True)
    canary.add_argument("--count", type=int, default=3)

    status = sub.add_parser("status", help="show current state and recent events")
    status.add_argument("--repo", required=True)
    status.add_argument("--limit", type=int, default=10)

    planning = sub.add_parser("planning-session", help="print the host-agent planning conversation handoff")
    planning.add_argument("--repo", required=True)
    planning.add_argument("--course-key", required=True)

    usage = sub.add_parser("usage", help="report or import model usage without transcripts")
    usage_action = usage.add_subparsers(dest="usage_action", required=True)
    usage_report = usage_action.add_parser("report", help="show provider-reported token usage")
    usage_report.add_argument("--repo")
    usage_report.add_argument("--since", help="ISO-8601 lower bound for usage records")
    usage_report.add_argument(
        "--summary",
        action="store_true",
        help="print a compact audit summary instead of the full JSON report",
    )
    usage_sync = usage_action.add_parser("sync-openclaw", help="import OpenClaw session usage metadata")
    usage_sync.add_argument("--repo", required=True)
    usage_sync.add_argument("--session-filter")
    usage_sync.add_argument("--openclaw-executable")
    usage_sync.add_argument("--session-limit", type=int)

    approve = sub.add_parser("approve", help="approve one exact supervised action")
    approve.add_argument("--repo", required=True)
    approve.add_argument("--action-id", required=True)
    approve.add_argument("--by", default="owner")

    reject = sub.add_parser("reject", help="reject one proposed supervised action")
    reject.add_argument("--repo", required=True)
    reject.add_argument("--action-id", required=True)
    reject.add_argument("--reason", default="rejected by owner")

    ack = sub.add_parser("ack", help="acknowledge one repeated attention notification")
    ack.add_argument("--repo", required=True)
    ack.add_argument("--fingerprint", required=True)
    ack.add_argument("--event-type")

    details = sub.add_parser("details", help="show one stored proposal or recent event")
    details.add_argument("--repo", required=True)
    details.add_argument("--action-id")
    details.add_argument("--event-id")

    recover = sub.add_parser("recover-pr", help="recover an exact action that opened a PR before a crash")
    recover.add_argument("--repo", required=True)
    recover.add_argument("--action-id", required=True)
    recover.add_argument("--pr", required=True, type=int)
    recover.add_argument("--send", action="store_true")

    orchestrate = sub.add_parser("orchestrate", help="inspect or dispatch the configured worker queue")
    orchestrate.add_argument(
        "action",
        choices=("status", "health", "preflight", "canary", "dispatch", "reconcile", "diagnostics", "unblock"),
    )
    orchestrate.add_argument("--repo", required=True)
    orchestrate.add_argument("--card", help="Workboard card ID for the unblock action")
    orchestrate.add_argument("--canary-id", default="manual")
    orchestrate.add_argument("--run", action="store_true", help="materialize and dispatch a runtime canary")
    orchestrate.add_argument("--check", action="store_true", help="check a previously dispatched runtime canary")
    orchestrate.add_argument("--send", action="store_true")

    runtime_install = sub.add_parser("runtime-install", help="plan or install runtime worker agents")
    runtime_install.add_argument("--orchestrator", required=True)
    runtime_install.add_argument("--workspace-root", required=True, type=Path)
    runtime_install.add_argument("--apply", action="store_true")

    worker_protocol = sub.add_parser(
        "worker-protocol", help="perform a portable claimed-worker lifecycle operation"
    )
    worker_protocol.add_argument("action", choices=("claim", "heartbeat", "complete", "block"))
    worker_protocol.add_argument("--repo", required=True)
    worker_protocol.add_argument("--orchestrator")
    worker_protocol.add_argument("--card")
    worker_protocol.add_argument("--agent-id")
    worker_protocol.add_argument("--owner-id", required=True)
    worker_protocol.add_argument("--token", required=True)
    worker_protocol.add_argument("--note", default="working")
    worker_protocol.add_argument("--summary")
    worker_protocol.add_argument("--proof-note")
    worker_protocol.add_argument("--proof-url")
    worker_protocol.add_argument("--reason")

    merge_gate = sub.add_parser(
        "merge-gate", help="evaluate or execute the deterministic Workboard autonomous merge gate"
    )
    merge_gate.add_argument("--repo", required=True)
    merge_gate.add_argument("--pr", required=True, type=int)
    merge_gate.add_argument("--final-card", required=True)
    merge_gate.add_argument("--merge", action="store_true")

    schedule = sub.add_parser("schedule", help="install or render a two-hour Captain schedule")
    schedule.add_argument("--repo", required=True)
    schedule.add_argument("--harness", required=True)
    schedule.add_argument("--kind", metavar="KIND", required=True, help="registered scheduler adapter kind")
    schedule.add_argument("--every", default="2h")
    schedule.add_argument("--cron", default="0 */2 * * *")
    schedule.add_argument("--openclaw-executable", default="openclaw")
    schedule.add_argument("--enable", action="store_true")
    schedule.add_argument("--live", action="store_true")
    schedule.add_argument("--watch", action="store_true")
    schedule.add_argument("--continue-run", action="store_true")

    runtime_schedule = sub.add_parser(
        "orchestration-schedule", help="install or render a frequent worker reconcile schedule"
    )
    runtime_schedule.add_argument("--repo", required=True)
    runtime_schedule.add_argument(
        "--kind", metavar="KIND", required=True, help="registered scheduler adapter kind"
    )
    runtime_schedule.add_argument("--every", default="5m")
    runtime_schedule.add_argument("--cron", default="*/5 * * * *")
    runtime_schedule.add_argument("--openclaw-executable", default="openclaw")
    runtime_schedule.add_argument("--enable", action="store_true")
    return parser


def _runtime(config: AppConfig, repo_name: str, harness_name: str) -> tuple[ControlPlaneEngine, DeepBaselineCollector]:
    repo = config.repo(repo_name)
    harness_config = config.harnesses.get(harness_name)
    if harness_config is None:
        raise KeyError(f"unknown harness: {harness_name}")
    state = StateStore(config.state_dir / "state.db")
    github = GhGitHubProvider(cwd=repo.local_path)
    harness = build_harness(harness_config)
    models = config.model_policy(harness_name, repo_profiles=repo.model_profiles)
    notifier = build_notifier(repo.notification)
    orchestrator = _orchestrator(config, repo_name)
    engine = ControlPlaneEngine(
        config,
        state,
        github,
        harness,
        notifier,
        models,
        orchestrator=orchestrator,
        usage_sync=_usage_synchronizer(config, repo, harness_config.kind),
    )
    return engine, DeepBaselineCollector(config, state, github, models, model_invoker=engine.run_model)


def _usage_synchronizer(
    config: AppConfig,
    repo: RepoConfig,
    harness_kind: str,
) -> UsageTelemetryAdapter | None:
    """Attach runtime telemetry without coupling the engine to OpenClaw."""
    if harness_kind != "openclaw":
        return None

    executable = _openclaw_executable_for_repo(config, repo)

    def synchronize(state: StateStore, target_repo: RepoConfig) -> dict[str, Any]:
        del target_repo
        return _sync_openclaw_sessions_for_portfolio(
            config,
            state,
            fallback_executable=executable,
        )

    return CallbackUsageTelemetryAdapter(synchronize)


def _default_direct_orchestrator_config(config: AppConfig, repo: RepoConfig) -> DirectOrchestratorConfig:
    """Build the board-free runtime used when a repo omits an orchestrator."""
    slug = repo.full_name.lower().replace("/", "-").replace(".", "-")
    workers = WorkerAssignments(
        **{
            role: f"captains-chair-{slug}-{role}"
            for role in (
                "captain",
                "coder",
                "reviewer",
                "tester",
                "ux_reviewer",
                "final_reviewer",
                "merger",
                "verifier",
            )
        }
    )
    return DirectOrchestratorConfig(
        database_path=config.state_dir / "orchestrators" / f"{slug}.db",
        board_prefix="captains-chair-direct",
        workers=workers,
    )


def _orchestrator_config(config: AppConfig, repo: RepoConfig) -> Any:
    if repo.orchestrator is None:
        return _default_direct_orchestrator_config(config, repo)
    return config.orchestrators[repo.orchestrator]


def _orchestrator(config: AppConfig, repo_name: str) -> WorkflowOrchestrator:
    repo = config.repo(repo_name)
    value = _orchestrator_config(config, repo)
    try:
        worktrees = WorktreeManager(config.state_dir / "worktrees")

        def cleanup_workspace(repo_config: RepoConfig, workspace: WorkspaceRef) -> bool:
            if workspace.kind != "worktree" or workspace.path is None:
                raise ValueError("only managed worktree workspaces may be cleaned")
            return worktrees.remove_path(repo_config, workspace.path)

        github = GhGitHubProvider(cwd=repo.local_path)
        return build_work_queue_orchestrator(
            value,
            workspace_cleanup=cleanup_workspace,
            completion_validator=GitHubCompletionValidator(github),
        )
    except (RuntimeAdapterUnavailable, RuntimeAdapterContractError) as exc:
        raise ValueError(str(exc)) from exc


def _board_id(config: AppConfig, repo_name: str) -> str:
    repo = config.repo(repo_name)
    value = _orchestrator_config(config, repo)
    return repo.orchestration_board or (f"{value.board_prefix}-{repo.full_name.replace('/', '-').lower()}")


def print_schedule_result(kind: str, spec: ScheduleSpec, executable: str) -> None:
    scheduler = build_scheduler(kind, executable)
    if kind not in {"cron", "systemd", "task-scheduler"}:
        installed = scheduler.install(spec)
        print(json.dumps(installed.__dict__, indent=2))
        return

    renderer = getattr(scheduler, "render", None)
    if not callable(renderer):
        raise RuntimeError(
            f"scheduler adapter {kind!r} does not provide render(); "
            "use an install-capable adapter or add a renderer"
        )
    rendered = renderer(spec)
    if kind == "cron":
        print(str(rendered))
    else:
        print(json.dumps(rendered, indent=2))


def _expected_worker_models(config: AppConfig, repo_name: str) -> dict[str, str]:
    repo = config.repo(repo_name)
    if repo.orchestrator is None:
        return {}
    orchestrator = config.orchestrators.get(repo.orchestrator)
    worker_models = getattr(orchestrator, "worker_models", None)
    if worker_models is None:
        return {}
    workers = getattr(orchestrator, "workers", None)
    expected: dict[str, str] = {}
    for role, model in worker_models.model_dump().items():
        role_name = str(role)
        model_name = str(model)
        expected[role_name] = model_name
        agent_id = getattr(workers, role_name, None)
        if agent_id:
            agent_name = str(agent_id)
            expected[agent_name] = model_name
            expected[agent_name.removeprefix("github-")] = model_name
    return expected


def _openclaw_session_limit(config: AppConfig, repo_name: str) -> int:
    repo = config.repo(repo_name)
    if repo.orchestrator is None:
        return DEFAULT_SESSION_LIMIT
    orchestrator = config.orchestrators.get(repo.orchestrator)
    if orchestrator is None:
        return DEFAULT_SESSION_LIMIT
    value = getattr(orchestrator, "session_limit", DEFAULT_SESSION_LIMIT)
    return value if isinstance(value, int) else DEFAULT_SESSION_LIMIT


def _openclaw_executable_for_repo(
    config: AppConfig,
    repo: RepoConfig,
    fallback: str = "openclaw",
) -> str:
    if repo.orchestrator:
        orchestrator = config.orchestrators.get(repo.orchestrator)
        if orchestrator is not None:
            return str(getattr(orchestrator, "executable", fallback))
    return fallback


def _sync_openclaw_sessions_for_portfolio(
    config: AppConfig,
    state: StateStore,
    *,
    fallback_executable: str,
) -> dict[str, Any]:
    """Import each managed repo before enforcing the account-wide usage guard."""
    reports = [
        sync_openclaw_sessions(
            state,
            repo=managed.full_name,
            executable=_openclaw_executable_for_repo(config, managed, fallback_executable),
            expected_models=_expected_worker_models(config, managed.full_name),
            session_limit=_openclaw_session_limit(config, managed.full_name),
        )
        for managed in config.repos
    ]
    if len(reports) == 1:
        report = reports[0]
        if report.get("session_limit_reached"):
            return {
                **report,
                "status": "degraded",
                "error": "OpenClaw session window is full; same-day usage may be incomplete",
            }
        return report
    window_incomplete = any(bool(item.get("session_limit_reached")) for item in reports)
    return {
        "status": "degraded" if window_incomplete else "ok",
        "repos": reports,
        "sessions_seen": sum(int(item.get("sessions_seen") or 0) for item in reports),
        "sessions_imported": sum(int(item.get("sessions_imported") or 0) for item in reports),
        "sessions_with_usage": sum(int(item.get("sessions_with_usage") or 0) for item in reports),
        "session_limit_reached": window_incomplete,
        **(
            {"error": "one or more OpenClaw session windows are full; same-day usage may be incomplete"}
            if window_incomplete
            else {}
        ),
    }


def _usage_guard(
    config: AppConfig,
    repo_name: str,
    state: StateStore,
    *,
    orchestrator_executable: str | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Sync metadata and decide whether a queue pass may start workers."""
    config.repo(repo_name)
    usage_sync: dict[str, Any] | None = None
    if orchestrator_executable:
        try:
            usage_sync = _sync_openclaw_sessions_for_portfolio(
                config,
                state,
                fallback_executable=orchestrator_executable,
            )
        except Exception as exc:
            # Telemetry must never prevent recovery, but a configured hard limit
            # fails closed when the latest usage cannot be reconciled.
            usage_sync = {"status": "degraded", "error": str(exc)[:1000]}
    state.prune_usage(config.usage.retention_days)
    since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    budget = dispatch_budget(
        # Worker dispatch shares account-wide token limits across all managed
        # repositories; per-repo filtering remains available in usage reports.
        state.usage_summary(since=since),
        config.usage,
    )
    if (
        usage_sync is not None
        and usage_sync.get("status") == "degraded"
        and (
            config.usage.daily_token_limit is not None
            or config.usage.model_daily_token_limits
            or config.usage.block_on_unknown
        )
    ):
        budget = {
            **budget,
            "allowed": False,
            "reason": "usage telemetry sync failed; new worker sessions are suppressed until it recovers",
        }
    return budget, usage_sync


def _send_event(state: StateStore, notifier: Notifier, event: EventRecord) -> EventRecord | None:
    """Send one event while retaining a durable failure event for scheduled callers."""
    try:
        notifier.send(event)
    except NotificationError as exc:
        return state.record_notification_failure(event, str(exc))
    return None


def _dispatch_exit_code(result: dict[str, Any]) -> int:
    if result.get("status") == "dispatch_suppressed":
        return 2
    model_health = result.get("model_health")
    if isinstance(model_health, dict) and cast(dict[str, object], model_health).get("status") == "degraded":
        return 2
    return 0


def _reconcile_exit_code(
    result: ReconcileResult,
    diagnostics: dict[str, Any],
    notification_failures: list[EventRecord],
) -> int:
    if (
        notification_failures
        or result.user_blockers
        or result.workspace_cleanup_failures
        or result.recovery_warnings
    ):
        return 2
    if str(diagnostics.get("status") or "").lower() == "degraded":
        return 2
    return _dispatch_exit_code(result.dispatch)


def _orchestration_preflight(
    config: AppConfig,
    repo: RepoConfig,
    board_id: str,
    orchestrator: Any,
    state: StateStore,
    *,
    executable: str | None,
) -> tuple[dict[str, Any], int]:
    """Check the runtime boundary without invoking models or dispatching workers."""
    adapter = orchestrator.adapter
    failures: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {
        "adapter_contract": {
            "status": "passed",
            "detail": "The queue and claimed-worker contract was validated while constructing the adapter.",
        }
    }

    worker_protocol = _preflight_worker_protocol(adapter)
    if worker_protocol is not None:
        checks["worker_protocol"] = worker_protocol
        if worker_protocol.get("status") == "failed":
            failures.append(str(worker_protocol.get("error") or "worker lifecycle helper is unavailable"))

    try:
        cards = adapter.list_cards(board_id)
        counts = {
            status.value: sum(1 for card in cards if card.status == status)
            for status in QueueStatus
            if any(card.status == status for card in cards)
        }
        checks["queue_read"] = {"status": "passed", "cards": len(cards), "counts": counts}
    except Exception as exc:
        cards = []
        detail = str(exc)[:2000]
        checks["queue_read"] = {"status": "failed", "error": detail}
        failures.append(f"Workboard queue could not be read: {detail}")

    health = worker_model_health(adapter)
    checks["worker_models"] = health
    if health.get("status") == "degraded":
        failures.append("configured worker model routes are not healthy")
    elif health.get("status") == "not_supported":
        warnings.append("runtime does not expose a worker model health check")

    try:
        diagnostics = _board_diagnostics(adapter, board_id)
        diagnostics_status = str(diagnostics.get("status") or "ok").lower()
        findings = _preflight_diagnostic_rows(diagnostics)
        finding_count = len(findings)
        checks["workboard_diagnostics"] = {
            "status": (
                "failed"
                if diagnostics_status == "degraded"
                else "warning"
                if finding_count
                else "passed"
            ),
            "finding_count": finding_count,
            "findings": findings,
        }
        if diagnostics_status == "degraded":
            failures.append("Workboard diagnostics reported degraded health")
        elif finding_count:
            warnings.append(f"Workboard diagnostics returned {finding_count} finding(s)")
    except Exception as exc:
        detail = str(exc)[:2000]
        checks["workboard_diagnostics"] = {"status": "failed", "error": detail}
        failures.append(f"Workboard diagnostics failed: {detail}")

    try:
        budget, usage_sync = _usage_guard(
            config,
            repo.full_name,
            state,
            orchestrator_executable=executable,
        )
        checks["usage_limits"] = budget
        checks["usage_sync"] = usage_sync or {"status": "not_requested"}
        if not budget.get("allowed", False):
            failures.append(str(budget.get("reason") or "usage guard denied worker dispatch"))
        elif isinstance(usage_sync, dict) and usage_sync.get("status") == "degraded":
            warnings.append("usage telemetry sync is degraded; configured token limits are not currently hard-blocking")
        if budget.get("daily_token_limit") is None and not budget.get("model_limits"):
            warnings.append("no daily token limit is configured")
        telemetry_gaps = int(budget.get("unknown_records") or 0)
        if telemetry_gaps:
            warnings.append(
                "usage telemetry is incomplete; missing tokens remain unknown "
                f"(unknown={budget.get('unknown_records', 0)})"
            )
    except Exception as exc:
        detail = str(exc)[:2000]
        checks["usage_limits"] = {"status": "failed", "error": detail}
        failures.append(f"usage preflight failed: {detail}")

    if repo.operation_mode == OperationMode.DISABLED:
        next_action = (
            "Captain is disabled; no worker canary or dispatch was started. Keep it paused until usage is explicitly resumed."
        )
    elif failures:
        next_action = "Resolve the listed preflight failures; no worker was dispatched."
    elif warnings:
        next_action = (
            "Review the listed warnings, then run the no-op Workboard worker canary; "
            "this preflight did not invoke a model or dispatch a worker."
        )
    else:
        next_action = (
            "Run the no-op Workboard worker canary; this preflight did not invoke a model or dispatch a worker."
        )
    disabled = repo.operation_mode == OperationMode.DISABLED
    health_status = "degraded" if failures else "ready_with_warnings" if warnings else "ready"
    payload = {
        "status": "disabled" if disabled else health_status,
        "health_status": health_status,
        "repo": repo.full_name,
        "board_id": board_id,
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "next_action": next_action,
    }
    return payload, 0 if disabled or not failures else 2


def _preflight_worker_protocol(adapter: Any) -> dict[str, Any] | None:
    """Verify the configured worker lifecycle helper without mutating state."""
    runtime_config = getattr(adapter, "config", None)
    command_value = getattr(runtime_config, "captains_chair_command", None)
    if not isinstance(command_value, (tuple, list)) or not command_value:
        return None
    values = cast(tuple[object, ...] | list[object], command_value)
    command = [str(item) for item in values]
    executable = command[0].strip()
    if not executable:
        return {"status": "failed", "error": "worker lifecycle helper command has no executable"}
    if shutil.which(executable) is None and not Path(executable).is_file():
        return {
            "status": "failed",
            "executable": executable,
            "error": f"worker lifecycle helper executable was not found: {executable}",
        }
    result = run_command([*command, "--help"], timeout=30)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[:1000]
        return {
            "status": "failed",
            "executable": executable,
            "error": f"worker lifecycle helper could not start: {detail or 'unknown error'}",
        }
    return {"status": "passed", "executable": executable}


def _preflight_diagnostic_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten Workboard diagnostics without echoing full card metadata or notes."""
    raw = payload.get("diagnostics")
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in cast(list[Any], raw):
        if not isinstance(item, dict):
            continue
        entry = cast(dict[str, Any], item)
        card = entry.get("card")
        card_row = cast(dict[str, Any], card) if isinstance(card, dict) else {}
        nested = entry.get("diagnostics")
        children: list[Any] = cast(list[Any], nested) if isinstance(nested, list) else [entry]
        for child in children:
            if not isinstance(child, dict):
                continue
            finding = cast(dict[str, Any], child)
            actions = finding.get("actions")
            action_labels = [
                str(cast(dict[str, Any], action).get("label") or cast(dict[str, Any], action).get("kind"))
                for action in cast(list[Any], actions)
                if isinstance(action, dict)
            ] if isinstance(actions, list) else []
            rows.append(
                {
                    "card_id": card_row.get("id") or finding.get("card_id"),
                    "card_title": card_row.get("title"),
                    "source_url": card_row.get("sourceUrl") or finding.get("source_url"),
                    "kind": finding.get("kind"),
                    "severity": finding.get("severity"),
                    "title": finding.get("title"),
                    "detail": str(finding.get("detail") or finding.get("reason") or "")[:500],
                    "actions": action_labels,
                }
            )
    return rows[:25]


def _board_diagnostics(adapter: Any, board_id: str) -> dict[str, Any]:
    """Prefer board-filtered diagnostics while keeping portable adapters compatible."""
    board_method = getattr(adapter, "diagnostics_for_board", None)
    if callable(board_method):
        value: object = board_method(board_id)
        if isinstance(value, dict):
            return cast(dict[str, Any], value)
    value = cast(object, adapter.diagnostics())
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def cmd_doctor(config: AppConfig) -> int:
    problems: list[str] = []
    for name, harness in config.harnesses.items():
        if shutil.which(harness.executable) is None and not Path(harness.executable).is_file():
            problems.append(f"harness {name} executable not found: {harness.executable}")
    if shutil.which("gh") is None:
        problems.append("GitHub CLI gh is not installed")
    else:
        auth = run_command(["gh", "auth", "status"], timeout=30)
        if auth.returncode:
            problems.append("GitHub CLI is not authenticated")
    for repo in config.repos:
        if not repo.local_path.is_dir():
            problems.append(f"repository path missing: {repo.full_name} -> {repo.local_path}")
        if not (repo.local_path / repo.planning_doc).is_file():
            problems.append(f"planning document missing: {repo.full_name} -> {repo.planning_doc}")
        if repo.require_project_manifest and not (repo.local_path / repo.project_manifest).is_file():
            problems.append(f"project manifest missing: {repo.full_name} -> {repo.project_manifest}")
    if problems:
        print(json.dumps({"status": "degraded", "problems": problems}, indent=2))
        return 2
    print(
        json.dumps(
            {"status": "ok", "repos": len(config.repos), "harnesses": list(config.harnesses)}, indent=2
        )
    )
    return 0


def _card_block_reason(card: Any) -> str:
    raw_metadata = getattr(card, "metadata", {})
    metadata = cast(dict[str, Any], raw_metadata) if isinstance(raw_metadata, dict) else {}
    raw_protocol = cast(object, metadata.get("workerProtocol"))
    protocol = cast(dict[str, Any], raw_protocol) if isinstance(raw_protocol, dict) else {}
    detail = protocol.get("detail")
    if isinstance(detail, str):
        return detail.strip()
    raw_comments = cast(object, metadata.get("comments"))
    if isinstance(raw_comments, list):
        comments = cast(list[object], raw_comments)
        for item in reversed(comments):
            if isinstance(item, dict):
                body = cast(dict[str, Any], item).get("body")
                if isinstance(body, str):
                    return body.strip()
    return ""


def _run_canary_live(
    config: AppConfig,
    args: argparse.Namespace,
    repo: RepoConfig,
    orchestrator: WorkflowOrchestrator,
    state: StateStore,
    executable: str | None,
    canary_board: str,
    spec: QueueCardSpec,
) -> int:
    with state.lease(repo.full_name, _cli_lease_owner("canary")):
        payload: dict[str, Any] = {
            "status": "preparing",
            "repo": repo.full_name,
            "board_id": canary_board,
        }
        budget, usage_sync = _usage_guard(
            config,
            args.repo,
            state,
            orchestrator_executable=executable,
        )
        payload["dispatch_budget"] = budget
        payload["usage_sync"] = usage_sync
        if not budget.get("allowed", False):
            payload.update(
                {
                    "status": "dispatch_suppressed",
                    "reason": budget.get("reason"),
                    "next_action": "Resolve usage telemetry or budget policy before dispatching the canary.",
                }
            )
            print(json.dumps(payload, indent=2, default=str))
            return 2
        model_health = worker_model_health(orchestrator.adapter)
        payload["model_health"] = model_health
        if model_health.get("status") == "degraded":
            payload.update(
                {
                    "status": "dispatch_suppressed",
                    "reason": "worker model health is degraded",
                    "next_action": "Repair worker model routes before dispatching the canary.",
                }
            )
            print(json.dumps(payload, indent=2, default=str))
            return 2
        orchestrator.adapter.ensure_board(
            canary_board,
            f"{repo.full_name} runtime canary",
            "Disposable CAPTAINS_CHAIR runtime validation cards; no repository changes are allowed.",
            repo.local_path,
        )
        card = orchestrator.adapter.create_card(canary_board, spec)
        payload["card"] = summarize_canary_card(card)
        existing = evaluate_canary_card(card, canary_id=args.canary_id)
        if existing.status == "passed":
            payload.update(
                {
                    "status": "already_passed",
                    "reason": existing.reason,
                    "next_action": "No new worker was dispatched; retain this proof as the canary result.",
                }
            )
            print(json.dumps(payload, indent=2, default=str))
            return 0
        if card.status in {QueueStatus.TODO, QueueStatus.REVIEW}:
            card = orchestrator.adapter.reclaim_card(
                card.id,
                status=QueueStatus.READY,
                reason="TECHNICAL_canary_promoted_for_dispatch",
            )
            payload["card"] = summarize_canary_card(card)
        dispatch = orchestrator.adapter.dispatch(canary_board)
        activity = any(
            isinstance(dispatch.get(key), list) and bool(dispatch[key])
            for key in ("started", "orchestrated", "promoted")
        ) or bool(dispatch.get("count"))
        if not activity:
            payload.update(
                {
                    "status": "dispatch_suppressed",
                    "dispatch": dispatch,
                    "reason": "Workboard dispatch returned no worker activity for the canary",
                    "next_action": (
                        "Inspect the OpenClaw Workboard worker registration and ready-card promotion; "
                        "the canary was not counted as dispatched."
                    ),
                }
            )
            print(json.dumps(payload, indent=2, default=str))
            return 2
        payload.update(
            {
                "status": "dispatched",
                "dispatch": dispatch,
                "next_action": (
                    f"After the worker completes, run orchestrate canary --check --repo {repo.full_name} "
                    f"--canary-id {args.canary_id} --card {card.id}."
                ),
            }
        )
        print(json.dumps(payload, indent=2, default=str))
        return _dispatch_exit_code(dispatch)


def _run_cli_lease_action(
    repo: str,
    operation: str,
    action: Callable[[], int],
) -> int:
    try:
        return action()
    except LeaseBusyError as exc:
        print(
            json.dumps(
                {
                    "status": "busy",
                    "repo": repo,
                    "operation": operation,
                    "reason": str(exc),
                    "next_action": "Another Captain process owns this repository lease; retry on the next scheduled pass.",
                },
                indent=2,
            )
        )
        return 0


def _run_orchestrate_unblock(
    state: StateStore,
    repo: RepoConfig,
    adapter: WorkQueueAdapter,
    board_id: str,
    card_id: str | None,
) -> int:
    with state.lease(repo.full_name, _cli_lease_owner("unblock")):
        if not card_id:
            raise ValueError("orchestrate unblock requires --card")
        target = next((card for card in adapter.list_cards(board_id) if card.id == card_id), None)
        if target is None:
            raise ValueError(f"Workboard card was not found: {card_id}")
        if target.status != QueueStatus.BLOCKED:
            raise ValueError(f"Workboard card is not blocked: {card_id}")
        reason = _card_block_reason(target)
        if not reason.upper().startswith(OWNER_RESUME_PREFIXES):
            raise ValueError(
                "only explicit owner blockers or OWNER_PAUSED cards may be resumed; "
                "technical failures must follow repair/recovery"
            )
        card = adapter.unblock_card(card_id)
        print(
            json.dumps(
                {
                    "status": "unblocked",
                    "card": card.model_dump(mode="json"),
                    "next_action": "Run orchestrate reconcile to evaluate dependencies and dispatch eligible work.",
                },
                indent=2,
                default=str,
            )
        )
        return 0


def _run_orchestrate_dispatch(
    config: AppConfig,
    args: argparse.Namespace,
    repo: RepoConfig,
    adapter: WorkQueueAdapter,
    state: StateStore,
    board_id: str,
    executable: str | None,
) -> int:
    with state.lease(repo.full_name, _cli_lease_owner("dispatch")):
        budget, usage_sync = _usage_guard(
            config,
            args.repo,
            state,
            orchestrator_executable=executable,
        )
        dispatch_result: dict[str, Any]
        if budget["allowed"]:
            model_health = worker_model_health(adapter)
            if model_health.get("status") not in {"ok", "not_supported"}:
                dispatch_result = {
                    "status": "dispatch_suppressed",
                    "reason": "worker model health is not valid; no new sessions were started",
                    "promoted": [],
                    "count": 0,
                    "model_health": model_health,
                }
            else:
                dispatch_result = {**adapter.dispatch(board_id), "model_health": model_health}
        else:
            dispatch_result = {
                "status": "dispatch_suppressed",
                "reason": budget["reason"],
                "promoted": [],
                "count": 0,
            }
        print(
            json.dumps(
                {**dispatch_result, "dispatch_budget": budget, "usage_sync": usage_sync},
                indent=2,
                default=str,
            )
        )
        return _dispatch_exit_code(dispatch_result)


def _run_orchestrate_reconcile_live(
    config: AppConfig,
    args: argparse.Namespace,
    repo: RepoConfig,
    orchestrator: WorkflowOrchestrator,
    state: StateStore,
    board_id: str,
    executable: str | None,
) -> int:
    with state.lease(repo.full_name, _cli_lease_owner("reconcile")):
        budget, usage_sync = _usage_guard(
            config,
            args.repo,
            state,
            orchestrator_executable=executable,
        )
        result = orchestrator.reconcile(
            repo,
            dispatch=bool(budget["allowed"]),
            dispatch_reason=None if budget["allowed"] else str(budget["reason"]),
        )
        cards = orchestrator.adapter.list_cards(board_id)
        try:
            diagnostics = _board_diagnostics(orchestrator.adapter, board_id)
        except Exception as exc:
            diagnostics = {"status": "degraded", "error": str(exc)}
        events = project_queue_events(
            state,
            repo,
            cards,
            protocol_retries=result.protocol_retries,
            technical_retries=result.retried,
            repairs_created=result.repairs_created,
            control_plane_recoveries=result.control_plane_recoveries,
            workspace_cleanup_failures=result.workspace_cleanup_failures,
            recovery_warnings=result.recovery_warnings,
            diagnostics=diagnostics,
        )
        if args.send:
            notifier = build_notifier(repo.notification)
            notification_failures: list[EventRecord] = []
            for event in tuple(events):
                failure = _send_event(state, notifier, event)
                if failure is not None:
                    notification_failures.append(failure)
            events.extend(notification_failures)
        else:
            notification_failures = []
        print(
            json.dumps(
                {
                    **result.__dict__,
                    "dispatch_budget": budget,
                    "usage_sync": usage_sync,
                    "diagnostics": diagnostics,
                    "events": [event.model_dump(mode="json") for event in events],
                    "notification_failures": [
                        event.model_dump(mode="json") for event in notification_failures
                    ],
                },
                indent=2,
                default=str,
            )
        )
        if notification_failures:
            return 2
        return _reconcile_exit_code(result, diagnostics, notification_failures)


def _queue_reconcile_failure_event(
    state: StateStore,
    repo: RepoConfig,
    board_id: str,
    exc: Exception,
) -> tuple[EventRecord, bool]:
    detail = str(exc)[:2000] or exc.__class__.__name__
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "repo": repo.full_name,
                "board_id": board_id,
                "exception": exc.__class__.__name__,
                "detail": detail,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    existing = next(
        (
            event
            for event in state.recent_events(repo.full_name, 200)
            if event.event_type == "QUEUE_DEGRADED" and event.fingerprint == fingerprint
        ),
        None,
    )
    if existing is not None:
        return existing, False
    state.transition(repo.full_name, RunState.DEGRADED)
    return (
        state.record_event(
            repo=repo.full_name,
            run_id=f"reconcile-{fingerprint[:12]}",
            state=RunState.DEGRADED,
            event_type="QUEUE_DEGRADED",
            summary="Queue reconciliation could not complete.",
            reason=detail,
            fingerprint=fingerprint,
            evidence={
                "board_id": board_id,
                "error": detail,
                "next_action": (
                    "Restore Workboard, GitHub, or runtime access and rerun the next scheduled reconciliation; "
                    "no new worker session was started."
                ),
            },
        ),
        True,
    )


def _run_orchestrate_reconcile(
    config: AppConfig,
    args: argparse.Namespace,
    repo: RepoConfig,
    orchestrator: WorkflowOrchestrator,
    state: StateStore,
    board_id: str,
    executable: str | None,
) -> int:
    try:
        return _run_orchestrate_reconcile_live(
            config,
            args,
            repo,
            orchestrator,
            state,
            board_id,
            executable,
        )
    except LeaseBusyError:
        raise
    except Exception as exc:
        event, created = _queue_reconcile_failure_event(state, repo, board_id, exc)
        notification_failure = None
        if args.send and created:
            notification_failure = _send_event(state, build_notifier(repo.notification), event)
        print(
            json.dumps(
                {
                    "status": "degraded",
                    "repo": repo.full_name,
                    "board_id": board_id,
                    "error": str(exc)[:2000],
                    "event": event.model_dump(mode="json"),
                    "notification_failure": (
                        notification_failure.model_dump(mode="json")
                        if notification_failure is not None
                        else None
                    ),
                    "notification_suppressed": bool(args.send and not created),
                    "next_action": event.evidence.get("next_action"),
                },
                indent=2,
                default=str,
            )
        )
        return 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "schema":
        write_json_schema(args.output)
        print(args.output)
        return 0
    try:
        config = load_config(args.config)
        config.state_dir.mkdir(parents=True, exist_ok=True)
        config.artifact_dir.mkdir(parents=True, exist_ok=True)
        if args.command == "doctor":
            return cmd_doctor(config)
        if args.command == "status":
            state = StateStore(config.state_dir / "state.db")
            events = state.recent_events(args.repo, args.limit)
            print(
                json.dumps(
                    {
                        "repo": args.repo,
                        "state": state.current_state(args.repo).value,
                        "events": [event.model_dump(mode="json") for event in events],
                    },
                    indent=2,
                    default=str,
                )
            )
            return 2 if state.current_state(args.repo) in {RunState.BLOCKED, RunState.DEGRADED} else 0
        if args.command == "planning-session":
            repo = config.repo(args.repo)
            course = CourseStore(repo.local_path).load(args.course_key)
            print(json.dumps(planning_session(course), indent=2, default=str))
            return 0
        if args.command == "usage":
            state = StateStore(config.state_dir / "state.db")
            if args.usage_action == "report":
                state.prune_usage(config.usage.retention_days)
                summary = state.usage_summary(repo=args.repo, since=args.since)
                report = build_usage_report(summary, config.usage)
                if args.summary:
                    print(usage_summary_text(report, repo=args.repo, since=args.since))
                else:
                    print(json.dumps(report, indent=2, default=str))
                return 0
            repo = config.repo(args.repo)
            executable = args.openclaw_executable
            if executable is None and config.orchestrators:
                executable = next(iter(config.orchestrators.values())).executable
            result = sync_openclaw_sessions(
                state,
                repo=repo.full_name,
                executable=executable or "openclaw",
                session_filter=args.session_filter,
                expected_models=_expected_worker_models(config, repo.full_name),
                session_limit=args.session_limit
                if args.session_limit is not None
                else _openclaw_session_limit(config, repo.full_name),
            )
            print(json.dumps(result, indent=2, default=str))
            return 0
        if args.command == "approve":
            config.repo(args.repo)
            state = StateStore(config.state_dir / "state.db")
            proposal = state.proposal(args.repo, args.action_id)
            if proposal is None or proposal["status"] != "proposed":
                raise ValueError("action ID is not a current stored proposal")
            state.approve(args.repo, args.action_id, args.by)
            print(json.dumps({"status": "approved", "repo": args.repo, "action_id": args.action_id}))
            return 0
        if args.command == "reject":
            config.repo(args.repo)
            state = StateStore(config.state_dir / "state.db")
            proposal = state.proposal(args.repo, args.action_id)
            if proposal is None or proposal["status"] != "proposed":
                raise ValueError("action ID is not a current stored proposal")
            state.set_proposal_status(args.repo, args.action_id, "rejected")
            print(
                json.dumps(
                    {
                        "status": "rejected",
                        "repo": args.repo,
                        "action_id": args.action_id,
                        "reason": args.reason,
                    }
                )
            )
            return 0
        if args.command == "ack":
            config.repo(args.repo)
            state = StateStore(config.state_dir / "state.db")
            count = state.acknowledge_attention(args.repo, args.fingerprint, args.event_type)
            print(json.dumps({"status": "acknowledged", "repo": args.repo, "count": count}))
            return 0
        if args.command == "details":
            config.repo(args.repo)
            state = StateStore(config.state_dir / "state.db")
            if bool(args.action_id) == bool(args.event_id):
                raise ValueError("pass exactly one of --action-id or --event-id")
            if args.action_id:
                proposal = state.proposal(args.repo, args.action_id)
                if proposal is None:
                    raise ValueError("proposal was not found")
                print(json.dumps(proposal, indent=2, default=str))
                return 0
            events = [
                event for event in state.recent_events(args.repo, 100) if event.event_id == args.event_id
            ]
            if not events:
                raise ValueError("event was not found in the recent event window")
            print(json.dumps(events[0].model_dump(mode="json"), indent=2, default=str))
            return 0
        if args.command == "recover-pr":
            repo = config.repo(args.repo)
            mode_block = _control_plane_mutation_block(
                repo,
                operation="recover-pr",
                mutation="PR recovery and state mutation were skipped",
                next_action="Set operation_mode to supervised or autonomous before recovering Captain work.",
            )
            if mode_block is not None:
                print(
                    json.dumps(mode_block, indent=2)
                )
                return 0
            state = StateStore(config.state_dir / "state.db")
            proposal = state.proposal(repo.full_name, args.action_id)
            if proposal is None:
                raise ValueError("action ID is not a stored proposal")
            proposal_status = str(proposal.get("status") or "")
            if proposal_status == "executed":
                active = state.active_work(repo.full_name)
                if (
                    active is not None
                    and active.get("action_id") == args.action_id
                    and int(active.get("pr_number") or 0) == args.pr
                ):
                    print(
                        json.dumps(
                            {
                                "status": "already_recovered",
                                "repo": repo.full_name,
                                "action_id": args.action_id,
                                "pr": args.pr,
                                "branch": active.get("branch"),
                                "head_sha": active.get("head_sha"),
                                "next_action": "Run current-head independent review after GitHub checks complete.",
                            },
                            indent=2,
                            default=str,
                        )
                    )
                    return 0
                raise ValueError("action ID was already executed and cannot be recovered again")
            if proposal_status != "proposed":
                raise ValueError(
                    f"action ID is not recoverable because its proposal status is {proposal_status!r}"
                )
            decision = PlanDecision.model_validate(proposal["decision"])
            github = GhGitHubProvider(cwd=repo.local_path)
            pr = github.pull_request(repo, args.pr)
            branch = str(pr.get("headRefName") or "")
            if str(pr.get("baseRefName") or "") != repo.default_branch:
                raise ValueError("recovery PR does not target the configured default branch")
            if not branch.startswith("captains_chair/"):
                raise ValueError("recovery PR is not on an CAPTAINS_CHAIR-owned branch")
            files_value = pr.get("files")
            file_rows = cast(list[Any], files_value) if isinstance(files_value, list) else []
            file_objects = [cast(dict[str, Any], item) for item in file_rows if isinstance(item, dict)]
            files = tuple(str(item.get("path")) for item in file_objects if item.get("path"))
            allowed = (
                {repo.planning_doc, repo.project_manifest}
                if decision.action == ActionKind.UPDATE_PLAN
                else set(files)
            )
            unexpected = sorted(set(files) - allowed)
            if unexpected:
                raise ValueError(f"recovery PR contains unexpected paths: {unexpected}")
            if (
                repo.require_project_manifest
                and decision.action == ActionKind.UPDATE_PLAN
                and repo.project_manifest not in files
            ):
                raise ValueError(f"recovery PR is missing required project manifest: {repo.project_manifest}")
            state.transition(repo.full_name, RunState.PR_OPEN)
            state.save_active_work(
                repo.full_name,
                action_id=args.action_id,
                pr_number=args.pr,
                branch=branch,
                head_sha=str(pr.get("headRefOid") or ""),
                status="pr_open",
                decision=decision.model_dump(mode="json"),
            )
            state.set_proposal_status(repo.full_name, args.action_id, "executed")
            state.consume_approval(repo.full_name, args.action_id)
            event = state.record_event(
                repo=repo.full_name,
                run_id=args.action_id[:16],
                state=RunState.PR_OPEN,
                event_type="PR_RECOVERED",
                summary=decision.summary,
                reason="The exact approved action opened a valid PR before cleanup failed; CAPTAINS_CHAIR recovered it without creating a duplicate.",
                fingerprint=args.action_id,
                evidence={
                    "next_action": "Run current-head independent review after GitHub checks complete.",
                    "links": [pr.get("url")],
                    "pr": args.pr,
                    "branch": branch,
                    "head_sha": pr.get("headRefOid"),
                    "files": files,
                },
            )
            if args.send:
                notification_failure = _send_event(
                    state, build_notifier(repo.notification), event
                )
            else:
                notification_failure = None
            print(json.dumps(event.model_dump(mode="json"), indent=2, default=str))
            return 2 if notification_failure is not None else 0
        if args.command == "orchestrate":
            repo = config.repo(args.repo)
            if (
                args.action in {"dispatch", "reconcile", "unblock"}
                or (args.action == "canary" and args.run)
            ):
                mode_block = _control_plane_mutation_block(
                    repo,
                    operation=f"orchestrate {args.action}",
                    mutation="no worker, model, or Workboard mutation was started",
                    next_action="Set operation_mode to supervised or autonomous before resuming worker orchestration.",
                )
                if mode_block is not None:
                    print(json.dumps(mode_block, indent=2))
                    return 0
            orchestrator = _orchestrator(config, args.repo)
            board_id = _board_id(config, args.repo)
            state = StateStore(config.state_dir / "state.db")
            orchestrator_config = config.orchestrators[repo.orchestrator] if repo.orchestrator else None
            executable = orchestrator_config.executable if orchestrator_config is not None else None
            if args.action == "canary":
                if args.run and args.check:
                    raise ValueError("orchestrate canary cannot use --run and --check together")
                canary_board = canary_board_id(repo)
                if args.check:
                    if not args.card:
                        raise ValueError("orchestrate canary --check requires --card")
                    target = next(
                        (card for card in orchestrator.adapter.list_cards(canary_board) if card.id == args.card),
                        None,
                    )
                    if target is None:
                        raise ValueError(f"canary card was not found: {args.card}")
                    result = evaluate_canary_card(target, canary_id=args.canary_id)
                    print(
                        json.dumps(
                            {
                                "status": result.status,
                                "repo": repo.full_name,
                                "board_id": canary_board,
                                "card": summarize_canary_card(target),
                                "reason": result.reason,
                            },
                            indent=2,
                            default=str,
                        )
                    )
                    return 0 if result.status == "passed" else 2

                spec = build_canary_spec(
                    repo,
                    canary_id=args.canary_id,
                    worker_id=orchestrator_config.workers.tester if orchestrator_config else "github-tester",
                    max_runtime_seconds=orchestrator_config.max_runtime_seconds if orchestrator_config else 3600,
                    max_retries=orchestrator_config.max_retries if orchestrator_config else 2,
                )
                if not args.run:
                    print(
                        json.dumps(
                            {
                                "status": "planned",
                                "repo": repo.full_name,
                                "board_id": canary_board,
                                "card_key": spec.key,
                                "worker": spec.agent_id,
                                "next_action": (
                                    "Rerun with --run after usage is approved; this plan-only phase does not create a card, "
                                    "invoke a model, or dispatch a worker."
                                ),
                            },
                            indent=2,
                        )
                    )
                    return 0

                return _run_cli_lease_action(
                    repo.full_name,
                    "canary",
                    lambda: _run_canary_live(
                        config,
                        args,
                        repo,
                        orchestrator,
                        state,
                        executable,
                        canary_board,
                        spec,
                    ),
                )
            if args.action == "health":
                health = worker_model_health(orchestrator.adapter)
                print(json.dumps(health, indent=2, default=str))
                return 2 if health.get("status") == "degraded" else 0
            if args.action == "preflight":
                payload, exit_code = _orchestration_preflight(
                    config,
                    repo,
                    board_id,
                    orchestrator,
                    state,
                    executable=executable,
                )
                print(json.dumps(payload, indent=2, default=str))
                return exit_code
            if args.action == "unblock":
                return _run_cli_lease_action(
                    repo.full_name,
                    "unblock",
                    lambda: _run_orchestrate_unblock(
                        state,
                        repo,
                        orchestrator.adapter,
                        board_id,
                        args.card,
                    ),
                )
            if args.action == "dispatch":
                return _run_cli_lease_action(
                    repo.full_name,
                    "dispatch",
                    lambda: _run_orchestrate_dispatch(
                        config,
                        args,
                        repo,
                        orchestrator.adapter,
                        state,
                        board_id,
                        executable,
                    ),
                )
            elif args.action == "reconcile":
                return _run_cli_lease_action(
                    repo.full_name,
                    "reconcile",
                    lambda: _run_orchestrate_reconcile(
                        config,
                        args,
                        repo,
                        orchestrator,
                        state,
                        board_id,
                        executable,
                    ),
                )
            elif args.action == "diagnostics":
                print(json.dumps(_board_diagnostics(orchestrator.adapter, board_id), indent=2, default=str))
            else:
                cards = orchestrator.adapter.list_cards(board_id)
                print(
                    json.dumps(
                        {
                            "board_id": board_id,
                            "cards": [card.model_dump(mode="json") for card in cards],
                        },
                        indent=2,
                        default=str,
                    )
                )
            return 0
        if args.command == "runtime-install":
            value = config.orchestrators.get(args.orchestrator)
            if value is None:
                raise KeyError(f"unknown orchestrator: {args.orchestrator}")
            if not isinstance(value, OpenClawWorkboardConfig):
                raise ValueError(f"runtime-install only supports openclaw_workboard, not {value.kind}")
            installer = OpenClawRuntimeInstaller(value)
            actions = (
                installer.install(args.workspace_root) if args.apply else installer.plan(args.workspace_root)
            )
            print(json.dumps([item.__dict__ for item in actions], indent=2))
            return 0
        if args.command == "worker-protocol":
            repo = config.repo(args.repo)
            if args.orchestrator is not None and repo.orchestrator != args.orchestrator:
                raise ValueError(
                    f"repository {repo.full_name} is configured for orchestrator "
                    f"{repo.orchestrator!r}, not {args.orchestrator!r}"
                )
            mode_block = _control_plane_mutation_block(
                repo,
                operation=f"worker-protocol {args.action}",
                mutation="Workboard lifecycle mutation was skipped",
                next_action="Set operation_mode to supervised or autonomous before changing a worker card.",
            )
            if mode_block is not None:
                print(
                    json.dumps(
                        {**mode_block, "action": args.action, "card": args.card},
                        indent=2,
                    )
                )
                return 0
            if args.action != "claim" and not args.card:
                raise ValueError(f"{args.action} requires --card")
            proof: dict[str, str] | None = None
            if args.action == "complete":
                if not args.summary or not args.proof_note:
                    raise ValueError("complete requires --summary and --proof-note")
                proof = {
                    "status": "passed",
                    "label": "CAPTAINS_CHAIR worker proof",
                    "note": args.proof_note,
                }
                if args.proof_url:
                    proof["url"] = args.proof_url
            elif args.action == "block" and not args.reason:
                raise ValueError("block requires --reason")
            configured = config.orchestrators.get(repo.orchestrator) if repo.orchestrator else None
            adapter = (
                OpenClawWorkboardAdapter(configured)
                if isinstance(configured, OpenClawWorkboardConfig)
                else _orchestrator(config, repo.full_name).adapter
            )
            lifecycle = cast(WorkerLifecycleAdapter, adapter)
            card: QueueCard
            if args.action == "claim":
                claim_card = getattr(adapter, "claim_card", None)
                claim_next = getattr(adapter, "claim_next_card", None)
                if args.card and callable(claim_card):
                    card = cast(
                        QueueCard,
                        claim_card(args.card, owner_id=args.owner_id, token=args.token),
                    )
                elif callable(claim_next):
                    claimed = cast(
                        QueueCard | None,
                        claim_next(
                            _board_id(config, repo.full_name),
                            owner_id=args.owner_id,
                            token=args.token,
                            agent_id=args.agent_id,
                        ),
                    )
                    if claimed is None:
                        print(json.dumps({"status": "idle", "repo": repo.full_name}, indent=2))
                        return 0
                    card = claimed
                else:
                    raise ValueError(
                        f"orchestrator {repo.orchestrator or 'direct'} does not expose portable claims"
                    )
            elif args.action == "heartbeat":
                card = lifecycle.heartbeat_card(
                    cast(str, args.card),
                    owner_id=args.owner_id,
                    token=args.token,
                    note=args.note,
                )
            elif args.action == "complete":
                assert proof is not None
                card = lifecycle.complete_claimed_card(
                    cast(str, args.card),
                    owner_id=args.owner_id,
                    token=args.token,
                    summary=args.summary,
                    proof=(proof,),
                )
            else:
                card = lifecycle.block_claimed_card(
                    cast(str, args.card),
                    owner_id=args.owner_id,
                    token=args.token,
                    reason=args.reason,
                )
            print(json.dumps(card.model_dump(mode="json"), indent=2, default=str))
            return 0
        if args.command == "merge-gate":
            repo = config.repo(args.repo)
            mode_block = (
                _control_plane_mutation_block(
                    repo,
                    operation="merge-gate",
                    mutation="GitHub merge was skipped",
                    next_action="Set operation_mode to autonomous before requesting an autonomous merge gate.",
                )
                if args.merge
                else None
            )
            if mode_block is not None:
                print(
                    json.dumps(mode_block, indent=2)
                )
                return 0
            orchestrator = _orchestrator(config, repo.full_name)
            cards = orchestrator.adapter.list_cards(_board_id(config, repo.full_name))
            final_card = next((card for card in cards if card.id == args.final_card), None)
            if final_card is None:
                raise ValueError(f"final review card was not found: {args.final_card}")
            github = GhGitHubProvider(cwd=repo.local_path)
            reviewed_head = final_review_head(final_card)
            gate = github.gate(repo, args.pr, review_head_sha=reviewed_head)
            policy = evaluate_workboard_merge(repo, final_card, gate)
            merged = False
            if args.merge and policy.allowed:
                github.merge(repo, args.pr)
                merged = True
            print(
                json.dumps(
                    {
                        "repo": repo.full_name,
                        "pr": args.pr,
                        "final_card": final_card.id,
                        "reviewed_head": reviewed_head,
                        "current_head": gate.head_sha,
                        "allowed": policy.allowed,
                        "requires_owner": policy.requires_owner,
                        "reason": policy.reason,
                        "merged": merged,
                    },
                    indent=2,
                )
            )
            return 0 if policy.allowed else 2
        if args.command == "schedule":
            repo = config.repo(args.repo)
            if args.harness not in config.harnesses:
                raise KeyError(f"unknown harness: {args.harness}")
            cycle_mode = "--live" if args.live else "--shadow"
            cycle_flags = ("--watch",) if args.watch else (("--continue-run",) if args.continue_run else ())
            spec = ScheduleSpec(
                name=("captains-chair-watch-" if args.watch else "captains-chair-") + repo.full_name.replace("/", "-").lower(),
                argv=(
                    "captains-chair",
                    "--config",
                    str(args.config.resolve()),
                    "cycle",
                    "--repo",
                    repo.full_name,
                    "--harness",
                    args.harness,
                    cycle_mode,
                    *cycle_flags,
                ),
                cwd=repo.local_path,
                every=args.every,
                cron=args.cron,
                enabled=args.enable,
            )
            print_schedule_result(args.kind, spec, args.openclaw_executable)
            return 0
        if args.command == "orchestration-schedule":
            repo = config.repo(args.repo)
            _board_id(config, repo.full_name)
            spec = ScheduleSpec(
                name="captains-chair-dispatch-" + repo.full_name.replace("/", "-").lower(),
                argv=(
                    "captains-chair",
                    "--config",
                    str(args.config.resolve()),
                    "orchestrate",
                    "reconcile",
                    "--repo",
                    repo.full_name,
                    "--send",
                ),
                cwd=repo.local_path,
                every=args.every,
                cron=args.cron,
                enabled=args.enable,
            )
            print_schedule_result(args.kind, spec, args.openclaw_executable)
            return 0
        repo = config.repo(args.repo)
        if args.command == "model-check" and repo.operation_mode == OperationMode.DISABLED:
            print(
                json.dumps(
                    {
                        "status": "disabled",
                        "repo": repo.full_name,
                        "reason": "repository Captain is disabled; model health check was skipped",
                        "next_action": "Set operation_mode to advisory, supervised, or autonomous before checking a model route.",
                    },
                    indent=2,
                )
            )
            return 0
        engine, collector = _runtime(config, args.repo, args.harness)
        if args.command == "model-check":
            prompt = (
                "This is a harness health check. Do not inspect or modify the repository. "
                'Return status "ok" and a short message naming the resolved model route.'
            )
            result = engine.run_model(
                repo,
                f"model-check:{time.time_ns()}",
                "model-health",
                prompt,
                models=config.model_policy(
                    args.harness,
                    repo_profiles=repo.model_profiles,
                ).for_role(args.role),
                output_model=HarnessHealth,
                cwd=repo.local_path,
                writable=False,
            )
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "resolved_model": result.resolved_model,
                        "attempts": [item.model_dump(mode="json") for item in result.attempts],
                        "response": result.output,
                    },
                    indent=2,
                )
            )
            return 0
        if args.command == "baseline":
            if repo.operation_mode == OperationMode.DISABLED:
                print(
                    json.dumps(
                        {
                            "status": "disabled",
                            "repo": repo.full_name,
                            "reason": "repository Captain is disabled; baseline collection and analysis were skipped",
                            "next_action": "Set operation_mode to advisory, supervised, or autonomous before running a baseline.",
                        },
                        indent=2,
                    )
                )
                return 0
            try:
                with engine.state.lease(repo.full_name, _cli_lease_owner("baseline")):
                    _, artifact = collector.collect(
                        repo,
                        harness=engine.harness if args.analyze else None,
                        analyze=args.analyze,
                        run_checks=args.run_checks,
                    )
            except LeaseBusyError as exc:
                print(
                    json.dumps(
                        {
                            "status": "busy",
                            "repo": repo.full_name,
                            "operation": "baseline",
                            "reason": str(exc),
                            "next_action": "Let the active baseline finish; the next scheduled or manual run can reuse its evidence.",
                        },
                        indent=2,
                    )
                )
                return 0
            except ModelCallSuppressedError as exc:
                result = engine.record_model_suppressed(repo, exc, notify=args.send)
                print(
                    json.dumps(
                        {
                            "status": "suppressed",
                            "repo": repo.full_name,
                            "summary": render_event(result.event),
                            "next_action": result.event.evidence.get("next_action"),
                        },
                        indent=2,
                    )
                )
                return result.exit_code
            event = engine.state.recent_events(repo.full_name, 1)[0]
            notification_failure = (
                _send_event(engine.state, engine.notifier, event) if args.send else None
            )
            print(
                json.dumps(
                    {
                        "status": "ready" if notification_failure is None else "degraded",
                        "artifact": str(artifact),
                        "summary": render_event(event),
                        "notification_failure": (
                            notification_failure.model_dump(mode="json")
                            if notification_failure is not None
                            else None
                        ),
                    },
                    indent=2,
                )
            )
            return 2 if notification_failure is not None else 0
        if args.command == "cycle":
            if args.watch:
                try:
                    result = engine.watch(repo, shadow=not args.live, execute=args.live)
                except LeaseBusyError:
                    print(
                        f"{repo.full_name.split('/', 1)[-1]} | Watch skipped\nStatus: another Captain run is active"
                    )
                    return 0
                if result is None:
                    print(
                        f"{repo.full_name.split('/', 1)[-1]} | Watch idle\nStatus: no active PR or post-merge work"
                    )
                    return 0
                print(render_event(result.event))
                return 0 if result.event.event_type in WATCH_WAITING_EVENTS else result.exit_code

            try:
                messages: list[str] = []
                result = engine.cycle(
                    repo,
                    shadow=not args.live,
                    execute=args.live,
                    force_replan=args.force_replan,
                )
                messages.append(render_event(result.event))
                if args.continue_run and args.live:
                    deadline = time.monotonic() + 30 * 60
                    for _ in range(6):
                        if result.event.event_type not in CONTINUATION_EVENTS or time.monotonic() >= deadline:
                            break
                        result = engine.cycle(repo, shadow=False, execute=True)
                        messages.append(render_event(result.event))
                print("\n\n".join(messages))
                return result.exit_code
            except LeaseBusyError as exc:
                print(
                    f"{repo.full_name.split('/', 1)[-1]} | Cycle skipped\n"
                    f"Status: another Captain run is active ({exc})\n"
                    "Next: the next scheduled pass will retry without starting duplicate work"
                )
                return 0
        if args.command == "shadow-canary":
            if repo.operation_mode == OperationMode.DISABLED:
                print(f"{repo.full_name} | Captain disabled | No model calls or Workboard work")
                return 0
            if not 1 <= args.count <= 10:
                raise ValueError("count must be between 1 and 10")
            codes = [engine.cycle(repo, shadow=True, execute=False).exit_code for _ in range(args.count)]
            return max(codes)
        raise ValueError(f"unsupported command: {args.command}")
    except Exception as exc:
        print(f"captains_chair failed: {exc}", file=sys.stderr)
        return 3


def _cli_lease_owner(operation: str) -> str:
    return f"cli:{operation}:{os.getpid()}:{uuid.uuid4().hex}"
