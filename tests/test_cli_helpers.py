from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import captains_chair.cli as cli
from captains_chair.courses import CourseStore
from captains_chair.models import (
    ActionKind,
    EventRecord,
    OpenClawWorkboardConfig,
    OperationMode,
    PlanDecision,
    WorkerAssignments,
)
from captains_chair.orchestration import QueueStatus, ReconcileResult
from captains_chair.state import StateStore
from tests.helpers import app_config, repo_config
from tests.test_cli_orchestration import _write_config  # pyright: ignore[reportPrivateUsage]
from tests.test_courses import ready_course


def _reconcile(**changes: Any) -> ReconcileResult:
    values: dict[str, Any] = {
        "board_id": "board",
        "proof_retries": (),
        "protocol_retries": (),
        "repairs_created": (),
        "retried": (),
        "control_plane_recoveries": (),
        "unblocked": (),
        "user_blockers": (),
        "dispatch": {"status": "ok"},
    }
    values.update(changes)
    return ReconcileResult(**values)


@pytest.mark.parametrize(
    ("mode", "blocked"),
    (
        (OperationMode.DISABLED, True),
        (OperationMode.ADVISORY, True),
        (OperationMode.SUPERVISED, False),
        (OperationMode.AUTONOMOUS, False),
    ),
)
def test_control_plane_mutation_block_matches_operation_mode(
    tmp_path: Path,
    mode: OperationMode,
    blocked: bool,
) -> None:
    repo = repo_config(tmp_path, mode=mode)
    result = cli._control_plane_mutation_block(  # pyright: ignore[reportPrivateUsage]
        repo,
        operation="test",
        mutation="mutation skipped",
        next_action="resume",
    )
    assert (result is not None) is blocked
    if result is not None:
        assert result["status"] == mode.value
        assert result["next_action"] == "resume"


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        ({"status": "dispatch_suppressed"}, 2),
        ({"status": "ok", "model_health": {"status": "degraded"}}, 2),
        ({"status": "ok", "model_health": {"status": "not_supported"}}, 0),
        ({"status": "ok"}, 0),
    ),
)
def test_dispatch_and_reconcile_exit_codes_fail_closed(payload: dict[str, Any], expected: int) -> None:
    assert cli._dispatch_exit_code(payload) == expected  # pyright: ignore[reportPrivateUsage]
    result = _reconcile(
        user_blockers=("USER_SECRET: missing",) if expected == 2 else (),
    )
    diagnostics = {"status": "ok" if expected == 0 else "degraded"}
    assert cli._reconcile_exit_code(result, diagnostics, []) == expected  # pyright: ignore[reportPrivateUsage]


def test_reconcile_exit_code_includes_cleanup_and_notification_failures() -> None:
    assert (
        cli._reconcile_exit_code(  # pyright: ignore[reportPrivateUsage]
            _reconcile(workspace_cleanup_failures=("worktree",)),
            {"status": "ok"},
            [],
        )
        == 2
    )
    assert (
        cli._reconcile_exit_code(  # pyright: ignore[reportPrivateUsage]
            _reconcile(recovery_warnings=("recovery warning",)),
            {"status": "ok"},
            cast(list[EventRecord], [object()]),
        )
        == 2
    )


@pytest.mark.parametrize(
    ("metadata", "expected"),
    (
        ({"workerProtocol": {"detail": " USER_SECRET: provide token "}}, "USER_SECRET: provide token"),
        ({"comments": [{"body": "first"}, {"body": " latest "}]}, "latest"),
        ({"comments": ["not a comment"]}, ""),
        ({}, ""),
    ),
)
def test_card_block_reason_prefers_protocol_detail_and_latest_comment(
    metadata: dict[str, Any],
    expected: str,
) -> None:
    card = SimpleNamespace(metadata=metadata)
    assert cli._card_block_reason(card) == expected  # pyright: ignore[reportPrivateUsage]


def test_preflight_diagnostic_rows_are_flattened_and_bounded() -> None:
    payload = {
        "diagnostics": [
            {
                "card": {"id": "card-1", "title": "Card", "sourceUrl": "https://example/card"},
                "diagnostics": [
                    {
                        "kind": "stranded_ready",
                        "severity": "warning",
                        "title": "Ready but unclaimed",
                        "detail": "Inspect worker",
                        "actions": [{"label": "Claim"}, {"kind": "retry"}],
                    },
                    "ignore me",
                ],
            },
            {"card_id": "card-2", "reason": "fallback"},
            "ignore me",
        ]
    }
    rows = cli._preflight_diagnostic_rows(payload)  # pyright: ignore[reportPrivateUsage]
    assert rows == [
        {
            "card_id": "card-1",
            "card_title": "Card",
            "source_url": "https://example/card",
            "kind": "stranded_ready",
            "severity": "warning",
            "title": "Ready but unclaimed",
            "detail": "Inspect worker",
            "actions": ["Claim", "retry"],
        },
        {
            "card_id": "card-2",
            "card_title": None,
            "source_url": None,
            "kind": None,
            "severity": None,
            "title": None,
            "detail": "fallback",
            "actions": [],
        },
    ]


def test_board_diagnostics_prefers_board_filter_and_supports_legacy_adapter() -> None:
    class BoardAdapter:
        def diagnostics_for_board(self, board_id: str) -> dict[str, Any]:
            return {"board": board_id}

        def diagnostics(self) -> dict[str, Any]:
            raise AssertionError("board-filtered diagnostics should be preferred")

    class LegacyAdapter:
        def diagnostics(self) -> dict[str, Any]:
            return {"legacy": True}

    assert cli._board_diagnostics(BoardAdapter(), "board") == {"board": "board"}  # pyright: ignore[reportPrivateUsage]
    assert cli._board_diagnostics(LegacyAdapter(), "board") == {"legacy": True}  # pyright: ignore[reportPrivateUsage]


def test_worker_protocol_preflight_covers_missing_and_invocation_failures(
    monkeypatch: Any,
) -> None:
    assert cli._preflight_worker_protocol(SimpleNamespace()) is None  # pyright: ignore[reportPrivateUsage]
    blank = cli._preflight_worker_protocol(  # pyright: ignore[reportPrivateUsage]
        SimpleNamespace(config=SimpleNamespace(captains_chair_command=("",)))
    )
    assert blank == {"status": "failed", "error": "worker lifecycle helper command has no executable"}
    missing = cli._preflight_worker_protocol(  # pyright: ignore[reportPrivateUsage]
        SimpleNamespace(config=SimpleNamespace(captains_chair_command=("missing-helper",)))
    )
    assert missing is not None and missing["status"] == "failed"

    def failing_command(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(returncode=1, stderr="failed", stdout="")

    monkeypatch.setattr(cli, "run_command", failing_command)
    failed = cli._preflight_worker_protocol(  # pyright: ignore[reportPrivateUsage]
        SimpleNamespace(config=SimpleNamespace(captains_chair_command=(sys.executable,)))
    )
    assert failed is not None
    assert failed["status"] == "failed"
    assert "failed" in failed["error"]


def test_worker_protocol_preflight_accepts_a_working_module_command() -> None:
    result = cli._preflight_worker_protocol(  # pyright: ignore[reportPrivateUsage]
        SimpleNamespace(
            config=SimpleNamespace(captains_chair_command=(sys.executable, "-m", "captains_chair"))
        )
    )
    assert result == {"status": "passed", "executable": sys.executable}


def test_direct_orchestrator_defaults_are_stable_and_board_free(tmp_path: Path) -> None:
    repo = repo_config(tmp_path)
    config = app_config(tmp_path, repo)
    direct = cli._default_direct_orchestrator_config(config, repo)  # pyright: ignore[reportPrivateUsage]
    assert direct.board_prefix == "captains-chair-direct"
    assert direct.database_path.parent == config.state_dir / "orchestrators"
    assert direct.workers.coder.startswith("captains-chair-example-project-coder")
    assert cli._board_id(config, repo.full_name) == "captains-chair-direct-example-project"  # pyright: ignore[reportPrivateUsage]


def _openclaw_config(tmp_path: Path) -> tuple[Any, Any]:
    base_repo = repo_config(tmp_path)
    repo = base_repo.model_copy(update={"orchestrator": "openclaw"})
    workers = WorkerAssignments(
        captain="captain-agent",
        coder="coder-agent",
        reviewer="reviewer-agent",
        tester="tester-agent",
        ux_reviewer="ux-agent",
        final_reviewer="final-agent",
        merger="merge-agent",
        verifier="verify-agent",
    )
    orchestrator = OpenClawWorkboardConfig(
        workers=workers,
        executable="openclaw-test",
        session_limit=17,
    )
    config = app_config(tmp_path, base_repo).model_copy(
        update={"repos": (repo,), "orchestrators": {"openclaw": orchestrator}}
    )
    return config, repo


def test_openclaw_worker_model_and_runtime_helpers_resolve_configured_and_default_routes(
    tmp_path: Path,
) -> None:
    config, repo = _openclaw_config(tmp_path)
    expected = cli._expected_worker_models(config, repo.full_name)  # pyright: ignore[reportPrivateUsage]

    assert expected["coder"] == "codex/gpt-5.3-codex-spark"
    assert expected["coder-agent"] == expected["coder"]
    assert cli._openclaw_session_limit(config, repo.full_name) == 17  # pyright: ignore[reportPrivateUsage]
    assert cli._openclaw_executable_for_repo(config, repo) == "openclaw-test"  # pyright: ignore[reportPrivateUsage]

    direct = repo.model_copy(update={"orchestrator": None})
    assert cli._expected_worker_models(config.model_copy(update={"repos": (direct,)}), direct.full_name) == {}  # pyright: ignore[reportPrivateUsage]
    assert cli._openclaw_session_limit(config.model_copy(update={"repos": (direct,)}), direct.full_name) == cli.DEFAULT_SESSION_LIMIT  # pyright: ignore[reportPrivateUsage]
    assert cli._openclaw_executable_for_repo(config, direct, "fallback") == "fallback"  # pyright: ignore[reportPrivateUsage]


def test_openclaw_portfolio_usage_sync_reports_single_and_multi_repo_degradation(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    config, repo = _openclaw_config(tmp_path)
    state = StateStore(config.state_dir / "state.db")
    calls: list[str] = []

    def sync(
        state: Any,
        *,
        repo: str,
        executable: str,
        expected_models: dict[str, str],
        session_context: dict[str, dict[str, str]],
        session_limit: int,
    ) -> dict[str, Any]:
        del state, executable, expected_models, session_context, session_limit
        calls.append(repo)
        return {"sessions_seen": 2, "sessions_imported": 1, "sessions_with_usage": 1, "session_limit_reached": True}

    monkeypatch.setattr(cli, "sync_openclaw_sessions", sync)
    one = cli._sync_openclaw_sessions_for_portfolio(  # pyright: ignore[reportPrivateUsage]
        config, state, fallback_executable="fallback"
    )
    assert one["status"] == "degraded"
    assert "session window is full" in one["error"]

    second = repo.model_copy(update={"full_name": "example/second", "local_path": tmp_path / "second"})
    multi_config = config.model_copy(update={"repos": (repo, second)})
    multi = cli._sync_openclaw_sessions_for_portfolio(  # pyright: ignore[reportPrivateUsage]
        multi_config, state, fallback_executable="fallback"
    )
    assert multi["status"] == "degraded"
    assert multi["sessions_seen"] == 4
    assert calls == [repo.full_name, repo.full_name, second.full_name]


def test_print_schedule_result_uses_install_and_render_boundaries(
    monkeypatch: Any,
    capsys: Any,
    tmp_path: Path,
) -> None:
    spec = SimpleNamespace()

    class InstallScheduler:
        def install(self, value: Any) -> Any:
            assert value is spec
            return SimpleNamespace(kind="test", identifier="job-1", enabled=True)

    class RenderScheduler:
        def render(self, value: Any) -> str:
            assert value is spec
            return "rendered"

    def install_scheduler(kind: str, executable: str) -> Any:
        del kind, executable
        return InstallScheduler()

    monkeypatch.setattr(cli, "build_scheduler", install_scheduler)
    cli.print_schedule_result("custom", cast(Any, spec), "openclaw")
    assert json.loads(capsys.readouterr().out)["identifier"] == "job-1"

    def render_scheduler(kind: str, executable: str) -> Any:
        del kind, executable
        return RenderScheduler()

    monkeypatch.setattr(cli, "build_scheduler", render_scheduler)
    cli.print_schedule_result("cron", cast(Any, spec), "openclaw")
    assert capsys.readouterr().out.strip() == "rendered"


def test_queue_status_enum_is_used_by_preflight_fixture() -> None:
    assert QueueStatus.READY.value == "ready"


def test_main_schema_status_and_usage_report_commands(tmp_path: Path, capsys: Any) -> None:
    config_path = _write_config(tmp_path)
    schema_path = tmp_path / "generated-schema.json"

    assert cli.main(["schema", "--output", str(schema_path)]) == 0
    assert json.loads(schema_path.read_text(encoding="utf-8"))["title"] == "AppConfig"
    capsys.readouterr()

    assert cli.main(["--config", str(config_path), "status", "--repo", "example/project"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["state"] == "unbaselined"
    assert status["events"] == []

    assert cli.main(["--config", str(config_path), "usage", "report", "--summary"]) == 0
    assert "Captain's Chair token audit" in capsys.readouterr().out


def test_main_planning_session_uses_durable_course_context(tmp_path: Path, capsys: Any) -> None:
    config_path = _write_config(tmp_path)
    CourseStore(tmp_path).save(ready_course())

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "planning-session",
                "--repo",
                "example/project",
                "--course-key",
                "feature-search",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["course_key"] == "feature-search"
    assert payload["mutation_requires_course_approval"] is True


def test_main_proposal_approval_rejection_ack_and_details(tmp_path: Path, capsys: Any) -> None:
    config_path = _write_config(tmp_path)
    config = cli.load_config(config_path)
    state = StateStore(config.state_dir / "state.db")
    decision = PlanDecision(
        action=ActionKind.REPORT_ONLY,
        summary="Report current state",
        reason="The repository needs a status report.",
    )
    state.save_proposal("example/project", "action-1", "snapshot-1", decision.model_dump(mode="json"))

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "approve",
                "--repo",
                "example/project",
                "--action-id",
                "action-1",
                "--by",
                "owner@example.com",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "approved"

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "details",
                "--repo",
                "example/project",
                "--action-id",
                "action-1",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "proposed"

    state.save_proposal("example/project", "action-2", "snapshot-1", decision.model_dump(mode="json"))
    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "reject",
                "--repo",
                "example/project",
                "--action-id",
                "action-2",
                "--reason",
                "out of scope",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "rejected"

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "ack",
                "--repo",
                "example/project",
                "--fingerprint",
                "fingerprint-1",
                "--event-type",
                "ATTENTION_REQUIRED",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "acknowledged"


def test_main_schedule_commands_build_expected_specs(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)
    captured: list[tuple[str, Any, str]] = []

    def capture(kind: str, spec: Any, executable: str) -> None:
        captured.append((kind, spec, executable))

    monkeypatch.setattr(cli, "print_schedule_result", capture)
    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "schedule",
                "--repo",
                "example/project",
                "--harness",
                "test",
                "--kind",
                "cron",
                "--enable",
                "--watch",
            ]
        )
        == 0
    )
    assert captured[0][0] == "cron"
    assert captured[0][1].name.startswith("captains-chair-watch-")
    assert "--watch" in captured[0][1].argv
    capsys.readouterr()

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "orchestration-schedule",
                "--repo",
                "example/project",
                "--kind",
                "cron",
            ]
        )
        == 0
    )
    assert captured[1][1].name == "captains-chair-dispatch-example-project"
    assert "orchestrate" in captured[1][1].argv


def test_main_orchestrate_status_and_diagnostics_are_read_only(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    config_path = _write_config(tmp_path)

    class Adapter:
        def list_cards(self, board_id: str) -> list[Any]:
            assert board_id == "captains-chair-example-project"
            return []

        def diagnostics(self) -> dict[str, Any]:
            return {"status": "ok", "diagnostics": []}

    orchestrator = SimpleNamespace(adapter=Adapter())
    def fake_orchestrator(config: Any, repo: Any) -> Any:
        del config, repo
        return orchestrator

    monkeypatch.setattr(cli, "_orchestrator", fake_orchestrator)
    assert cli.main(["--config", str(config_path), "orchestrate", "status", "--repo", "example/project"]) == 0
    assert json.loads(capsys.readouterr().out)["cards"] == []
    assert cli.main(["--config", str(config_path), "orchestrate", "diagnostics", "--repo", "example/project"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
