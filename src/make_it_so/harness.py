from __future__ import annotations

import json
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar, cast

from pydantic import BaseModel

from make_it_so.command import CommandRunner, run_command
from make_it_so.json_tools import decode_first_json
from make_it_so.model_policy import models_match
from make_it_so.models import (
    HarnessConfig,
    HarnessInvocation,
    HarnessResult,
    ModelAttempt,
    ModelTarget,
    ModelUsage,
    RoleModels,
)
from make_it_so.plugins import EntryPointProvider, load_entrypoint_plugins

OutputModel = TypeVar("OutputModel", bound=BaseModel)


class HarnessExecutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        role: str | None = None,
        attempts: tuple[ModelAttempt, ...] = (),
        session_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.role = role
        self.attempts = attempts
        self.session_id = session_id


class HarnessAdapterContractError(RuntimeError):
    """Raised when an installed harness does not implement the adapter contract."""


class HarnessAdapter(ABC):
    def __init__(self, config: HarnessConfig, runner: CommandRunner = run_command) -> None:
        self.config = config
        self.runner = runner

    @abstractmethod
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
    ) -> dict[str, Any] | HarnessInvocation:
        raise NotImplementedError

    def run(
        self,
        *,
        prompt: str,
        models: RoleModels,
        role: str,
        output_model: type[OutputModel],
        cwd: Path,
        writable: bool,
    ) -> HarnessResult:
        targets = (models.primary, *models.fallbacks) if models.allow_fallback else (models.primary,)
        session_id = str(uuid.uuid4())
        attempts: list[ModelAttempt] = []
        for attempt_index, target in enumerate(targets):
            started = time.monotonic()
            reported_model: str | None = None
            attempt_usage = ModelUsage(prompt_bytes=len(prompt.encode("utf-8")))
            # Keep fallback attempts in fresh provider sessions while preserving
            # the root UUID for usage correlation and one-call accounting.
            attempt_session_id = f"attempt-{attempt_index}:{session_id}"
            try:
                invocation = self.invoke(
                    prompt=prompt,
                    model=target,
                    role=role,
                    output_model=output_model,
                    cwd=cwd,
                    writable=writable,
                    session_id=attempt_session_id,
                )
                if isinstance(invocation, HarnessInvocation):
                    payload = invocation.payload
                    usage = invocation.usage
                else:
                    payload = invocation
                    usage = ModelUsage()
                attempt_usage = ModelUsage(
                    reported_model=usage.reported_model,
                    input_tokens=usage.input_tokens,
                    cached_input_tokens=usage.cached_input_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    reasoning_tokens=usage.reasoning_tokens,
                    output_tokens=usage.output_tokens,
                    total_tokens=usage.total_tokens,
                    prompt_bytes=len(prompt.encode("utf-8")),
                    response_bytes=usage.response_bytes or 0,
                    source=usage.source,
                )
                reported_model = attempt_usage.reported_model
                if reported_model and not models_match(target.model, reported_model):
                    raise HarnessExecutionError(
                        f"model route mismatch: requested {target.model}, provider reported {reported_model}"
                    )
                parsed = output_model.model_validate(payload)
                attempts.append(
                    ModelAttempt(
                        model=target.model,
                        reported_model=reported_model,
                        agent=target.agent,
                        session_id=attempt_session_id,
                        success=True,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        input_tokens=attempt_usage.input_tokens,
                        cached_input_tokens=attempt_usage.cached_input_tokens,
                        cache_write_tokens=attempt_usage.cache_write_tokens,
                        reasoning_tokens=attempt_usage.reasoning_tokens,
                        output_tokens=attempt_usage.output_tokens,
                        total_tokens=attempt_usage.total_tokens,
                        prompt_bytes=attempt_usage.prompt_bytes,
                        response_bytes=attempt_usage.response_bytes,
                        usage_source=attempt_usage.source,
                    )
                )
                return HarnessResult(
                    role=role,
                    output=parsed.model_dump(mode="json"),
                    attempts=tuple(attempts),
                    resolved_model=target.model,
                    session_id=session_id,
                )
            except Exception as exc:
                attempts.append(
                    ModelAttempt(
                        model=target.model,
                        reported_model=reported_model,
                        agent=target.agent,
                        session_id=attempt_session_id,
                        success=False,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        error=str(exc)[:2000],
                        input_tokens=attempt_usage.input_tokens,
                        cached_input_tokens=attempt_usage.cached_input_tokens,
                        cache_write_tokens=attempt_usage.cache_write_tokens,
                        reasoning_tokens=attempt_usage.reasoning_tokens,
                        output_tokens=attempt_usage.output_tokens,
                        total_tokens=attempt_usage.total_tokens,
                        prompt_bytes=attempt_usage.prompt_bytes,
                        response_bytes=attempt_usage.response_bytes,
                        usage_source=attempt_usage.source,
                    )
                )
        detail = "; ".join(f"{item.model}: {item.error}" for item in attempts)
        raise HarnessExecutionError(
            f"all {role} model attempts failed: {detail}",
            role=role,
            attempts=tuple(attempts),
            session_id=session_id,
        )


class OpenClawAdapter(HarnessAdapter):
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
    ) -> dict[str, Any] | HarnessInvocation:
        del cwd
        if writable:
            raise HarnessExecutionError(
                "OpenClaw writable execution is disabled because it cannot enforce a workspace sandbox; use the Codex adapter"
            )
        schema = json.dumps(strict_output_schema(output_model), separators=(",", ":"))
        structured_prompt = (
            prompt
            + "\n\nYour response MUST be one JSON object that validates against this exact JSON Schema. "
            "Use only these field names; do not add wrapper objects or commentary:\n" + schema
        )
        if len(structured_prompt.encode("utf-8")) > 110_000:
            raise HarnessExecutionError(
                "OpenClaw prompt exceeds the safe command-argument limit; split it into evidence batches"
            )
        agent = model.agent or self.config.default_agent or "codex-harness"
        command = [
            self.config.executable,
            "agent",
            "--agent",
            agent,
            "--model",
            model.model,
            "--thinking",
            model.thinking,
            "--session-key",
            f"agent:{agent}:make_it_so:{role}:{session_id}",
            "--message",
            structured_prompt,
            "--json",
            "--timeout",
            str(self.config.timeout_seconds),
        ]
        result = self.runner(command, timeout=self.config.timeout_seconds + 60)
        if result.returncode:
            raise HarnessExecutionError((result.stderr or result.stdout).strip()[:3000])
        try:
            decoded = decode_first_json(result.stdout)
            if not isinstance(decoded, dict):
                raise HarnessExecutionError("OpenClaw response was not a JSON object")
            envelope = cast(dict[str, Any], decoded)
            result_value = envelope.get("result")
            result_object = cast(dict[str, Any], result_value) if isinstance(result_value, dict) else {}
            payloads_value = result_object.get("payloads")
            payload_list = cast(list[Any], payloads_value) if isinstance(payloads_value, list) else []
            text = (
                cast(dict[str, Any], payload_list[0]).get("text")
                if payload_list and isinstance(payload_list[0], dict)
                else envelope.get("summary")
            )
            if not isinstance(text, str):
                raise HarnessExecutionError("OpenClaw response did not contain text")
            payload = cast(dict[str, Any], json.loads(_extract_json(text)))
            usage = _find_usage(envelope, source="openclaw") or ModelUsage(source="unreported")
            usage = _usage_with_response_bytes(usage, len(text.encode("utf-8")))
            return HarnessInvocation(payload=payload, usage=usage)
        except (ValueError, TypeError, AttributeError) as exc:
            raise HarnessExecutionError(f"invalid OpenClaw structured response: {exc}") from exc


class CodexAdapter(HarnessAdapter):
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
    ) -> dict[str, Any] | HarnessInvocation:
        del role, session_id
        with tempfile.TemporaryDirectory(prefix="make_it_so-codex-") as temp_dir:
            schema_path = Path(temp_dir) / "schema.json"
            output_path = Path(temp_dir) / "final.json"
            schema_path.write_text(json.dumps(strict_output_schema(output_model), indent=2), encoding="utf-8")
            command = [
                self.config.executable,
                "exec",
                "--json",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "--sandbox",
                "workspace-write" if writable else "read-only",
                "--cd",
                str(cwd),
                "--model",
                model.model,
                "-",
            ]
            result = self.runner(
                command,
                cwd=cwd,
                input_text=prompt,
                timeout=self.config.timeout_seconds + 60,
            )
            if result.returncode:
                raise HarnessExecutionError((result.stderr or result.stdout).strip()[:3000])
            if not output_path.is_file():
                raise HarnessExecutionError("Codex did not write the structured final response")
            try:
                payload = cast(
                    dict[str, Any],
                    json.loads(_extract_json(output_path.read_text(encoding="utf-8"))),
                )
                usage = _find_usage_from_lines(result.stdout, source="codex") or ModelUsage(
                    source="unreported"
                )
                usage = _usage_with_response_bytes(usage, output_path.stat().st_size)
                return HarnessInvocation(payload=payload, usage=usage)
            except json.JSONDecodeError as exc:
                raise HarnessExecutionError(f"invalid Codex structured response: {exc}") from exc


def _extract_json(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        last_fence = stripped.rfind("```")
        if first_newline >= 0 and last_fence > first_newline:
            stripped = stripped[first_newline + 1 : last_fence].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise HarnessExecutionError("response did not contain a JSON object")
    return stripped[start : end + 1]


def _usage_with_response_bytes(usage: ModelUsage, response_bytes: int) -> ModelUsage:
    return ModelUsage(
        reported_model=usage.reported_model,
        input_tokens=usage.input_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        prompt_bytes=usage.prompt_bytes or 0,
        response_bytes=response_bytes,
        source=usage.source,
    )


_USAGE_KEYS = {
    "input_tokens": ("input_tokens", "inputTokens", "prompt_tokens", "promptTokens"),
    "cached_input_tokens": (
        "cached_input_tokens",
        "cachedInputTokens",
        "cache_read_input_tokens",
        "cacheReadInputTokens",
    ),
    "cache_write_tokens": (
        "cache_write_tokens",
        "cacheWriteTokens",
        "cache_creation_input_tokens",
        "cacheCreationInputTokens",
    ),
    "reasoning_tokens": (
        "reasoning_tokens",
        "reasoningTokens",
        "reasoning_token_count",
        "reasoningTokenCount",
    ),
    "output_tokens": ("output_tokens", "outputTokens", "completion_tokens", "completionTokens"),
    "total_tokens": ("total_tokens", "totalTokens"),
}


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _find_usage(
    value: Any,
    *,
    source: str,
    inherited_model: str | None = None,
) -> ModelUsage | None:
    if isinstance(value, Mapping):
        mapped = cast(Mapping[str, Any], value)
        model_value = mapped.get("model")
        reported_model = str(model_value).strip() if model_value else inherited_model
        values: dict[str, int | None] = {}
        for normalized, aliases in _USAGE_KEYS.items():
            for alias in aliases:
                if alias in mapped:
                    values[normalized] = _nonnegative_int(mapped[alias])
                    break
        if values.get("reasoning_tokens") is None:
            for details_key in ("output_tokens_details", "outputTokensDetails"):
                details = mapped.get(details_key)
                if isinstance(details, Mapping):
                    details_mapping = cast(Mapping[str, Any], details)
                    for alias in _USAGE_KEYS["reasoning_tokens"]:
                        if alias in details_mapping:
                            values["reasoning_tokens"] = _nonnegative_int(details_mapping[alias])
                            break
                    if values.get("reasoning_tokens") is not None:
                        break
        if any(item is not None for item in values.values()):
            return ModelUsage(
                reported_model=reported_model,
                input_tokens=values.get("input_tokens"),
                cached_input_tokens=values.get("cached_input_tokens"),
                cache_write_tokens=values.get("cache_write_tokens"),
                reasoning_tokens=values.get("reasoning_tokens"),
                output_tokens=values.get("output_tokens"),
                total_tokens=values.get("total_tokens"),
                prompt_bytes=0,
                response_bytes=0,
                source=source,
            )
        for child in mapped.values():
            found = _find_usage(child, source=source, inherited_model=reported_model)
            if found is not None:
                return found
        if reported_model:
            return ModelUsage(reported_model=reported_model, source=source)
    elif isinstance(value, list):
        children = cast(list[Any], value)
        candidates = [
            _find_usage(child, source=source, inherited_model=inherited_model)
            for child in children
        ]
        candidates = [item for item in candidates if item is not None]
        if candidates:
            return max(candidates, key=lambda item: item.total_tokens or 0)
        if inherited_model:
            return ModelUsage(reported_model=inherited_model, source=source)
    return None


def _find_usage_from_lines(text: str, *, source: str) -> ModelUsage | None:
    candidates: list[ModelUsage] = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        found = _find_usage(value, source=source)
        if found is not None:
            candidates.append(found)
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.total_tokens or 0)


def strict_output_schema(output_model: type[BaseModel]) -> dict[str, Any]:
    schema = output_model.model_json_schema()
    _require_all_properties(schema)
    return schema


def _require_all_properties(value: Any) -> None:
    if isinstance(value, dict):
        object_value = cast(dict[str, Any], value)
        # Codex's structured-output validator requires $ref nodes to contain
        # only the reference. Pydantic adds field defaults beside enum refs.
        if "$ref" in object_value:
            for key in tuple(object_value):
                if key != "$ref":
                    del object_value[key]
        properties = object_value.get("properties")
        if isinstance(properties, dict):
            property_map = cast(dict[str, Any], properties)
            object_value["required"] = sorted(property_map)
            object_value.setdefault("additionalProperties", False)
        for child in object_value.values():
            _require_all_properties(child)
    elif isinstance(value, list):
        list_value = cast(list[Any], value)
        for child in list_value:
            _require_all_properties(child)


HarnessAdapterBuilder = Callable[[HarnessConfig, CommandRunner], object]


class HarnessAdapterRegistry:
    """Explicit model-harness registry for runtime-specific execution adapters."""

    def __init__(self) -> None:
        self._builders: dict[str, HarnessAdapterBuilder] = {}
        self._loaded_plugins: set[str] = set()

    def register(self, kind: str, builder: HarnessAdapterBuilder, *, replace: bool = False) -> None:
        normalized = kind.strip()
        if not normalized:
            raise ValueError("harness adapter kind must not be empty")
        if normalized in self._builders and not replace:
            raise ValueError(f"harness adapter is already registered: {normalized}")
        self._builders[normalized] = builder

    def discover(self, *, provider: EntryPointProvider | None = None) -> tuple[str, ...]:
        """Discover packaged harness adapters without coupling core execution to them."""
        if provider is None:
            return load_entrypoint_plugins(
                self,
                group="make_it_so.harness_adapters",
                loaded=self._loaded_plugins,
            )
        return load_entrypoint_plugins(
            self,
            group="make_it_so.harness_adapters",
            provider=provider,
            loaded=self._loaded_plugins,
        )

    def build(self, config: HarnessConfig, runner: CommandRunner) -> HarnessAdapter:
        builder = self._builders.get(config.kind)
        if builder is None:
            raise ValueError(
                f"harness kind {config.kind} has no installed adapter; "
                "register a HarnessAdapter with HarnessAdapterRegistry"
            )
        return validate_harness_adapter(builder(config, runner))


def validate_harness_adapter(adapter: object) -> HarnessAdapter:
    """Validate a runtime harness before any model invocation can begin."""
    if not isinstance(adapter, HarnessAdapter):
        raise HarnessAdapterContractError(
            "harness adapter must subclass HarnessAdapter and implement invoke"
        )
    return adapter


def _build_openclaw_harness(config: HarnessConfig, runner: CommandRunner) -> HarnessAdapter:
    if config.kind != "openclaw":
        raise TypeError("openclaw harness adapter requires an openclaw HarnessConfig")
    return OpenClawAdapter(config, runner)


def _build_codex_harness(config: HarnessConfig, runner: CommandRunner) -> HarnessAdapter:
    if config.kind != "codex":
        raise TypeError("codex harness adapter requires a codex HarnessConfig")
    return CodexAdapter(config, runner)


DEFAULT_HARNESS_ADAPTERS = HarnessAdapterRegistry()
DEFAULT_HARNESS_ADAPTERS.register("openclaw", _build_openclaw_harness)
DEFAULT_HARNESS_ADAPTERS.register("codex", _build_codex_harness)


def register_harness_adapter(
    kind: str,
    builder: HarnessAdapterBuilder,
    *,
    replace: bool = False,
) -> None:
    """Register a production harness adapter in the process-wide default registry."""
    DEFAULT_HARNESS_ADAPTERS.register(kind, builder, replace=replace)


def build_harness(
    config: HarnessConfig,
    runner: CommandRunner = run_command,
    *,
    registry: HarnessAdapterRegistry = DEFAULT_HARNESS_ADAPTERS,
) -> HarnessAdapter:
    if registry is DEFAULT_HARNESS_ADAPTERS:
        registry.discover()
    return registry.build(config, runner)
