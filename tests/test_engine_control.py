from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from make_it_so.courses import CourseError, CourseStore, approve_course
from make_it_so.engine import ControlPlaneEngine, ModelCallSuppressedError
from make_it_so.github import RepositorySnapshot
from make_it_so.harness import HarnessExecutionError
from make_it_so.models import (
    ActionKind,
    ApplicationSurface,
    CourseStatus,
    EventRecord,
    HarnessConfig,
    HarnessResult,
    ModelAttempt,
    ModelProfile,
    ModelTarget,
    OperationMode,
    PlanDecision,
    RepoConfig,
    RequirementStatus,
    RunState,
    UsageConfig,
    WorkerResult,
    WorkPackageStatus,
)
from make_it_so.readiness import REQUIRED_READINESS_CATEGORIES
from make_it_so.state import StateStore
from make_it_so.worktrees import Worktree
from tests.helpers import app_config, model_policy, repo_config
from tests.test_courses import course, ready_course, rebind_readiness_review


class NoopNotifier:
    def send(self, event: Any) -> None:
        del event


def test_declared_web_ui_requires_acceptance_review_when_legacy_toggle_is_disabled(tmp_path: Path) -> None:
    repo = repo_config(tmp_path).model_copy(
        update={"surfaces": frozenset({ApplicationSurface.WEB_UI}), "ux_enabled": False}
    )
    engine = object.__new__(ControlPlaneEngine)
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement the dashboard",
        reason="The planned web UI package is ready.",
    )

    assert engine._requires_ux_review(repo, {"files": []}, decision) is True  # pyright: ignore[reportPrivateUsage]


class SuccessfulHarness:
    config = HarnessConfig(kind="codex", executable="codex")

    def run(self, **kwargs: Any) -> HarnessResult:
        del kwargs
        return HarnessResult(
            role="test",
            output=WorkerResult(summary="model completed").model_dump(mode="json"),
            attempts=(ModelAttempt(model="test-model", success=True, total_tokens=12, duration_ms=0),),
            resolved_model="test-model",
            session_id="session-1",
        )


class FailingHarness:
    config = HarnessConfig(kind="codex", executable="codex")

    def run(self, **kwargs: Any) -> HarnessResult:
        del kwargs
        raise HarnessExecutionError(
            "provider unavailable",
            attempts=(ModelAttempt(model="test-model", success=False, total_tokens=7, duration_ms=0),),
            session_id="session-failed",
        )


class EmptyAttemptFailingHarness(FailingHarness):
    def run(self, **kwargs: Any) -> HarnessResult:
        del kwargs
        raise HarnessExecutionError("provider unavailable without attempts")


class ReadinessHarness(SuccessfulHarness):
    def run(self, **kwargs: Any) -> HarnessResult:
        assert kwargs["role"] == "readiness_reviewer"
        assert kwargs["writable"] is False
        assert '"authenticated": true' in kwargs["prompt"]
        assert '"operation_mode": "supervised"' in kwargs["prompt"]
        return HarnessResult(
            role="readiness_reviewer",
            output={
                "verdict": "ready",
                "summary": "All prerequisites are covered.",
                "checks": [
                    {
                        "category": category,
                        "status": "verified",
                        "finding": f"{category} covered",
                        "evidence": ["repository evidence"],
                    }
                    for category in REQUIRED_READINESS_CATEGORIES
                ],
                "requirements": [
                    {
                        "key": "success",
                        "verified": True,
                        "finding": "Success is measurable.",
                        "evidence": ["README.md"],
                    }
                ],
                "next_questions": [],
            },
            attempts=(ModelAttempt(model="test-model", success=True, duration_ms=1),),
            resolved_model="test-model",
            session_id="readiness-session",
        )


class SnapshotGitHub:
    def __init__(self) -> None:
        self.calls = 0

    def snapshot(self, repo: Any) -> RepositorySnapshot:
        del repo
        self.calls += 1
        return RepositorySnapshot({}, [], [], ["main"], [])

    def readiness_evidence(self, repo: Any) -> dict[str, Any]:
        del repo
        return {"authenticated": True, "default_branch_sha": "main-sha"}


class ActivePrGitHub(SnapshotGitHub):
    def pull_request(self, repo: Any, number: int) -> dict[str, Any]:
        del repo
        return {
            "number": number,
            "headRefName": "make_it_so/work/42",
            "headRefOid": "head-42",
            "url": "https://github.com/example/project/pull/42",
        }


class IssueMutationGitHub(SnapshotGitHub):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, object]] = []

    def create_issue(self, repo: RepoConfig, title: str, body: str) -> dict[str, Any]:
        del repo
        self.calls.append(("create", (title, body)))
        return {"url": "https://github.test/issues/7"}

    def update_issue(
        self, repo: RepoConfig, number: int, title: str | None, body: str | None
    ) -> None:
        del repo
        self.calls.append(("update", (number, title, body)))

    def label_issue(self, repo: RepoConfig, number: int, labels: tuple[str, ...]) -> None:
        del repo
        self.calls.append(("label", (number, labels)))

    def retarget_issue(
        self,
        repo: RepoConfig,
        number: int,
        milestone: str | None,
        assignees: tuple[str, ...],
    ) -> None:
        del repo
        self.calls.append(("retarget", (number, milestone, assignees)))

    def close_issue(self, repo: RepoConfig, number: int, reason: str) -> None:
        del repo
        self.calls.append(("close", (number, reason)))


class ImplementationGitHub(SnapshotGitHub):
    def create_pull_request(
        self,
        repo: RepoConfig,
        *,
        branch: str,
        title: str,
        body: str,
        draft: bool = True,
    ) -> dict[str, Any]:
        del repo, title, body, draft
        return {
            "number": 19,
            "url": "https://github.test/example/project/pull/19",
            "headRefOid": "head-19",
            "headRefName": branch,
        }


def make_engine(tmp_path: Path, *, mode: OperationMode = OperationMode.SUPERVISED, harness: Any = None, config: Any = None) -> tuple[ControlPlaneEngine, StateStore]:
    repo = repo_config(tmp_path, mode=mode)
    app = config or app_config(tmp_path, repo)
    state = StateStore(app.state_dir / "state.db")
    engine = ControlPlaneEngine(
        app,
        state,
        cast(Any, SimpleNamespace()),
        cast(Any, harness or SuccessfulHarness()),
        cast(Any, NoopNotifier()),
        model_policy(),
    )
    return engine, state


def test_run_model_records_success_and_respects_disabled_mode(tmp_path: Path) -> None:
    engine, state = make_engine(tmp_path)
    repo = repo_config(tmp_path, mode=OperationMode.SUPERVISED)
    result = engine.run_model(
        repo,
        "run-1",
        "coder",
        "bounded prompt",
        models=model_policy().coder,
        output_model=WorkerResult,
        cwd=tmp_path,
        writable=False,
    )
    assert result.resolved_model == "test-model"
    assert state.usage_summary(repo=repo.full_name)["direct_calls"]["calls"] == 1

    disabled_engine, _ = make_engine(tmp_path / "disabled", mode=OperationMode.DISABLED)
    with pytest.raises(ModelCallSuppressedError, match="disabled"):
        disabled_engine.run_model(
            repo_config(tmp_path / "disabled", mode=OperationMode.DISABLED),
            "run-disabled",
            "coder",
            "prompt",
            models=model_policy().coder,
            output_model=WorkerResult,
            cwd=tmp_path,
            writable=False,
        )


def test_engine_runs_readiness_reviewer_and_persists_awaiting_approval(tmp_path: Path) -> None:
    engine, state = make_engine(tmp_path, harness=ReadinessHarness())
    engine.github = cast(Any, SnapshotGitHub())
    repo = repo_config(tmp_path, mode=OperationMode.SUPERVISED)
    value = course()
    answered = value.readiness[0].model_copy(
        update={
            "status": RequirementStatus.ANSWERED,
            "answer": "The ranked search flow meets its documented latency target.",
        }
    )
    CourseStore(tmp_path).save(value.model_copy(update={"readiness": (answered,)}))

    reviewed = engine.review_course_readiness(repo, value.key, "run-readiness")

    assert reviewed.status == CourseStatus.AWAITING_APPROVAL
    assert reviewed.readiness_review is not None
    assert reviewed.readiness_review.session_id == "readiness-session"
    assert CourseStore(tmp_path).load(value.key).status == CourseStatus.AWAITING_APPROVAL
    usage = state.usage_summary(repo=repo.full_name)
    assert usage["direct_calls"]["calls"] == 1
    dimensions = state.usage_dimensions(repo.full_name)
    assert dimensions[0]["course_key"] == value.key
    assert dimensions[0]["stage"] == "readiness_review"


def test_run_model_records_failed_attempts_and_usage_suppression(tmp_path: Path) -> None:
    failing, state = make_engine(tmp_path, harness=FailingHarness())
    repo = repo_config(tmp_path)
    with pytest.raises(HarnessExecutionError, match="provider unavailable"):
        failing.run_model(
            repo,
            "run-failed",
            "coder",
            "prompt",
            models=model_policy().coder,
            output_model=WorkerResult,
            cwd=tmp_path,
            writable=False,
        )
    assert state.usage_summary(repo=repo.full_name)["direct_calls"]["calls"] == 1

    limited_config = app_config(
        tmp_path / "limited",
        repo_config(tmp_path / "limited"),
    ).model_copy(update={"usage": UsageConfig(daily_token_limit=1, block_on_unknown=False)})
    limited, limited_state = make_engine(tmp_path / "limited", config=limited_config)
    limited_state.record_model_call(
        "example/project",
        "old-run",
        "coder",
        "test-model",
        [{"total_tokens": 1, "input_tokens": 1, "output_tokens": 0}],
    )
    with pytest.raises(ModelCallSuppressedError, match="daily token limit"):
        limited.run_model(
            repo_config(tmp_path / "limited"),
            "run-limited",
            "coder",
            "prompt",
            models=model_policy().coder,
            output_model=WorkerResult,
            cwd=tmp_path,
            writable=False,
        )


def test_run_model_does_not_record_unknown_usage_when_provider_returns_no_attempts(tmp_path: Path) -> None:
    engine, state = make_engine(tmp_path, harness=EmptyAttemptFailingHarness())
    repo = repo_config(tmp_path)

    with pytest.raises(HarnessExecutionError, match="without attempts"):
        engine.run_model(
            repo,
            "run-empty-attempts",
            "coder",
            "prompt",
            models=model_policy().coder,
            output_model=WorkerResult,
            cwd=tmp_path,
            writable=False,
        )

    assert state.usage_summary(repo=repo.full_name)["direct_calls"]["calls"] == 0


@pytest.mark.parametrize(
    ("sync_result", "expected"),
    (({"status": "degraded", "reason": "stale"}, "usage telemetry sync was degraded"),),
)
def test_run_model_suppresses_degraded_usage_sync(
    tmp_path: Path,
    sync_result: dict[str, object],
    expected: str,
) -> None:
    repo = repo_config(tmp_path)
    config = app_config(tmp_path, repo).model_copy(
        update={"usage": UsageConfig(daily_token_limit=100, block_on_unknown=False)}
    )
    engine, _ = make_engine(tmp_path, config=config)

    class Sync:
        def synchronize(self, state: StateStore, repo: RepoConfig) -> dict[str, Any]:
            del state, repo
            return sync_result

    engine.usage_sync = Sync()
    with pytest.raises(ModelCallSuppressedError, match=expected):
        engine.run_model(
            repo,
            "run-sync-degraded",
            "coder",
            "prompt",
            models=model_policy().coder,
            output_model=WorkerResult,
            cwd=tmp_path,
            writable=False,
        )


def test_run_model_suppresses_usage_sync_failures(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    config = app_config(tmp_path, repo).model_copy(
        update={"usage": UsageConfig(daily_token_limit=100, block_on_unknown=False)}
    )
    engine, _ = make_engine(tmp_path, config=config)

    class Sync:
        def synchronize(self, state: StateStore, repo: RepoConfig) -> dict[str, Any]:
            del state, repo
            raise RuntimeError("usage backend unavailable")

    engine.usage_sync = Sync()
    with pytest.raises(ModelCallSuppressedError, match="usage telemetry sync failed"):
        engine.run_model(
            repo,
            "run-sync-failed",
            "coder",
            "prompt",
            models=model_policy().coder,
            output_model=WorkerResult,
            cwd=tmp_path,
            writable=False,
        )


def test_record_model_suppressed_persists_a_direct_command_blocker_without_notifying(
    tmp_path: Path,
) -> None:
    engine, state = make_engine(tmp_path)
    repo = repo_config(tmp_path)
    error = ModelCallSuppressedError(
        repo.full_name,
        "planner",
        {"allowed": False, "reason": "daily token limit reached"},
    )

    result = engine.record_model_suppressed(repo, error, notify=False)

    assert result.event.event_type == "MODEL_CALL_SUPPRESSED"
    assert result.event.evidence["role"] == "planner"
    assert state.latest_operational_event(repo.full_name) == result.event


def test_course_context_ignores_completed_courses(tmp_path: Path) -> None:
    engine, _ = make_engine(tmp_path)
    completed = course().model_copy(update={"status": CourseStatus.COMPLETED})
    CourseStore(tmp_path).save(completed)

    selected, blocker = engine._course_context(repo_config(tmp_path))  # pyright: ignore[reportPrivateUsage]

    assert selected is None
    assert blocker is None


def test_course_context_fails_closed_when_course_is_required_but_missing(tmp_path: Path) -> None:
    engine, _ = make_engine(tmp_path)

    selected, blocker = engine._course_context(  # pyright: ignore[reportPrivateUsage]
        repo_config(tmp_path, require_engaged_course=True)
    )

    assert selected is None
    assert blocker == "an approved course is required before repository work can begin"


def test_course_context_returns_invalid_state_blocker(tmp_path: Path) -> None:
    engine, _ = make_engine(tmp_path)
    courses = tmp_path / ".make-it-so" / "courses"
    courses.mkdir(parents=True)
    (courses / "broken.yaml").write_text("status: [not valid", encoding="utf-8")

    selected, blocker = engine._course_context(repo_config(tmp_path))  # pyright: ignore[reportPrivateUsage]

    assert selected is None
    assert blocker is not None and blocker.startswith("course state is invalid")


def test_cycle_requires_an_analyzed_baseline_before_reading_repository_state(tmp_path: Path) -> None:
    engine, state = make_engine(tmp_path, mode=OperationMode.AUTONOMOUS)
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)

    result = engine.cycle(repo, shadow=False, execute=True)

    assert result.event.event_type == "BASELINE_REQUIRED"
    assert result.exit_code == 2
    assert state.current_state(repo.full_name) == RunState.DEGRADED


def test_watch_returns_none_without_baseline_or_active_work(tmp_path: Path) -> None:
    engine, state = make_engine(tmp_path, mode=OperationMode.AUTONOMOUS)
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)

    assert engine.watch(repo) is None

    artifact = tmp_path / "baseline.json"
    artifact.write_text("{}", encoding="utf-8")
    state.save_baseline(repo.full_name, "baseline", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    engine.github = cast(Any, SnapshotGitHub())
    assert engine.watch(repo) is None

    state.transition(repo.full_name, RunState.READY)
    state.transition(repo.full_name, RunState.PR_OPEN)
    assert engine.watch(repo) is None


def test_watch_reports_a_paused_course_before_touching_github(tmp_path: Path) -> None:
    engine, state = make_engine(tmp_path, mode=OperationMode.AUTONOMOUS)
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    artifact = tmp_path / "baseline.json"
    artifact.write_text("{}", encoding="utf-8")
    state.save_baseline(repo.full_name, "baseline", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    paused = approve_course(ready_course(), "owner@example.com").model_copy(
        update={"status": CourseStatus.PAUSED}
    )
    CourseStore(tmp_path).save(paused)
    github = SnapshotGitHub()
    engine.github = cast(Any, github)

    result = engine.watch(repo)

    assert result is not None
    assert result.event.event_type == "COURSE_PAUSED"
    assert result.exit_code == 2
    assert github.calls == 0


def test_watch_reports_active_pr_status_in_shadow_mode(tmp_path: Path) -> None:
    engine, state = make_engine(tmp_path, mode=OperationMode.AUTONOMOUS)
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    artifact = tmp_path / "baseline.json"
    artifact.write_text("{}", encoding="utf-8")
    state.save_baseline(repo.full_name, "baseline", artifact, analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    state.transition(repo.full_name, RunState.PR_OPEN)
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement active package",
        reason="The package is approved.",
        target_pr=42,
    )
    state.save_active_work(
        repo.full_name,
        action_id="active-42",
        pr_number=42,
        branch="make_it_so/work/42",
        head_sha="old-head",
        status="pr_open",
        decision=decision.model_dump(mode="json"),
    )
    engine.github = cast(Any, ActivePrGitHub())

    result = engine.watch(repo, shadow=True)

    assert result is not None
    assert result.event.event_type == "ACTIVE_PR_STATUS"
    assert result.event.evidence["head_sha"] == "head-42"


@pytest.mark.parametrize(
    ("status", "expected_fragment"),
    (
        (CourseStatus.DRAFT, "must pass readiness"),
        (CourseStatus.READINESS_REVIEW, "must pass readiness"),
        (CourseStatus.AWAITING_APPROVAL, "must pass readiness"),
        (CourseStatus.PAUSED, "paused"),
        (CourseStatus.BLOCKED, "blocked"),
        (CourseStatus.ENGAGED, None),
    ),
)
def test_course_context_reports_each_gate_status(
    tmp_path: Path,
    status: CourseStatus,
    expected_fragment: str | None,
) -> None:
    engine, _ = make_engine(tmp_path)
    value = ready_course().model_copy(update={"status": status})
    if status == CourseStatus.ENGAGED:
        value = approve_course(value, "owner@example.com")
    CourseStore(tmp_path).save(value)
    selected, blocker = engine._course_context(repo_config(tmp_path))  # pyright: ignore[reportPrivateUsage]
    assert selected is not None
    if expected_fragment is None:
        assert blocker is None
    else:
        assert blocker is not None and expected_fragment in blocker


def test_course_context_fails_closed_for_multiple_active_courses(tmp_path: Path) -> None:
    engine, _ = make_engine(tmp_path)
    first = approve_course(
        rebind_readiness_review(ready_course().model_copy(update={"key": "first"})),
        "owner@example.com",
    )
    second = course().model_copy(update={"key": "second", "status": CourseStatus.DRAFT})
    CourseStore(tmp_path).save(first)
    CourseStore(tmp_path).save(second)
    selected, blocker = engine._course_context(repo_config(tmp_path))  # pyright: ignore[reportPrivateUsage]
    assert selected is None
    assert blocker is not None and "multiple active courses" in blocker


def test_engine_resolves_course_package_and_stage_model_layers(tmp_path: Path) -> None:
    engine, _ = make_engine(tmp_path)
    repo = repo_config(tmp_path)
    package = course().work_packages[0].model_copy(
        update={"model_profiles": {"coder": ModelProfile(primary=ModelTarget(model="package-model"))}}
    )
    engaged = approve_course(
        rebind_readiness_review(ready_course().model_copy(
            update={
                "work_packages": (package, *course().work_packages[1:]),
                "model_profiles": {
                    "coder": ModelProfile(primary=ModelTarget(model="course-model")),
                    "stage:implementation": ModelProfile(primary=ModelTarget(model="stage-model")),
                },
            }
        )),
        "owner@example.com",
    )
    CourseStore(tmp_path).save(engaged)
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement package",
        reason="The package is ready.",
        course_key=engaged.key,
        work_package_key=package.key,
    )

    package_models = engine._models_for(  # pyright: ignore[reportPrivateUsage]
        repo, "coder", decision=decision
    )
    stage_models = engine._models_for(  # pyright: ignore[reportPrivateUsage]
        repo, "coder", decision=decision, stage="implementation"
    )
    assert package_models.primary.model == "package-model"
    assert stage_models.primary.model == "stage-model"


def test_engine_model_resolution_falls_back_when_course_storage_is_invalid(tmp_path: Path) -> None:
    engine, _ = make_engine(tmp_path)
    courses = tmp_path / ".make-it-so" / "courses"
    courses.mkdir(parents=True)
    (courses / "broken.yaml").write_text("status: [not valid", encoding="utf-8")

    models = engine._models_for(  # pyright: ignore[reportPrivateUsage]
        repo_config(tmp_path), "comment_adjudicator"
    )

    assert models.primary.model == model_policy().reviewer.primary.model


def test_engine_course_package_status_sync_and_usage_sync_fallbacks(tmp_path: Path) -> None:
    engine, _ = make_engine(tmp_path)
    repo = repo_config(tmp_path)
    engaged = approve_course(ready_course(), "owner@example.com")
    CourseStore(tmp_path).save(engaged)
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement package",
        reason="The package is ready.",
        course_key=engaged.key,
        work_package_key="index",
    )

    engine._set_course_package_status(  # pyright: ignore[reportPrivateUsage]
        repo, decision, WorkPackageStatus.EXECUTING
    )
    assert CourseStore(tmp_path).load(engaged.key).work_packages[0].status == WorkPackageStatus.EXECUTING
    engine._set_course_package_status(  # pyright: ignore[reportPrivateUsage]
        repo, PlanDecision(action=ActionKind.IMPLEMENT, summary="No package", reason="No package"), WorkPackageStatus.COMPLETE
    )

    with pytest.raises(CourseError, match="not engaged"):
        CourseStore(tmp_path).save(engaged.model_copy(update={"status": CourseStatus.PAUSED}))
        engine._set_course_package_status(repo, decision, WorkPackageStatus.COMPLETE)  # pyright: ignore[reportPrivateUsage]
    CourseStore(tmp_path).save(engaged)
    with pytest.raises(CourseError, match="no work package"):
        engine._set_course_package_status(  # pyright: ignore[reportPrivateUsage]
            repo, decision.model_copy(update={"work_package_key": "missing"}), WorkPackageStatus.COMPLETE
        )

    assert engine._synchronize_usage(repo) == {}  # pyright: ignore[reportPrivateUsage]

    class SyncObject:
        def synchronize(self, state: StateStore, value: RepoConfig) -> dict[str, Any]:
            del state, value
            return {"status": "ok", "tokens": 4}

    engine.usage_sync = cast(Any, SyncObject())
    assert engine._synchronize_usage(repo) == {"status": "ok", "tokens": 4}  # pyright: ignore[reportPrivateUsage]

    def legacy_sync(state: StateStore, value: RepoConfig) -> dict[str, Any]:
        del state, value
        return {"status": "legacy"}

    engine.usage_sync = legacy_sync
    assert engine._synchronize_usage(repo) == {"status": "legacy"}  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("blocker", "state", "event_type"),
    (
        ("USER_SECRET: configure key", RunState.BLOCKED, "ATTENTION_REQUIRED"),
        ("TECHNICAL: worker crashed", RunState.DEGRADED, "EXECUTION_FAILED"),
    ),
)
def test_worker_blocker_result_preserves_owner_and_technical_semantics(
    tmp_path: Path,
    blocker: str,
    state: RunState,
    event_type: str,
) -> None:
    engine, store = make_engine(tmp_path)
    repo = repo_config(tmp_path)
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement feature",
        reason="Approved scope",
        target_pr=42,
    )
    store.transition(repo.full_name, RunState.BASELINE_REVIEW)
    store.transition(repo.full_name, RunState.READY)
    store.save_active_work(
        repo.full_name,
        action_id="action-1",
        pr_number=42,
        branch="make_it_so/work/42",
        head_sha="head-1",
        status="pr_open",
        decision=decision.model_dump(mode="json"),
    )
    result = engine._worker_blocked_result(  # pyright: ignore[reportPrivateUsage]
        repo,
        "run-1",
        decision,
        "fingerprint",
        blocker,
        {"model": "test-model"},
    )
    assert result.event.event_type == event_type
    assert result.event.state == state
    assert result.event.evidence["blocker_kind"] in {"user_secret", "technical"}
    active = store.active_work(repo.full_name)
    assert active is not None
    assert active["status"] in {"owner_blocked", "repair_requested"}


@pytest.mark.parametrize(
    ("requires_owner", "expected_state", "event_type", "next_action"),
    (
        (True, RunState.BLOCKED, "APPROVAL_REQUIRED", "Approve or change repo policy"),
        (False, RunState.DEGRADED, "ACTION_BLOCKED", "Fix the policy block"),
    ),
)
def test_active_policy_blocker_distinguishes_owner_and_technical_paths(
    tmp_path: Path,
    requires_owner: bool,
    expected_state: RunState,
    event_type: str,
    next_action: str,
) -> None:
    engine, _ = make_engine(tmp_path)
    repo = repo_config(tmp_path)
    decision = PlanDecision(
        action=ActionKind.REVIEW_PR,
        summary="Review the active pull request",
        reason="The active branch needs a fresh review.",
        target_pr=42,
    )

    result = engine._active_policy_blocked(  # pyright: ignore[reportPrivateUsage]
        repo,
        "run-policy-blocked",
        decision,
        "policy-fingerprint",
        {"number": 42, "url": "https://github.test/pr/42"},
        "policy denied the active PR action",
        requires_owner,
    )

    assert result.event.state == expected_state
    assert result.event.event_type == event_type
    assert next_action in result.event.evidence["next_action"]


def _proposal_event(repo: RepoConfig, decision: PlanDecision, action_id: str, fingerprint: str) -> EventRecord:
    return EventRecord(
        event_id="event-proposal",
        repo=repo.full_name,
        run_id="run-proposal",
        state=RunState.BLOCKED,
        event_type="ACTION_PROPOSED",
        summary=decision.summary,
        reason=decision.reason,
        fingerprint="proposal-fingerprint",
        evidence={"action_id": action_id, "decision": decision.model_dump(mode="json")},
    ).model_copy(update={"evidence": {"action_id": action_id, "model": "test-model"}})


def test_autonomous_proposal_resumes_only_for_matching_durable_evidence(tmp_path: Path) -> None:
    engine, state = make_engine(tmp_path, mode=OperationMode.AUTONOMOUS)
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    decision = PlanDecision(
        action=ActionKind.REPORT_ONLY,
        summary="Resume the status report",
        reason="The previous autonomous proposal is still valid.",
    )
    action_id = "proposal-1"
    fingerprint = "snapshot-1"
    state.save_proposal(repo.full_name, action_id, fingerprint, decision.model_dump(mode="json"))
    state.transition(repo.full_name, RunState.BLOCKED)
    event = _proposal_event(repo, decision, action_id, fingerprint)

    result = engine._resume_autonomous_proposal(  # pyright: ignore[reportPrivateUsage]
        repo, "run-resume", decision, event, fingerprint
    )

    assert result is not None
    assert result.event.event_type == "STATUS_REPORTED"
    stored = state.proposal(repo.full_name, action_id)
    assert stored is not None and stored["status"] == "executed"
    assert (
        engine._resume_autonomous_proposal(  # pyright: ignore[reportPrivateUsage]
            repo,
            "run-resume-missing",
            decision,
            event.model_copy(update={"evidence": {}}),
            fingerprint,
        )
        is None
    )
    assert (
        engine._resume_autonomous_proposal(  # pyright: ignore[reportPrivateUsage]
            repo, "run-resume-stale", decision, event, "different-snapshot"
        )
        is None
    )


@pytest.mark.parametrize(
    ("action", "decision_updates", "call_name"),
    (
        (
            ActionKind.CREATE_ISSUE,
            {"issue_title": "New issue", "issue_body": "Details"},
            "create",
        ),
        (
            ActionKind.UPDATE_ISSUE,
            {"target_issue": 7, "issue_title": "Updated", "issue_body": "New details"},
            "update",
        ),
        (
            ActionKind.LABEL_ISSUE,
            {"target_issue": 7, "issue_labels": ("ready",)},
            "label",
        ),
        (
            ActionKind.RETARGET_ISSUE,
            {"target_issue": 7, "issue_milestone": "Sprint 2"},
            "retarget",
        ),
        (ActionKind.CLOSE_ISSUE, {"target_issue": 7}, "close"),
    ),
)
def test_execute_issue_reconciliation_actions(tmp_path: Path, action: ActionKind, decision_updates: dict[str, Any], call_name: str) -> None:
    engine, state = make_engine(tmp_path)
    repo = repo_config(tmp_path)
    github = IssueMutationGitHub()
    engine.github = cast(Any, github)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    state.transition(repo.full_name, RunState.PLANNING)
    decision = PlanDecision(
        action=action,
        summary=f"{action.value} issue",
        reason="The issue needs reconciliation.",
        **decision_updates,
    )

    result = engine._execute(  # pyright: ignore[reportPrivateUsage]
        repo,
        "run-issue",
        decision,
        "issue-fingerprint",
        {"model": "test-model"},
    )

    assert result.event.event_type == {
        ActionKind.CREATE_ISSUE: "ISSUE_CREATED",
        ActionKind.UPDATE_ISSUE: "ISSUE_UPDATED",
        ActionKind.LABEL_ISSUE: "ISSUE_LABELED",
        ActionKind.RETARGET_ISSUE: "ISSUE_RETARGETED",
        ActionKind.CLOSE_ISSUE: "ISSUE_CLOSED",
    }[action]
    assert github.calls[0][0] == call_name


@pytest.mark.parametrize(
    "action",
    (ActionKind.REPORT_ONLY, ActionKind.NO_ACTION),
)
def test_execute_read_only_actions_returns_to_ready(tmp_path: Path, action: ActionKind) -> None:
    engine, state = make_engine(tmp_path)
    repo = repo_config(tmp_path)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    state.transition(repo.full_name, RunState.PLANNING)
    result = engine._execute(  # pyright: ignore[reportPrivateUsage]
        repo,
        "run-read-only",
        PlanDecision(action=action, summary="Report status", reason="No mutation is needed."),
        "read-only-fingerprint",
        {},
    )
    assert result.event.event_type == "STATUS_REPORTED"
    assert state.current_state(repo.full_name) == RunState.READY


def test_implement_uses_isolated_worktree_and_opens_a_draft_pr(tmp_path: Path) -> None:
    engine, state = make_engine(tmp_path)
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    worktree_path = tmp_path / "isolated-worktree"
    worktree_path.mkdir()
    (worktree_path / "src").mkdir()
    (worktree_path / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    worktree = Worktree(
        path=worktree_path,
        branch="make_it_so/work/19",
        base="origin/main",
        push_branch="make_it_so/work/19",
    )

    def create_worktree(*args: Any, **kwargs: Any) -> Worktree:
        del args, kwargs
        return worktree

    def changed_paths(path: Path) -> list[str]:
        del path
        return ["src/app.py"]

    def no_op(*args: Any, **kwargs: Any) -> None:
        del args, kwargs

    engine.github = cast(Any, ImplementationGitHub())
    engine.worktrees = cast(Any, SimpleNamespace(create=create_worktree))
    engine._changed_paths = changed_paths  # pyright: ignore[reportPrivateUsage]
    engine._assert_worktree_identity = no_op  # pyright: ignore[reportPrivateUsage]
    engine._commit_and_push = no_op  # pyright: ignore[reportPrivateUsage]
    engine._cleanup_successful_worktree = no_op  # pyright: ignore[reportPrivateUsage]
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement isolated work",
        reason="The next approved package is ready.",
        target_issue=19,
    )
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    state.transition(repo.full_name, RunState.PLANNING)

    result = engine._implement(  # pyright: ignore[reportPrivateUsage]
        repo,
        "run-implement",
        decision,
        "implement-fingerprint",
        {"approved_action_id": "approved-19"},
    )

    assert result.event.event_type == "PR_OPENED"
    assert result.event.evidence["pr"]["number"] == 19
    active = state.active_work(repo.full_name)
    assert active is not None
    assert active["pr_number"] == 19
    assert active["branch"] == "make_it_so/work/19"
