from __future__ import annotations

import hashlib
import json
import shlex
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, cast

from make_it_so.adapters import UsageTelemetryAdapter
from make_it_so.command import CommandRunner, require_success, run_command
from make_it_so.courses import (
    CourseError,
    CourseStore,
    eligible_work_packages,
    readiness_report,
    set_work_package_status,
)
from make_it_so.documents import assert_durable_document
from make_it_so.github import GitHubProvider, RepositorySnapshot
from make_it_so.harness import HarnessAdapter, HarnessExecutionError
from make_it_so.models import (
    ActionKind,
    ActionScope,
    AppConfig,
    CommentTriage,
    CompletionPolicy,
    Course,
    CourseStatus,
    EventRecord,
    FinalReview,
    FinalVerdict,
    IndependentReview,
    ModelPolicy,
    OperationMode,
    PlanDecision,
    RepoConfig,
    ReviewVerdict,
    RoleModels,
    RunState,
    UXReview,
    WorkerResult,
    WorkPackage,
    WorkPackageStatus,
)
from make_it_so.notifications import NotificationError, Notifier, requires_owner_attention
from make_it_so.orchestration import (
    BlockerKind,
    EnqueuedWorkflow,
    WorkflowOrchestrator,
    WorkspaceRef,
    classify_blocker,
)
from make_it_so.policy import (
    evaluate_action,
    evaluate_control_plane_completion,
    evaluate_merge,
    evaluate_owner_completion,
)
from make_it_so.prompting import load_prompt
from make_it_so.readiness import (
    ReadinessReviewDecision,
    apply_readiness_review,
    build_readiness_prompt,
)
from make_it_so.security import safe_changed_paths, scan_secrets
from make_it_so.state import StateStore
from make_it_so.usage import dispatch_budget
from make_it_so.worktrees import Worktree, WorktreeManager

LegacyUsageSynchronizer = Callable[[StateStore, RepoConfig], dict[str, Any]]
UsageSynchronizer = UsageTelemetryAdapter | LegacyUsageSynchronizer
_DIRECT_CONTROL_PLANE_ACTIONS = frozenset(
    {
        ActionKind.REPORT_ONLY,
        ActionKind.NO_ACTION,
        ActionKind.CREATE_ISSUE,
        ActionKind.UPDATE_ISSUE,
        ActionKind.LABEL_ISSUE,
        ActionKind.RETARGET_ISSUE,
        ActionKind.CLOSE_ISSUE,
    }
)
_COURSE_SCOPED_ACTIONS = frozenset(
    {
        ActionKind.IMPLEMENT,
        ActionKind.UPDATE_PLAN,
        ActionKind.CREATE_ISSUE,
        ActionKind.UPDATE_ISSUE,
        ActionKind.LABEL_ISSUE,
        ActionKind.RETARGET_ISSUE,
        ActionKind.CLOSE_ISSUE,
    }
)
_DIRECT_RETRYABLE_ACTIONS = frozenset(
    {
        ActionKind.UPDATE_ISSUE,
        ActionKind.LABEL_ISSUE,
        ActionKind.RETARGET_ISSUE,
        ActionKind.CLOSE_ISSUE,
    }
)
_DIRECT_EXECUTION_ATTEMPT_LIMIT = 2


@dataclass(frozen=True)
class CycleResult:
    event: EventRecord
    exit_code: int


@dataclass(frozen=True)
class PlanningDocument:
    text: str
    source: str
    fingerprint: str


class ModelCallSuppressedError(RuntimeError):
    """Raised before a direct model call when repository policy suppresses it."""

    def __init__(self, repo: str, role: str, decision: dict[str, Any]) -> None:
        self.repo = repo
        self.role = role
        self.decision = decision
        super().__init__(
            f"{role} model call suppressed for {repo}: "
            f"{decision.get('reason') or 'model usage policy denied the call'}"
        )


class WorkerBlockedError(RuntimeError):
    """Raised when a direct worker returns an explicit blocker instead of proof."""

    def __init__(self, reason: str) -> None:
        self.reason = reason.strip() or "TECHNICAL: worker returned an empty blocker"
        super().__init__(self.reason)


class ControlPlaneEngine:
    def __init__(
        self,
        config: AppConfig,
        state: StateStore,
        github: GitHubProvider,
        harness: HarnessAdapter,
        notifier: Notifier,
        models: ModelPolicy,
        orchestrator: WorkflowOrchestrator | None = None,
        runner: CommandRunner = run_command,
        usage_sync: UsageSynchronizer | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.github = github
        self.harness = harness
        self.notifier = notifier
        self.models = models
        self.orchestrator = orchestrator
        self.runner = runner
        self.usage_sync = usage_sync
        self.worktrees = WorktreeManager(config.state_dir / "worktrees", runner)

    def _synchronize_usage(self, repo: RepoConfig) -> dict[str, Any]:
        usage_sync = self.usage_sync
        if usage_sync is None:
            return {}
        synchronize = getattr(usage_sync, "synchronize", None)
        if callable(synchronize):
            return cast(dict[str, Any], synchronize(self.state, repo))
        return cast(LegacyUsageSynchronizer, usage_sync)(self.state, repo)

    def _runtime_name(self) -> str:
        value = getattr(getattr(self.harness, "config", None), "kind", None)
        return str(value).strip() if value else "unknown"

    def _models_for(
        self,
        repo: RepoConfig,
        role: str,
        *,
        course: Course | None = None,
        decision: PlanDecision | None = None,
        stage: str | None = None,
    ) -> RoleModels:
        """Resolve global, repo, course, package, and stage model layers."""
        try:
            selected = self.models.for_role(role)
        except ValueError:
            if role == "comment_adjudicator":
                selected = self.models.reviewer
            elif role == "readiness_reviewer":
                selected = self.models.planner
            else:
                selected = self.models.coder
        try:
            if course is None:
                courses = CourseStore(repo.local_path).list()
                course = next(
                    (item for item in courses if item.status == CourseStatus.ENGAGED),
                    None,
                )
        except CourseError:
            course = None
        package: WorkPackage | None = None
        if course is not None and decision is not None and decision.work_package_key:
            package = next(
                (item for item in course.work_packages if item.key == decision.work_package_key),
                None,
            )
        layers = [repo.model_profiles]
        if course is not None:
            layers.append(course.model_profiles)
        if package is not None:
            layers.append(package.model_profiles)
        for layer in layers:
            if role in layer:
                selected = layer[role]
        if stage:
            stage_keys = (f"stage:{stage}", stage)
            for layer in layers:
                for key in stage_keys:
                    if key in layer:
                        selected = layer[key]
        return selected

    def review_course_readiness(
        self,
        repo: RepoConfig,
        course_key: str,
        run_id: str,
    ) -> Course:
        """Run and persist a fresh, read-only review bound to course inputs."""
        store = CourseStore(repo.local_path)
        course = store.load(course_key)
        models = self._models_for(repo, "readiness_reviewer", course=course)
        source_evidence: dict[str, object] = {
            "github": self.github.readiness_evidence(repo),
            "runtime_policy": {
                "harness": self._runtime_name(),
                "operation_mode": repo.operation_mode.value,
                "completion_policy": repo.completion_policy.value,
                "allow_autonomous_merge": repo.allow_autonomous_merge,
                "deploy_is_merge_gate": repo.deploy_is_merge_gate,
                "schedule_enabled": repo.schedule_enabled,
                "daily_token_limit": self.config.usage.daily_token_limit,
                "model_daily_token_limits": self.config.usage.model_daily_token_limits,
                "block_on_unknown_usage": self.config.usage.block_on_unknown,
                "model_routes": {
                    role: self._models_for(repo, role, course=course).model_dump(mode="json")
                    for role in (
                        "baseline",
                        "planner",
                        "coder",
                        "reviewer",
                        "tester",
                        "ux_reviewer",
                        "final_reviewer",
                    )
                },
            },
            "control_plane_capabilities": {
                "fresh_branch_and_worktree": True,
                "pull_request_creation": True,
                "independent_review": True,
                "targeted_checks": True,
                "review_comment_adjudication": True,
                "final_review": True,
                "durable_stage_events": True,
                "per_stage_model_token_telemetry": True,
                "evidence_state_dir_configured": bool(self.config.state_dir),
            },
        }
        prompt = build_readiness_prompt(course, source_evidence)
        result = self.run_model(
            repo,
            run_id,
            "readiness_reviewer",
            prompt,
            models=models,
            output_model=ReadinessReviewDecision,
            cwd=repo.local_path,
            writable=False,
            course_key=course.key,
            stage="readiness_review",
        )
        decision = ReadinessReviewDecision.model_validate(result.output)
        updated = apply_readiness_review(
            course,
            decision,
            result,
            models,
            provider=self._runtime_name(),
            source_evidence=source_evidence,
        )
        status = (
            CourseStatus.AWAITING_APPROVAL
            if readiness_report(updated).ready
            else CourseStatus.READINESS_REVIEW
        )
        updated = updated.model_copy(update={"status": status})
        store.save(updated)
        return updated

    def _set_course_package_status(
        self,
        repo: RepoConfig,
        decision: PlanDecision,
        status: WorkPackageStatus,
    ) -> None:
        """Keep durable course state aligned with the worker workflow boundary."""
        if not decision.course_key or not decision.work_package_key:
            return
        store = CourseStore(repo.local_path)
        course = store.load(decision.course_key)
        if course.status != CourseStatus.ENGAGED:
            raise CourseError(f"course {course.key!r} is not engaged")
        package = next(
            (item for item in course.work_packages if item.key == decision.work_package_key),
            None,
        )
        if package is None:
            raise CourseError(
                f"course {course.key!r} has no work package {decision.work_package_key!r}"
            )
        if package.status != status:
            store.save(set_work_package_status(course, package.key, status))

    def _record_model_call(
        self,
        repo: str,
        run_id: str,
        role: str,
        result: Any,
        prompt: str,
        *,
        course_key: str | None = None,
        work_package_key: str | None = None,
        stage: str | None = None,
    ) -> None:
        """Persist model-cost telemetry without retaining prompt contents."""
        self.state.record_model_call(
            repo,
            run_id,
            role,
            result.resolved_model,
            [item.model_dump(mode="json") for item in result.attempts],
            prompt_fingerprint=_fingerprint(prompt),
            session_id=result.session_id,
            runtime=self._runtime_name(),
            course_key=course_key,
            work_package_key=work_package_key,
            stage=stage or role,
        )

    def run_model(
        self,
        repo: RepoConfig,
        run_id: str,
        role: str,
        prompt: str,
        *,
        models: RoleModels,
        output_model: type[Any],
        cwd: Path,
        writable: bool,
        course_key: str | None = None,
        work_package_key: str | None = None,
        stage: str | None = None,
    ) -> Any:
        if repo.operation_mode == OperationMode.DISABLED:
            raise ModelCallSuppressedError(
                repo.full_name,
                role,
                {
                    "allowed": False,
                    "reason": "repository Captain is disabled; model invocation was skipped",
                    "daily_token_limit": self.config.usage.daily_token_limit,
                },
            )
        if (
            self.config.usage.daily_token_limit is not None
            or self.config.usage.model_daily_token_limits
            or self.config.usage.block_on_unknown
        ):
            if self.usage_sync is not None:
                try:
                    sync_result = self._synchronize_usage(repo)
                except Exception as exc:
                    raise ModelCallSuppressedError(
                        repo.full_name,
                        role,
                        {
                            "allowed": False,
                            "reason": "usage telemetry sync failed; direct model call was suppressed",
                            "error": str(exc)[:1000],
                            "daily_token_limit": self.config.usage.daily_token_limit,
                        },
                    ) from exc
                if sync_result.get("status") == "degraded":
                    raise ModelCallSuppressedError(
                        repo.full_name,
                        role,
                        {
                            "allowed": False,
                            "reason": "usage telemetry sync was degraded; direct model call was suppressed",
                            "usage_sync": sync_result,
                            "daily_token_limit": self.config.usage.daily_token_limit,
                        },
                    )
            since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            decision = dispatch_budget(
                # Configured token limits protect the provider/account, not one
                # repository. Keep repo filtering for reports, but admit calls
                # against the global same-day ledger.
                self.state.usage_summary(since=since),
                self.config.usage,
            )
            if not decision.get("allowed", False):
                raise ModelCallSuppressedError(repo.full_name, role, decision)
        try:
            result = self.harness.run(
                prompt=prompt,
                models=models,
                role=role,
                output_model=output_model,
                cwd=cwd,
                writable=writable,
            )
        except HarnessExecutionError as exc:
            if exc.attempts:
                self.state.record_model_call(
                    repo.full_name,
                    run_id,
                    role,
                    "unresolved",
                    [item.model_dump(mode="json") for item in exc.attempts],
                    prompt=prompt,
                    session_id=exc.session_id,
                    runtime=self._runtime_name(),
                    course_key=course_key,
                    work_package_key=work_package_key,
                    stage=stage or role,
                )
            raise
        self._record_model_call(
            repo.full_name,
            run_id,
            role,
            result,
            prompt,
            course_key=course_key,
            work_package_key=work_package_key,
            stage=stage,
        )
        return result

    def _model_suppressed_result(
        self,
        repo: RepoConfig,
        run_id: str,
        error: ModelCallSuppressedError,
        *,
        notify: bool = True,
    ) -> CycleResult:
        decision = dict(error.decision)
        fingerprint = _fingerprint(
            {
                "repo": repo.full_name,
                "role": error.role,
                "decision": decision,
            }
        )
        event = self._emit(
            repo,
            run_id,
            RunState.DEGRADED,
            "MODEL_CALL_SUPPRESSED",
            f"{error.role} model call was suppressed",
            str(error),
            fingerprint,
            {
                "next_action": (
                    "Review `make_it_so usage report`, reconcile provider telemetry, or adjust the configured daily budget; "
                    "no model call was made."
                ),
                "role": error.role,
                "usage_budget": decision,
            },
            notify=notify,
        )
        return CycleResult(event, 2)

    def record_model_suppressed(
        self,
        repo: RepoConfig,
        error: ModelCallSuppressedError,
        *,
        notify: bool = True,
    ) -> CycleResult:
        """Persist a suppressed call from a command outside the cycle loop."""
        return self._model_suppressed_result(
            repo,
            f"model-suppressed:{uuid.uuid4()}",
            error,
            notify=notify,
        )

    def _course_context(self, repo: RepoConfig) -> tuple[Course | None, str | None]:
        """Return the one active course and fail closed while it is not engaged."""
        try:
            courses = [
                course
                for course in CourseStore(repo.local_path).list()
                if course.status != CourseStatus.COMPLETED
            ]
        except CourseError as exc:
            return None, f"course state is invalid: {str(exc)[:1000]}"
        if len(courses) > 1:
            keys = ", ".join(course.key for course in courses)
            return None, f"multiple active courses are present ({keys}); only one course may be engaged per repository"
        if not courses:
            if repo.require_engaged_course:
                return None, "an approved course is required before repository work can begin"
            return None, None
        course = courses[0]
        if course.status == CourseStatus.ENGAGED:
            return course, None
        if course.status == CourseStatus.PAUSED:
            return course, f"course {course.key!r} is paused"
        if course.status == CourseStatus.BLOCKED:
            return course, f"course {course.key!r} is blocked"
        return course, f"course {course.key!r} must pass readiness review and receive owner approval before work begins"

    def _course_blocked_result(
        self,
        repo: RepoConfig,
        run_id: str,
        course: Course | None,
        reason: str,
    ) -> CycleResult:
        current = course.status.value if course is not None else "invalid"
        event_type = "COURSE_PAUSED" if current == CourseStatus.PAUSED.value else "COURSE_APPROVAL_REQUIRED"
        fingerprint = _fingerprint({"course": course.model_dump(mode="json") if course else None, "reason": reason})
        event = self._emit(
            repo,
            run_id,
            RunState.BLOCKED,
            event_type,
            "Course work is waiting for its next human decision.",
            reason,
            fingerprint,
            {
                "course_key": course.key if course else None,
                "course_status": current,
                "next_action": (
                    "Complete readiness answers and approve the course in the dashboard."
                    if current not in {CourseStatus.PAUSED.value, CourseStatus.BLOCKED.value}
                    else "Resume the course or resolve its blocker in the dashboard."
                ),
            },
        )
        return CycleResult(event, 2)

    def watch(
        self, repo: RepoConfig, *, shadow: bool = False, execute: bool = True
    ) -> CycleResult | None:
        """Advance only active PR or post-merge work; never select new work."""
        run_id = str(uuid.uuid4())
        with self.state.lease(repo.full_name, run_id):
            if repo.operation_mode == OperationMode.DISABLED:
                return self._disabled_result(repo, run_id, "watch")
            if self.state.baseline(repo.full_name) is None:
                return None
            course, course_blocker = self._course_context(repo)
            if course_blocker is not None:
                return self._course_blocked_result(repo, run_id, course, course_blocker)
            self._sync_default_branch_if_clean(repo)
            snapshot = self.github.snapshot(repo)
            snapshot_fingerprint = _fingerprint(snapshot.as_dict())
            current_state = self.state.current_state(repo.full_name)
            if current_state == RunState.POST_MERGE_VERIFICATION:
                return self._verify_post_merge(repo, run_id, snapshot, snapshot_fingerprint)
            if current_state not in {RunState.PR_OPEN, RunState.REPAIRING, RunState.COMPLETION_READY}:
                return None
            active = self.state.active_work(repo.full_name)
            if active is None:
                return None
            try:
                return self._continue_active_work(
                    repo,
                    run_id,
                    active,
                    snapshot_fingerprint,
                    shadow=shadow,
                    execute=execute,
                )
            except ModelCallSuppressedError as exc:
                return self._model_suppressed_result(repo, run_id, exc)
            except WorkerBlockedError as exc:
                return self._worker_blocked_result(
                    repo,
                    run_id,
                    PlanDecision.model_validate(active["decision"]),
                    snapshot_fingerprint,
                    exc.reason,
                    {"active_pr": active.get("pr_number")},
                )

    def cycle(
        self,
        repo: RepoConfig,
        *,
        shadow: bool = True,
        execute: bool = False,
        force_replan: bool = False,
    ) -> CycleResult:
        run_id = str(uuid.uuid4())
        with self.state.lease(repo.full_name, run_id):
            if repo.operation_mode == OperationMode.DISABLED:
                return self._disabled_result(repo, run_id, "cycle")
            if self.state.baseline(repo.full_name) is None:
                event = self._emit(
                    repo,
                    run_id,
                    RunState.DEGRADED,
                    "BASELINE_REQUIRED",
                    "A deep baseline is required.",
                    "Normal planning cannot begin until a current baseline exists.",
                    "baseline-required",
                    {"next_action": f"Run make_it_so baseline --repo {repo.full_name} --analyze"},
                )
                return CycleResult(event, 2)

            course, course_blocker = self._course_context(repo)
            if course_blocker is not None:
                return self._course_blocked_result(repo, run_id, course, course_blocker)
            default_branch_synced = self._sync_default_branch_if_clean(repo)
            snapshot = self.github.snapshot(repo)
            snapshot_fingerprint = _fingerprint(snapshot.as_dict())
            repo_policy_fingerprint = _fingerprint(repo.model_dump(mode="json"))
            current_state = self.state.current_state(repo.full_name)
            operational_event = self.state.latest_operational_event(repo.full_name)
            effective_state = current_state
            if (
                current_state == RunState.DEGRADED
                and operational_event is not None
                and operational_event.state != RunState.DEGRADED
            ):
                effective_state = operational_event.state
            if effective_state == RunState.POST_MERGE_VERIFICATION:
                return self._verify_post_merge(repo, run_id, snapshot, snapshot_fingerprint)
            if effective_state in {
                RunState.PR_OPEN,
                RunState.REVIEWING,
                RunState.REPAIRING,
                RunState.COMPLETION_READY,
            }:
                active = self.state.active_work(repo.full_name)
                if active is not None and not force_replan:
                    try:
                        return self._continue_active_work(
                            repo,
                            run_id,
                            active,
                            snapshot_fingerprint,
                            shadow=shadow,
                            execute=execute,
                        )
                    except ModelCallSuppressedError as exc:
                        return self._model_suppressed_result(repo, run_id, exc)
                    except WorkerBlockedError as exc:
                        return self._worker_blocked_result(
                            repo,
                            run_id,
                            PlanDecision.model_validate(active["decision"]),
                            snapshot_fingerprint,
                            exc.reason,
                            {"active_pr": active.get("pr_number")},
                        )
            if effective_state == RunState.BLOCKED and not force_replan:
                recent_event = self.state.latest_operational_event(repo.full_name)
                recent = [recent_event] if recent_event is not None else []
                recoverable = self._recoverable_autonomous_decision(repo, recent[0]) if recent else None
                if recoverable is not None and recent:
                    resumed = self._resume_autonomous_proposal(
                        repo,
                        run_id,
                        recoverable,
                        recent[0],
                        snapshot_fingerprint,
                    )
                    if resumed is not None:
                        return resumed
                    self.state.transition(repo.full_name, RunState.PLANNING)
                elif self.state.approved_proposal(repo.full_name) is None and recent and requires_owner_attention(
                    recent[0].event_type, recent[0].evidence
                ):
                    event = self._emit(
                        repo,
                        run_id,
                        RunState.BLOCKED,
                        "ATTENTION_REQUIRED",
                        recent[0].summary,
                        recent[0].reason,
                        recent[0].fingerprint,
                        {
                            **recent[0].evidence,
                            "next_action": recent[0].evidence.get("next_action")
                            or "Owner input is still required before this can continue.",
                            "original_event": recent[0].event_type,
                        },
                    )
                    return CycleResult(event, 2)
            stored = self.state.approved_proposal(repo.full_name)
            if stored is not None:
                decision = PlanDecision.model_validate(stored["decision"])
                approval_fingerprint = _approval_fingerprint(snapshot)
                if stored["snapshot_fingerprint"] not in {
                    snapshot_fingerprint,
                    approval_fingerprint,
                }:
                    self.state.set_proposal_status(repo.full_name, stored["action_id"], "stale")
                    event = self._emit(
                        repo,
                        run_id,
                        RunState.BLOCKED,
                        "APPROVAL_STALE",
                        "The approved action no longer matches live repository evidence.",
                        "GitHub state changed after the proposal was approved.",
                        snapshot_fingerprint,
                        {"next_action": "Run a fresh planning cycle and approve its new action ID."},
                    )
                    return CycleResult(event, 2)
                action_id = str(stored["action_id"])
                self.state.transition(repo.full_name, RunState.PLANNING)
                policy = evaluate_action(repo, decision, execute=execute, shadow=shadow, approved=True)
                if policy.allowed:
                    approved_fingerprint = _fingerprint(
                        {
                            "snapshot": snapshot_fingerprint,
                            "action": decision.action.value,
                            "scope": decision.scope.value,
                        }
                    )
                    if self.orchestrator is not None and decision.action not in _DIRECT_CONTROL_PLANE_ACTIONS:
                        return self._queue_approved_workflow(
                            repo,
                            run_id,
                            decision,
                            action_id,
                            approved_fingerprint,
                            consume_approval=True,
                            model_evidence={"approved_action_id": action_id},
                        )
                    try:
                        result = self._execute(
                            repo,
                            run_id,
                            decision,
                            approved_fingerprint,
                            {"approved_action_id": action_id},
                        )
                        self.state.consume_approval(repo.full_name, action_id)
                        self.state.set_proposal_status(repo.full_name, action_id, "executed")
                        return result
                    except WorkerBlockedError as exc:
                        self.state.set_proposal_status(repo.full_name, action_id, "blocked")
                        return self._worker_blocked_result(
                            repo,
                            run_id,
                            decision,
                            snapshot_fingerprint,
                            exc.reason,
                            {"approved_action_id": action_id},
                        )
                    except Exception:
                        self.state.set_proposal_status(repo.full_name, action_id, "failed")
                        raise
            try:
                planning_document = self._planning_document(repo, default_branch_synced)
            except Exception as exc:
                fingerprint = _fingerprint(
                    {
                        "snapshot": snapshot_fingerprint,
                        "repo_policy": repo_policy_fingerprint,
                        "error": str(exc),
                    }
                )
                event = self._emit(
                    repo,
                    run_id,
                    RunState.DEGRADED,
                    "PLANNING_CONTEXT_UNAVAILABLE",
                    "The Captain could not obtain a trustworthy default-branch planning document.",
                    str(exc)[:2000],
                    fingerprint,
                    {
                        "next_action": "Restore the repository remote/default branch, then rerun the Captain cycle; no model call was made.",
                        "snapshot_fingerprint": snapshot_fingerprint,
                        "repo_policy_fingerprint": repo_policy_fingerprint,
                    },
                )
                return CycleResult(event, 2)

            recent_event = self.state.latest_operational_event(repo.full_name)
            recent = [recent_event] if recent_event is not None else []
            active_workflow_count: int | None = None
            if self.orchestrator is not None:
                try:
                    active_workflow_count = self.orchestrator.active_workflow_count(repo)
                except Exception as exc:
                    fingerprint = _fingerprint(
                        {
                            "snapshot": snapshot_fingerprint,
                            "repo_policy": repo_policy_fingerprint,
                            "reason": str(exc),
                        }
                    )
                    event = self._emit(
                        repo,
                        run_id,
                        RunState.DEGRADED,
                        "WORKBOARD_CAPACITY_UNKNOWN",
                        "Workboard capacity could not be read.",
                        str(exc)[:2000],
                        fingerprint,
                        {
                            "next_action": "Restore Workboard access and rerun reconciliation; no new worker workflow was started.",
                            "error": str(exc)[:2000],
                            "snapshot_fingerprint": snapshot_fingerprint,
                            "repo_policy_fingerprint": repo_policy_fingerprint,
                        },
                    )
                    return CycleResult(event, 2)
            plan_input_fingerprint = _fingerprint(
                {
                    "snapshot": snapshot_fingerprint,
                    "repo_policy": repo_policy_fingerprint,
                    "active_workflow_count": active_workflow_count,
                    "planning_document": planning_document.fingerprint,
                    "course": course.model_dump(mode="json") if course is not None else None,
                }
            )
            if not force_replan and recent:
                direct_retry = self._retry_direct_action(
                    repo,
                    run_id,
                    recent[0],
                    snapshot_fingerprint,
                    plan_input_fingerprint,
                    execute=execute,
                    shadow=shadow,
                )
                if direct_retry is not None:
                    return direct_retry
            if (
                not force_replan
                and recent
                and recent[0].event_type == "STALLED"
                and recent[0].evidence.get("snapshot_fingerprint") == snapshot_fingerprint
                and recent[0].evidence.get("repo_policy_fingerprint")
                == repo_policy_fingerprint
                and recent[0].evidence.get("plan_input_fingerprint") == plan_input_fingerprint
            ):
                return CycleResult(recent[0], 2)
            failed_plan_inputs_unchanged = bool(
                recent
                and (
                    recent[0].evidence.get("plan_input_fingerprint") == plan_input_fingerprint
                    or (
                        "plan_input_fingerprint" not in recent[0].evidence
                        and recent[0].evidence.get("snapshot_fingerprint") == snapshot_fingerprint
                        and recent[0].evidence.get("repo_policy_fingerprint")
                        == repo_policy_fingerprint
                    )
                )
            )
            if (
                not force_replan
                and recent
                and recent[0].event_type
                in {"EXECUTION_FAILED", "PLANNING_FAILED", "WORKFLOW_QUEUE_FAILED"}
                and failed_plan_inputs_unchanged
            ):
                event = self._emit(
                    repo,
                    run_id,
                    RunState.DEGRADED,
                    "STALLED",
                    recent[0].summary,
                    "The previous Captain attempt failed and the evidence is unchanged; planning and retry are suppressed until evidence changes or force-replan is requested.",
                    plan_input_fingerprint,
                    {
                        **recent[0].evidence,
                        "next_action": recent[0].evidence.get("next_action")
                        or "Change the underlying evidence or run one forced replan.",
                        "snapshot_fingerprint": snapshot_fingerprint,
                        "repo_policy_fingerprint": repo_policy_fingerprint,
                        "plan_input_fingerprint": plan_input_fingerprint,
                    },
                )
                return CycleResult(event, 2)
            if (
                not force_replan
                and recent
                and recent[0].event_type
                in {
                    "ACTION_PROPOSED",
                    "APPROVAL_REQUIRED",
                    "ACTION_BLOCKED",
                    "CONTROL_PLANE_MAINTENANCE_REQUIRED",
                    "WORKFLOW_CAPACITY_WAIT",
                }
                and recent[0].evidence.get("plan_input_fingerprint") == plan_input_fingerprint
            ):
                if recent[0].event_type == "WORKFLOW_CAPACITY_WAIT":
                    return CycleResult(recent[0], 0)
                event = self._emit(
                    repo,
                    run_id,
                    RunState.DEGRADED,
                    "STALLED",
                    recent[0].summary,
                    "The planner inputs are unchanged and the previous decision has not produced a state change; planning is suppressed until evidence changes or force-replan is requested.",
                    plan_input_fingerprint,
                    {
                        **recent[0].evidence,
                        "next_action": recent[0].evidence.get("next_action")
                        or "Change repository evidence or run one forced replan.",
                        "snapshot_fingerprint": snapshot_fingerprint,
                        "repo_policy_fingerprint": repo_policy_fingerprint,
                        "plan_input_fingerprint": plan_input_fingerprint,
                    },
                )
                return CycleResult(event, 2)
            self.state.transition(repo.full_name, RunState.PLANNING)
            try:
                decision, model_evidence = self._plan(
                    repo,
                    run_id,
                    snapshot,
                    planning_document=planning_document,
                    course=course,
                )
            except ModelCallSuppressedError as exc:
                return self._model_suppressed_result(repo, run_id, exc)
            except Exception as exc:
                event = self._emit(
                    repo,
                    run_id,
                    RunState.DEGRADED,
                    "PLANNING_FAILED",
                    "The Captain planner failed before selecting a work item.",
                    str(exc)[:2000],
                    plan_input_fingerprint,
                    {
                        "next_action": "Inspect the provider/model failure; unchanged evidence suppresses another planner call until force-replan or a state change.",
                        "snapshot_fingerprint": snapshot_fingerprint,
                        "repo_policy_fingerprint": repo_policy_fingerprint,
                        "plan_input_fingerprint": plan_input_fingerprint,
                    },
                )
                return CycleResult(event, 3)
            action_id = _fingerprint(
                {
                    "snapshot": snapshot_fingerprint,
                    "decision": decision.model_dump(mode="json"),
                }
            )
            approval_fingerprint = _approval_fingerprint(snapshot)
            cycle_fingerprint = _fingerprint(
                {
                    "snapshot": snapshot_fingerprint,
                    "repo_policy": repo.model_dump(mode="json"),
                    "plan_input": plan_input_fingerprint,
                    "action": decision.action.value,
                    "scope": decision.scope.value,
                    "target_pr": decision.target_pr,
                    "target_issue": decision.target_issue,
                    "requires_owner_approval": decision.requires_owner_approval,
                    "owner_blocker": decision.owner_blocker,
                    "active_workflow_count": active_workflow_count,
                }
            )

            if not force_replan and self.state.repeated_fingerprint(repo.full_name, cycle_fingerprint) >= 1:
                event = self._emit(
                    repo,
                    run_id,
                    RunState.DEGRADED,
                    "STALLED",
                    decision.summary,
                    "The same evidence and decision repeated without a state-changing result; further model calls are suppressed.",
                    cycle_fingerprint,
                    {
                        "next_action": decision.reason,
                        "decision": decision.model_dump(mode="json"),
                        "snapshot_fingerprint": snapshot_fingerprint,
                        "repo_policy_fingerprint": repo_policy_fingerprint,
                        "plan_input_fingerprint": plan_input_fingerprint,
                    },
                )
                return CycleResult(event, 2)

            if decision.scope == ActionScope.CONTROL_PLANE:
                event = self._emit(
                    repo,
                    run_id,
                    RunState.DEGRADED,
                    "CONTROL_PLANE_MAINTENANCE_REQUIRED",
                    decision.summary,
                    decision.reason,
                    cycle_fingerprint,
                    {
                        "next_action": decision.summary,
                        "decision": decision.model_dump(mode="json"),
                        "plan_input_fingerprint": plan_input_fingerprint,
                        **model_evidence,
                    },
                )
                return CycleResult(event, 2)

            if course is not None and decision.action in _COURSE_SCOPED_ACTIONS:
                eligible = {item.key for item in eligible_work_packages(course)}
                if decision.course_key != course.key or decision.work_package_key not in eligible:
                    event = self._emit(
                        repo,
                        run_id,
                        RunState.BLOCKED,
                        "COURSE_WORK_PACKAGE_REQUIRED",
                        decision.summary,
                        "The planner selected course-scoped work without an eligible work-package identity.",
                        cycle_fingerprint,
                        {
                            "course_key": course.key,
                            "selected_course_key": decision.course_key,
                            "selected_work_package_key": decision.work_package_key,
                            "eligible_work_packages": sorted(eligible),
                            "next_action": "Replan against the eligible course work packages or update the course charter.",
                            "decision": decision.model_dump(mode="json"),
                            **model_evidence,
                        },
                    )
                    return CycleResult(event, 2)

            self.state.save_proposal(
                repo.full_name,
                action_id,
                approval_fingerprint,
                decision.model_dump(mode="json"),
            )

            approved = self.state.is_approved(repo.full_name, action_id)
            policy = evaluate_action(repo, decision, execute=execute, shadow=shadow, approved=approved)
            if shadow or repo.operation_mode.value == "advisory":
                self.state.transition(repo.full_name, RunState.READY)
                event = self._emit(
                    repo,
                    run_id,
                    RunState.READY,
                    "ACTION_PROPOSED",
                    decision.summary,
                    decision.reason,
                    cycle_fingerprint,
                    {
                        "next_action": _next_action(decision, policy.reason),
                        "decision": decision.model_dump(mode="json"),
                        "action_id": action_id,
                        "approval_fingerprint": approval_fingerprint,
                        "plan_input_fingerprint": plan_input_fingerprint,
                        **model_evidence,
                    },
                )
                return CycleResult(event, 0)
            if not policy.allowed:
                target = RunState.BLOCKED if policy.requires_owner else RunState.DEGRADED
                invalid_planner_approval = (
                    decision.requires_owner_approval and decision.owner_blocker is None
                )
                event = self._emit(
                    repo,
                    run_id,
                    target,
                    "APPROVAL_REQUIRED"
                    if policy.requires_owner
                    else "PLANNER_APPROVAL_INVALID"
                    if invalid_planner_approval
                    else "ACTION_BLOCKED",
                    decision.summary,
                    policy.reason,
                    cycle_fingerprint,
                    {
                        "next_action": (
                            f"Approve action {action_id} and rerun the live cycle."
                            if policy.requires_owner
                            else "Allow the next autonomous cycle to replan this decision."
                            if invalid_planner_approval
                            else decision.reason
                        ),
                        "decision": decision.model_dump(mode="json"),
                        "action_id": action_id,
                        "approval_fingerprint": approval_fingerprint,
                        "plan_input_fingerprint": plan_input_fingerprint,
                        **model_evidence,
                    },
                )
                return CycleResult(event, 2)

            if self.orchestrator is not None and decision.action not in _DIRECT_CONTROL_PLANE_ACTIONS:
                if self.orchestrator.has_active_workflow(repo, decision):
                    return self._workboard_workflow_already_queued(
                        repo, run_id, decision, cycle_fingerprint, model_evidence
                    )
                if (
                    active_workflow_count is not None
                    and active_workflow_count >= repo.max_parallel_prs
                ):
                    self.state.transition(repo.full_name, RunState.READY)
                    event = self._emit(
                        repo,
                        run_id,
                        RunState.READY,
                        "WORKFLOW_CAPACITY_WAIT",
                        decision.summary,
                        f"{active_workflow_count} active Workboard workflow(s) already consume the configured limit of {repo.max_parallel_prs}.",
                        cycle_fingerprint,
                        {
                            "next_action": "Wait for an active workflow to complete; fully owner-blocked workflows remain isolated and do not consume this slot.",
                            "active_workflow_count": active_workflow_count,
                            "max_parallel_prs": repo.max_parallel_prs,
                            "decision": decision.model_dump(mode="json"),
                            "plan_input_fingerprint": plan_input_fingerprint,
                            **model_evidence,
                        },
                    )
                    return CycleResult(event, 0)
                try:
                    queued = self._queue_workflow(repo, decision, action_id)
                except Exception as exc:
                    self.state.set_proposal_status(repo.full_name, action_id, "failed")
                    event = self._emit(
                        repo,
                        run_id,
                        RunState.DEGRADED,
                        "WORKFLOW_QUEUE_FAILED",
                        decision.summary,
                        str(exc)[:2000],
                        cycle_fingerprint,
                        {
                            "next_action": "Restore Workboard access or repair the isolated workspace; unchanged evidence suppresses another planner call.",
                            "action_id": action_id,
                            "decision": decision.model_dump(mode="json"),
                            "snapshot_fingerprint": snapshot_fingerprint,
                            "repo_policy_fingerprint": repo_policy_fingerprint,
                            "plan_input_fingerprint": plan_input_fingerprint,
                        },
                    )
                    return CycleResult(event, 3)
                self.state.transition(repo.full_name, RunState.READY)
                self.state.set_proposal_status(repo.full_name, action_id, "queued")
                if approved:
                    self.state.consume_approval(repo.full_name, action_id)
                event = self._emit(
                    repo,
                    run_id,
                    RunState.READY,
                    "WORKFLOW_QUEUED",
                    decision.summary,
                    "The policy-approved action was decomposed into role-separated queue cards for configured workers.",
                    cycle_fingerprint,
                    {
                        "next_action": "The configured queue runtime will claim the first dependency-ready card.",
                        "action_id": action_id,
                        "board_id": queued.board_id,
                        "root_card_id": queued.root_card_id,
                        "stage_cards": queued.stage_cards,
                        "decision": decision.model_dump(mode="json"),
                        **model_evidence,
                    },
                )
                return CycleResult(event, 0)

            try:
                result = self._execute(repo, run_id, decision, cycle_fingerprint, model_evidence)
                self.state.set_proposal_status(repo.full_name, action_id, "executed")
                if approved:
                    self.state.consume_approval(repo.full_name, action_id)
                return result
            except ModelCallSuppressedError as exc:
                return self._model_suppressed_result(repo, run_id, exc)
            except WorkerBlockedError as exc:
                return self._worker_blocked_result(
                    repo,
                    run_id,
                    decision,
                    cycle_fingerprint,
                    exc.reason,
                    {
                        **model_evidence,
                        "proposal_action_id": action_id,
                        "snapshot_fingerprint": snapshot_fingerprint,
                        "repo_policy_fingerprint": repo_policy_fingerprint,
                        "plan_input_fingerprint": plan_input_fingerprint,
                    },
                )
            except Exception as exc:
                event = self._emit(
                    repo,
                    run_id,
                    RunState.DEGRADED,
                    "EXECUTION_FAILED",
                    decision.summary,
                    str(exc)[:2000],
                    cycle_fingerprint,
                    {
                        "next_action": (
                            "The Captain will retry this deterministic issue action automatically on the next cycle."
                            if decision.action in _DIRECT_RETRYABLE_ACTIONS
                            else "Inspect the failure and retry only after the underlying evidence changes."
                        ),
                        "action_id": action_id,
                        "execution_attempt": 1,
                        "automatic_retry": decision.action in _DIRECT_RETRYABLE_ACTIONS,
                        "decision": decision.model_dump(mode="json"),
                        "snapshot_fingerprint": snapshot_fingerprint,
                        "repo_policy_fingerprint": repo_policy_fingerprint,
                        "plan_input_fingerprint": plan_input_fingerprint,
                    },
                )
                return CycleResult(event, 3)

    def _queue_workflow(
        self, repo: RepoConfig, decision: PlanDecision, action_id: str
    ) -> EnqueuedWorkflow:
        if self.orchestrator is None:
            raise RuntimeError("Workboard workflow requested without an orchestrator")
        workspace: WorkspaceRef | None = None
        worktree: Worktree | None = None
        if decision.action in {ActionKind.IMPLEMENT, ActionKind.UPDATE_PLAN}:
            work_id = str(decision.target_issue or action_id[:16])
            worktree = self.worktrees.create(
                repo,
                work_id,
                lane="docs" if decision.action == ActionKind.UPDATE_PLAN else "work",
            )
            workspace = WorkspaceRef(
                kind="worktree",
                path=worktree.path,
                branch=worktree.branch,
                push_branch=worktree.push_branch,
            )
        elif decision.action == ActionKind.REPAIR_PR:
            if decision.target_pr is None:
                raise ValueError("repair_pr requires target_pr")
            pr = self.github.pull_request(repo, decision.target_pr)
            remote_branch = str(pr.get("headRefName") or "")
            if not remote_branch:
                raise ValueError("repair_pr target PR does not expose a head branch")
            worktree = self.worktrees.checkout_existing(
                repo, f"pr-{decision.target_pr}-{action_id[:8]}", remote_branch
            )
            workspace = WorkspaceRef(
                kind="worktree",
                path=worktree.path,
                branch=worktree.branch,
                push_branch=worktree.push_branch,
            )
        elif decision.action == ActionKind.REVIEW_PR:
            if decision.target_pr is None:
                raise ValueError("review_pr requires target_pr")
            pr = self.github.pull_request(repo, decision.target_pr)
            remote_branch = str(pr.get("headRefName") or "")
            if not remote_branch:
                raise ValueError("review_pr target PR does not expose a head branch")
            worktree = self.worktrees.checkout_existing(
                repo, f"pr-{decision.target_pr}-{action_id[:8]}", remote_branch, lane="review"
            )
            workspace = WorkspaceRef(
                kind="worktree",
                path=worktree.path,
                branch=worktree.branch,
                push_branch=worktree.push_branch,
            )
        try:
            return self.orchestrator.enqueue(repo, decision, action_id, workspace=workspace)
        except Exception as exc:
            cleanup_error = self._cleanup_unowned_worktree(repo, decision, worktree)
            if cleanup_error:
                raise RuntimeError(f"{exc}; isolated workspace cleanup failed: {cleanup_error}") from exc
            raise

    def _cleanup_unowned_worktree(
        self,
        repo: RepoConfig,
        decision: PlanDecision,
        worktree: Worktree | None,
    ) -> str | None:
        """Remove a fresh checkout only when a failed queue owns no cards yet."""
        if worktree is None or self.orchestrator is None:
            return None
        try:
            if self.orchestrator.has_active_workflow(repo, decision):
                return None
            self.worktrees.remove(repo, worktree)
        except Exception as exc:
            return str(exc)[:500]
        return None

    def _queue_approved_workflow(
        self,
        repo: RepoConfig,
        run_id: str,
        decision: PlanDecision,
        action_id: str,
        fingerprint: str,
        *,
        consume_approval: bool,
        model_evidence: dict[str, Any],
    ) -> CycleResult:
        """Send a stored proposal through the same queue gates as new autonomous work."""
        if self.orchestrator is None:
            raise RuntimeError("approved workflow requested without an orchestrator")
        evidence = {**model_evidence, "action_id": action_id, "decision": decision.model_dump(mode="json")}
        try:
            if self.orchestrator.has_active_workflow(repo, decision):
                self.state.transition(repo.full_name, RunState.READY)
                self.state.set_proposal_status(repo.full_name, action_id, "queued")
                if consume_approval:
                    self.state.consume_approval(repo.full_name, action_id)
                return self._workboard_workflow_already_queued(
                    repo, run_id, decision, fingerprint, evidence
                )
            active_count = self.orchestrator.active_workflow_count(repo)
        except Exception as exc:
            event = self._emit(
                repo,
                run_id,
                RunState.DEGRADED,
                "WORKBOARD_CAPACITY_UNKNOWN",
                "Workboard capacity could not be read for the approved action.",
                str(exc)[:2000],
                fingerprint,
                {
                    "next_action": "Restore Workboard access and rerun the stored action; no worker workflow was started.",
                    **evidence,
                },
            )
            return CycleResult(event, 2)
        if active_count >= repo.max_parallel_prs:
            self.state.transition(repo.full_name, RunState.READY)
            event = self._emit(
                repo,
                run_id,
                RunState.READY,
                "WORKFLOW_CAPACITY_WAIT",
                decision.summary,
                f"{active_count} active Workboard workflow(s) already consume the configured limit of {repo.max_parallel_prs}.",
                fingerprint,
                {
                    "next_action": "Wait for an active workflow to complete; the approved action remains queued for the next capacity window.",
                    "active_workflow_count": active_count,
                    "max_parallel_prs": repo.max_parallel_prs,
                    **evidence,
                },
            )
            return CycleResult(event, 0)
        try:
            queued = self._queue_workflow(repo, decision, action_id)
            self._set_course_package_status(repo, decision, WorkPackageStatus.EXECUTING)
        except Exception as exc:
            self.state.set_proposal_status(repo.full_name, action_id, "failed")
            event = self._emit(
                repo,
                run_id,
                RunState.DEGRADED,
                "WORKFLOW_QUEUE_FAILED",
                decision.summary,
                str(exc)[:2000],
                fingerprint,
                {
                    "next_action": "Restore Workboard access or repair the isolated workspace; the approved action was not executed directly.",
                    **evidence,
                },
            )
            return CycleResult(event, 3)
        self.state.transition(repo.full_name, RunState.READY)
        self.state.set_proposal_status(repo.full_name, action_id, "queued")
        if consume_approval:
            self.state.consume_approval(repo.full_name, action_id)
        event = self._emit(
            repo,
            run_id,
            RunState.READY,
            "WORKFLOW_QUEUED",
            decision.summary,
            "The approved action was decomposed into role-separated Workboard cards for runtime workers.",
            fingerprint,
            {
                "next_action": "The runtime dispatcher will claim the first dependency-ready card.",
                "board_id": queued.board_id,
                "root_card_id": queued.root_card_id,
                "stage_cards": queued.stage_cards,
                **evidence,
            },
        )
        return CycleResult(event, 0)

    def _recoverable_autonomous_decision(
        self, repo: RepoConfig, event: EventRecord
    ) -> PlanDecision | None:
        if repo.operation_mode != OperationMode.AUTONOMOUS:
            return None
        if requires_owner_attention(event.event_type, event.evidence):
            return None
        raw = event.evidence.get("decision")
        if not isinstance(raw, dict):
            return None
        try:
            decision = PlanDecision.model_validate(raw)
        except ValueError:
            return None
        policy = evaluate_action(repo, decision, execute=True, shadow=False, approved=False)
        return decision if policy.allowed else None

    def _retry_direct_action(
        self,
        repo: RepoConfig,
        run_id: str,
        event: EventRecord,
        snapshot_fingerprint: str,
        plan_input_fingerprint: str,
        *,
        execute: bool,
        shadow: bool,
    ) -> CycleResult | None:
        """Retry only idempotent issue mutations without re-planning.

        A provider failure can occur after GitHub applied an issue mutation. The
        retryable operations are safe to replay, while issue creation remains
        fail-closed because a second attempt could create a duplicate issue.
        """
        if (
            repo.operation_mode != OperationMode.AUTONOMOUS
            or not execute
            or shadow
            or event.event_type != "EXECUTION_FAILED"
            or event.evidence.get("automatic_retry") is not True
            or event.evidence.get("plan_input_fingerprint") != plan_input_fingerprint
        ):
            return None
        raw_decision = event.evidence.get("decision")
        action_id = event.evidence.get("action_id")
        if not isinstance(raw_decision, dict) or not isinstance(action_id, str):
            return None
        try:
            decision = PlanDecision.model_validate(raw_decision)
        except ValueError:
            return None
        if decision.action not in _DIRECT_RETRYABLE_ACTIONS:
            return None
        attempt_value = event.evidence.get("execution_attempt", 1)
        attempt = attempt_value if isinstance(attempt_value, int) and attempt_value > 0 else 1
        if attempt >= _DIRECT_EXECUTION_ATTEMPT_LIMIT:
            self.state.set_proposal_status(repo.full_name, action_id, "failed")
            return None

        next_attempt = attempt + 1
        self.state.transition(repo.full_name, RunState.EXECUTING)
        model_evidence = {
            "retry_of": event.event_id,
            "execution_attempt": next_attempt,
            "automatic_retry": True,
        }
        try:
            result = self._execute(
                repo,
                run_id,
                decision,
                event.fingerprint,
                model_evidence,
            )
            self.state.set_proposal_status(repo.full_name, action_id, "executed")
            return result
        except Exception as exc:
            failed = self._emit(
                repo,
                run_id,
                RunState.DEGRADED,
                "EXECUTION_FAILED",
                decision.summary,
                str(exc)[:2000],
                event.fingerprint,
                {
                    "next_action": (
                        "Automatic deterministic retries are exhausted; inspect the provider failure or run one forced replan."
                    ),
                    "action_id": action_id,
                    "execution_attempt": next_attempt,
                    "automatic_retry": True,
                    "decision": decision.model_dump(mode="json"),
                    "snapshot_fingerprint": snapshot_fingerprint,
                    "plan_input_fingerprint": plan_input_fingerprint,
                    "retry_of": event.event_id,
                },
            )
            return CycleResult(failed, 3)

    def _resume_autonomous_proposal(
        self,
        repo: RepoConfig,
        run_id: str,
        decision: PlanDecision,
        event: EventRecord,
        snapshot_fingerprint: str,
    ) -> CycleResult | None:
        action_id = event.evidence.get("action_id")
        if not isinstance(action_id, str):
            return None
        stored = self.state.proposal(repo.full_name, action_id)
        if (
            stored is None
            or stored["status"] != "proposed"
            or stored["snapshot_fingerprint"]
            not in {snapshot_fingerprint, event.evidence.get("approval_fingerprint")}
        ):
            return None
        self.state.transition(repo.full_name, RunState.PLANNING)
        try:
            if self.orchestrator is not None and decision.action not in _DIRECT_CONTROL_PLANE_ACTIONS:
                return self._queue_approved_workflow(
                    repo,
                    run_id,
                    decision,
                    action_id,
                    event.fingerprint,
                    consume_approval=False,
                    model_evidence={
                        "resumed_action_id": action_id,
                        "model": event.evidence.get("model"),
                        "model_attempts": event.evidence.get("model_attempts", []),
                    },
                )
            result = self._execute(
                repo,
                run_id,
                decision,
                event.fingerprint,
                {
                    "resumed_action_id": action_id,
                    "model": event.evidence.get("model"),
                    "model_attempts": event.evidence.get("model_attempts", []),
                },
            )
            self.state.set_proposal_status(repo.full_name, action_id, "executed")
            return result
        except ModelCallSuppressedError as exc:
            return self._model_suppressed_result(repo, run_id, exc)
        except WorkerBlockedError as exc:
            self.state.set_proposal_status(repo.full_name, action_id, "blocked")
            return self._worker_blocked_result(
                repo,
                run_id,
                decision,
                event.fingerprint,
                exc.reason,
                {"resumed_action_id": action_id},
            )
        except Exception as exc:
            self.state.set_proposal_status(repo.full_name, action_id, "failed")
            failed = self._emit(
                repo,
                run_id,
                RunState.DEGRADED,
                "EXECUTION_FAILED",
                decision.summary,
                str(exc)[:2000],
                event.fingerprint,
                {
                    "next_action": "The Captain will retry only after the failure evidence changes.",
                    "action_id": action_id,
                },
            )
            return CycleResult(failed, 3)

    def _plan(
        self,
        repo: RepoConfig,
        run_id: str,
        snapshot: RepositorySnapshot,
        *,
        planning_document: PlanningDocument,
        course: Course | None = None,
    ) -> tuple[PlanDecision, dict[str, Any]]:
        baseline = self.state.baseline(repo.full_name)
        baseline_evidence: dict[str, Any] | None = baseline
        if baseline:
            artifact_value = baseline.get("artifact_path")
            artifact_path = Path(str(artifact_value)) if artifact_value else None
            if artifact_path and artifact_path.is_file():
                try:
                    artifact_payload = json.loads(artifact_path.read_text(encoding="utf-8"))
                    baseline_evidence = {
                        **baseline,
                        "analysis": artifact_payload.get("analysis"),
                        "checks": artifact_payload.get("checks"),
                        "source_counts": artifact_payload.get("source_inventory", {}).get("counts"),
                        "evidence_exclusions": artifact_payload.get("evidence_exclusions"),
                    }
                except (json.JSONDecodeError, OSError, AttributeError):
                    baseline_evidence = {**baseline, "artifact_read_error": True}
        evidence_text = json.dumps(
            {
                "repo": repo.model_dump(mode="json"),
                "github": snapshot.as_dict(),
                "baseline": baseline_evidence,
                "planning_document": planning_document.text,
                "planning_document_source": planning_document.source,
                "planning_document_fingerprint": planning_document.fingerprint,
                "course": course.model_dump(mode="json") if course is not None else None,
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
        prompt = load_prompt("planner.md") + "\n\n" + evidence_text[:90_000]
        result = self.run_model(
            repo,
            run_id,
            "planner",
            prompt=prompt,
            models=self._models_for(repo, "planner", course=course, stage="planning"),
            output_model=PlanDecision,
            cwd=repo.local_path,
            writable=False,
            course_key=course.key if course else None,
            stage="planning",
        )
        return PlanDecision.model_validate(result.output), {
            "model": result.resolved_model,
            "model_attempts": [item.model_dump(mode="json") for item in result.attempts],
            "planning_document_source": planning_document.source,
            "planning_document_fingerprint": planning_document.fingerprint,
        }

    def _execute(
        self,
        repo: RepoConfig,
        run_id: str,
        decision: PlanDecision,
        fingerprint: str,
        model_evidence: dict[str, Any],
    ) -> CycleResult:
        if decision.action in {ActionKind.REPORT_ONLY, ActionKind.NO_ACTION}:
            self.state.transition(repo.full_name, RunState.READY)
            return CycleResult(
                self._emit(
                    repo,
                    run_id,
                    RunState.READY,
                    "STATUS_REPORTED",
                    decision.summary,
                    decision.reason,
                    fingerprint,
                    {"next_action": decision.reason, **model_evidence},
                ),
                0,
            )
        if decision.action in {ActionKind.IMPLEMENT, ActionKind.UPDATE_PLAN}:
            return self._implement(repo, run_id, decision, fingerprint, model_evidence)
        if decision.action == ActionKind.REPAIR_PR:
            return self._repair(repo, run_id, decision, fingerprint, model_evidence)
        if decision.action == ActionKind.REVIEW_PR:
            if decision.target_pr is None:
                raise ValueError("review_pr requires target_pr")
            return self._review(repo, run_id, decision.target_pr, decision, fingerprint, model_evidence)
        if decision.action == ActionKind.CREATE_ISSUE:
            if not decision.issue_title:
                raise ValueError("create_issue requires issue_title")
            issue = self.github.create_issue(
                repo, decision.issue_title, decision.issue_body or decision.reason
            )
            self.state.transition(repo.full_name, RunState.READY)
            return CycleResult(
                self._emit(
                    repo,
                    run_id,
                    RunState.READY,
                    "ISSUE_CREATED",
                    decision.summary,
                    decision.reason,
                    fingerprint,
                    {
                        "next_action": "Continue with the newly queued work item.",
                        "links": [issue.get("url")],
                        **model_evidence,
                    },
                ),
                0,
            )
        if decision.action == ActionKind.UPDATE_ISSUE:
            if decision.target_issue is None:
                raise ValueError("update_issue requires target_issue")
            self.github.update_issue(repo, decision.target_issue, decision.issue_title, decision.issue_body)
            self.state.transition(repo.full_name, RunState.READY)
            return CycleResult(
                self._emit(
                    repo,
                    run_id,
                    RunState.READY,
                    "ISSUE_UPDATED",
                    decision.summary,
                    decision.reason,
                    fingerprint,
                    {
                        "next_action": "Implement the updated issue as the active work contract.",
                        "issue": decision.target_issue,
                        "links": [
                            f"https://github.com/{repo.full_name}/issues/{decision.target_issue}"
                        ],
                        **model_evidence,
                    },
                ),
                0,
            )
        if decision.action == ActionKind.LABEL_ISSUE:
            if decision.target_issue is None:
                raise ValueError("label_issue requires target_issue")
            if not decision.issue_labels:
                raise ValueError("label_issue requires at least one issue_labels entry")
            self.github.label_issue(repo, decision.target_issue, decision.issue_labels)
            self.state.transition(repo.full_name, RunState.READY)
            return CycleResult(
                self._emit(
                    repo,
                    run_id,
                    RunState.READY,
                    "ISSUE_LABELED",
                    decision.summary,
                    decision.reason,
                    fingerprint,
                    {
                        "next_action": "Continue with the labeled issue as the active work contract.",
                        "issue": decision.target_issue,
                        "labels": list(decision.issue_labels),
                        "links": [
                            f"https://github.com/{repo.full_name}/issues/{decision.target_issue}"
                        ],
                        **model_evidence,
                    },
                ),
                0,
            )
        if decision.action == ActionKind.RETARGET_ISSUE:
            if decision.target_issue is None:
                raise ValueError("retarget_issue requires target_issue")
            if decision.issue_milestone is None and not decision.issue_assignees:
                raise ValueError(
                    "retarget_issue requires issue_milestone or at least one issue_assignees entry"
                )
            self.github.retarget_issue(
                repo,
                decision.target_issue,
                decision.issue_milestone,
                decision.issue_assignees,
            )
            self.state.transition(repo.full_name, RunState.READY)
            return CycleResult(
                self._emit(
                    repo,
                    run_id,
                    RunState.READY,
                    "ISSUE_RETARGETED",
                    decision.summary,
                    decision.reason,
                    fingerprint,
                    {
                        "next_action": "Continue with the retargeted issue as the active work contract.",
                        "issue": decision.target_issue,
                        "milestone": decision.issue_milestone,
                        "assignees": list(decision.issue_assignees),
                        "links": [
                            f"https://github.com/{repo.full_name}/issues/{decision.target_issue}"
                        ],
                        **model_evidence,
                    },
                ),
                0,
            )
        if decision.action == ActionKind.CLOSE_ISSUE:
            if decision.target_issue is None:
                raise ValueError("close_issue requires target_issue")
            self.github.close_issue(repo, decision.target_issue, decision.reason)
            self.state.transition(repo.full_name, RunState.READY)
            return CycleResult(
                self._emit(
                    repo,
                    run_id,
                    RunState.READY,
                    "ISSUE_CLOSED",
                    decision.summary,
                    decision.reason,
                    fingerprint,
                    {
                        "next_action": "Select the next unblocked work item.",
                        "issue": decision.target_issue,
                        "links": [
                            f"https://github.com/{repo.full_name}/issues/{decision.target_issue}"
                        ],
                        **model_evidence,
                    },
                ),
                0,
            )
        raise ValueError(f"executor does not support action {decision.action.value}")

    def _continue_active_work(
        self,
        repo: RepoConfig,
        run_id: str,
        active: dict[str, Any],
        snapshot_fingerprint: str,
        *,
        shadow: bool,
        execute: bool,
    ) -> CycleResult:
        decision = PlanDecision.model_validate(active["decision"])
        number = int(active["pr_number"])
        pr = self.github.pull_request(repo, number)
        branch = str(pr.get("headRefName") or active.get("branch") or "")
        head_sha = str(pr.get("headRefOid") or active.get("head_sha") or "")
        status = str(active.get("status") or "pr_open")
        fingerprint = _fingerprint(
            {
                "snapshot": snapshot_fingerprint,
                "active_action": active.get("action_id"),
                "pr": number,
                "branch": branch,
                "head_sha": head_sha,
                "status": status,
            }
        )
        recent_event = self.state.latest_operational_event(repo.full_name)
        recent = [recent_event] if recent_event is not None else []
        if shadow or repo.operation_mode.value == "advisory":
            if recent and recent[0].event_type == "ACTIVE_PR_STATUS" and recent[0].fingerprint == fingerprint:
                return CycleResult(recent[0], 0)
            event = self._emit(
                repo,
                run_id,
                RunState.PR_OPEN,
                "ACTIVE_PR_STATUS",
                f"PR #{number} is the active work item.",
                "Shadow/advisory mode does not mutate active PRs.",
                fingerprint,
                {
                    "next_action": "Run a live cycle when this repo is allowed to continue PR work.",
                    "links": [pr.get("url")],
                    "pr": number,
                    "head_sha": head_sha,
                },
            )
            return CycleResult(event, 0)

        if self.orchestrator is not None:
            review_decision = decision.model_copy(
                update={"action": ActionKind.REVIEW_PR, "target_pr": number}
            )
            return self._queue_or_reuse_workboard_review(
                repo,
                run_id,
                review_decision,
                fingerprint,
                {"active_pr": number, "head_sha": head_sha, "branch": branch},
            )

        if status == "repair_requested" or self.state.current_state(repo.full_name) == RunState.REPAIRING:
            repair_decision = decision.model_copy(update={"action": ActionKind.REPAIR_PR, "target_pr": number})
            policy = evaluate_action(repo, repair_decision, execute=execute, shadow=shadow, approved=False)
            if not policy.allowed:
                return self._active_policy_blocked(
                    repo, run_id, repair_decision, fingerprint, pr, policy.reason, policy.requires_owner
                )
            return self._repair(
                repo,
                run_id,
                repair_decision,
                fingerprint,
                {"active_pr": number, "original_action": decision.action.value},
            )

        review_decision = decision.model_copy(update={"action": ActionKind.REVIEW_PR, "target_pr": number})
        policy = evaluate_action(repo, review_decision, execute=execute, shadow=shadow, approved=False)
        if not policy.allowed:
            return self._active_policy_blocked(
                repo, run_id, review_decision, fingerprint, pr, policy.reason, policy.requires_owner
            )
        gate = self.github.gate(repo, number, review_head_sha=head_sha)
        gate_fingerprint = _fingerprint(
            {
                "snapshot": snapshot_fingerprint,
                "active_action": active.get("action_id"),
                "pr": number,
                "branch": branch,
                "head_sha": head_sha,
                "gate": gate.model_dump(mode="json"),
            }
        )
        if not gate.checks_green:
            self.state.update_active_work(repo.full_name, status="pr_open", head_sha=head_sha)
            if (
                recent
                and recent[0].event_type == "PR_CHECKS_WAITING"
                and recent[0].fingerprint == gate_fingerprint
            ):
                return CycleResult(recent[0], 2)
            event = self._emit(
                repo,
                run_id,
                RunState.PR_OPEN,
                "PR_CHECKS_WAITING",
                f"PR #{number} is waiting on required checks.",
                "The Captain will not spend reviewer tokens until required checks are green.",
                gate_fingerprint,
                {
                    "next_action": "Wait for GitHub checks to finish or repair failed checks if they stay red.",
                    "links": [pr.get("url")],
                    "pr": number,
                    "head_sha": head_sha,
                    "checks": [item.model_dump(mode="json") for item in gate.required_checks],
                },
            )
            return CycleResult(event, 2)

        if (
            status in {"pr_open", "completion_ready"}
            and recent
            and recent[0].event_type in {"REVIEW_WAITING", "COMPLETION_READY"}
            and recent[0].fingerprint == gate_fingerprint
        ):
            return CycleResult(recent[0], 2 if recent[0].event_type == "REVIEW_WAITING" else 0)
        return self._review(repo, run_id, number, review_decision, gate_fingerprint, {"active_pr": number})

    def _disabled_result(self, repo: RepoConfig, run_id: str, operation: str) -> CycleResult:
        """Stop before GitHub, model, or Workboard calls for an explicitly paused repo."""
        event = self._emit(
            repo,
            run_id,
            RunState.DEGRADED,
            "CONTROL_PLANE_DISABLED",
            "Repository Captain is disabled",
            f"The configured repository Captain mode is disabled; {operation} performed no model, GitHub, or Workboard work.",
            "captain-disabled",
            {
                "next_action": "Set operation_mode to advisory, supervised, or autonomous before resuming Captain work.",
                "operation_mode": repo.operation_mode.value,
            },
        )
        return CycleResult(event, 0)

    def _worker_blocked_result(
        self,
        repo: RepoConfig,
        run_id: str,
        decision: PlanDecision,
        fingerprint: str,
        blocker: str,
        model_evidence: dict[str, Any],
    ) -> CycleResult:
        kind = classify_blocker(blocker)
        owner_required = kind != BlockerKind.TECHNICAL
        state = RunState.BLOCKED if owner_required else RunState.DEGRADED
        event_type = "ATTENTION_REQUIRED" if owner_required else "EXECUTION_FAILED"
        if decision.target_pr is not None:
            self.state.update_active_work(
                repo.full_name,
                status="owner_blocked" if owner_required else "repair_requested",
            )
        evidence = {
            "next_action": (
                "Resolve the owner blocker, then rerun the active Captain cycle."
                if owner_required
                else "Retry the worker after the technical cause changes; unrelated work can continue."
            ),
            "blocker": blocker,
            "blocker_kind": kind.value,
            "owner_required": owner_required,
            "decision": decision.model_dump(mode="json"),
            **model_evidence,
        }
        if decision.target_pr is not None:
            evidence.update(
                {
                    "pr": decision.target_pr,
                    "links": [_pr_link(repo, decision.target_pr)],
                }
            )
        event = self._emit(
            repo,
            run_id,
            state,
            event_type,
            f"Worker blocked: {decision.summary}",
            blocker,
            fingerprint,
            evidence,
        )
        return CycleResult(event, 2)

    def _queue_or_reuse_workboard_review(
        self,
        repo: RepoConfig,
        run_id: str,
        decision: PlanDecision,
        fingerprint: str,
        model_evidence: dict[str, Any],
    ) -> CycleResult:
        if self.orchestrator is None:
            raise RuntimeError("Workboard review requested without an orchestrator")
        try:
            already_active = self.orchestrator.has_active_workflow(repo, decision)
        except Exception as exc:
            return self._review_workflow_queue_failed(repo, run_id, decision, fingerprint, model_evidence, exc)
        if already_active:
            return self._workboard_workflow_already_queued(
                repo, run_id, decision, fingerprint, model_evidence
            )
        action_id = _fingerprint(
            {
                "workboard_review": repo.full_name,
                "target_pr": decision.target_pr,
                "target_issue": decision.target_issue,
                "head_sha": model_evidence.get("head_sha"),
            }
        )
        workspace: WorkspaceRef | None = None
        worktree: Worktree | None = None
        if decision.target_pr is not None:
            remote_branch = str(model_evidence.get("branch") or "").strip()
            if not remote_branch:
                return self._review_workflow_queue_failed(
                    repo,
                    run_id,
                    decision,
                    fingerprint,
                    model_evidence,
                    RuntimeError("active PR head branch is missing"),
                )
            try:
                head_token = str(model_evidence.get("head_sha") or action_id[:12])[:12]
                worktree = self.worktrees.checkout_existing(
                    repo,
                    f"pr-{decision.target_pr}-{head_token}",
                    remote_branch,
                    lane="review",
                )
                workspace = WorkspaceRef(
                    kind="worktree",
                    path=worktree.path,
                    branch=worktree.branch,
                    push_branch=worktree.push_branch,
                )
            except Exception as exc:
                return self._review_workflow_queue_failed(
                    repo, run_id, decision, fingerprint, model_evidence, exc
                )
        try:
            queued = self.orchestrator.enqueue(repo, decision, action_id, workspace=workspace)
        except Exception as exc:
            cleanup_error = self._cleanup_unowned_worktree(repo, decision, worktree)
            if cleanup_error:
                exc = RuntimeError(f"{exc}; review workspace cleanup failed: {cleanup_error}")
            return self._review_workflow_queue_failed(
                repo, run_id, decision, fingerprint, model_evidence, exc
            )
        return CycleResult(
            self._emit(
                repo,
                run_id,
                RunState.REVIEWING,
                "WORKFLOW_QUEUED",
                decision.summary,
                "The active PR was handed to role-separated Workboard review workers.",
                fingerprint,
                {
                    "next_action": "The configured queue runtime will claim the dependency-ready review and test cards.",
                    "action_id": action_id,
                    "board_id": queued.board_id,
                    "root_card_id": queued.root_card_id,
                    "stage_cards": queued.stage_cards,
                    "links": [_pr_link(repo, decision.target_pr)],
                    **model_evidence,
                },
            ),
            0,
        )

    def _review_workflow_queue_failed(
        self,
        repo: RepoConfig,
        run_id: str,
        decision: PlanDecision,
        fingerprint: str,
        model_evidence: dict[str, Any],
        error: Exception,
    ) -> CycleResult:
        evidence = {
            "next_action": "Repair the isolated PR workspace or Workboard connection; the active PR will be retried automatically.",
            "decision": decision.model_dump(mode="json"),
            "links": [_pr_link(repo, decision.target_pr)],
            "pr": decision.target_pr,
            **model_evidence,
        }
        return CycleResult(
            self._emit(
                repo,
                run_id,
                RunState.PR_OPEN,
                "WORKFLOW_QUEUE_FAILED",
                decision.summary,
                str(error)[:2000],
                _fingerprint({"fingerprint": fingerprint, "error": str(error)}),
                evidence,
            ),
            2,
        )

    def _workboard_workflow_already_queued(
        self,
        repo: RepoConfig,
        run_id: str,
        decision: PlanDecision,
        fingerprint: str,
        model_evidence: dict[str, Any],
    ) -> CycleResult:
        recent_event = self.state.latest_operational_event(repo.full_name)
        recent = [recent_event] if recent_event is not None else []
        if recent and recent[0].event_type == "WORKFLOW_ALREADY_QUEUED" and recent[0].fingerprint == fingerprint:
            return CycleResult(recent[0], 0)
        current = self.state.current_state(repo.full_name)
        event_state = (
            RunState.REVIEWING
            if current in {RunState.PR_OPEN, RunState.REVIEWING, RunState.REPAIRING}
            else RunState.READY
        )
        return CycleResult(
            self._emit(
                repo,
                run_id,
                event_state,
                "WORKFLOW_ALREADY_QUEUED",
                (
                    f"PR #{decision.target_pr} already has an active Workboard workflow."
                    if decision.target_pr is not None
                    else f"Issue #{decision.target_issue} already has an active Workboard workflow."
                    if decision.target_issue is not None
                    else "This work item already has an active Workboard workflow."
                ),
                "No duplicate workers were started for the same issue or pull request.",
                fingerprint,
                {
                    "next_action": "Let the existing role-separated workers finish; the dispatcher will advance dependencies.",
                    "links": ([_pr_link(repo, decision.target_pr)] if decision.target_pr is not None else []),
                    "pr": decision.target_pr,
                    "issue": decision.target_issue,
                    **model_evidence,
                },
            ),
            0,
        )

    def _active_policy_blocked(
        self,
        repo: RepoConfig,
        run_id: str,
        decision: PlanDecision,
        fingerprint: str,
        pr: dict[str, Any],
        reason: str,
        requires_owner: bool,
    ) -> CycleResult:
        target = RunState.BLOCKED if requires_owner else RunState.DEGRADED
        event = self._emit(
            repo,
            run_id,
            target,
            "APPROVAL_REQUIRED" if requires_owner else "ACTION_BLOCKED",
            decision.summary,
            reason,
            fingerprint,
            {
                "next_action": (
                    "Approve or change repo policy before the Captain continues this active PR."
                    if requires_owner
                    else "Fix the policy block, then rerun the cycle."
                ),
                "decision": decision.model_dump(mode="json"),
                "links": [pr.get("url")],
                "pr": pr.get("number"),
            },
        )
        return CycleResult(event, 2)

    def _implement(
        self,
        repo: RepoConfig,
        run_id: str,
        decision: PlanDecision,
        fingerprint: str,
        model_evidence: dict[str, Any],
    ) -> CycleResult:
        self.state.transition(repo.full_name, RunState.EXECUTING)
        self._set_course_package_status(repo, decision, WorkPackageStatus.EXECUTING)
        work_id = decision.work_item_id or run_id[:8]
        lane = "docs" if decision.action == ActionKind.UPDATE_PLAN else "work"
        worktree = self.worktrees.create(repo, work_id, lane=lane)
        try:
            prompt = _worker_prompt(repo, decision, worktree)
            result = self.run_model(
                repo,
                run_id,
                "coder",
                prompt=prompt,
                models=self._models_for(repo, "coder", decision=decision, stage="implementation"),
                output_model=WorkerResult,
                cwd=worktree.path,
                writable=True,
                course_key=decision.course_key,
                work_package_key=decision.work_package_key,
                stage="implementation",
            )
            _worker_result_or_raise(result.output)
            self._assert_worktree_identity(worktree)
            changed = self._changed_paths(worktree.path)
            if decision.action == ActionKind.UPDATE_PLAN:
                allowed_docs = {repo.planning_doc, repo.project_manifest}
                unexpected = [path for path in changed if path not in allowed_docs]
                if unexpected:
                    raise RuntimeError(f"docs lane changed unexpected paths: {unexpected}")
                plan_path = worktree.path / repo.planning_doc
                assert_durable_document(plan_path.read_text(encoding="utf-8"))
                if repo.require_project_manifest and not (worktree.path / repo.project_manifest).is_file():
                    raise RuntimeError(f"required project manifest was not created: {repo.project_manifest}")
            safe, excluded = safe_changed_paths(worktree.path, changed)
            if excluded or set(safe) != set(changed):
                raise RuntimeError(f"unsafe changed paths were excluded: {excluded}")
            secret_findings = scan_secrets(worktree.path, safe)
            if secret_findings:
                raise RuntimeError(f"secret scan blocked files: {secret_findings}")
            configured_checks = select_checks(
                repo.docs_checks if decision.action == ActionKind.UPDATE_PLAN else repo.checks,
                changed,
                repo.ux_paths,
            )
            checks = self._run_checks(worktree.path, configured_checks, source_path=repo.local_path)
            failed = [item for item in checks if item["returncode"] != 0]
            if failed:
                raise RuntimeError("configured checks failed: " + _failed_check_summary(failed))
            self._commit_and_push(worktree, safe, decision.summary)
            body = _pull_request_body(repo, decision, safe, checks, result.resolved_model)
            pr = self.github.create_pull_request(
                repo,
                branch=worktree.push_branch,
                title=decision.summary[:180],
                body=body,
                draft=True,
            )
            pr_number = int(pr.get("number") or 0)
            if pr_number <= 0:
                raise RuntimeError("created pull request did not include a PR number")
            self.state.save_active_work(
                repo.full_name,
                action_id=str(model_evidence.get("approved_action_id")) if model_evidence.get("approved_action_id") else None,
                pr_number=pr_number,
                branch=worktree.push_branch,
                head_sha=str(pr.get("headRefOid") or ""),
                status="pr_open",
                decision=decision.model_dump(mode="json"),
            )
            cleanup_warning = self._cleanup_successful_worktree(repo, worktree)
            self.state.transition(repo.full_name, RunState.PR_OPEN)
            event = self._emit(
                repo,
                run_id,
                RunState.PR_OPEN,
                "PR_OPENED",
                decision.summary,
                "The isolated worker completed the selected scope and configured checks passed.",
                fingerprint,
                {
                    "next_action": "Wait for GitHub checks, then run independent and final review.",
                    "links": [pr.get("url")],
                    "pr": pr,
                    "branch": worktree.push_branch,
                    "checks": checks,
                    "excluded": excluded,
                    "model": result.resolved_model,
                    **({"worktree_cleanup_warning": cleanup_warning} if cleanup_warning else {}),
                    **model_evidence,
                },
            )
            return CycleResult(event, 0)
        except BaseException as exc:
            self._discard_failed_worktree(repo, worktree, exc)
            raise

    def _repair(
        self,
        repo: RepoConfig,
        run_id: str,
        decision: PlanDecision,
        fingerprint: str,
        model_evidence: dict[str, Any],
    ) -> CycleResult:
        if decision.target_pr is None:
            raise ValueError("repair_pr requires target_pr")
        pr = self.github.pull_request(repo, decision.target_pr)
        remote_branch = str(pr.get("headRefName") or "")
        if not remote_branch:
            raise ValueError("pull request head branch is missing")
        self.state.transition(repo.full_name, RunState.REPAIRING)
        self._set_course_package_status(repo, decision, WorkPackageStatus.REVIEWING)
        worktree = self.worktrees.checkout_existing(
            repo, f"pr-{decision.target_pr}-{run_id[:8]}", remote_branch
        )
        try:
            prompt = (
                _worker_prompt(repo, decision, worktree)
                + "\n\nAddress only the current review findings and failing checks:\n"
                + json.dumps(
                    self._review_findings_context(repo, decision.target_pr),
                    indent=2,
                    default=str,
                )[:80_000]
            )
            result = self.run_model(
                repo,
                run_id,
                "repair",
                prompt=prompt,
                models=self._models_for(repo, "coder", decision=decision, stage="repair"),
                output_model=WorkerResult,
                cwd=worktree.path,
                writable=True,
                course_key=decision.course_key,
                work_package_key=decision.work_package_key,
                stage="repair",
            )
            _worker_result_or_raise(result.output)
            self._assert_worktree_identity(worktree)
            changed = self._changed_paths(worktree.path)
            is_docs_repair = model_evidence.get("original_action") == ActionKind.UPDATE_PLAN.value
            if is_docs_repair:
                allowed_docs = {repo.planning_doc, repo.project_manifest}
                unexpected = [path for path in changed if path not in allowed_docs]
                if unexpected:
                    raise RuntimeError(f"docs repair changed unexpected paths: {unexpected}")
                plan_path = worktree.path / repo.planning_doc
                if plan_path.is_file():
                    assert_durable_document(plan_path.read_text(encoding="utf-8"))
                if repo.require_project_manifest and not (worktree.path / repo.project_manifest).is_file():
                    raise RuntimeError(f"required project manifest was not created: {repo.project_manifest}")
            safe, excluded = safe_changed_paths(worktree.path, changed)
            if excluded or scan_secrets(worktree.path, safe):
                raise RuntimeError("repair produced excluded paths or secret findings")
            checks = self._run_checks(
                worktree.path,
                select_checks(repo.docs_checks if is_docs_repair else repo.checks, changed, repo.ux_paths),
                source_path=repo.local_path,
            )
            failed = [item for item in checks if item["returncode"] != 0]
            if failed:
                raise RuntimeError("repair checks failed: " + _failed_check_summary(failed))
            self._commit_and_push(worktree, safe, f"fix: address PR {decision.target_pr} review findings")
            cleanup_warning = self._cleanup_successful_worktree(repo, worktree)
            refreshed = self.github.pull_request(repo, decision.target_pr)
            self.state.update_active_work(
                repo.full_name,
                status="pr_open",
                head_sha=str(refreshed.get("headRefOid") or pr.get("headRefOid") or ""),
            )
            self.state.transition(repo.full_name, RunState.PR_OPEN)
            return CycleResult(
                self._emit(
                    repo,
                    run_id,
                    RunState.PR_OPEN,
                    "PR_REPAIRED",
                    decision.summary,
                    "Repair changes were pushed to the existing PR branch after configured checks passed.",
                    fingerprint,
                    {
                        "next_action": "Rerun independent and final review on the new head.",
                        "links": [pr.get("url")],
                        **({"worktree_cleanup_warning": cleanup_warning} if cleanup_warning else {}),
                        **model_evidence,
                    },
                ),
                0,
            )
        except BaseException as exc:
            self._discard_failed_worktree(repo, worktree, exc)
            raise

    def _review_findings_context(self, repo: RepoConfig, target_pr: int) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for event in self.state.recent_events(repo.full_name, 20):
            if event.event_type not in {"REVIEW_BLOCKED", "FINAL_REVIEW_BLOCKED"}:
                continue
            if event.evidence.get("pr") != target_pr:
                continue
            events.append(
                {
                    "event_type": event.event_type,
                    "summary": event.summary,
                    "reason": event.reason,
                    "head_sha": event.evidence.get("head_sha"),
                    "findings": event.evidence.get("findings", []),
                    "next_action": event.evidence.get("next_action"),
                }
            )
        return events

    def _triage_comments(
        self,
        repo: RepoConfig,
        run_id: str,
        decision: PlanDecision,
        pr: dict[str, Any],
        diff: str,
        threads: list[dict[str, Any]],
    ) -> tuple[CommentTriage, str] | None:
        head_sha = str(pr.get("headRefOid") or "")
        active: list[dict[str, Any]] = []
        for thread in threads:
            if thread.get("isResolved") or thread.get("isOutdated"):
                continue
            comments = thread.get("comments")
            if not isinstance(comments, list) or not comments:
                continue
            active.append(thread)
        if not active:
            return None
        prompt = (
            load_prompt("comment-adjudicator.md")
            + "\n\n"
            + json.dumps(
                {"repo": repo.model_dump(mode="json"), "pr": pr, "head_sha": head_sha, "threads": active, "diff": diff},
                default=str,
            )[:250_000]
        )
        models = self._models_for(
            repo,
            "comment_adjudicator",
            decision=decision,
            stage="review",
        )
        result = self.run_model(
            repo,
            run_id,
            "comment-adjudicator",
            prompt=prompt,
            models=models,
            output_model=CommentTriage,
            cwd=repo.local_path,
            writable=False,
            course_key=decision.course_key,
            work_package_key=decision.work_package_key,
            stage="comment_adjudication",
        )
        triage = CommentTriage.model_validate(result.output)
        if triage.head_sha != head_sha:
            raise RuntimeError(
                f"comment adjudicator returned stale head {triage.head_sha}; current PR head is {head_sha}"
            )
        return triage, result.resolved_model

    def _requires_ux_review(
        self, repo: RepoConfig, pr: dict[str, Any], decision: PlanDecision
    ) -> bool:
        if not repo.ux_enabled:
            return False
        files_value = pr.get("files")
        file_rows = cast(list[Any], files_value) if isinstance(files_value, list) else []
        files = [
            str(cast(dict[str, Any], item).get("path"))
            for item in file_rows
            if isinstance(item, dict) and cast(dict[str, Any], item).get("path")
        ]
        decision_text = json.dumps(decision.model_dump(mode="json"), default=str).lower()
        return bool(
            any(
                any(
                    path.startswith(pattern.rstrip("*")) or fnmatch(path, pattern)
                    for pattern in repo.ux_paths
                )
                for path in files
            )
            or any(term in decision_text for term in ("frontend", "user interface", "ui flow", "usability"))
        )

    def _ux_review(
        self,
        repo: RepoConfig,
        run_id: str,
        number: int,
        decision: PlanDecision,
        pr: dict[str, Any],
        diff: str,
        threads: list[dict[str, Any]],
    ) -> tuple[UXReview, str, str | None]:
        remote_branch = str(pr.get("headRefName") or "")
        head_sha = str(pr.get("headRefOid") or "")
        worktree = self.worktrees.checkout_existing(
            repo, f"ux-{number}-{head_sha[:12]}", remote_branch
        )
        try:
            prompt = (
                load_prompt("ux-reviewer.md")
                + "\n\n"
                + json.dumps(
                    {
                        "repo_policy": repo.model_dump(mode="json"),
                        "pr": pr,
                        "threads": threads,
                        "diff": diff,
                    },
                    default=str,
                )[:300_000]
            )
            models = self._models_for(
                repo,
                "ux_reviewer",
                decision=decision,
                stage="ux_review",
            )
            result = self.run_model(
                repo,
                run_id,
                "ux-review",
                prompt=prompt,
                models=models,
                output_model=UXReview,
                cwd=worktree.path,
                writable=False,
                course_key=decision.course_key,
                work_package_key=decision.work_package_key,
                stage="ux_review",
            )
            review = UXReview.model_validate(result.output)
            cleanup_warning = self._cleanup_successful_worktree(repo, worktree)
            return review, result.resolved_model, cleanup_warning
        except BaseException as exc:
            self._discard_failed_worktree(repo, worktree, exc)
            raise

    def _review(
        self,
        repo: RepoConfig,
        run_id: str,
        number: int,
        decision: PlanDecision,
        fingerprint: str,
        model_evidence: dict[str, Any],
    ) -> CycleResult:
        self.state.transition(repo.full_name, RunState.REVIEWING)
        self._set_course_package_status(repo, decision, WorkPackageStatus.REVIEWING)
        pr = self.github.pull_request(repo, number)
        diff = self.github.pull_request_diff(repo, number)
        threads = self.github.review_threads(repo, number)
        head_sha = str(pr.get("headRefOid") or "")
        comment_triage = self._triage_comments(repo, run_id, decision, pr, diff, threads)
        comment_evidence: dict[str, Any] = {}
        if comment_triage is not None:
            triage, triage_model = comment_triage
            comment_evidence = {"comment_triage": triage.model_dump(mode="json"), "comment_triage_model": triage_model}
            if triage.owner_decisions:
                self.github.comment_pull_request(
                    repo,
                    number,
                    _comment_triage_comment("Owner decision required for review comments", head_sha, triage),
                )
                self.state.update_active_work(repo.full_name, status="owner_blocked", head_sha=head_sha)
                self.state.transition(repo.full_name, RunState.BLOCKED)
                return CycleResult(
                    self._emit(
                        repo,
                        run_id,
                        RunState.BLOCKED,
                        "ATTENTION_REQUIRED",
                        triage.summary,
                        "Review-comment adjudication identified a decision that cannot be safely automated.",
                        fingerprint,
                        {
                            "next_action": "Resolve the review-comment decision, then rerun the current-head review.",
                            "links": [pr.get("url")],
                            "pr": number,
                            "head_sha": head_sha,
                            **comment_evidence,
                            **model_evidence,
                        },
                    ),
                    2,
                )
            if triage.verdict == ReviewVerdict.REQUEST_CHANGES:
                self.github.comment_pull_request(
                    repo,
                    number,
                    _comment_triage_comment("Review-comment triage requested changes", head_sha, triage),
                )
                self.state.update_active_work(repo.full_name, status="repair_requested", head_sha=head_sha)
                self.state.transition(repo.full_name, RunState.REPAIRING)
                return CycleResult(
                    self._emit(
                        repo,
                        run_id,
                        RunState.REPAIRING,
                        "COMMENT_TRIAGE_BLOCKED",
                        triage.summary,
                        "The independent comment adjudicator accepted actionable review findings on the current head.",
                        fingerprint,
                        {
                            "next_action": "Queue a repair limited to the accepted review comments.",
                            "links": [pr.get("url")],
                            "pr": number,
                            "head_sha": head_sha,
                            "findings": [item.model_dump(mode="json") for item in triage.accepted_findings],
                            **comment_evidence,
                            **model_evidence,
                        },
                    ),
                    2,
                )
        review_prompt = (
            load_prompt("reviewer.md")
            + "\n\n"
            + json.dumps(
                {
                    "decision": decision.model_dump(mode="json"),
                    "pr": pr,
                    "threads": threads,
                    "comment_triage": comment_evidence,
                    "diff": diff,
                },
                default=str,
            )[:300_000]
        )
        independent_result = self.run_model(
            repo,
            run_id,
            "independent-review",
            prompt=review_prompt,
            models=self._models_for(repo, "reviewer", decision=decision, stage="review"),
            output_model=IndependentReview,
            cwd=repo.local_path,
            writable=False,
            course_key=decision.course_key,
            work_package_key=decision.work_package_key,
            stage="independent_review",
        )
        independent = IndependentReview.model_validate(independent_result.output)
        if independent.verdict == ReviewVerdict.REQUEST_CHANGES:
            self.github.comment_pull_request(
                repo,
                number,
                _review_comment("Independent review requested changes", head_sha, independent),
            )
            self.state.update_active_work(repo.full_name, status="repair_requested", head_sha=head_sha)
            self.state.transition(repo.full_name, RunState.REPAIRING)
            event = self._emit(
                repo,
                run_id,
                RunState.REPAIRING,
                "REVIEW_BLOCKED",
                independent.summary,
                "Independent review found blocking changes on the current PR head.",
                fingerprint,
                {
                    "next_action": "Queue a repair limited to the listed findings.",
                    "links": [pr.get("url")],
                    "pr": number,
                    "head_sha": head_sha,
                    "findings": [item.model_dump(mode="json") for item in independent.findings],
                    **model_evidence,
                },
            )
            return CycleResult(event, 2)

        ux_evidence: dict[str, Any] = {}
        if self._requires_ux_review(repo, pr, decision):
            ux, ux_model, ux_cleanup_warning = self._ux_review(
                repo, run_id, number, decision, pr, diff, threads
            )
            ux_evidence = {"ux_review": ux.model_dump(mode="json"), "ux_model": ux_model}
            if ux_cleanup_warning:
                ux_evidence["ux_worktree_cleanup_warning"] = ux_cleanup_warning
            if ux.verdict == ReviewVerdict.REQUEST_CHANGES:
                self.github.comment_pull_request(
                    repo,
                    number,
                    _ux_review_comment("Usability review requested changes", head_sha, ux),
                )
                self.state.update_active_work(repo.full_name, status="repair_requested", head_sha=head_sha)
                self.state.transition(repo.full_name, RunState.REPAIRING)
                return CycleResult(
                    self._emit(
                        repo,
                        run_id,
                        RunState.REPAIRING,
                        "UX_REVIEW_BLOCKED",
                        ux.summary,
                        "Dedicated usability review found blocking frontend issues.",
                        fingerprint,
                        {
                            "next_action": "Queue a repair limited to the usability findings.",
                            "links": [pr.get("url")],
                            "pr": number,
                            "head_sha": head_sha,
                            "findings": [item.model_dump(mode="json") for item in ux.findings],
                            **ux_evidence,
                        },
                    ),
                    2,
                )

        if bool(pr.get("isDraft")):
            self.github.mark_ready(repo, number)
            pr = self.github.pull_request(repo, number)

        final_prompt = (
            load_prompt("final-reviewer.md")
            + "\n\n"
            + json.dumps(
                {
                    "repo_policy": repo.model_dump(mode="json"),
                    "decision": decision.model_dump(mode="json"),
                    "pr": pr,
                    "threads": threads,
                    "independent_review": independent.model_dump(mode="json"),
                    **comment_evidence,
                    **ux_evidence,
                },
                default=str,
            )[:200_000]
        )
        final_result = self.run_model(
            repo,
            run_id,
            "final-review",
            prompt=final_prompt,
            models=self._models_for(repo, "final_reviewer", decision=decision, stage="final_review"),
            output_model=FinalReview,
            cwd=repo.local_path,
            writable=False,
            course_key=decision.course_key,
            work_package_key=decision.work_package_key,
            stage="final_review",
        )
        final = FinalReview.model_validate(final_result.output)
        if final.owner_blocker:
            blocker_kind = classify_blocker(final.owner_blocker)
            if blocker_kind == BlockerKind.TECHNICAL:
                self.github.comment_pull_request(
                    repo,
                    number,
                    _final_review_comment("Final review returned an unclassified blocker", head_sha, final),
                )
                self.state.update_active_work(repo.full_name, status="repair_requested", head_sha=head_sha)
                self.state.transition(repo.full_name, RunState.REPAIRING)
                return CycleResult(
                    self._emit(
                        repo,
                        run_id,
                        RunState.REPAIRING,
                        "FINAL_REVIEW_BLOCKED",
                        final.summary,
                        "The final reviewer supplied a blocker without an owner-escalation prefix; autonomous repair remains possible.",
                        fingerprint,
                        {
                            "next_action": "Repair the final-review blocker or return a correctly classified owner blocker.",
                            "links": [pr.get("url")],
                            "pr": number,
                            "head_sha": head_sha,
                            "blocker": final.owner_blocker,
                            **ux_evidence,
                        },
                    ),
                    2,
                )
            self.github.comment_pull_request(
                repo,
                number,
                _final_review_comment("Owner decision required", head_sha, final),
            )
            self.state.update_active_work(repo.full_name, status="owner_blocked", head_sha=head_sha)
            self.state.transition(repo.full_name, RunState.BLOCKED)
            return CycleResult(
                self._emit(
                    repo,
                    run_id,
                    RunState.BLOCKED,
                    "ATTENTION_REQUIRED",
                    final.summary,
                    final.owner_blocker,
                    fingerprint,
                    {
                        "next_action": "Resolve the owner blocker, then rerun the active Captain cycle.",
                        "links": [pr.get("url")],
                        "pr": number,
                        "head_sha": head_sha,
                        "blocker": final.owner_blocker,
                        "blocker_kind": blocker_kind.value,
                        **ux_evidence,
                    },
                ),
                2,
            )
        if final.verdict == FinalVerdict.REQUEST_CHANGES:
            self.github.comment_pull_request(
                repo,
                number,
                _final_review_comment("Final Captain review requested changes", head_sha, final),
            )
            self.state.update_active_work(repo.full_name, status="repair_requested", head_sha=head_sha)
            self.state.transition(repo.full_name, RunState.REPAIRING)
            return CycleResult(
                self._emit(
                    repo,
                    run_id,
                    RunState.REPAIRING,
                    "FINAL_REVIEW_BLOCKED",
                    final.summary,
                    "The final Captain gate requested changes.",
                    fingerprint,
                    {
                        "next_action": "Repair the stated final-gate blockers.",
                        "links": [pr.get("url")],
                        **ux_evidence,
                    },
                ),
                2,
            )

        gate = self.github.gate(repo, number, review_head_sha=head_sha)
        if repo.completion_policy == CompletionPolicy.OWNER_APPROVAL:
            completion = evaluate_owner_completion(repo, final.verdict, gate)
        elif repo.completion_policy == CompletionPolicy.CONTROL_PLANE_COMPLETE:
            completion = evaluate_control_plane_completion(repo, final.verdict, gate)
        else:
            completion = evaluate_merge(repo, final.verdict, gate)

        if completion.allowed and repo.completion_policy == CompletionPolicy.AUTO_MERGE:
            self.github.comment_pull_request(
                repo,
                number,
                _final_review_comment("Autonomous merge gates passed", head_sha, final),
            )
            self.github.merge(repo, number)
            merge_commit_sha = self.github.default_branch_sha(repo)
            self.state.clear_active_work(repo.full_name)
            self.state.transition(repo.full_name, RunState.COMPLETION_READY)
            self.state.transition(repo.full_name, RunState.MERGED)
            self.state.transition(repo.full_name, RunState.POST_MERGE_VERIFICATION)
            return CycleResult(
                self._emit(
                    repo,
                    run_id,
                    RunState.POST_MERGE_VERIFICATION,
                    "PR_MERGED",
                    final.summary,
                    "Independent review, final review, current-head evidence, required checks, and merge policy all passed.",
                    fingerprint,
                    {
                        "next_action": "Verify main CI and deployment outcomes before selecting dependent work.",
                        "links": [pr.get("url")],
                        "merged_head_sha": merge_commit_sha,
                        "pr_head_sha": head_sha,
                        "course_key": decision.course_key,
                        "work_package_key": decision.work_package_key,
                        **ux_evidence,
                    },
                ),
                0,
            )

        if completion.allowed:
            owner_completion = repo.completion_policy == CompletionPolicy.OWNER_APPROVAL
            event_type = "COMPLETION_READY" if owner_completion else "CONTROL_PLANE_COMPLETED"
            next_action = (
                "Apply the configured owner completion decision."
                if owner_completion
                else "The Captain work item is complete; merge remains an owner action."
            )
            self.github.comment_pull_request(
                repo,
                number,
                _final_review_comment("Ready for configured completion decision", head_sha, final),
            )
            self.state.update_active_work(repo.full_name, status="completion_ready", head_sha=head_sha)
            self._set_course_package_status(
                repo,
                decision,
                WorkPackageStatus.COMPLETE
                if repo.completion_policy == CompletionPolicy.CONTROL_PLANE_COMPLETE
                else WorkPackageStatus.REVIEWING,
            )
            self.state.transition(repo.full_name, RunState.COMPLETION_READY)
            return CycleResult(
                self._emit(
                    repo,
                    run_id,
                RunState.COMPLETION_READY,
                    event_type,
                    final.summary,
                    completion.reason,
                    fingerprint,
                    {
                        "next_action": next_action,
                        "links": [pr.get("url")],
                        "owner_required": owner_completion,
                        **ux_evidence,
                    },
                ),
                2 if owner_completion else 0,
            )

        self.state.transition(repo.full_name, RunState.PR_OPEN)
        self.state.update_active_work(repo.full_name, status="pr_open", head_sha=head_sha)
        return CycleResult(
            self._emit(
                repo,
                run_id,
                RunState.PR_OPEN,
                "REVIEW_WAITING",
                final.summary,
                completion.reason,
                fingerprint,
                {
                    "next_action": "Wait for the missing gate evidence, then rerun current-head review.",
                    "links": [pr.get("url")],
                    "completion_gate": completion.reason,
                    **comment_evidence,
                    **ux_evidence,
                },
            ),
            2,
        )

    def _verify_post_merge(
        self,
        repo: RepoConfig,
        run_id: str,
        snapshot: RepositorySnapshot,
        snapshot_fingerprint: str,
    ) -> CycleResult:
        merge_event = next(
            (
                event
                for event in self.state.recent_events(repo.full_name, 30)
                if event.event_type == "PR_MERGED" and event.evidence.get("merged_head_sha")
            ),
            None,
        )
        if merge_event is None:
            event = self._emit(
                repo,
                run_id,
                RunState.DEGRADED,
                "POST_MERGE_EVIDENCE_MISSING",
                "Post-merge verification cannot identify the merged head.",
                "The merge event did not record a head SHA.",
                snapshot_fingerprint,
                {"next_action": "Inspect the merge event before dispatching dependent work."},
            )
            return CycleResult(event, 2)
        head_sha = (
            str(merge_event.evidence["merged_head_sha"])
            if merge_event.evidence.get("merged_head_sha")
            else self.github.default_branch_sha(repo)
        )
        outcome, reason, links = classify_post_merge_runs(
            snapshot.workflow_runs, head_sha, repo.deploy_is_merge_gate
        )
        fingerprint = _fingerprint(
            {"snapshot": snapshot_fingerprint, "head_sha": head_sha, "outcome": outcome}
        )
        recent_event = self.state.latest_operational_event(repo.full_name)
        recent = [recent_event] if recent_event is not None else []
        if recent and recent[0].event_type == "POST_MERGE_WAITING" and recent[0].fingerprint == fingerprint:
            return CycleResult(recent[0], 2)
        if outcome == "waiting":
            event = self._emit(
                repo,
                run_id,
                RunState.POST_MERGE_VERIFICATION,
                "POST_MERGE_WAITING",
                "Waiting for current-head main CI evidence.",
                reason,
                fingerprint,
                {"next_action": "Recheck after GitHub workflow state changes.", "links": links},
            )
            return CycleResult(event, 2)
        if outcome == "failed":
            event = self._emit(
                repo,
                run_id,
                RunState.DEGRADED,
                "POST_MERGE_FAILED",
                "Post-merge verification failed.",
                reason,
                fingerprint,
                {"next_action": "Diagnose the failed main workflow before dependent work.", "links": links},
            )
            return CycleResult(event, 2)
        self.state.transition(repo.full_name, RunState.READY)
        course_key = merge_event.evidence.get("course_key")
        work_package_key = merge_event.evidence.get("work_package_key")
        if isinstance(course_key, str) and isinstance(work_package_key, str):
            self._set_course_package_status(
                repo,
                PlanDecision(
                    action=ActionKind.IMPLEMENT,
                    summary="post-merge course package",
                    reason="post-merge verification",
                    course_key=course_key,
                    work_package_key=work_package_key,
                ),
                WorkPackageStatus.COMPLETE,
            )
        event = self._emit(
            repo,
            run_id,
            RunState.READY,
            "POST_MERGE_VERIFIED",
            "Main CI passed for the merged head.",
            reason,
            fingerprint,
            {"next_action": "Select the next unblocked work item.", "links": links},
        )
        return CycleResult(event, 0)

    def _changed_paths(self, path: Path) -> list[str]:
        result = self.runner(["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"])
        require_success(result, "inspect worker changes")
        values: list[str] = []
        for line in result.stdout.splitlines():
            relative = line[3:].split(" -> ")[-1].strip()
            if relative:
                values.append(Path(relative).as_posix())
        return sorted(set(values))

    def _sync_default_branch_if_clean(self, repo: RepoConfig) -> bool:
        status = self.runner(["git", "-C", str(repo.local_path), "status", "--porcelain"])
        if status.returncode or status.stdout.strip():
            return False
        branch = self.runner(["git", "-C", str(repo.local_path), "branch", "--show-current"])
        if branch.returncode or branch.stdout.strip() != repo.default_branch:
            return False
        fetch = self.runner(
            ["git", "-C", str(repo.local_path), "fetch", "origin", repo.default_branch],
            timeout=300,
        )
        if fetch.returncode:
            return False
        merge = self.runner(
            ["git", "-C", str(repo.local_path), "merge", "--ff-only", f"origin/{repo.default_branch}"],
            timeout=300,
        )
        return merge.returncode == 0

    def _planning_document(self, repo: RepoConfig, default_branch_synced: bool) -> PlanningDocument:
        local_path = repo.local_path / repo.planning_doc
        if default_branch_synced:
            text = (
                local_path.read_text(encoding="utf-8", errors="replace")[:100_000]
                if local_path.is_file()
                else "<missing>"
            )
            source = f"local:{repo.default_branch}"
        else:
            root = repo.local_path.resolve()
            fetch = self.runner(
                ["git", "-C", str(root), "fetch", "--quiet", "origin", repo.default_branch],
                timeout=300,
            )
            show = (
                self.runner(
                    [
                        "git",
                        "-C",
                        str(root),
                        "show",
                        f"origin/{repo.default_branch}:{repo.planning_doc}",
                    ],
                    timeout=60,
                )
                if fetch.returncode == 0
                else fetch
            )
            if show.returncode == 0:
                text = show.stdout[:100_000]
                source = f"origin/{repo.default_branch}"
            elif not (root / ".git").exists():
                text = (
                    local_path.read_text(encoding="utf-8", errors="replace")[:100_000]
                    if local_path.is_file()
                    else "<missing>"
                )
                source = "local:unverified-no-git-metadata"
            else:
                detail = (show.stderr or show.stdout).strip()[:1000]
                raise RuntimeError(
                    f"could not read origin/{repo.default_branch}:{repo.planning_doc}: {detail or 'unknown git error'}"
                )
        return PlanningDocument(
            text=text,
            source=source,
            fingerprint=_fingerprint({"source": source, "text": text}),
        )

    def _assert_worktree_identity(self, worktree: Worktree) -> None:
        branch = require_success(
            self.runner(["git", "-C", str(worktree.path), "branch", "--show-current"]),
            "inspect worker branch",
        ).strip()
        if branch != worktree.branch:
            raise RuntimeError(
                f"worker changed branch identity: expected {worktree.branch}, observed {branch}"
            )

    def _discard_failed_worktree(
        self,
        repo: RepoConfig,
        worktree: Worktree,
        original_error: BaseException,
    ) -> None:
        discard = getattr(self.worktrees, "discard", None)
        try:
            if callable(discard):
                discard(repo, worktree)
            else:
                # Test doubles and older adapter shims may only expose clean removal.
                self.worktrees.remove(repo, worktree)
        except Exception as cleanup_error:
            raise RuntimeError(
                f"{original_error}; failed to clean failed worktree {worktree.path}: {cleanup_error}"
            ) from original_error

    def _cleanup_successful_worktree(self, repo: RepoConfig, worktree: Worktree) -> str | None:
        """Keep a successful PR resumable even when clean local cleanup needs a force discard."""
        try:
            self.worktrees.remove(repo, worktree)
            return None
        except Exception as remove_error:
            discard = getattr(self.worktrees, "discard", None)
            if not callable(discard):
                return f"clean worktree removal failed: {str(remove_error)[:500]}"
            try:
                discard(repo, worktree)
            except Exception as discard_error:
                return (
                    f"clean removal failed ({str(remove_error)[:250]}); "
                    f"force discard also failed ({str(discard_error)[:250]})"
                )
            return f"clean removal failed; force-discarded local worktree: {str(remove_error)[:500]}"

    def _run_checks(
        self, path: Path, checks: tuple[str, ...], *, source_path: Path
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for check in checks:
            command = worktree_check_command(check, source_path, path)
            result = self.runner(command, cwd=path, timeout=1800)
            results.append(
                {
                    "command": check,
                    "executed_command": shlex.join(command),
                    "returncode": result.returncode,
                    "stdout_tail": result.stdout[-2000:],
                    "stderr_tail": result.stderr[-2000:],
                }
            )
        return results

    def _commit_and_push(self, worktree: Worktree, changed: list[str], message: str) -> None:
        if changed:
            require_success(
                self.runner(["git", "-C", str(worktree.path), "add", "--", *changed]), "stage changes"
            )
            require_success(
                self.runner(["git", "-C", str(worktree.path), "commit", "-m", message[:180]], timeout=180),
                "commit changes",
            )
        ahead = self.runner(
            ["git", "-C", str(worktree.path), "rev-list", "--count", f"{worktree.base}..HEAD"]
        )
        require_success(ahead, "count branch commits")
        if int(ahead.stdout.strip() or "0") <= 0:
            raise RuntimeError("worker produced no commit or changed file")
        require_success(
            self.runner(
                [
                    "git",
                    "-C",
                    str(worktree.path),
                    "push",
                    "-u",
                    "origin",
                    f"HEAD:refs/heads/{worktree.push_branch}",
                ],
                timeout=300,
            ),
            "push branch",
        )

    def _emit(
        self,
        repo: RepoConfig,
        run_id: str,
        state: RunState,
        event_type: str,
        summary: str,
        reason: str,
        fingerprint: str,
        evidence: dict[str, Any],
        notify: bool = True,
    ) -> EventRecord:
        self.state.transition(repo.full_name, state)
        if requires_owner_attention(event_type, evidence):
            evidence = {
                **evidence,
                "attention_level": self.state.note_attention(repo.full_name, fingerprint, event_type),
            }
        event = self.state.record_event(
            repo=repo.full_name,
            run_id=run_id,
            state=state,
            event_type=event_type,
            summary=summary,
            reason=reason,
            fingerprint=fingerprint,
            evidence=evidence,
        )
        if notify:
            try:
                self.notifier.send(event)
            except NotificationError as exc:
                self.state.record_notification_failure(event, str(exc))
                raise
        return event


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _approval_fingerprint(snapshot: RepositorySnapshot) -> str:
    """Bind approval to repository state while ignoring volatile workflow history."""
    evidence = snapshot.as_dict()
    evidence.pop("workflow_runs", None)
    return _fingerprint(evidence)


def _worker_result_or_raise(value: dict[str, Any]) -> WorkerResult:
    worker = WorkerResult.model_validate(value)
    if worker.blocked:
        raise WorkerBlockedError(worker.blocker or "TECHNICAL: worker reported a blocker")
    return worker


def _pr_link(repo: RepoConfig, number: int | None) -> str | None:
    return f"https://github.com/{repo.full_name}/pull/{number}" if number else None


def worktree_check_command(check: str, source_path: Path, worktree_path: Path) -> list[str]:
    translated = check.replace(source_path.as_posix(), worktree_path.as_posix())
    translated = translated.replace(str(source_path), str(worktree_path))
    return shlex.split(translated, posix=True)


def select_checks(
    checks: tuple[str, ...], changed_paths: list[str], ux_paths: tuple[str, ...]
) -> tuple[str, ...]:
    """Run targeted local checks; GitHub CI remains the complete repository gate."""
    frontend_changed = any(
        any(path.startswith(pattern.rstrip("*")) or fnmatch(path, pattern) for pattern in ux_paths)
        for path in changed_paths
    )
    if frontend_changed:
        return checks
    return tuple(
        check
        for check in checks
        if not any(token in check.lower() for token in ("npm", "node:", "frontend"))
    )


def _failed_check_summary(failed: list[dict[str, Any]]) -> str:
    summaries: list[str] = []
    for item in failed:
        output = str(item.get("stderr_tail") or item.get("stdout_tail") or "no output")
        output = " ".join(output.split())
        summaries.append(
            f"{item.get('command')} exited {item.get('returncode')}: {output[:320]}"
        )
    return "; ".join(summaries)


def _next_action(decision: PlanDecision, policy_reason: str) -> str:
    return f"{decision.summary} ({policy_reason})"


def _worker_prompt(repo: RepoConfig, decision: PlanDecision, worktree: Worktree) -> str:
    decision_data = decision.model_dump(mode="json")
    decision_data["checks"] = []
    return (
        load_prompt("coder.md")
        + "\n\n"
        + json.dumps(
            {
                "repo": repo.full_name,
                "worktree": str(worktree.path),
                "branch": worktree.branch,
                "decision": decision_data,
                "required_project_manifest": (
                    repo.project_manifest if repo.require_project_manifest else None
                ),
            },
            indent=2,
            default=str,
        )
    )


def _pull_request_body(
    repo: RepoConfig, decision: PlanDecision, paths: list[str], checks: list[dict[str, Any]], model: str
) -> str:
    lines = [
        "## Make It So work item",
        "",
        f"Repository: {repo.full_name}",
        f"Work item: {decision.work_item_id or 'unassigned'}",
        f"Model: {model}",
        "",
        "## Goal",
        "",
        decision.summary,
        "",
        decision.reason,
        "",
        "## Acceptance criteria",
        "",
    ]
    lines.extend(f"- {item}" for item in decision.acceptance_criteria)
    lines.extend(["", "## Changed files", ""])
    lines.extend(f"- {path}" for path in paths)
    lines.extend(["", "## Verification", ""])
    lines.extend(f"- {item['command']}: rc={item['returncode']}" for item in checks)
    return "\n".join(lines)


def _review_comment(title: str, head_sha: str, review: IndependentReview) -> str:
    lines = [f"## {title}", "", f"Reviewed head: `{head_sha}`", "", review.summary]
    if review.findings:
        lines.extend(["", "### Findings", ""])
        for finding in review.findings:
            location = f" ({finding.path}:{finding.line})" if finding.path else ""
            lines.append(f"- **{finding.priority} {finding.title}**{location}: {finding.detail}")
    if review.residual_risks:
        lines.extend(["", "### Residual risks", ""])
        lines.extend(f"- {risk}" for risk in review.residual_risks)
    return "\n".join(lines)


def _comment_triage_comment(title: str, head_sha: str, triage: CommentTriage) -> str:
    lines = [f"## {title}", "", f"Adjudicated head: `{head_sha}`", "", triage.summary]
    if triage.decisions:
        lines.extend(["", "### Decisions", ""])
        for decision in triage.decisions:
            lines.append(f"- `{decision.thread_id}`: **{decision.disposition.value}**: {decision.rationale}")
    if triage.accepted_findings:
        lines.extend(["", "### Accepted findings", ""])
        for finding in triage.accepted_findings:
            location = f" ({finding.path}:{finding.line})" if finding.path else ""
            lines.append(f"- **{finding.priority} {finding.title}**{location}: {finding.detail}")
    if triage.owner_decisions:
        lines.extend(["", "### Owner decisions", ""])
        lines.extend(f"- {item}" for item in triage.owner_decisions)
    return "\n".join(lines)


def _ux_review_comment(title: str, head_sha: str, review: UXReview) -> str:
    lines = [f"## {title}", "", f"Reviewed head: `{head_sha}`", "", review.summary]
    lines.extend(
        [
            "",
            f"Contrast passed: {review.contrast_passed}",
            f"Functionality passed: {review.functionality_passed}",
            f"Cohesion passed: {review.cohesion_passed}",
        ]
    )
    if review.flows_tested:
        lines.extend(["", "### Flows tested", ""])
        lines.extend(f"- {flow}" for flow in review.flows_tested)
    if review.findings:
        lines.extend(["", "### Findings", ""])
        for finding in review.findings:
            location = f" ({finding.path}:{finding.line})" if finding.path else ""
            lines.append(f"- **{finding.priority} {finding.title}**{location}: {finding.detail}")
    return "\n".join(lines)


def _final_review_comment(title: str, head_sha: str, review: FinalReview) -> str:
    lines = [
        f"## {title}",
        "",
        f"Reviewed head: `{head_sha}`",
        f"Verdict: `{review.verdict.value}`",
        "",
        review.summary,
        "",
        f"Scope match: {review.scope_match}",
        f"Checks green: {review.checks_green}",
        f"Unresolved threads: {review.unresolved_threads}",
    ]
    if review.residual_risks:
        lines.extend(["", "### Residual risks", ""])
        lines.extend(f"- {risk}" for risk in review.residual_risks)
    if review.owner_blocker:
        lines.extend(["", f"Owner blocker: `{review.owner_blocker}`"])
    return "\n".join(lines)


def classify_post_merge_runs(
    workflow_runs: list[dict[str, Any]], head_sha: str, deploy_is_gate: bool
) -> tuple[str, str, list[str]]:
    matching = [item for item in workflow_runs if str(item.get("headSha") or "") == head_sha]
    deploy = [item for item in matching if "deploy" in str(item.get("workflowName") or "").lower()]
    ci = [item for item in matching if item not in deploy]
    links = [str(item.get("url")) for item in matching if item.get("url")]
    required = [*ci, *deploy] if deploy_is_gate else ci
    if not required or any(str(item.get("status") or "").lower() != "completed" for item in required):
        return "waiting", "Required main workflows are missing or still running.", links
    failures = [
        item
        for item in required
        if str(item.get("conclusion") or "").lower() not in {"success", "neutral", "skipped"}
    ]
    if failures:
        names = ", ".join(str(item.get("workflowName") or "unknown") for item in failures)
        return "failed", f"Required main workflows failed: {names}.", links
    deploy_failures = [
        item
        for item in deploy
        if str(item.get("status") or "").lower() == "completed"
        and str(item.get("conclusion") or "").lower() not in {"success", "neutral", "skipped"}
    ]
    if deploy_failures:
        return (
            "passed",
            "Main CI passed; deployment failed and remains a separate release blocker.",
            links,
        )
    return "passed", "All required main workflows passed for the merged head.", links
