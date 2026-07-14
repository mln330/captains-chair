import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

import captains_chair.cli as cli
from captains_chair.command import CommandResult
from captains_chair.scheduler import (
    InstalledSchedule,
    OpenClawScheduler,
    Scheduler,
    SchedulerAdapterRegistry,
    ScheduleSpec,
    SystemCronScheduler,
    TaskScheduler,
    build_scheduler,
)


def spec(tmp_path: Path) -> ScheduleSpec:
    return ScheduleSpec(
        name="captains_chair-example-project",
        argv=("python", "-m", "captains_chair", "cycle", "--shadow"),
        cwd=tmp_path,
    )


def no_op_runner(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: int = 60,
) -> CommandResult:
    del command, cwd, input_text, timeout
    return CommandResult(0, "", "")


def test_openclaw_schedule_is_disabled_by_default(tmp_path: Path) -> None:
    commands: list[Sequence[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(command)
        if command[2] == "list":
            return CommandResult(0, json.dumps({"jobs": []}), "")
        return CommandResult(0, json.dumps({"id": "job-1"}), "")

    result = OpenClawScheduler("openclaw", runner).install(spec(tmp_path))
    assert result.identifier == "job-1"
    assert "--disabled" in commands[1]
    assert "--command-argv" in commands[1]
    assert commands[0][2] == "list"
    assert commands[1][2] == "add"


def test_openclaw_schedule_accepts_warning_prefixed_json(tmp_path: Path) -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        if command[2] == "list":
            return CommandResult(0, "migration warning\n{\"jobs\":[]}", "")
        return CommandResult(0, "migration warning\n{\"job\":{\"id\":\"job-2\"}}", "")

    result = OpenClawScheduler("openclaw", runner).install(spec(tmp_path))

    assert result.identifier == "job-2"


def test_openclaw_schedule_rejects_success_without_identifier(tmp_path: Path) -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        if command[2] == "list":
            return CommandResult(0, "{\"jobs\":[]}", "")
        return CommandResult(0, "{\"job\":{\"status\":\"created\"}}", "")

    with pytest.raises(RuntimeError, match="schedule ID"):
        OpenClawScheduler("openclaw", runner).install(spec(tmp_path))


def test_openclaw_schedule_install_is_idempotent(tmp_path: Path) -> None:
    commands: list[Sequence[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(command)
        return CommandResult(
            0,
            json.dumps(
                {
                    "jobs": [
                        {
                            "id": "job-existing",
                            "name": "captains_chair-example-project",
                            "enabled": False,
                            "schedule": {"kind": "every", "everyMs": 7_200_000},
                            "payload": {
                                "kind": "command",
                                "argv": list(spec(tmp_path).argv),
                                "cwd": str(tmp_path),
                            },
                        }
                    ]
                }
            ),
            "",
        )

    result = OpenClawScheduler("openclaw", runner).install(spec(tmp_path))

    assert result.identifier == "job-existing"
    assert len(commands) == 1
    assert commands[0][2] == "list"


def test_openclaw_schedule_reconciles_enabled_state(tmp_path: Path) -> None:
    value = replace(spec(tmp_path), enabled=True)
    commands: list[Sequence[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(command)
        if command[2] == "list":
            return CommandResult(
                0,
                json.dumps(
                    {
                        "jobs": [
                            {
                                "id": "job-existing",
                                "name": value.name,
                                "enabled": False,
                                "schedule": {"kind": "every", "everyMs": 7_200_000},
                                "payload": {
                                    "kind": "command",
                                    "argv": list(value.argv),
                                    "cwd": str(value.cwd),
                                },
                            }
                        ]
                    }
                ),
                "",
            )
        return CommandResult(0, "", "")

    result = OpenClawScheduler("openclaw", runner).install(value)

    assert result == InstalledSchedule(kind="openclaw", identifier="job-existing", enabled=True)
    assert commands[1][1:4] == ["cron", "enable", "job-existing"]


def test_openclaw_schedule_conflict_fails_closed(tmp_path: Path) -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return CommandResult(
            0,
            json.dumps(
                {
                    "jobs": [
                        {
                            "id": "job-conflict",
                            "name": "captains_chair-example-project",
                            "enabled": False,
                            "schedule": {"kind": "every", "everyMs": 300_000},
                            "payload": {
                                "kind": "command",
                                "argv": ["python", "-m", "captains_chair", "cycle", "--different"],
                                "cwd": str(tmp_path),
                            },
                        }
                    ]
                }
            ),
            "",
        )

    with pytest.raises(RuntimeError, match="different specification"):
        OpenClawScheduler("openclaw", runner).install(spec(tmp_path))


def test_openclaw_schedule_list_failure_fails_closed(tmp_path: Path) -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return CommandResult(1, "", "gateway unavailable")

    with pytest.raises(RuntimeError, match="inspect OpenClaw schedules"):
        OpenClawScheduler("openclaw", runner).install(spec(tmp_path))


def test_portable_schedule_renderers(tmp_path: Path) -> None:
    value = spec(tmp_path)
    cron = SystemCronScheduler().render(value)
    task = TaskScheduler().render(value)
    assert cron.startswith("0 */2 * * *")
    assert "python -m captains_chair cycle --shadow" in cron
    assert task[:3] == ["schtasks", "/Create", "/TN"]


def test_watch_schedule_can_run_on_a_short_heartbeat(tmp_path: Path) -> None:
    value = ScheduleSpec(
        name="captains_chair-watch-example-project",
        argv=("python", "-m", "captains_chair", "cycle", "--watch", "--live"),
        cwd=tmp_path,
        every="10m",
    )

    assert "python -m captains_chair cycle --watch --live" in SystemCronScheduler().render(value)
    assert value.every == "10m"


def test_scheduler_registry_builds_builtin_and_extension_adapters(tmp_path: Path) -> None:
    registry = SchedulerAdapterRegistry()

    class HermesScheduler:
        def install(self, spec: ScheduleSpec) -> InstalledSchedule:
            return InstalledSchedule(kind="hermes", identifier=spec.name, enabled=spec.enabled)

    registry.register("hermes", lambda executable, runner: HermesScheduler())

    adapter = build_scheduler("hermes", registry=registry)
    assert isinstance(adapter, HermesScheduler)
    assert adapter.install(spec(tmp_path)).identifier == "captains_chair-example-project"


def test_scheduler_registry_rejects_unknown_and_duplicate_kinds() -> None:
    registry = SchedulerAdapterRegistry()

    with pytest.raises(ValueError, match="no installed adapter"):
        registry.build("hermes", "hermes", no_op_runner)

    registry.register("hermes", lambda executable, runner: TaskScheduler())
    with pytest.raises(ValueError, match="already registered"):
        registry.register("hermes", lambda executable, runner: TaskScheduler())


def test_cli_dispatches_registered_runtime_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = SchedulerAdapterRegistry()

    class HermesScheduler:
        def install(self, spec: ScheduleSpec) -> InstalledSchedule:
            return InstalledSchedule(kind="hermes", identifier=spec.name, enabled=spec.enabled)

    registry.register("hermes", lambda _executable, _runner: HermesScheduler())

    def build_test_scheduler(kind: str, executable: str) -> Scheduler:
        return registry.build(kind, executable, no_op_runner)

    monkeypatch.setattr(
        cli,
        "build_scheduler",
        build_test_scheduler,
    )

    cli.print_schedule_result("hermes", spec(tmp_path), "hermes")

    assert json.loads(capsys.readouterr().out)["kind"] == "hermes"


def test_default_scheduler_registry_keeps_builtin_renderers_available(tmp_path: Path) -> None:
    cron = build_scheduler("cron")
    task_scheduler = build_scheduler("task-scheduler")
    assert isinstance(cron, SystemCronScheduler)
    assert isinstance(task_scheduler, TaskScheduler)
    assert cron.render(spec(tmp_path)).startswith("0 */2 * * *")
