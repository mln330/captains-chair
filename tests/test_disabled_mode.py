from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from make_it_so.engine import ControlPlaneEngine
from make_it_so.github import GitHubProvider
from make_it_so.harness import HarnessAdapter
from make_it_so.models import OperationMode
from make_it_so.notifications import Notifier
from make_it_so.state import StateStore
from tests.helpers import app_config, model_policy, repo_config


class ExplodingGitHub:
    def snapshot(self, repo: Any) -> Any:
        raise AssertionError("disabled Number 1 must not read GitHub")


class ExplodingHarness:
    def run(self, **kwargs: Any) -> Any:
        raise AssertionError("disabled Number 1 must not invoke a model")


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
