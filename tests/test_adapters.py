from __future__ import annotations

from pathlib import Path
from typing import Any

from captains_chair.adapters import (
    CallbackUsageTelemetryAdapter,
    InteractionAdapter,
    NativeInteractionAdapter,
    UsageTelemetryAdapter,
)
from captains_chair.models import CheckpointStatus, RepoConfig, RequirementStatus
from captains_chair.notifications import NotifierAdapter, StdoutNotifier
from captains_chair.scheduler import InstalledSchedule, SchedulerAdapter, ScheduleSpec
from captains_chair.state import StateStore
from tests.test_courses import course, ready_course


def test_native_interaction_adapter_preserves_durable_course_operations() -> None:
    adapter = NativeInteractionAdapter()
    value = course()

    assert isinstance(adapter, InteractionAdapter)
    assert adapter.planning_session(value)["next_questions"] == ["What does success mean?"]
    answered = adapter.resolve_requirement(
        value,
        "success",
        RequirementStatus.VERIFIED,
        answer="The search flow is fast and ranked.",
        evidence=("owner",),
    )
    resolved = adapter.resolve_checkpoint(
        ready_course(),
        "ui-demo",
        CheckpointStatus.RESOLVED,
        resolved_by="owner",
        evidence=("demo",),
    )

    assert answered.readiness[0].status == RequirementStatus.VERIFIED
    assert resolved.checkpoints[0].status == CheckpointStatus.RESOLVED


def test_callback_usage_adapter_is_a_replaceable_telemetry_boundary(tmp_path: Path) -> None:
    calls: list[str] = []

    def synchronize(_state: Any, repo: Any) -> dict[str, Any]:
        calls.append(repo.full_name)
        return {"status": "ok", "total_tokens": 12}

    adapter = CallbackUsageTelemetryAdapter(synchronize)
    result = adapter.synchronize(
        StateStore(tmp_path / "state.db"),
        RepoConfig(full_name="example/project", local_path=tmp_path, planning_doc="PLAN.md"),
    )

    assert isinstance(adapter, UsageTelemetryAdapter)
    assert result == {"status": "ok", "total_tokens": 12}
    assert calls == ["example/project"]


def test_delivery_and_scheduler_contract_names_are_runtime_checkable() -> None:
    class MemoryScheduler:
        def install(self, spec: ScheduleSpec) -> InstalledSchedule:
            return InstalledSchedule("memory", spec.name, spec.enabled)

    assert isinstance(StdoutNotifier(), NotifierAdapter)
    assert isinstance(MemoryScheduler(), SchedulerAdapter)
