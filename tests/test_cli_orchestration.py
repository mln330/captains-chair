from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

import make_it_so.cli as cli
from make_it_so.canary import canary_board_id, canary_proof_marker
from make_it_so.courses import CourseStore
from make_it_so.direct_orchestrator import DirectOrchestrator
from make_it_so.models import (
    ActionKind,
    AppConfig,
    HarnessConfig,
    HarnessResult,
    ModelAttempt,
    OpenClawWorkboardConfig,
    OperationMode,
    PlanDecision,
    RepoConfig,
    RunState,
    UsageConfig,
    WorkerAssignments,
)
from make_it_so.orchestration import QueueCard, QueueCardSpec, QueueStatus, ReconcileResult
from make_it_so.runtime import RuntimeAdapterContractError
from make_it_so.state import StateStore
from tests.fakes import InMemoryWorkQueue
from tests.helpers import app_config, model_policy, repo_config
from tests.test_courses import ready_course


class FakeQueue:
    def __init__(self, *, diagnostics_error: bool = False) -> None:
        self.dispatch_calls = 0
        self.diagnostics_error = diagnostics_error
        self.unblocked: list[str] = []

    def list_cards(self, board_id: str) -> list[QueueCard]:
        del board_id
        return [
            QueueCard(
                id="card-1",
                title="Implementation is waiting for a worker",
                status=QueueStatus.READY,
                labels=("stage:implementation",),
                agent_id="github-coder",
                source_url="https://github.com/example/project/issues/39",
            )
        ]

    def diagnostics(self) -> dict[str, Any]:
        if self.diagnostics_error:
            raise RuntimeError("gateway timeout")
        return {
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
                            "detail": "The worker has not claimed the card.",
                        }
                    ],
                }
            ]
        }

    def dispatch(self, board_id: str) -> dict[str, Any]:
        del board_id
        self.dispatch_calls += 1
        return {"promoted": [], "count": 0}

    def unblock_card(self, card_id: str) -> QueueCard:
        self.unblocked.append(card_id)
        return QueueCard(
            id=card_id,
            title="Resumed card",
            status=QueueStatus.TODO,
            labels=("stage:implementation",),
        )


class FakeOrchestrator:
    def __init__(self, *, diagnostics_error: bool = False) -> None:
        self.adapter = FakeQueue(diagnostics_error=diagnostics_error)
        self.reconcile_calls: list[tuple[bool, str | None]] = []

    def reconcile(
        self,
        repo: RepoConfig,
        *,
        dispatch: bool,
        dispatch_reason: str | None,
    ) -> ReconcileResult:
        del repo
        self.reconcile_calls.append((dispatch, dispatch_reason))
        return ReconcileResult(
            board_id="make-it-so-example-project",
            proof_retries=(),
            protocol_retries=(),
            repairs_created=(),
            retried=(),
            control_plane_recoveries=(),
            unblocked=(),
            user_blockers=(),
            dispatch={"promoted": [], "count": 0},
        )


def _write_config(tmp_path: Path, *, operation_mode: OperationMode = OperationMode.SUPERVISED) -> Path:
    repo = repo_config(tmp_path).model_copy(
        update={
            "orchestrator": "workers",
            "orchestration_board": "make-it-so-example-project",
            "operation_mode": operation_mode,
        }
    )
    config = AppConfig(
        version=1,
        state_dir=tmp_path / "state",
        artifact_dir=tmp_path / "artifacts",
        harnesses={"test": HarnessConfig(kind="codex", executable="codex", timeout_seconds=30)},
        orchestrators={
            "workers": OpenClawWorkboardConfig(
                workers=WorkerAssignments(
                    captain="captain",
                    coder="coder",
                    reviewer="reviewer",
                    tester="tester",
                    ux_reviewer="ux",
                    final_reviewer="final",
                    merger="merger",
                    verifier="verifier",
                )
            )
        },
        models=model_policy(),
        repos=(repo,),
    )
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    return path


def test_planning_session_cli_returns_native_host_handoff(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = repo_config(tmp_path)
    config = app_config(tmp_path, repo)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    CourseStore(repo.local_path).save(ready_course())

    assert cli.main(
        [
            "--config",
            str(config_path),
            "planning-session",
            "--repo",
            repo.full_name,
            "--course-key",
            "feature-search",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["interaction"] == "host_agent_conversation"
    assert payload["mutation_requires_course_approval"] is True


def test_repo_without_orchestrator_uses_direct_runtime_by_default(tmp_path: Path) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))

    orchestrator = cli._orchestrator(config, "example/project")  # pyright: ignore[reportPrivateUsage]

    assert isinstance(orchestrator.adapter, DirectOrchestrator)
    assert cli._board_id(  # pyright: ignore[reportPrivateUsage]
        config, "example/project"
    ) == "make-it-so-direct-example-project"


def test_preflight_reports_ready_without_dispatching_workers(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    orchestrator = FakeOrchestrator()

    def fake_orchestrator(config: AppConfig, repo_name: str) -> FakeOrchestrator:
        del config, repo_name
        return orchestrator

    def allow_usage_guard(
        config: AppConfig,
        repo_name: str,
        state: StateStore,
        *,
        orchestrator_executable: str | None,
    ) -> tuple[dict[str, Any], None]:
        del config, repo_name, state, orchestrator_executable
        return {"allowed": True, "reason": "test"}, None

    monkeypatch.setattr(cli, "_orchestrator", fake_orchestrator)
    monkeypatch.setattr(cli, "_usage_guard", allow_usage_guard)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "orchestrate",
                "preflight",
                "--repo",
                "example/project",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ready_with_warnings"
    assert payload["checks"]["adapter_contract"]["status"] == "passed"
    assert payload["checks"]["queue_read"]["cards"] == 1
    assert payload["checks"]["workboard_diagnostics"]["finding_count"] == 1
    assert (
        payload["checks"]["workboard_diagnostics"]["findings"][0]["title"]
        == "Assigned card is ready but unclaimed"
    )
    assert payload["checks"]["workboard_diagnostics"].get("payload") is None
    assert "no daily token limit is configured" in payload["warnings"]
    assert payload["warnings"] == [
        "runtime does not expose a worker model health check",
        "Workboard diagnostics returned 1 finding(s)",
        "no daily token limit is configured",
    ]
    assert "did not invoke a model or dispatch a worker" in payload["next_action"]
    assert orchestrator.adapter.dispatch_calls == 0


def test_preflight_fails_closed_on_worker_health_or_diagnostics(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    orchestrator = FakeOrchestrator(diagnostics_error=True)

    def fake_orchestrator(config: AppConfig, repo_name: str) -> FakeOrchestrator:
        del config, repo_name
        return orchestrator

    def allow_usage_guard(
        config: AppConfig,
        repo_name: str,
        state: StateStore,
        *,
        orchestrator_executable: str | None,
    ) -> tuple[dict[str, Any], None]:
        del config, repo_name, state, orchestrator_executable
        return {"allowed": True, "reason": "test"}, None

    monkeypatch.setattr(cli, "_orchestrator", fake_orchestrator)
    monkeypatch.setattr(cli, "_usage_guard", allow_usage_guard)
    monkeypatch.setattr(
        orchestrator.adapter,
        "validate_worker_models",
        lambda: {"status": "degraded", "mismatches": [{"agent_id": "coder"}]},
        raising=False,
    )

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "orchestrate",
                "preflight",
                "--repo",
                "example/project",
            ]
        )
        == 2
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "degraded"
    assert payload["checks"]["worker_models"]["status"] == "degraded"
    assert payload["checks"]["workboard_diagnostics"]["status"] == "failed"
    assert len(payload["failures"]) == 2
    assert "no worker was dispatched" in payload["next_action"]
    assert orchestrator.adapter.dispatch_calls == 0


def test_preflight_fails_when_worker_lifecycle_helper_is_unavailable(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    orchestrator = FakeOrchestrator()
    monkeypatch.setattr(
        orchestrator.adapter,
        "config",
        SimpleNamespace(make_it_so_command=("missing-make_it_so-helper",)),
        raising=False,
    )

    def fake_orchestrator(config: AppConfig, repo_name: str) -> FakeOrchestrator:
        del config, repo_name
        return orchestrator

    def allow_usage_guard(
        config: AppConfig,
        repo_name: str,
        state: StateStore,
        *,
        orchestrator_executable: str | None,
    ) -> tuple[dict[str, Any], None]:
        del config, repo_name, state, orchestrator_executable
        return {"allowed": True, "reason": "test"}, None

    monkeypatch.setattr(cli, "_orchestrator", fake_orchestrator)
    monkeypatch.setattr(cli, "_usage_guard", allow_usage_guard)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "orchestrate",
                "preflight",
                "--repo",
                "example/project",
            ]
        )
        == 2
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"]["worker_protocol"]["status"] == "failed"
    assert "worker lifecycle helper executable was not found" in payload["failures"][0]
    assert orchestrator.adapter.dispatch_calls == 0


def test_worker_protocol_preflight_accepts_a_real_python_module_prefix(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    orchestrator = FakeOrchestrator()
    monkeypatch.setattr(
        orchestrator.adapter,
        "config",
        SimpleNamespace(make_it_so_command=(sys.executable, "-m", "make_it_so")),
        raising=False,
    )

    def fake_orchestrator(config: AppConfig, repo_name: str) -> FakeOrchestrator:
        del config, repo_name
        return orchestrator

    def allow_usage_guard(
        config: AppConfig,
        repo_name: str,
        state: StateStore,
        *,
        orchestrator_executable: str | None,
    ) -> tuple[dict[str, Any], None]:
        del config, repo_name, state, orchestrator_executable
        return {"allowed": True, "reason": "test"}, None

    monkeypatch.setattr(cli, "_orchestrator", fake_orchestrator)
    monkeypatch.setattr(cli, "_usage_guard", allow_usage_guard)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "orchestrate",
                "preflight",
                "--repo",
                "example/project",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"]["worker_protocol"]["status"] == "passed"
    assert orchestrator.adapter.dispatch_calls == 0


def test_disabled_preflight_does_not_recommend_a_canary(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path, operation_mode=OperationMode.DISABLED)
    orchestrator = FakeOrchestrator()

    def fake_orchestrator(config: AppConfig, repo_name: str) -> FakeOrchestrator:
        del config, repo_name
        return orchestrator

    def allow_usage_guard(
        config: AppConfig,
        repo_name: str,
        state: StateStore,
        *,
        orchestrator_executable: str | None,
    ) -> tuple[dict[str, Any], None]:
        del config, repo_name, state, orchestrator_executable
        return {"allowed": True, "reason": "test"}, None

    monkeypatch.setattr(cli, "_orchestrator", fake_orchestrator)
    monkeypatch.setattr(cli, "_usage_guard", allow_usage_guard)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "orchestrate",
                "preflight",
                "--repo",
                "example/project",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "disabled"
    assert payload["health_status"] == "ready_with_warnings"
    assert "Number One is disabled" in payload["next_action"]
    assert "canary" in payload["next_action"]


@pytest.mark.parametrize(
    "extra_args",
    (
        ("dispatch",),
        ("reconcile",),
        ("unblock", "--card", "card-1"),
        ("canary", "--run"),
    ),
)
def test_disabled_orchestration_mutations_stop_before_adapter_construction(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
    extra_args: tuple[str, ...],
) -> None:
    config_path = _write_config(tmp_path, operation_mode=OperationMode.DISABLED)

    def refuse_orchestrator(config: AppConfig, repo_name: str) -> Any:
        del config, repo_name
        raise AssertionError("disabled orchestration must stop before adapter construction")

    monkeypatch.setattr(cli, "_orchestrator", refuse_orchestrator)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "orchestrate",
                *extra_args,
                "--repo",
                "example/project",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "disabled"
    assert "no worker, model, or Workboard mutation" in payload["reason"]


@pytest.mark.parametrize(
    "extra_args",
    (
        ("dispatch",),
        ("reconcile",),
        ("unblock", "--card", "card-1"),
        ("canary", "--run"),
    ),
)
def test_advisory_orchestration_mutations_stop_before_adapter_construction(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
    extra_args: tuple[str, ...],
) -> None:
    config_path = _write_config(tmp_path, operation_mode=OperationMode.ADVISORY)

    def refuse_orchestrator(config: AppConfig, repo_name: str) -> Any:
        del config, repo_name
        raise AssertionError("advisory orchestration must stop before adapter construction")

    monkeypatch.setattr(cli, "_orchestrator", refuse_orchestrator)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "orchestrate",
                *extra_args,
                "--repo",
                "example/project",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "advisory"
    assert "no worker, model, or Workboard mutation" in payload["reason"]


def test_disabled_model_check_stops_before_runtime_or_provider_call(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path, operation_mode=OperationMode.DISABLED)

    def refuse_runtime(config: AppConfig, repo_name: str, harness_name: str) -> Any:
        del config, repo_name, harness_name
        raise AssertionError("disabled model check must stop before runtime construction")

    monkeypatch.setattr(cli, "_runtime", refuse_runtime)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "model-check",
                "--repo",
                "example/project",
                "--harness",
                "test",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "disabled"
    assert "model health check was skipped" in payload["reason"]


def test_model_check_probes_the_selected_role_model(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    captured: dict[str, Any] = {}

    class FakeEngine:
        def run_model(self, *args: Any, **kwargs: Any) -> HarnessResult:
            del args
            captured["models"] = kwargs["models"]
            return HarnessResult(
                role="model-health",
                output={"status": "ok", "message": "test-model"},
                attempts=(ModelAttempt(model="test-model", success=True, duration_ms=1),),
                resolved_model="test-model",
                session_id="session-1",
            )

    def fake_runtime(config: AppConfig, repo: str, harness: str) -> tuple[FakeEngine, None]:
        del config, repo, harness
        return FakeEngine(), None

    monkeypatch.setattr(cli, "_runtime", fake_runtime)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "model-check",
                "--repo",
                "example/project",
                "--harness",
                "test",
                "--role",
                "coder",
            ]
        )
        == 0
    )

    json.loads(capsys.readouterr().out)
    assert captured["models"] is not None
    assert captured["models"].primary.model == "test-model"


@pytest.mark.parametrize("role", ("tester", "ux_reviewer"))
def test_model_check_optional_worker_roles_fall_back_to_coder_policy_for_legacy_configs(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
    role: str,
) -> None:
    config_path = _write_config(tmp_path)
    captured: dict[str, Any] = {}

    class FakeEngine:
        def run_model(self, *args: Any, **kwargs: Any) -> HarnessResult:
            del args
            captured["models"] = kwargs["models"]
            return HarnessResult(
                role="model-health",
                output={"status": "ok", "message": "test-model"},
                attempts=(ModelAttempt(model="test-model", success=True, duration_ms=1),),
                resolved_model="test-model",
                session_id="session-1",
            )

    def fake_runtime(config: AppConfig, repo: str, harness: str) -> tuple[FakeEngine, None]:
        del config, repo, harness
        return FakeEngine(), None

    monkeypatch.setattr(cli, "_runtime", fake_runtime)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "model-check",
                "--repo",
                "example/project",
                "--harness",
                "test",
                "--role",
                role,
            ]
        )
        == 0
    )

    json.loads(capsys.readouterr().out)
    assert captured["models"].primary.model == "test-model"


def test_canary_plan_run_and_check_use_only_workboard(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    queue = InMemoryWorkQueue()
    orchestrator = SimpleNamespace(adapter=queue)

    def fake_orchestrator(config: AppConfig, repo_name: str) -> Any:
        del config, repo_name
        return orchestrator

    def allow_usage_guard(
        config: AppConfig,
        repo_name: str,
        state: StateStore,
        *,
        orchestrator_executable: str | None,
    ) -> tuple[dict[str, Any], None]:
        del config, repo_name, state, orchestrator_executable
        return {"allowed": True, "reason": "test"}, None

    monkeypatch.setattr(cli, "_orchestrator", fake_orchestrator)
    monkeypatch.setattr(cli, "_usage_guard", allow_usage_guard)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "orchestrate",
                "canary",
                "--repo",
                "example/project",
                "--canary-id",
                "smoke",
            ]
        )
        == 0
    )
    planned = json.loads(capsys.readouterr().out)
    assert planned["status"] == "planned"
    assert queue.boards == set()

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "orchestrate",
                "canary",
                "--repo",
                "example/project",
                "--canary-id",
                "smoke",
                "--run",
            ]
        )
        == 0
    )
    dispatched = json.loads(capsys.readouterr().out)
    card_id = dispatched["card"]["id"]
    assert dispatched["status"] == "dispatched"
    assert dispatched["dispatch"]["started"] == [card_id]
    assert canary_board_id(repo_config(tmp_path)) in queue.boards

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "orchestrate",
                "canary",
                "--repo",
                "example/project",
                "--canary-id",
                "smoke",
                "--check",
                "--card",
                card_id,
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["status"] == "pending"

    queue.complete_card(
        card_id,
        summary="Canary completed",
        proof=({"status": "passed", "note": canary_proof_marker("smoke")},),
    )
    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "orchestrate",
                "canary",
                "--repo",
                "example/project",
                "--canary-id",
                "smoke",
                "--check",
                "--card",
                card_id,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "passed"


def test_canary_budget_denial_does_not_materialize_card(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    queue = InMemoryWorkQueue()
    orchestrator = SimpleNamespace(adapter=queue)

    def fake_orchestrator(config: AppConfig, repo_name: str) -> Any:
        del config, repo_name
        return orchestrator

    def deny_usage_guard(
        config: AppConfig,
        repo_name: str,
        state: StateStore,
        *,
        orchestrator_executable: str | None,
    ) -> tuple[dict[str, Any], None]:
        del config, repo_name, state, orchestrator_executable
        return {"allowed": False, "reason": "budget exhausted"}, None

    monkeypatch.setattr(cli, "_orchestrator", fake_orchestrator)
    monkeypatch.setattr(
        cli,
        "_usage_guard",
        deny_usage_guard,
    )

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "orchestrate",
                "canary",
                "--repo",
                "example/project",
                "--canary-id",
                "denied",
                "--run",
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dispatch_suppressed"
    assert queue.boards == set()


def test_reconcile_cli_reports_queue_diagnostics_and_next_action(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    orchestrator = FakeOrchestrator()

    def fake_orchestrator(config: AppConfig, repo_name: str) -> FakeOrchestrator:
        del config, repo_name
        return orchestrator

    def allow_usage_guard(
        config: AppConfig,
        repo_name: str,
        state: StateStore,
        *,
        orchestrator_executable: str | None,
    ) -> tuple[dict[str, Any], None]:
        del config, repo_name, state, orchestrator_executable
        return {"allowed": True, "reason": "test"}, None

    monkeypatch.setattr(cli, "_orchestrator", fake_orchestrator)
    monkeypatch.setattr(cli, "_usage_guard", allow_usage_guard)

    assert cli.main(["--config", str(config_path), "orchestrate", "reconcile", "--repo", "example/project", "--send"]) == 0

    output = capsys.readouterr().out
    assert "Assigned card is ready but unclaimed" in output
    assert "Reconcile the queue" in output
    payload_start = output.find('{\n  "board_id"')
    assert payload_start >= 0
    payload = json.loads(output[payload_start:])
    assert payload["diagnostics"]["diagnostics"]
    assert orchestrator.reconcile_calls == [(True, None)]


def test_reconcile_cli_reports_busy_when_repository_lease_is_held(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    orchestrator = FakeOrchestrator()
    state = StateStore(tmp_path / "state" / "state.db")

    def fake_orchestrator(config: AppConfig, repo_name: str) -> FakeOrchestrator:
        del config, repo_name
        return orchestrator

    monkeypatch.setattr(cli, "_orchestrator", fake_orchestrator)
    with state.lease("example/project", "scheduled-pass"):
        assert (
            cli.main(
                [
                    "--config",
                    str(config_path),
                    "orchestrate",
                    "reconcile",
                    "--repo",
                    "example/project",
                ]
            )
            == 0
        )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "busy"
    assert payload["operation"] == "reconcile"
    assert "scheduled-pass" in payload["reason"]
    assert orchestrator.reconcile_calls == []


def test_cycle_cli_reports_busy_without_turning_normal_overlap_into_failure(
    tmp_path: Path,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    state = StateStore(tmp_path / "state" / "state.db")

    with state.lease("example/project", "scheduled-pass"):
        assert (
            cli.main(
                [
                    "--config",
                    str(config_path),
                    "cycle",
                    "--repo",
                    "example/project",
                    "--harness",
                    "test",
                    "--live",
                ]
            )
            == 0
        )

    output = capsys.readouterr().out
    assert "Cycle skipped" in output
    assert "scheduled-pass" in output
    assert "duplicate work" in output


def test_baseline_cli_does_not_duplicate_an_active_deep_run(
    tmp_path: Path,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    state = StateStore(tmp_path / "state" / "state.db")

    with state.lease("example/project", "active-baseline"):
        assert (
            cli.main(
                [
                    "--config",
                    str(config_path),
                    "baseline",
                    "--repo",
                    "example/project",
                    "--harness",
                    "test",
                    "--no-analyze",
                    "--no-run-checks",
                ]
            )
            == 0
        )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "busy"
    assert payload["operation"] == "baseline"
    assert "active-baseline" in payload["reason"]


def test_reconcile_cli_persists_and_deduplicates_provider_failure(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)

    class FailingOrchestrator:
        adapter = FakeQueue()

        def reconcile(
            self,
            repo: RepoConfig,
            *,
            dispatch: bool,
            dispatch_reason: str | None,
        ) -> ReconcileResult:
            del repo, dispatch, dispatch_reason
            raise RuntimeError("Workboard gateway unavailable")

    orchestrator = FailingOrchestrator()

    def fake_orchestrator(config: AppConfig, repo_name: str) -> FailingOrchestrator:
        del config, repo_name
        return orchestrator

    monkeypatch.setattr(cli, "_orchestrator", fake_orchestrator)
    command = [
        "--config",
        str(config_path),
        "orchestrate",
        "reconcile",
        "--repo",
        "example/project",
        "--send",
    ]

    assert cli.main(command) == 2
    first_output = capsys.readouterr().out
    assert "Number One HANDLING" in first_output
    first_payload = json.loads(first_output[first_output.rfind('{\n  "status"'):])
    assert first_payload["status"] == "degraded"
    assert first_payload["event"]["event_type"] == "QUEUE_DEGRADED"
    assert first_payload["notification_suppressed"] is False

    assert cli.main(command) == 2
    second_output = capsys.readouterr().out
    second_payload = json.loads(second_output[second_output.rfind('{\n  "status"'):])
    assert second_payload["notification_suppressed"] is True
    state = StateStore(tmp_path / "state" / "state.db")
    assert [event for event in state.recent_events("example/project", 20) if event.event_type == "QUEUE_DEGRADED"]
    assert len(
        [event for event in state.recent_events("example/project", 20) if event.event_type == "QUEUE_DEGRADED"]
    ) == 1


def test_disabled_merge_gate_refuses_github_mutation(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path, operation_mode=OperationMode.DISABLED)

    def fail_if_runtime_is_built(config: AppConfig, repo_name: str) -> Any:
        del config, repo_name
        raise AssertionError("disabled merge must stop before building the runtime")

    monkeypatch.setattr(cli, "_orchestrator", fail_if_runtime_is_built)
    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "merge-gate",
                "--repo",
                "example/project",
                "--pr",
                "34",
                "--final-card",
                "card-1",
                "--merge",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "disabled"
    assert "merge was skipped" in payload["reason"]


def test_advisory_merge_gate_refuses_github_mutation(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path, operation_mode=OperationMode.ADVISORY)

    def fail_if_runtime_is_built(config: AppConfig, repo_name: str) -> Any:
        del config, repo_name
        raise AssertionError("advisory merge must stop before building the runtime")

    monkeypatch.setattr(cli, "_orchestrator", fail_if_runtime_is_built)
    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "merge-gate",
                "--repo",
                "example/project",
                "--pr",
                "34",
                "--final-card",
                "card-1",
                "--merge",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "advisory"
    assert "merge was skipped" in payload["reason"]


def test_reconcile_cli_reports_automatic_recovery_progress(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)

    class RecoveryQueue(FakeQueue):
        def list_cards(self, board_id: str) -> list[QueueCard]:
            del board_id
            return [
                QueueCard(
                    id="retry-target",
                    title="Retry implementation",
                    status=QueueStatus.READY,
                    labels=("stage:implementation",),
                    agent_id="github-coder",
                ),
                QueueCard(
                    id="repair-1",
                    title="Repair review findings",
                    status=QueueStatus.READY,
                    labels=("stage:repair",),
                    agent_id="github-coder",
                    source_url="https://github.com/example/project/pull/40",
                ),
                QueueCard(
                    id="recovery-1",
                    title="Replan failed work",
                    status=QueueStatus.READY,
                    labels=("stage:control_plane_action",),
                    agent_id="make-it-so",
                ),
            ]

        def diagnostics(self) -> dict[str, Any]:
            return {}

    class RecoveryOrchestrator(FakeOrchestrator):
        def __init__(self) -> None:
            self.adapter = RecoveryQueue()
            self.reconcile_calls: list[tuple[bool, str | None]] = []

        def reconcile(
            self,
            repo: RepoConfig,
            *,
            dispatch: bool,
            dispatch_reason: str | None,
        ) -> ReconcileResult:
            del repo
            self.reconcile_calls.append((dispatch, dispatch_reason))
            return ReconcileResult(
                board_id="make-it-so-example-project",
                proof_retries=(),
                protocol_retries=(),
                repairs_created=("repair-1",),
                retried=("retry-target",),
                control_plane_recoveries=("recovery-1",),
                unblocked=(),
                user_blockers=(),
                dispatch={"promoted": [], "count": 0},
            )

    orchestrator = RecoveryOrchestrator()

    def fake_orchestrator(config: AppConfig, repo_name: str) -> RecoveryOrchestrator:
        del config, repo_name
        return orchestrator

    def allow_usage_guard(
        config: AppConfig,
        repo_name: str,
        state: StateStore,
        *,
        orchestrator_executable: str | None,
    ) -> tuple[dict[str, Any], None]:
        del config, repo_name, state, orchestrator_executable
        return {"allowed": True, "reason": "test"}, None

    monkeypatch.setattr(cli, "_orchestrator", fake_orchestrator)
    monkeypatch.setattr(cli, "_usage_guard", allow_usage_guard)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "orchestrate",
                "reconcile",
                "--repo",
                "example/project",
                "--send",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Technical retry" in output
    assert "Repair queued" in output
    assert "Number One recovery queued" in output
    assert "ACTION NEEDED" not in output
    payload_start = output.find('{\n  "board_id"')
    assert payload_start >= 0
    payload = json.loads(output[payload_start:])
    assert [event["event_type"] for event in payload["events"]] == [
        "TECHNICAL_RETRY",
        "REPAIR_QUEUED",
        "CONTROL_PLANE_RECOVERY_QUEUED",
    ]


def test_reconcile_cli_records_notification_failure_and_returns_degraded(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    orchestrator = FakeOrchestrator()

    def fake_orchestrator(config: AppConfig, repo_name: str) -> FakeOrchestrator:
        del config, repo_name
        return orchestrator

    def allow_usage_guard(
        config: AppConfig,
        repo_name: str,
        state: StateStore,
        *,
        orchestrator_executable: str | None,
    ) -> tuple[dict[str, Any], None]:
        del config, repo_name, state, orchestrator_executable
        return {"allowed": True, "reason": "test"}, None

    class FailingNotifier:
        def send(self, event: Any) -> None:
            del event
            raise cli.NotificationError("Discord route unavailable")

    def failing_notifier(config: Any) -> FailingNotifier:
        del config
        return FailingNotifier()

    monkeypatch.setattr(cli, "_orchestrator", fake_orchestrator)
    monkeypatch.setattr(cli, "_usage_guard", allow_usage_guard)
    monkeypatch.setattr(cli, "build_notifier", failing_notifier)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "orchestrate",
                "reconcile",
                "--repo",
                "example/project",
                "--send",
            ]
        )
        == 2
    )

    payload = json.loads(capsys.readouterr().out)
    assert len(payload["notification_failures"]) == 1
    assert payload["notification_failures"][0]["event_type"] == "NOTIFICATION_FAILED"
    state = StateStore(tmp_path / "state" / "state.db")
    assert state.current_state("example/project").value == "degraded"


def test_dispatch_cli_suppresses_workers_when_usage_guard_denies(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    orchestrator = FakeOrchestrator()

    def fake_orchestrator(config: AppConfig, repo_name: str) -> FakeOrchestrator:
        del config, repo_name
        return orchestrator

    def deny_usage_guard(
        config: AppConfig,
        repo_name: str,
        state: StateStore,
        *,
        orchestrator_executable: str | None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        del config, repo_name, state, orchestrator_executable
        return (
            {"allowed": False, "reason": "usage telemetry is incomplete"},
            {"status": "degraded"},
        )

    monkeypatch.setattr(cli, "_orchestrator", fake_orchestrator)
    monkeypatch.setattr(cli, "_usage_guard", deny_usage_guard)

    assert cli.main(["--config", str(config_path), "orchestrate", "dispatch", "--repo", "example/project"]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dispatch_suppressed"
    assert payload["dispatch_budget"]["allowed"] is False
    assert orchestrator.adapter.dispatch_calls == 0


def test_dispatch_cli_returns_degraded_when_worker_model_health_fails(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    orchestrator = FakeOrchestrator()

    def fake_orchestrator(config: AppConfig, repo_name: str) -> FakeOrchestrator:
        del config, repo_name
        return orchestrator

    def allow_usage_guard(
        config: AppConfig,
        repo_name: str,
        state: StateStore,
        *,
        orchestrator_executable: str | None,
    ) -> tuple[dict[str, Any], None]:
        del config, repo_name, state, orchestrator_executable
        return {"allowed": True, "reason": "test"}, None

    monkeypatch.setattr(cli, "_orchestrator", fake_orchestrator)
    monkeypatch.setattr(cli, "_usage_guard", allow_usage_guard)
    monkeypatch.setattr(
        orchestrator.adapter,
        "validate_worker_models",
        lambda: {"status": "degraded", "mismatches": [{"agent_id": "coder"}]},
        raising=False,
    )

    assert cli.main(["--config", str(config_path), "orchestrate", "dispatch", "--repo", "example/project"]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dispatch_suppressed"
    assert payload["model_health"]["status"] == "degraded"
    assert orchestrator.adapter.dispatch_calls == 0


def test_health_cli_checks_worker_routes_without_dispatch(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    orchestrator = FakeOrchestrator()

    def fake_orchestrator(config: AppConfig, repo_name: str) -> FakeOrchestrator:
        del config, repo_name
        return orchestrator

    monkeypatch.setattr(cli, "_orchestrator", fake_orchestrator)

    assert cli.main(["--config", str(config_path), "orchestrate", "health", "--repo", "example/project"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"status": "not_supported"}
    assert orchestrator.adapter.dispatch_calls == 0


def test_unblock_cli_resumes_one_workboard_card_without_dispatch(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    orchestrator = FakeOrchestrator()

    def fake_orchestrator(config: AppConfig, repo_name: str) -> FakeOrchestrator:
        del config, repo_name
        return orchestrator

    monkeypatch.setattr(cli, "_orchestrator", fake_orchestrator)
    def blocked_cards(board_id: str) -> list[QueueCard]:
        del board_id
        return [
            QueueCard(
                id="card-9",
                title="Blocked on owner secret",
                status=QueueStatus.BLOCKED,
                metadata={"workerProtocol": {"detail": "USER_SECRET: provide key"}},
            )
        ]

    monkeypatch.setattr(orchestrator.adapter, "list_cards", blocked_cards)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "orchestrate",
                "unblock",
                "--repo",
                "example/project",
                "--card",
                "card-9",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "unblocked"
    assert payload["card"]["id"] == "card-9"
    assert orchestrator.adapter.unblocked == ["card-9"]
    assert orchestrator.adapter.dispatch_calls == 0


def test_unblock_cli_refuses_technical_failure(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    orchestrator = FakeOrchestrator()

    def fake_orchestrator(config: AppConfig, repo_name: str) -> FakeOrchestrator:
        del config, repo_name
        return orchestrator

    def technical_card(board_id: str) -> list[QueueCard]:
        del board_id
        return [
            QueueCard(
                id="card-10",
                title="Technical failure",
                status=QueueStatus.BLOCKED,
                metadata={"workerProtocol": {"detail": "TECHNICAL: tests fail"}},
            )
        ]

    monkeypatch.setattr(cli, "_orchestrator", fake_orchestrator)
    monkeypatch.setattr(orchestrator.adapter, "list_cards", technical_card)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "orchestrate",
                "unblock",
                "--repo",
                "example/project",
                "--card",
                "card-10",
            ]
        )
        == 3
    )

    assert "technical failures must follow repair/recovery" in capsys.readouterr().err
    assert orchestrator.adapter.unblocked == []


def test_reconcile_cli_surfaces_diagnostics_rpc_failure(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    orchestrator = FakeOrchestrator(diagnostics_error=True)

    def fake_orchestrator(config: AppConfig, repo_name: str) -> FakeOrchestrator:
        del config, repo_name
        return orchestrator

    def allow_usage_guard(
        config: AppConfig,
        repo_name: str,
        state: StateStore,
        *,
        orchestrator_executable: str | None,
    ) -> tuple[dict[str, Any], None]:
        del config, repo_name, state, orchestrator_executable
        return {"allowed": True, "reason": "test"}, None

    monkeypatch.setattr(cli, "_orchestrator", fake_orchestrator)
    monkeypatch.setattr(cli, "_usage_guard", allow_usage_guard)

    assert cli.main(["--config", str(config_path), "orchestrate", "reconcile", "--repo", "example/project", "--send"]) == 2

    output = capsys.readouterr().out
    assert "Workboard diagnostics are unavailable" in output
    assert "gateway timeout" in output
    assert "Check the configured queue runtime" in output


def test_usage_guard_blocks_dispatch_when_telemetry_sync_is_degraded_without_budget(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    config_path = _write_config(tmp_path)
    config = cli.load_config(config_path).model_copy(
        update={"usage": UsageConfig(block_on_unknown=True)}
    )
    state = StateStore(config.state_dir / "state.db")

    def degraded_sync(*_: Any, **__: Any) -> dict[str, Any]:
        return {"status": "degraded", "error": "session endpoint unavailable"}

    monkeypatch.setattr(cli, "sync_openclaw_sessions", degraded_sync)

    usage_guard = cli.__dict__["_usage_guard"]
    budget, usage_sync = usage_guard(
        config,
        "example/project",
        state,
        orchestrator_executable="openclaw",
    )

    assert usage_sync == {"status": "degraded", "error": "session endpoint unavailable"}
    assert budget["allowed"] is False
    assert "new worker sessions are suppressed" in budget["reason"]


def test_usage_guard_uses_account_wide_token_limit_across_repositories(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config = cli.load_config(config_path).model_copy(
        update={
            "usage": UsageConfig(daily_token_limit=1_000_000)
        }
    )
    state = StateStore(config.state_dir / "state.db")
    state.record_model_call(
        "other/project",
        "other-run",
        "planner",
        "gpt-5.5",
        [{"input_tokens": 1_000_000, "output_tokens": 0, "total_tokens": 1_000_000}],
    )

    usage_guard = cli.__dict__["_usage_guard"]
    budget, usage_sync = usage_guard(
        config,
        "example/project",
        state,
        orchestrator_executable=None,
    )

    assert usage_sync is None
    assert budget["allowed"] is False
    assert "daily token limit" in budget["reason"]


def test_usage_guard_imports_openclaw_sessions_for_every_managed_repository(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    config_path = _write_config(tmp_path)
    config = cli.load_config(config_path)
    second = config.repos[0].model_copy(
        update={
            "full_name": "other/project",
            "local_path": tmp_path / "other-project",
        }
    )
    config = config.model_copy(update={"repos": (config.repos[0], second)})
    state = StateStore(config.state_dir / "state.db")
    calls: list[str] = []

    def sync_sessions(*_: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(str(kwargs["repo"]))
        return {
            "sessions_seen": 1,
            "sessions_imported": 1,
            "sessions_with_usage": 1,
            "session_limit_reached": False,
        }

    monkeypatch.setattr(cli, "sync_openclaw_sessions", sync_sessions)

    budget, usage_sync = cli.__dict__["_usage_guard"](
        config,
        "example/project",
        state,
        orchestrator_executable="openclaw",
    )

    assert calls == ["example/project", "other/project"]
    assert usage_sync == {
        "status": "ok",
        "repos": [
            {
                "sessions_seen": 1,
                "sessions_imported": 1,
                "sessions_with_usage": 1,
                "session_limit_reached": False,
            },
            {
                "sessions_seen": 1,
                "sessions_imported": 1,
                "sessions_with_usage": 1,
                "session_limit_reached": False,
            },
        ],
        "sessions_seen": 2,
        "sessions_imported": 2,
        "sessions_with_usage": 2,
        "session_limit_reached": False,
    }
    assert budget["allowed"] is True


def test_usage_guard_blocks_when_any_openclaw_session_window_is_full(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    config_path = _write_config(tmp_path)
    config = cli.load_config(config_path)
    state = StateStore(config.state_dir / "state.db")

    def full_window_sync(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "sessions_seen": 1000,
            "sessions_imported": 1000,
            "sessions_with_usage": 1000,
            "session_limit_reached": True,
        }

    monkeypatch.setattr(cli, "sync_openclaw_sessions", full_window_sync)

    budget, usage_sync = cli.__dict__["_usage_guard"](
        config,
        "example/project",
        state,
        orchestrator_executable="openclaw",
    )

    assert usage_sync is not None
    assert usage_sync["status"] == "degraded"
    assert usage_sync["session_limit_reached"] is True
    assert budget["allowed"] is False
    assert "new worker sessions are suppressed" in budget["reason"]


def test_cli_converts_invalid_runtime_adapter_to_actionable_configuration_error(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)

    def invalid_runtime(config: Any, **kwargs: Any) -> Any:
        del config, kwargs
        raise RuntimeAdapterContractError("runtime adapter is missing required operations: dispatch")

    monkeypatch.setattr(cli, "build_work_queue_orchestrator", invalid_runtime)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "orchestrate",
                "health",
                "--repo",
                "example/project",
            ]
        )
        == 3
    )
    assert "missing required operations" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("action", "extra", "expected"),
    (
        (
            "heartbeat",
            ("--note", "still working"),
            {"owner_id": "coder", "token": "claim-1", "note": "still working"},
        ),
        (
            "complete",
            (
                "--summary",
                "PR opened",
                "--proof-note",
                "tests passed on head abc1234",
                "--proof-url",
                "https://github.com/example/project/pull/40",
            ),
            {
                "owner_id": "coder",
                "token": "claim-1",
                "summary": "PR opened",
                "proof_note": "tests passed on head abc1234",
                "proof_url": "https://github.com/example/project/pull/40",
            },
        ),
        (
            "block",
            ("--reason", "TECHNICAL: targeted test failed"),
            {"owner_id": "coder", "token": "claim-1", "reason": "TECHNICAL: targeted test failed"},
        ),
    ),
)
def test_worker_protocol_cli_routes_claimed_lifecycle_with_owner_token(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
    action: str,
    extra: tuple[str, ...],
    expected: dict[str, str],
) -> None:
    config_path = _write_config(tmp_path)
    calls: list[tuple[str, dict[str, Any]]] = []

    class ProtocolAdapter:
        def __init__(self, config: Any) -> None:
            del config

        def heartbeat_card(self, card_id: str, *, owner_id: str, token: str, note: str) -> QueueCard:
            calls.append(("heartbeat", {"card_id": card_id, "owner_id": owner_id, "token": token, "note": note}))
            return QueueCard(id=card_id, title="Worker", status=QueueStatus.RUNNING)

        def complete_claimed_card(
            self,
            card_id: str,
            *,
            owner_id: str,
            token: str,
            summary: str,
            proof: tuple[dict[str, Any], ...],
        ) -> QueueCard:
            row: dict[str, Any] = {
                "card_id": card_id,
                "owner_id": owner_id,
                "token": token,
                "summary": summary,
                "proof_note": proof[0]["note"],
                "proof_url": proof[0].get("url"),
            }
            calls.append(("complete", row))
            return QueueCard(id=card_id, title="Worker", status=QueueStatus.DONE)

        def block_claimed_card(
            self,
            card_id: str,
            *,
            owner_id: str,
            token: str,
            reason: str,
        ) -> QueueCard:
            calls.append(("block", {"card_id": card_id, "owner_id": owner_id, "token": token, "reason": reason}))
            return QueueCard(id=card_id, title="Worker", status=QueueStatus.BLOCKED)

    monkeypatch.setattr(cli, "OpenClawWorkboardAdapter", ProtocolAdapter)
    argv = [
        "--config",
        str(config_path),
        "worker-protocol",
        action,
        "--repo",
        "example/project",
        "--orchestrator",
        "workers",
        "--card",
        "card-1",
        "--owner-id",
        "coder",
        "--token",
        "claim-1",
        *extra,
    ]

    assert cli.main(argv) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["id"] == "card-1"
    assert calls[0][0] == action
    call = calls[0][1]
    for field, value in expected.items():
        assert call[field] == value


def test_worker_protocol_cli_rejects_completion_without_structured_proof(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)

    class UnexpectedAdapter:
        def __init__(self, config: Any) -> None:
            del config
            raise AssertionError("validation must happen before the RPC adapter is used")

    monkeypatch.setattr(cli, "OpenClawWorkboardAdapter", UnexpectedAdapter)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "worker-protocol",
                "complete",
                "--repo",
                "example/project",
                "--orchestrator",
                "workers",
                "--card",
                "card-1",
                "--owner-id",
                "coder",
                "--token",
                "claim-1",
                "--summary",
                "PR opened",
            ]
        )
        == 3
    )
    assert "complete requires --summary and --proof-note" in capsys.readouterr().err


def test_worker_protocol_claims_next_card_from_default_direct_orchestrator(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.SUPERVISED)
    config = app_config(tmp_path, repo)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    adapter = DirectOrchestrator(config.state_dir / "orchestrators" / "example-project.db")
    board_id = "make-it-so-direct-example-project"
    adapter.ensure_board(board_id, "Project", "Direct work", tmp_path)
    card = adapter.create_card(
        board_id,
        QueueCardSpec(
            key="package-1",
            title="Implement package",
            notes="Use the portable worker protocol.",
            status=QueueStatus.READY,
            agent_id="coder",
        ),
    )

    assert cli.main(
        [
            "--config",
            str(config_path),
            "worker-protocol",
            "claim",
            "--repo",
            repo.full_name,
            "--owner-id",
            "worker-1",
            "--token",
            "secret-token",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["id"] == card.id
    assert payload["status"] == "running"


def test_worker_protocol_does_not_mutate_disabled_repo(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["repos"][0]["operation_mode"] = "disabled"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    class UnexpectedAdapter:
        def __init__(self, config: Any) -> None:
            del config
            raise AssertionError("disabled worker protocol must stop before adapter construction")

    monkeypatch.setattr(cli, "OpenClawWorkboardAdapter", UnexpectedAdapter)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "worker-protocol",
                "heartbeat",
                "--repo",
                "example/project",
                "--orchestrator",
                "workers",
                "--card",
                "card-1",
                "--owner-id",
                "coder",
                "--token",
                "claim-1",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "disabled"
    assert payload["card"] == "card-1"


def test_worker_protocol_does_not_mutate_advisory_repo(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path, operation_mode=OperationMode.ADVISORY)

    class UnexpectedAdapter:
        def __init__(self, config: Any) -> None:
            del config
            raise AssertionError("advisory worker protocol must stop before adapter construction")

    monkeypatch.setattr(cli, "OpenClawWorkboardAdapter", UnexpectedAdapter)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "worker-protocol",
                "heartbeat",
                "--repo",
                "example/project",
                "--orchestrator",
                "workers",
                "--card",
                "card-1",
                "--owner-id",
                "coder",
                "--token",
                "claim-1",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "advisory"
    assert payload["card"] == "card-1"


def test_recover_pr_does_not_mutate_disabled_repo(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["repos"][0]["operation_mode"] = "disabled"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    class UnexpectedGitHub:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            raise AssertionError("disabled recovery must stop before GitHub access")

    monkeypatch.setattr(cli, "GhGitHubProvider", UnexpectedGitHub)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "recover-pr",
                "--repo",
                "example/project",
                "--action-id",
                "action-1",
                "--pr",
                "40",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "disabled"


def test_recover_pr_does_not_mutate_advisory_repo(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path, operation_mode=OperationMode.ADVISORY)

    class UnexpectedGitHub:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            raise AssertionError("advisory recovery must stop before GitHub access")

    monkeypatch.setattr(cli, "GhGitHubProvider", UnexpectedGitHub)

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "recover-pr",
                "--repo",
                "example/project",
                "--action-id",
                "action-1",
                "--pr",
                "40",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "advisory"


def test_recover_pr_is_idempotent_after_crash_after_push(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path, operation_mode=OperationMode.AUTONOMOUS)
    state = StateStore(tmp_path / "state" / "state.db")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement the recovered slice",
        reason="The worker pushed the PR before the local cleanup step crashed.",
        target_issue=39,
    )
    state.save_proposal(
        repo.full_name,
        "action-recover-1",
        "snapshot-1",
        decision.model_dump(mode="json"),
    )
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)

    class RecoveryGitHub:
        def pull_request(self, configured_repo: RepoConfig, number: int) -> dict[str, Any]:
            assert configured_repo.full_name == repo.full_name
            assert number == 40
            return {
                "number": number,
                "url": "https://github.com/example/project/pull/40",
                "baseRefName": "main",
                "headRefName": "make_it_so/work/39",
                "headRefOid": "head-40",
                "files": [{"path": "src/feature.py"}],
            }

    def recovery_github(**kwargs: Any) -> RecoveryGitHub:
        del kwargs
        return RecoveryGitHub()

    monkeypatch.setattr(cli, "GhGitHubProvider", recovery_github)
    argv = [
        "--config",
        str(config_path),
        "recover-pr",
        "--repo",
        repo.full_name,
        "--action-id",
        "action-recover-1",
        "--pr",
        "40",
    ]

    assert cli.main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["event_type"] == "PR_RECOVERED"
    proposal = state.proposal(repo.full_name, "action-recover-1")
    active = state.active_work(repo.full_name)
    assert proposal is not None
    assert active is not None
    assert proposal["status"] == "executed"
    assert active["pr_number"] == 40
    assert len(state.recent_events(repo.full_name, 10)) == 1

    assert cli.main(argv) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["status"] == "already_recovered"
    assert second["pr"] == 40
    assert len(state.recent_events(repo.full_name, 10)) == 1
