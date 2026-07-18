from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from make_it_so.engine import ControlPlaneEngine
from make_it_so.github import GhGitHubProvider, RepositorySnapshot
from make_it_so.harness import HarnessAdapter
from make_it_so.models import (
    ActionKind,
    ActionScope,
    EventRecord,
    HarnessConfig,
    ModelTarget,
    OperationMode,
    PlanDecision,
    RunState,
)
from make_it_so.notifications import Notifier
from make_it_so.state import StateStore
from tests.helpers import app_config, model_policy, repo_config


class PR34GitHub:
    calls: list[str]

    def __init__(self) -> None:
        self.calls = []

    def snapshot(self, repo: object) -> RepositorySnapshot:
        del repo
        self.calls.append("snapshot")
        return RepositorySnapshot(
            repo={"nameWithOwner": "NewmanZone/PrintHub"},
            issues=[],
            pull_requests=[
                {
                    "number": 34,
                    "title": "Update execution plan",
                    "headRefName": "make-it-so/docs/printhub-plan-sync",
                    "headRefOid": "stale-head",
                    "body": "stale planning text",
                }
            ],
            branches=["main", "make-it-so/docs/printhub-plan-sync"],
            workflow_runs=[],
        )


class MaintenanceHarness(HarnessAdapter):
    calls: int

    def __init__(self) -> None:
        super().__init__(HarnessConfig(kind="codex", executable="codex"))
        self.calls = 0

    def invoke(
        self,
        *,
        prompt: str,
        model: ModelTarget,
        role: str,
        output_model: type[BaseModel],
        cwd: Path,
        writable: bool,
        session_id: str,
    ) -> dict[str, Any]:
        del prompt, model, role, output_model, cwd, writable, session_id
        self.calls += 1
        return PlanDecision(
            action=ActionKind.MAINTENANCE,
            scope=ActionScope.CONTROL_PLANE,
            summary="Repair the external generator",
            reason="The generator is the root blocker, not the open docs PR.",
        ).model_dump(mode="json")


class MemoryNotifier:
    def __init__(self) -> None:
        self.events: list[str] = []

    def send(self, event: EventRecord) -> None:
        self.events.append(event.event_type)


def test_pr34_does_not_override_external_generator_repair(tmp_path: Path) -> None:
    planning = tmp_path / "ISSUES_EXECUTION_PLAN.md"
    planning.write_text("# Durable plan\n", encoding="utf-8")
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    state.save_baseline(repo.full_name, "baseline", tmp_path / "baseline.json", analyzed=True)
    state.transition(repo.full_name, RunState.BASELINE_REVIEW)
    state.transition(repo.full_name, RunState.READY)
    github = PR34GitHub()
    harness = MaintenanceHarness()
    notifier = MemoryNotifier()
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GhGitHubProvider, github),
        harness,
        cast(Notifier, notifier),
        model_policy(),
    )

    result = engine.cycle(repo, shadow=False, execute=True)

    assert result.exit_code == 2
    assert result.event.event_type == "CONTROL_PLANE_MAINTENANCE_REQUIRED"
    assert result.event.state == RunState.DEGRADED
    assert github.calls == ["snapshot"]
    assert notifier.events == ["CONTROL_PLANE_MAINTENANCE_REQUIRED"]
