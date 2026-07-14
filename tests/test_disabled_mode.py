from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from captains_chair.engine import ControlPlaneEngine
from captains_chair.github import GitHubProvider
from captains_chair.harness import HarnessAdapter
from captains_chair.models import OperationMode
from captains_chair.notifications import Notifier
from captains_chair.state import StateStore
from tests.helpers import app_config, model_policy, repo_config


class ExplodingGitHub:
    def snapshot(self, repo: Any) -> Any:
        raise AssertionError("disabled Captain must not read GitHub")


class ExplodingHarness:
    def run(self, **kwargs: Any) -> Any:
        raise AssertionError("disabled Captain must not invoke a model")


class MemoryNotifier:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def send(self, event: Any) -> None:
        self.events.append(event)


def test_disabled_cycle_stops_before_github_or_model(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.DISABLED)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    notifier = MemoryNotifier()
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GitHubProvider, ExplodingGitHub()),
        cast(HarnessAdapter, ExplodingHarness()),
        cast(Notifier, notifier),
        model_policy(),
    )

    result = engine.cycle(repo, shadow=False, execute=True)

    assert result.exit_code == 0
    assert result.event.event_type == "CONTROL_PLANE_DISABLED"
    assert notifier.events == [result.event]
    assert state.baseline(repo.full_name) is None


def test_disabled_watch_stops_before_active_work(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.DISABLED)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    engine = ControlPlaneEngine(
        config,
        state,
        cast(GitHubProvider, ExplodingGitHub()),
        cast(HarnessAdapter, ExplodingHarness()),
        cast(Notifier, MemoryNotifier()),
        model_policy(),
    )

    result = engine.watch(repo, shadow=False, execute=True)

    assert result is not None
    assert result.event.event_type == "CONTROL_PLANE_DISABLED"
