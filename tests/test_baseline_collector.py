from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest
from pydantic import BaseModel

from make_it_so.baseline import DeepBaselineCollector
from make_it_so.command import CommandResult, CommandRunner
from make_it_so.engine import ControlPlaneEngine
from make_it_so.github import GitHubProvider, RepositorySnapshot
from make_it_so.harness import HarnessAdapter
from make_it_so.models import (
    BaselineAnalysis,
    EventRecord,
    HarnessConfig,
    ModelProfile,
    ModelTarget,
    ReasoningEffort,
    RepoConfig,
)
from make_it_so.state import StateStore
from tests.helpers import app_config, model_policy, repo_config

OutputModel = TypeVar("OutputModel", bound=BaseModel)


class SnapshotGitHub:
    def snapshot(self, repo: RepoConfig) -> RepositorySnapshot:
        return RepositorySnapshot(
            {"nameWithOwner": repo.full_name, "defaultBranchRef": {"name": repo.default_branch}},
            [{"number": 39, "title": "Next slice"}],
            [],
            [repo.default_branch, "feature/example"],
            [{"workflowName": "CI", "conclusion": "success"}],
        )


class BaselineHarness(HarnessAdapter):
    def __init__(self, config: HarnessConfig, *, fail: bool = False) -> None:
        super().__init__(config)
        self.fail = fail
        self.prompts: list[str] = []
        self.models: list[str] = []

    def invoke(
        self,
        *,
        prompt: str,
        model: ModelTarget,
        role: str,
        output_model: type[OutputModel],
        cwd: Path,
        writable: bool,
        session_id: str,
    ) -> dict[str, Any]:
        self.models.append(model.model)
        del model, role, output_model, cwd, writable, session_id
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("baseline provider unavailable")
        return BaselineAnalysis(
            summary="The repository is ready for the next documented slice.",
            implementation_status=("source inventory collected",),
            gaps=("one planned slice remains",),
            next_work_items=("implement the next issue",),
        ).model_dump(mode="json")


class FailOnCallHarness(BaselineHarness):
    def __init__(self, config: HarnessConfig, *, fail_on: int) -> None:
        super().__init__(config)
        self.fail_on = fail_on

    def invoke(
        self,
        *,
        prompt: str,
        model: ModelTarget,
        role: str,
        output_model: type[OutputModel],
        cwd: Path,
        writable: bool,
        session_id: str,
    ) -> dict[str, Any]:
        if len(self.prompts) + 1 == self.fail_on:
            self.prompts.append(prompt)
            raise RuntimeError("synthetic synthesis interruption")
        return super().invoke(
            prompt=prompt,
            model=model,
            role=role,
            output_model=output_model,
            cwd=cwd,
            writable=writable,
            session_id=session_id,
        )


def command_runner(
    calls: list[Sequence[str]],
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: int = 60,
) -> CommandResult:
    del cwd, input_text, timeout
    calls.append(command)
    if command and command[0] == "npm":
        return CommandResult(1, "", "frontend check failed")
    return CommandResult(0, "ok\n", "")


def recording_runner(calls: list[Sequence[str]]) -> CommandRunner:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        return command_runner(
            calls,
            command,
            cwd=cwd,
            input_text=input_text,
            timeout=timeout,
        )

    return runner


def _repo(tmp_path: Path, *, checks: tuple[str, ...] = ()) -> RepoConfig:
    repo = repo_config(tmp_path).model_copy(
        update={"canonical_docs": ("README.md", "missing.md"), "checks": checks}
    )
    (tmp_path / "README.md").write_text("# Example\n", encoding="utf-8")
    (tmp_path / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    return repo


def test_collects_deep_evidence_and_excludes_secret_like_source(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (tmp_path / "src" / "secret.py").write_text(
        "api_key = '123456789012'\n", encoding="utf-8"
    )
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.js").write_text("ignored\n", encoding="utf-8")
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text("name: CI\n", encoding="utf-8")
    calls: list[Sequence[str]] = []
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    collector = DeepBaselineCollector(
        config,
        state,
        cast(GitHubProvider, SnapshotGitHub()),
        model_policy(),
        runner=recording_runner(calls),
    )

    payload, artifact = collector.collect(repo, analyze=False, run_checks=False)

    assert artifact.is_file()
    assert payload["documents"]["missing.md"] == "<missing>"
    assert "src/secret.py" in payload["evidence_exclusions"]
    assert "src/secret.py" not in payload["source_contents"]
    assert "node_modules/ignored.js" not in payload["source_inventory"]["all_files"]
    assert payload["ci_workflows"][".github/workflows/ci.yml"] == "name: CI\n"
    assert payload["github"]["issues"]
    assert payload["checks"] == []
    baseline = state.baseline(repo.full_name)
    assert baseline is not None
    assert baseline["analyzed"] == 0
    assert state.current_state(repo.full_name).value == "ready"
    assert calls and all(command[0] == "git" for command in calls)


def test_analyzed_baseline_runs_checks_batches_models_and_records_artifact(tmp_path: Path) -> None:
    repo = _repo(tmp_path, checks=("pytest", "npm test"))
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    harness = BaselineHarness(HarnessConfig(kind="codex", executable="codex"))
    calls: list[Sequence[str]] = []
    collector = DeepBaselineCollector(
        config,
        state,
        cast(GitHubProvider, SnapshotGitHub()),
        model_policy(),
        runner=recording_runner(calls),
    )

    payload, artifact = collector.collect(repo, harness=harness, analyze=True, run_checks=True)

    assert artifact.is_file()
    assert payload["analysis"]["summary"].startswith("The repository")
    assert [item["returncode"] for item in payload["checks"]] == [0, 1]
    assert len(harness.prompts) == 2
    assert "Synthesize" in harness.prompts[-1]
    baseline = state.baseline(repo.full_name)
    assert baseline is not None
    assert baseline["analyzed"] == 1
    assert state.usage_summary(repo.full_name)["direct_calls"]["calls"] == 2


def test_baseline_records_missing_check_executable_without_aborting(tmp_path: Path) -> None:
    repo = _repo(tmp_path, checks=("missing-pytest -q",))
    config = app_config(tmp_path, repo)

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        if command and command[0] == "missing-pytest":
            raise FileNotFoundError("missing-pytest was not found")
        return CommandResult(0, "ok\n", "")

    collector = DeepBaselineCollector(
        config,
        StateStore(config.state_dir / "state.db"),
        cast(GitHubProvider, SnapshotGitHub()),
        model_policy(),
        runner=runner,
    )

    payload, artifact = collector.collect(repo, analyze=False, run_checks=True)

    assert artifact.is_file()
    assert payload["checks"] == [
        {
            "command": "missing-pytest -q",
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": "missing-pytest was not found",
            "execution_error": "FileNotFoundError",
        }
    ]


def test_baseline_uses_named_profile_override(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    harness = BaselineHarness(HarnessConfig(kind="codex", executable="codex"))
    models = model_policy().model_copy(
        update={
            "profiles": {
                "baseline": ModelProfile(
                    primary=ModelTarget(
                        model="runtime-baseline", thinking=ReasoningEffort.MEDIUM
                    )
                )
            }
        }
    )
    collector = DeepBaselineCollector(
        config,
        state,
        cast(GitHubProvider, SnapshotGitHub()),
        models,
    )

    collector.collect(repo, harness=harness, analyze=True, run_checks=False)

    assert harness.models == ["runtime-baseline", "runtime-baseline"]


def test_identical_baseline_reuses_validated_analysis_without_model_calls(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    harness = BaselineHarness(HarnessConfig(kind="codex", executable="codex"))
    collector = DeepBaselineCollector(
        config,
        state,
        cast(GitHubProvider, SnapshotGitHub()),
        model_policy(),
    )

    first, first_artifact = collector.collect(repo, harness=harness, analyze=True, run_checks=False)
    second, second_artifact = collector.collect(repo, harness=harness, analyze=True, run_checks=False)

    assert first["fingerprint"] == second["fingerprint"]
    assert first["analysis_reused"] is False
    assert second["analysis_reused"] is True
    assert first_artifact != second_artifact
    assert not any(path.startswith("state/") for path in second["source_inventory"]["all_files"])
    assert not any(path.startswith("artifacts/") for path in second["source_inventory"]["all_files"])
    assert len(harness.prompts) == 2
    assert state.usage_summary(repo.full_name)["direct_calls"]["calls"] == 2


def test_interrupted_baseline_reuses_partial_batches_on_retry(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    harness = FailOnCallHarness(HarnessConfig(kind="codex", executable="codex"), fail_on=2)
    collector = DeepBaselineCollector(
        config,
        state,
        cast(GitHubProvider, SnapshotGitHub()),
        model_policy(),
    )

    with pytest.raises(RuntimeError, match="synthetic synthesis interruption"):
        collector.collect(repo, harness=harness, analyze=True, run_checks=False)

    checkpoint_files = list((config.artifact_dir / "example__project" / "baselines").glob("*.checkpoint.json"))
    assert len(checkpoint_files) == 1

    harness.fail_on = 0
    _, artifact = collector.collect(repo, harness=harness, analyze=True, run_checks=False)

    assert artifact.is_file()
    assert [prompt.count("Synthesize") for prompt in harness.prompts] == [0, 1, 1]
    assert not checkpoint_files[0].exists()


def test_baseline_can_use_the_shared_model_invocation_boundary(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    harness = BaselineHarness(HarnessConfig(kind="codex", executable="codex"))
    calls: list[tuple[str, str]] = []

    def invoke(
        invoked_repo: RepoConfig,
        run_id: str,
        role: str,
        prompt: str,
        **kwargs: Any,
    ) -> Any:
        calls.append((invoked_repo.full_name, role))
        return harness.run(
            prompt=prompt,
            models=kwargs["models"],
            role=role,
            output_model=kwargs["output_model"],
            cwd=kwargs["cwd"],
            writable=kwargs["writable"],
        )

    collector = DeepBaselineCollector(
        config,
        state,
        cast(GitHubProvider, SnapshotGitHub()),
        model_policy(),
        model_invoker=invoke,
    )

    collector.collect(repo, analyze=True, run_checks=False)

    assert calls == [
        (repo.full_name, "baseline-part-1"),
        (repo.full_name, "baseline-synthesis"),
    ]
    assert state.usage_summary(repo.full_name)["direct_calls"]["calls"] == 0


def test_engine_invoker_records_each_baseline_batch_in_usage_ledger(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    harness = BaselineHarness(HarnessConfig(kind="codex", executable="codex"))

    class MemoryNotifier:
        def send(self, event: EventRecord) -> None:
            del event

    engine = ControlPlaneEngine(
        config,
        state,
        cast(GitHubProvider, SnapshotGitHub()),
        harness,
        MemoryNotifier(),
        model_policy(),
    )
    collector = DeepBaselineCollector(
        config,
        state,
        cast(GitHubProvider, SnapshotGitHub()),
        model_policy(),
        model_invoker=engine.run_model,
    )

    collector.collect(repo, analyze=True, run_checks=False)

    usage = state.usage_summary(repo.full_name)
    assert usage["direct_calls"]["calls"] == 2
    assert {group["role"] for group in usage["direct_groups"]} == {
        "baseline-part-1",
        "baseline-synthesis",
    }


def test_failed_baseline_analysis_records_usage_and_leaves_baseline_unready(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    config = app_config(tmp_path, repo)
    state = StateStore(config.state_dir / "state.db")
    harness = BaselineHarness(HarnessConfig(kind="codex", executable="codex"), fail=True)
    collector = DeepBaselineCollector(
        config,
        state,
        cast(GitHubProvider, SnapshotGitHub()),
        model_policy(),
    )

    with pytest.raises(RuntimeError, match="all baseline-part-1 model attempts failed"):
        collector.collect(repo, harness=harness, analyze=True, run_checks=False)

    assert state.baseline(repo.full_name) is None
    assert state.current_state(repo.full_name).value == "baseline_review"
    usage = state.usage_summary(repo.full_name)["direct_calls"]
    assert usage["calls"] == 1
    assert usage["unknown_calls"] == 1
