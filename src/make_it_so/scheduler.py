from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from make_it_so.command import CommandRunner, require_success, run_command
from make_it_so.openclaw_workboard import OpenClawWorkboardError, decode_openclaw_json
from make_it_so.plugins import EntryPointProvider, load_entrypoint_plugins


@dataclass(frozen=True)
class ScheduleSpec:
    name: str
    argv: tuple[str, ...]
    cwd: Path
    every: str = "2h"
    cron: str = "0 */2 * * *"
    enabled: bool = False


@dataclass(frozen=True)
class InstalledSchedule:
    kind: str
    identifier: str
    enabled: bool


@runtime_checkable
class SchedulerAdapter(Protocol):
    def install(self, spec: ScheduleSpec) -> InstalledSchedule: ...


Scheduler = SchedulerAdapter
SchedulerBuilder = Callable[[str, CommandRunner], SchedulerAdapter]
SCHEDULER_ADAPTER_ENTRYPOINT_GROUP = "make_it_so.scheduler_adapters"


class OpenClawScheduler:
    def __init__(self, executable: str, runner: CommandRunner = run_command) -> None:
        self.executable = executable
        self.runner = runner

    def install(self, spec: ScheduleSpec) -> InstalledSchedule:
        existing = self._find_existing(spec)
        if existing is not None:
            return existing
        command = [
            self.executable,
            "cron",
            "add",
            "--name",
            spec.name,
            "--every",
            spec.every,
            "--command-argv",
            json.dumps(spec.argv),
            "--command-cwd",
            str(spec.cwd),
            "--no-deliver",
            "--json",
        ]
        if not spec.enabled:
            command.append("--disabled")
        output = require_success(self.runner(command, timeout=120), "install OpenClaw schedule")
        try:
            raw_payload = decode_openclaw_json(output)
            if not isinstance(raw_payload, dict):
                raise TypeError("schedule response was not a JSON object")
            payload = cast(dict[str, Any], raw_payload)
            job = payload.get("job")
            job_payload = cast(dict[str, Any], job) if isinstance(job, dict) else {}
            identifier = str(payload.get("id") or job_payload.get("id") or "")
        except (OpenClawWorkboardError, TypeError, AttributeError) as exc:
            raise RuntimeError(f"OpenClaw returned an unreadable schedule response: {exc}") from exc
        if not identifier:
            raise RuntimeError("OpenClaw did not return the installed schedule ID")
        return InstalledSchedule(kind="openclaw", identifier=identifier, enabled=spec.enabled)

    def _find_existing(self, spec: ScheduleSpec) -> InstalledSchedule | None:
        """Inspect before adding so retries cannot create overlapping dispatchers."""
        output = require_success(self.runner([self.executable, "cron", "list", "--json"], timeout=120), "inspect OpenClaw schedules")
        try:
            raw_payload = decode_openclaw_json(output)
            if not isinstance(raw_payload, dict):
                raise TypeError("schedule list was not a JSON object")
            payload = cast(dict[str, Any], raw_payload)
            jobs_value = payload.get("jobs", [])
            if not isinstance(jobs_value, list):
                raise TypeError("schedule list did not contain jobs")
            jobs = cast(list[Any], jobs_value)
        except (OpenClawWorkboardError, TypeError, AttributeError) as exc:
            raise RuntimeError(f"OpenClaw returned an unreadable schedule list: {exc}") from exc

        for raw_job in jobs:
            if not isinstance(raw_job, dict):
                continue
            job = cast(dict[str, Any], raw_job)
            if str(job.get("name") or "") != spec.name:
                continue
            identifier = str(job.get("id") or "")
            if not identifier:
                raise RuntimeError(f"OpenClaw schedule {spec.name!r} has no ID")
            if not _schedule_matches(job, spec):
                raise RuntimeError(
                    f"OpenClaw schedule name {spec.name!r} already exists with a different specification"
                )
            current_enabled = bool(job.get("enabled", True))
            if current_enabled != spec.enabled:
                self._set_enabled(identifier, spec.enabled)
            return InstalledSchedule(
                kind="openclaw",
                identifier=identifier,
                enabled=spec.enabled,
            )
        return None

    def _set_enabled(self, identifier: str, enabled: bool) -> None:
        action = "enable" if enabled else "disable"
        require_success(
            self.runner([self.executable, "cron", action, identifier], timeout=120),
            f"{action} OpenClaw schedule",
        )


class SystemCronScheduler:
    def render(self, spec: ScheduleSpec) -> str:
        command = " ".join(shlex.quote(item) for item in spec.argv)
        return f"{spec.cron} cd {shlex.quote(str(spec.cwd))} && {command}"

    def install(self, spec: ScheduleSpec) -> InstalledSchedule:
        raise RuntimeError(
            "system cron installation is intentionally explicit; add the line returned by render()"
        )


class SystemdScheduler:
    def render(self, spec: ScheduleSpec) -> tuple[str, str]:
        executable = " ".join(shlex.quote(item) for item in spec.argv)
        service = (
            "[Unit]\nDescription=Make It So cycle\n\n"
            "[Service]\nType=oneshot\n"
            f"WorkingDirectory={spec.cwd}\nExecStart={executable}\n"
        )
        timer = (
            "[Unit]\nDescription=Run Make It So every two hours\n\n"
            "[Timer]\nOnBootSec=5m\nOnUnitActiveSec=2h\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n"
        )
        return service, timer

    def install(self, spec: ScheduleSpec) -> InstalledSchedule:
        raise RuntimeError(
            "systemd installation is intentionally explicit; write and enable the units returned by render()"
        )


class TaskScheduler:
    def render(self, spec: ScheduleSpec) -> list[str]:
        task_command = " ".join(f'"{item}"' if " " in item else item for item in spec.argv)
        return [
            "schtasks",
            "/Create",
            "/TN",
            spec.name,
            "/TR",
            task_command,
            "/SC",
            "HOURLY",
            "/MO",
            "2",
            "/F",
        ]

    def install(self, spec: ScheduleSpec) -> InstalledSchedule:
        raise RuntimeError(
            "Task Scheduler installation is intentionally explicit; run the argv returned by render()"
        )


class SchedulerAdapterRegistry:
    """Resolve scheduler implementations without coupling the core to a runtime."""

    def __init__(self) -> None:
        self._builders: dict[str, SchedulerBuilder] = {}
        self._loaded_plugins: set[str] = set()

    def register(self, kind: str, builder: SchedulerBuilder, *, replace: bool = False) -> None:
        normalized = kind.strip()
        if not normalized:
            raise ValueError("scheduler adapter kind must not be empty")
        if normalized in self._builders and not replace:
            raise ValueError(f"scheduler adapter is already registered: {normalized}")
        self._builders[normalized] = builder

    def discover(self, *, provider: EntryPointProvider | None = None) -> tuple[str, ...]:
        if provider is None:
            return load_entrypoint_plugins(
                self,
                group=SCHEDULER_ADAPTER_ENTRYPOINT_GROUP,
                loaded=self._loaded_plugins,
            )
        return load_entrypoint_plugins(
            self,
            group=SCHEDULER_ADAPTER_ENTRYPOINT_GROUP,
            provider=provider,
            loaded=self._loaded_plugins,
        )

    def build(
        self,
        kind: str,
        executable: str,
        runner: CommandRunner,
    ) -> Scheduler:
        builder = self._builders.get(kind)
        if builder is None:
            raise ValueError(
                f"scheduler kind {kind} has no installed adapter; "
                "register a scheduler with SchedulerAdapterRegistry"
            )
        return builder(executable, runner)


def _build_openclaw_scheduler(executable: str, runner: CommandRunner) -> Scheduler:
    return OpenClawScheduler(executable, runner)


def _build_cron_scheduler(executable: str, runner: CommandRunner) -> Scheduler:
    del executable, runner
    return SystemCronScheduler()


def _build_systemd_scheduler(executable: str, runner: CommandRunner) -> Scheduler:
    del executable, runner
    return SystemdScheduler()


def _build_task_scheduler(executable: str, runner: CommandRunner) -> Scheduler:
    del executable, runner
    return TaskScheduler()


DEFAULT_SCHEDULER_ADAPTERS = SchedulerAdapterRegistry()
DEFAULT_SCHEDULER_ADAPTERS.register("openclaw", _build_openclaw_scheduler)
DEFAULT_SCHEDULER_ADAPTERS.register("cron", _build_cron_scheduler)
DEFAULT_SCHEDULER_ADAPTERS.register("systemd", _build_systemd_scheduler)
DEFAULT_SCHEDULER_ADAPTERS.register("task-scheduler", _build_task_scheduler)


def register_scheduler_adapter(
    kind: str,
    builder: SchedulerBuilder,
    *,
    replace: bool = False,
) -> None:
    """Register a scheduler adapter in the process-wide default registry."""
    DEFAULT_SCHEDULER_ADAPTERS.register(kind, builder, replace=replace)


def build_scheduler(
    kind: str,
    executable: str = "openclaw",
    runner: CommandRunner = run_command,
    *,
    registry: SchedulerAdapterRegistry = DEFAULT_SCHEDULER_ADAPTERS,
) -> Scheduler:
    if registry is DEFAULT_SCHEDULER_ADAPTERS:
        registry.discover()
    return registry.build(kind, executable, runner)


_DURATION_PATTERN = re.compile(r"^(?P<amount>[1-9][0-9]*)(?P<unit>[smhd])$")


def _duration_ms(value: str) -> int | None:
    match = _DURATION_PATTERN.fullmatch(value.strip().lower())
    if not match:
        return None
    multipliers = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
    return int(match.group("amount")) * multipliers[match.group("unit")]


def _schedule_matches(job: dict[str, Any], spec: ScheduleSpec) -> bool:
    payload_value = job.get("payload")
    if not isinstance(payload_value, dict):
        return False
    payload = cast(dict[str, Any], payload_value)
    if payload.get("kind") != "command":
        return False
    argv_value = payload.get("argv")
    if not isinstance(argv_value, list):
        return False
    argv = cast(list[Any], argv_value)
    if tuple(str(item) for item in argv) != spec.argv:
        return False
    if str(payload.get("cwd") or "") != str(spec.cwd):
        return False
    schedule_value = job.get("schedule")
    if not isinstance(schedule_value, dict):
        return False
    schedule = cast(dict[str, Any], schedule_value)
    if schedule.get("kind") == "every":
        existing_ms = schedule.get("everyMs", schedule.get("every_ms"))
        return isinstance(existing_ms, (int, float)) and int(existing_ms) == (_duration_ms(spec.every) or -1)
    return schedule.get("kind") == "cron" and schedule.get("expr") == spec.cron
