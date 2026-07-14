from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest
from pydantic import BaseModel

from captains_chair.command import CommandResult, CommandRunner
from captains_chair.harness import (
    CodexAdapter,
    HarnessAdapter,
    HarnessAdapterContractError,
    HarnessAdapterRegistry,
    HarnessExecutionError,
    OpenClawAdapter,
    build_harness,
    strict_output_schema,
)
from captains_chair.models import (
    HarnessConfig,
    HarnessInvocation,
    ModelTarget,
    ModelUsage,
    PlanDecision,
    RoleModels,
    WorkerResult,
)
from captains_chair.plugins import PluginDiscoveryError


class Output(BaseModel):
    value: str


OutputModel = TypeVar("OutputModel", bound=BaseModel)


class FallbackHarness(HarnessAdapter):
    def __init__(self, config: HarnessConfig, runner: CommandRunner | None = None) -> None:
        if runner is None:
            super().__init__(config)
        else:
            super().__init__(config, runner)
        self.session_ids: list[str] = []

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
        del prompt, role, output_model, cwd, writable
        self.session_ids.append(session_id)
        if model.model == "primary":
            raise RuntimeError("primary provider unavailable")
        return {"value": "ok"}


class ReportedModelHarness(HarnessAdapter):
    def __init__(self, config: HarnessConfig, reported_model: str) -> None:
        super().__init__(config)
        self.reported_model = reported_model

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
    ) -> HarnessInvocation:
        del prompt, model, role, output_model, cwd, writable, session_id
        return HarnessInvocation(
            payload={"value": "ok"},
            usage=ModelUsage(reported_model=self.reported_model),
        )


def test_fallback_preserves_original_failure() -> None:
    harness = FallbackHarness(HarnessConfig(kind="codex", executable="codex"))
    result = harness.run(
        prompt="test",
        models=RoleModels(
            primary=ModelTarget(model="primary"),
            fallbacks=(ModelTarget(model="fallback"),),
        ),
        role="coder",
        output_model=Output,
        cwd=Path.cwd(),
        writable=False,
    )
    assert result.resolved_model == "fallback"
    assert result.attempts[0].error == "primary provider unavailable"
    assert result.attempts[1].success
    assert harness.session_ids[0] != harness.session_ids[1]
    assert all(item.endswith(result.session_id) for item in harness.session_ids)
    assert [item.session_id for item in result.attempts] == harness.session_ids


def test_all_failed_harness_attempts_remain_available_for_usage_recording() -> None:
    harness = FallbackHarness(HarnessConfig(kind="codex", executable="codex"))

    with pytest.raises(HarnessExecutionError, match="all coder model attempts failed") as caught:
        harness.run(
            prompt="test",
            models=RoleModels(primary=ModelTarget(model="primary"), allow_fallback=False),
            role="coder",
            output_model=Output,
            cwd=Path.cwd(),
            writable=False,
        )

    assert len(caught.value.attempts) == 1
    assert caught.value.attempts[0].error == "primary provider unavailable"
    assert caught.value.attempts[0].prompt_bytes > 0


def test_harness_fails_closed_when_provider_reports_a_different_model() -> None:
    harness = ReportedModelHarness(
        HarnessConfig(kind="codex", executable="codex"), "unexpected/model"
    )

    with pytest.raises(HarnessExecutionError, match="model route mismatch"):
        harness.run(
            prompt="test",
            models=RoleModels(primary=ModelTarget(model="codex/expected")),
            role="coder",
            output_model=Output,
            cwd=Path.cwd(),
            writable=False,
        )


def test_harness_accepts_unqualified_provider_model_name() -> None:
    harness = ReportedModelHarness(
        HarnessConfig(kind="codex", executable="codex"), "gpt-5.3-codex"
    )

    result = harness.run(
        prompt="test",
        models=RoleModels(primary=ModelTarget(model="codex/gpt-5.3-codex")),
        role="coder",
        output_model=Output,
        cwd=Path.cwd(),
        writable=False,
    )

    assert result.resolved_model == "codex/gpt-5.3-codex"
    assert result.attempts[0].reported_model == "gpt-5.3-codex"


def test_openclaw_prompt_contains_exact_output_schema(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(list(command))
        return CommandResult(
            0,
            '{"result":{"payloads":[{"text":"{\\"value\\":\\"ok\\"}"}]}}',
            "",
        )

    adapter = OpenClawAdapter(
        HarnessConfig(kind="openclaw", executable="openclaw", timeout_seconds=30), runner
    )
    result = adapter.run(
        prompt="Return structured output.",
        models=RoleModels(primary=ModelTarget(model="test")),
        role="test",
        output_model=Output,
        cwd=tmp_path,
        writable=False,
    )
    message = commands[0][commands[0].index("--message") + 1]
    assert '"required":["value"]' in message
    assert result.output == {"value": "ok"}


def test_openclaw_accepts_harmless_stdout_prefix_and_suffix(tmp_path: Path) -> None:
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
            'migration warning\n{"result":{"payloads":[{"text":"{\\"value\\":\\"ok\\"}"}]}}\ntrailing note',
            "",
        )

    adapter = OpenClawAdapter(
        HarnessConfig(kind="openclaw", executable="openclaw", timeout_seconds=30), runner
    )

    result = adapter.run(
        prompt="Return structured output.",
        models=RoleModels(primary=ModelTarget(model="test")),
        role="test",
        output_model=Output,
        cwd=tmp_path,
        writable=False,
    )

    assert result.output == {"value": "ok"}


def test_openclaw_fails_closed_on_reported_model_mismatch(tmp_path: Path) -> None:
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
            '{"model":"gpt-5.6-sol","usage":{"input_tokens":10,"output_tokens":2},'
            '"result":{"payloads":[{"text":"{\\"value\\":\\"ok\\"}"}]}}',
            "",
        )

    adapter = OpenClawAdapter(
        HarnessConfig(kind="openclaw", executable="openclaw", timeout_seconds=30), runner
    )

    with pytest.raises(HarnessExecutionError, match="model route mismatch") as caught:
        adapter.run(
            prompt="Return structured output.",
            models=RoleModels(primary=ModelTarget(model="codex/gpt-5.5")),
            role="planner",
            output_model=Output,
            cwd=tmp_path,
            writable=False,
        )

    failed_attempt = caught.value.attempts[0]
    assert failed_attempt.input_tokens == 10
    assert failed_attempt.output_tokens == 2
    assert failed_attempt.response_bytes > 0


@pytest.mark.parametrize(
    ("writable", "expected_sandbox"),
    ((False, "read-only"), (True, "workspace-write")),
)
def test_codex_adapter_uses_structured_output_and_requested_sandbox(
    tmp_path: Path,
    writable: bool,
    expected_sandbox: str,
) -> None:
    commands: list[list[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        args = list(command)
        commands.append(args)
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text('{"value":"ok"}', encoding="utf-8")
        return CommandResult(0, '{"type":"turn.completed"}', "")

    adapter = CodexAdapter(
        HarnessConfig(kind="codex", executable="codex", timeout_seconds=30), runner
    )
    result = adapter.run(
        prompt="Return structured output.",
        models=RoleModels(primary=ModelTarget(model="test")),
        role="coder" if writable else "reviewer",
        output_model=Output,
        cwd=tmp_path,
        writable=writable,
    )

    command = commands[0]
    assert command[command.index("--sandbox") + 1] == expected_sandbox
    assert "--output-schema" in command
    assert result.output == {"value": "ok"}


def test_strict_output_schema_requires_all_worker_fields() -> None:
    schema = strict_output_schema(WorkerResult)

    assert set(schema["required"]) == {"summary", "changed_files", "checks_run", "blocked", "blocker"}
    assert schema["additionalProperties"] is False


def test_strict_output_schema_removes_defaults_from_refs() -> None:
    schema = strict_output_schema(PlanDecision)

    def assert_ref_nodes_are_clean(value: Any) -> None:
        if isinstance(value, dict):
            object_value = cast(dict[str, Any], value)
            if "$ref" in object_value:
                assert set(object_value) == {"$ref"}
            for child in object_value.values():
                assert_ref_nodes_are_clean(child)
        elif isinstance(value, list):
            list_value = cast(list[Any], value)
            for child in list_value:
                assert_ref_nodes_are_clean(child)

    assert_ref_nodes_are_clean(schema)


def test_openclaw_refuses_writable_execution(tmp_path: Path) -> None:
    adapter = OpenClawAdapter(HarnessConfig(kind="openclaw", executable="openclaw", timeout_seconds=30))
    with pytest.raises(HarnessExecutionError, match="workspace sandbox"):
        adapter.invoke(
            prompt="edit",
            model=ModelTarget(model="test"),
            role="coder",
            output_model=Output,
            cwd=tmp_path,
            writable=True,
            session_id="session",
        )


def test_hermes_harness_shape_fails_until_adapter_is_installed() -> None:
    with pytest.raises(ValueError, match="no installed adapter"):
        build_harness(HarnessConfig(kind="hermes", executable="hermes"))


def test_future_harness_can_register_without_changing_harness_core() -> None:
    registry = HarnessAdapterRegistry()

    def build_hermes(config: HarnessConfig, runner: CommandRunner) -> HarnessAdapter:
        return FallbackHarness(config, runner)

    registry.register("hermes", build_hermes)
    adapter = build_harness(
        HarnessConfig(kind="hermes", executable="hermes"),
        registry=registry,
    )

    assert isinstance(adapter, FallbackHarness)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("hermes", build_hermes)


def test_external_harness_kind_can_register_without_core_model_changes() -> None:
    registry = HarnessAdapterRegistry()

    def build_external(config: HarnessConfig, runner: CommandRunner) -> HarnessAdapter:
        return FallbackHarness(config, runner)

    registry.register("hermes-next", build_external)
    adapter = build_harness(
        HarnessConfig(
            kind="hermes-next",
            executable="hermes-next",
            settings={"session_kind": "disposable"},
        ),
        registry=registry,
    )

    assert isinstance(adapter, FallbackHarness)
    assert adapter.config.settings == {"session_kind": "disposable"}


def test_future_harness_can_discover_a_packaged_entrypoint() -> None:
    registry = HarnessAdapterRegistry()

    def build_hermes(config: HarnessConfig, runner: CommandRunner) -> HarnessAdapter:
        return FallbackHarness(config, runner)

    class EntryPoint:
        name = "hermes"
        group = "captains_chair.harness_adapters"

        def load(self) -> Any:
            def register(target: HarnessAdapterRegistry) -> None:
                target.register("hermes", build_hermes)

            return register

    assert registry.discover(provider=lambda: [EntryPoint()]) == ("hermes",)
    assert isinstance(
        build_harness(HarnessConfig(kind="hermes", executable="hermes"), registry=registry),
        FallbackHarness,
    )


def test_future_harness_plugin_fails_at_construction_when_contract_is_invalid() -> None:
    registry = HarnessAdapterRegistry()

    def build_invalid(config: HarnessConfig, runner: CommandRunner) -> HarnessAdapter:
        del config, runner
        return cast(HarnessAdapter, object())

    registry.register("hermes", build_invalid)

    with pytest.raises(HarnessAdapterContractError, match="must subclass HarnessAdapter"):
        build_harness(HarnessConfig(kind="hermes", executable="hermes"), registry=registry)


def test_harness_plugin_discovery_rejects_noncallable_registrar() -> None:
    registry = HarnessAdapterRegistry()

    class EntryPoint:
        name = "invalid"
        group = "captains_chair.harness_adapters"

        def load(self) -> str:
            return "not callable"

    with pytest.raises(PluginDiscoveryError, match="did not expose a callable registrar"):
        registry.discover(provider=lambda: [EntryPoint()])
