from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest
from pydantic import BaseModel

import make_it_so.harness as harness_module
from make_it_so.command import CommandResult, CommandRunner
from make_it_so.harness import (
    CodexAdapter,
    HarnessAdapter,
    HarnessAdapterContractError,
    HarnessAdapterRegistry,
    HarnessExecutionError,
    OpenClawAdapter,
    build_harness,
    strict_output_schema,
)
from make_it_so.models import (
    HarnessConfig,
    HarnessInvocation,
    ModelTarget,
    ModelUsage,
    PlanDecision,
    RoleModels,
    WorkerResult,
)
from make_it_so.plugins import PluginDiscoveryError


class Output(BaseModel):
    value: str


def empty_runner(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: int = 60,
) -> CommandResult:
    del command, cwd, input_text, timeout
    return CommandResult(0, "", "")


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
        HarnessConfig(kind="codex", executable="codex"), "gpt-5.3-codex-spark"
    )

    result = harness.run(
        prompt="test",
        models=RoleModels(primary=ModelTarget(model="codex/gpt-5.3-codex-spark")),
        role="coder",
        output_model=Output,
        cwd=Path.cwd(),
        writable=False,
    )

    assert result.resolved_model == "codex/gpt-5.3-codex-spark"
    assert result.attempts[0].reported_model == "gpt-5.3-codex-spark"


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


@pytest.mark.parametrize(
    ("stdout", "message"),
    (
        ('{"result":{"payloads":[]}}', "did not contain text"),
        ('{"result":{"payloads":[{"text":"not-json"}]}}', "did not contain a JSON object"),
        ('{"result":{"payloads":[{"text":"{invalid}"}]}}', "invalid OpenClaw structured response"),
        ('[1, 2]', "not a JSON object"),
    ),
)
def test_openclaw_rejects_malformed_structured_responses(
    tmp_path: Path,
    stdout: str,
    message: str,
) -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return CommandResult(0, stdout, "")

    adapter = OpenClawAdapter(HarnessConfig(kind="openclaw", executable="openclaw"), runner)
    with pytest.raises(HarnessExecutionError, match=message):
        adapter.run(
            prompt="Return structured output.",
            models=RoleModels(primary=ModelTarget(model="test")),
            role="reviewer",
            output_model=Output,
            cwd=tmp_path,
            writable=False,
        )


def test_openclaw_rejects_command_failures_and_oversized_prompts(tmp_path: Path) -> None:
    def failed_runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return CommandResult(1, "", "agent failed")

    adapter = OpenClawAdapter(HarnessConfig(kind="openclaw", executable="openclaw"), failed_runner)
    with pytest.raises(HarnessExecutionError, match="agent failed"):
        adapter.invoke(
            prompt="short",
            model=ModelTarget(model="test"),
            role="reviewer",
            output_model=Output,
            cwd=tmp_path,
            writable=False,
            session_id="session",
        )

    adapter = OpenClawAdapter(HarnessConfig(kind="openclaw", executable="openclaw"), failed_runner)
    with pytest.raises(HarnessExecutionError, match="command-argument limit"):
        adapter.invoke(
            prompt="x" * 111_000,
            model=ModelTarget(model="test"),
            role="reviewer",
            output_model=Output,
            cwd=tmp_path,
            writable=False,
            session_id="session",
        )


def test_codex_rejects_failed_missing_and_invalid_responses(tmp_path: Path) -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, timeout
        args = list(command)
        output_path = Path(args[args.index("--output-last-message") + 1])
        if input_text == "failed":
            return CommandResult(1, "", "codex failed")
        if input_text == "missing":
            return CommandResult(0, "", "")
        output_path.write_text("{invalid}" if input_text == "json-invalid" else "not-json", encoding="utf-8")
        return CommandResult(0, "", "")

    adapter = CodexAdapter(HarnessConfig(kind="codex", executable="codex"), runner)
    common: dict[str, Any] = {
        "models": RoleModels(primary=ModelTarget(model="test")),
        "role": "coder",
        "output_model": Output,
        "cwd": tmp_path,
        "writable": False,
    }
    with pytest.raises(HarnessExecutionError, match="codex failed"):
        adapter.run(**common, prompt="failed")
    with pytest.raises(HarnessExecutionError, match="did not write"):
        adapter.run(**common, prompt="missing")
    with pytest.raises(HarnessExecutionError, match="response did not contain"):
        adapter.run(**common, prompt="invalid")
    with pytest.raises(HarnessExecutionError, match="invalid Codex structured response"):
        adapter.run(**common, prompt="json-invalid")


def test_harness_usage_and_json_helpers_cover_nested_provider_shapes() -> None:
    assert harness_module._extract_json("```json\n{\"value\": \"ok\"}\n```") == '{"value": "ok"}'  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(HarnessExecutionError, match="did not contain"):
        harness_module._extract_json("plain text")  # pyright: ignore[reportPrivateUsage]
    assert harness_module._nonnegative_int(True) is None  # pyright: ignore[reportPrivateUsage]
    assert harness_module._nonnegative_int(2.0) == 2  # pyright: ignore[reportPrivateUsage]
    assert harness_module._nonnegative_int(-1) is None  # pyright: ignore[reportPrivateUsage]

    nested = harness_module._find_usage(  # pyright: ignore[reportPrivateUsage]
        {
            "model": "provider/model",
            "output_tokens_details": {"reasoning_tokens": 4},
            "inputTokens": 10,
            "outputTokens": 6,
            "totalTokens": 16,
        },
        source="test",
    )
    assert nested is not None and nested.reasoning_tokens == 4
    candidates = harness_module._find_usage_from_lines(  # pyright: ignore[reportPrivateUsage]
        "not-json\n{\"usage\":{\"total_tokens\":3}}\n{\"usage\":{\"total_tokens\":9}}",
        source="test",
    )
    assert candidates is not None and candidates.total_tokens == 9
    assert harness_module._find_usage([], source="test", inherited_model="fallback") is not None  # pyright: ignore[reportPrivateUsage]
    assert harness_module._find_usage({}, source="test") is None  # pyright: ignore[reportPrivateUsage]


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


def test_harness_registry_rejects_empty_kind_and_wrong_builtin_config() -> None:
    registry = HarnessAdapterRegistry()

    def builder(config: HarnessConfig, runner: CommandRunner) -> HarnessAdapter:
        return FallbackHarness(config, runner)

    with pytest.raises(ValueError, match="must not be empty"):
        registry.register(" ", builder)
    registry.register("replaceable", builder)
    registry.register("replaceable", builder, replace=True)
    with pytest.raises(TypeError, match="openclaw HarnessConfig"):
        harness_module._build_openclaw_harness(  # pyright: ignore[reportPrivateUsage]
            HarnessConfig(kind="codex", executable="codex"), empty_runner
        )
    with pytest.raises(TypeError, match="codex HarnessConfig"):
        harness_module._build_codex_harness(  # pyright: ignore[reportPrivateUsage]
            HarnessConfig(kind="openclaw", executable="openclaw"), empty_runner
        )


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
        group = "make_it_so.harness_adapters"

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
        group = "make_it_so.harness_adapters"

        def load(self) -> str:
            return "not callable"

    with pytest.raises(PluginDiscoveryError, match="did not expose a callable registrar"):
        registry.discover(provider=lambda: [EntryPoint()])
