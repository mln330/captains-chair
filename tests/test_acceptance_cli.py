from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

import captains_chair.cli as cli
from captains_chair.command import CommandResult
from captains_chair.models import (
    DirectOrchestratorConfig,
    HarnessConfig,
    OperationMode,
    RunState,
    WorkerAssignments,
)
from captains_chair.state import StateStore
from tests.helpers import app_config, repo_config
from tests.test_cli_orchestration import _write_config  # pyright: ignore[reportPrivateUsage]


def _workers() -> WorkerAssignments:
    return WorkerAssignments(
        captain="captain",
        coder="coder",
        reviewer="reviewer",
        tester="tester",
        ux_reviewer="ux",
        final_reviewer="final",
        merger="merger",
        verifier="verifier",
    )


def test_doctor_reports_healthy_and_all_operator_facing_failures(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    healthy_repo = repo_config(tmp_path)
    healthy = app_config(tmp_path, healthy_repo).model_copy(
        update={
            "harnesses": {
                "python": HarnessConfig(kind="codex", executable=sys.executable),
            }
        }
    )

    def healthy_which(name: str) -> str | None:
        return sys.executable if name == "gh" else None

    def successful_command(*_args: Any, **_kwargs: Any) -> CommandResult:
        return CommandResult(0, "", "")

    monkeypatch.setattr(cli.shutil, "which", healthy_which)
    monkeypatch.setattr(cli, "run_command", successful_command)
    assert cli.cmd_doctor(healthy) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"

    missing_repo = repo_config(tmp_path / "missing").model_copy(update={"require_project_manifest": True})
    degraded = app_config(tmp_path, missing_repo).model_copy(
        update={
            "harnesses": {
                "missing": HarnessConfig(kind="codex", executable="missing-codex"),
            }
        }
    )

    def missing_which(_name: str) -> None:
        return None

    monkeypatch.setattr(cli.shutil, "which", missing_which)
    assert cli.cmd_doctor(degraded) == 2
    failures = json.loads(capsys.readouterr().out)
    assert failures["status"] == "degraded"
    assert len(failures["problems"]) >= 4

    def available_which(_name: str) -> str:
        return sys.executable

    def failed_auth(*_args: Any, **_kwargs: Any) -> CommandResult:
        return CommandResult(1, "", "auth")

    monkeypatch.setattr(cli.shutil, "which", available_which)
    monkeypatch.setattr(cli, "run_command", failed_auth)
    assert cli.cmd_doctor(healthy) == 2
    assert "not authenticated" in capsys.readouterr().out


def test_cli_helpers_fail_closed_for_unknown_runtime_shapes(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec = cast(Any, SimpleNamespace())

    def no_renderer(_kind: str, _executable: str) -> SimpleNamespace:
        return SimpleNamespace()

    monkeypatch.setattr(cli, "build_scheduler", no_renderer)
    with pytest.raises(RuntimeError, match="does not provide render"):
        cli.print_schedule_result("systemd", spec, "openclaw")

    class Renderer:
        def render(self, value: Any) -> dict[str, str]:
            assert value is spec
            return {"unit": "captains-chair.service"}

    def renderer(_kind: str, _executable: str) -> Renderer:
        return Renderer()

    monkeypatch.setattr(cli, "build_scheduler", renderer)
    cli.print_schedule_result("systemd", spec, "openclaw")
    assert json.loads(capsys.readouterr().out)["unit"] == "captains-chair.service"

    repo = repo_config(tmp_path).model_copy(update={"orchestrator": "missing"})
    config = app_config(tmp_path, repo_config(tmp_path)).model_copy(
        update={"repos": (repo,), "orchestrators": {}}
    )
    assert cli._openclaw_session_limit(config, repo.full_name) == cli.DEFAULT_SESSION_LIMIT  # pyright: ignore[reportPrivateUsage]

    odd = config.model_copy(update={"orchestrators": {"missing": SimpleNamespace(session_limit="many")}})
    assert cli._openclaw_session_limit(odd, repo.full_name) == cli.DEFAULT_SESSION_LIMIT  # pyright: ignore[reportPrivateUsage]

    class Diagnostics:
        def diagnostics_for_board(self, _board: str) -> str:
            return "invalid"

        def diagnostics(self) -> str:
            return "invalid"

    assert cli._board_diagnostics(Diagnostics(), "board") == {}  # pyright: ignore[reportPrivateUsage]
    assert cli._card_block_reason(SimpleNamespace(metadata="invalid")) == ""  # pyright: ignore[reportPrivateUsage]
    assert (
        cli._card_block_reason(  # pyright: ignore[reportPrivateUsage]
            SimpleNamespace(metadata={"workerProtocol": {"detail": 5}, "comments": [{"body": 7}]})
        )
        == ""
    )


def test_main_reports_degraded_state_json_usage_and_invalid_ids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _write_config(tmp_path)
    config = cli.load_config(config_path)
    state = StateStore(config.state_dir / "state.db")
    state.transition("example/project", RunState.DEGRADED)
    event = state.record_event(
        repo="example/project",
        run_id="acceptance",
        state=RunState.DEGRADED,
        event_type="ACCEPTANCE_DEGRADED",
        summary="Acceptance fixture",
        reason="Exercise degraded reporting",
        fingerprint="acceptance",
        evidence={"next_action": "repair"},
    )

    assert cli.main(["--config", str(config_path), "status", "--repo", "example/project"]) == 2
    assert json.loads(capsys.readouterr().out)["state"] == "degraded"
    assert cli.main(["--config", str(config_path), "usage", "report"]) == 0
    assert "direct_calls" in json.loads(capsys.readouterr().out)
    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "details",
                "--repo",
                "example/project",
                "--event-id",
                event.event_id,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["event_id"] == event.event_id

    invalid_commands = (
        ["approve", "--repo", "example/project", "--action-id", "missing"],
        ["reject", "--repo", "example/project", "--action-id", "missing"],
        ["details", "--repo", "example/project"],
        ["details", "--repo", "example/project", "--action-id", "missing"],
        ["details", "--repo", "example/project", "--event-id", "missing"],
    )
    for command in invalid_commands:
        assert cli.main(["--config", str(config_path), *command]) == 3
        assert "failed" in capsys.readouterr().err


def test_main_canary_validation_and_plan_paths_are_non_mutating(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _write_config(tmp_path)

    class Adapter:
        def list_cards(self, _board: str) -> list[Any]:
            return []

    def orchestrator(_config: Any, _repo: str) -> SimpleNamespace:
        return SimpleNamespace(adapter=Adapter())

    monkeypatch.setattr(cli, "_orchestrator", orchestrator)
    common = ["--config", str(config_path), "orchestrate", "canary", "--repo", "example/project"]

    assert cli.main([*common, "--run", "--check", "--card", "card"]) == 3
    assert "cannot use" in capsys.readouterr().err
    assert cli.main([*common, "--check"]) == 3
    assert "requires --card" in capsys.readouterr().err
    assert cli.main([*common, "--check", "--card", "missing"]) == 3
    assert "not found" in capsys.readouterr().err
    assert cli.main(common) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["status"] == "planned"
    assert "does not create a card" in planned["next_action"]


def test_main_runtime_install_and_schedule_validation_are_explicit(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _write_config(tmp_path)
    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "runtime-install",
                "--orchestrator",
                "missing",
                "--workspace-root",
                str(tmp_path),
            ]
        )
        == 3
    )
    assert "unknown orchestrator" in capsys.readouterr().err

    repo = repo_config(tmp_path, mode=OperationMode.SUPERVISED).model_copy(update={"orchestrator": "direct"})
    direct_config = app_config(tmp_path, repo_config(tmp_path)).model_copy(
        update={
            "repos": (repo,),
            "orchestrators": {
                "direct": DirectOrchestratorConfig(
                    database_path=tmp_path / "direct.db",
                    workers=_workers(),
                )
            },
        }
    )
    direct_path = tmp_path / "direct.yaml"
    direct_path.write_text(yaml.safe_dump(direct_config.model_dump(mode="json")), encoding="utf-8")
    assert (
        cli.main(
            [
                "--config",
                str(direct_path),
                "runtime-install",
                "--orchestrator",
                "direct",
                "--workspace-root",
                str(tmp_path),
            ]
        )
        == 3
    )
    assert "only supports openclaw_workboard" in capsys.readouterr().err

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "schedule",
                "--repo",
                "example/project",
                "--harness",
                "missing",
                "--kind",
                "cron",
            ]
        )
        == 3
    )
    assert "unknown harness" in capsys.readouterr().err

    captured: list[Any] = []

    def capture_schedule(_kind: str, spec: Any, _executable: str) -> None:
        captured.append(spec)

    monkeypatch.setattr(cli, "print_schedule_result", capture_schedule)
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
                "--live",
                "--continue-run",
            ]
        )
        == 0
    )
    assert "--live" in captured[0].argv
    assert "--continue-run" in captured[0].argv


def test_main_dispatches_doctor_and_usage_sync(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _write_config(tmp_path)

    def doctor(_config: Any) -> int:
        return 2

    monkeypatch.setattr(cli, "cmd_doctor", doctor)
    assert cli.main(["--config", str(config_path), "doctor"]) == 2

    def sync(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "ok", "sessions_imported": 1}

    monkeypatch.setattr(cli, "sync_openclaw_sessions", sync)
    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "usage",
                "sync-openclaw",
                "--repo",
                "example/project",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["sessions_imported"] == 1
