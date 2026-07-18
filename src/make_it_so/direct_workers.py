from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Literal, Protocol, cast, runtime_checkable

from pydantic import Field, PrivateAttr, model_validator

from make_it_so.command import CommandRunner, run_command
from make_it_so.harness import strict_output_schema
from make_it_so.json_tools import decode_first_json
from make_it_so.models import ModelUsage, StrictModel
from make_it_so.orchestration import QueueCard


class WorkerExecutionError(RuntimeError):
    """Raised when a runtime cannot return a trustworthy worker outcome."""


class WorkerExecutionResult(StrictModel):
    status: Literal["completed", "blocked"]
    summary: str = Field(min_length=1)
    proof: tuple[dict[str, Any], ...] = ()
    reason: str | None = None
    _telemetry: WorkerExecutionTelemetry | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def validate_outcome(self) -> WorkerExecutionResult:
        if self.status == "completed" and not self.proof:
            raise ValueError("completed worker execution requires structured proof")
        if self.status == "blocked" and not (self.reason or "").strip():
            raise ValueError("blocked worker execution requires a reason")
        return self

    @property
    def telemetry(self) -> WorkerExecutionTelemetry | None:
        return self._telemetry

    def attach_telemetry(self, telemetry: WorkerExecutionTelemetry) -> None:
        self._telemetry = telemetry


class WorkerExecutionTelemetry(StrictModel):
    runtime: Literal["openclaw", "codex"]
    requested_model: str
    attempt_id: str
    duration_ms: int = Field(ge=0)
    usage: ModelUsage = Field(default_factory=ModelUsage)


@runtime_checkable
class WorkerExecutorAdapter(Protocol):
    """Launch one fresh host worker for a claimed direct-orchestrator card."""

    def execute(
        self,
        card: QueueCard,
        *,
        attempt_id: str,
        workspace: Path,
        model: str,
        timeout_seconds: int,
    ) -> WorkerExecutionResult: ...


class CommandWorkerExecutor:
    """Built-in OpenClaw and Codex worker process adapters.

    This boundary owns only process invocation and structured outcome parsing. The
    direct orchestrator remains authoritative for claims, leases, retries, and proof.
    """

    def __init__(
        self,
        runtime: Literal["openclaw", "codex"],
        executable: str,
        runner: CommandRunner = run_command,
    ) -> None:
        self.runtime = runtime
        self.executable = executable
        self.runner = runner

    def execute(
        self,
        card: QueueCard,
        *,
        attempt_id: str,
        workspace: Path,
        model: str,
        timeout_seconds: int,
    ) -> WorkerExecutionResult:
        workspace = workspace.resolve()
        prompt = _worker_prompt(card, attempt_id=attempt_id, workspace=workspace)
        if self.runtime == "codex":
            return self._run_codex(
                prompt,
                attempt_id=attempt_id,
                workspace=workspace,
                model=model,
                timeout_seconds=timeout_seconds,
            )
        return self._run_openclaw(
            card,
            prompt,
            attempt_id=attempt_id,
            model=model,
            timeout_seconds=timeout_seconds,
        )

    def _run_codex(
        self,
        prompt: str,
        *,
        attempt_id: str,
        workspace: Path,
        model: str,
        timeout_seconds: int,
    ) -> WorkerExecutionResult:
        with tempfile.TemporaryDirectory(prefix="make-it-so-worker-") as temp_dir:
            schema_path = Path(temp_dir) / "schema.json"
            output_path = Path(temp_dir) / "result.json"
            schema_path.write_text(
                json.dumps(strict_output_schema(WorkerExecutionResult), indent=2),
                encoding="utf-8",
            )
            command = [
                self.executable,
                "exec",
                "--json",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "--sandbox",
                "workspace-write",
                "--cd",
                str(workspace),
                "--model",
                _runtime_model("codex", model),
                "-",
            ]
            started = time.monotonic()
            try:
                result = self.runner(
                    command,
                    cwd=workspace,
                    input_text=prompt,
                    timeout=timeout_seconds + 60,
                )
            except (OSError, TimeoutError, subprocess.SubprocessError) as exc:
                raise WorkerExecutionError(f"Codex worker process failed: {exc}") from exc
            if result.returncode:
                raise WorkerExecutionError((result.stderr or result.stdout).strip()[:3000])
            if not output_path.is_file():
                raise WorkerExecutionError("Codex worker did not write its structured outcome")
            output_text = output_path.read_text(encoding="utf-8")
            outcome = _parse_result(output_text)
            outcome.attach_telemetry(
                WorkerExecutionTelemetry(
                    runtime="codex",
                    requested_model=_runtime_model("codex", model),
                    attempt_id=attempt_id,
                    duration_ms=max(0, round((time.monotonic() - started) * 1000)),
                    usage=_codex_usage(
                        result.stdout,
                        prompt_bytes=len(prompt.encode("utf-8")),
                        response_bytes=len(output_text.encode("utf-8")),
                    ),
                )
            )
            return outcome

    def _run_openclaw(
        self,
        card: QueueCard,
        prompt: str,
        *,
        attempt_id: str,
        model: str,
        timeout_seconds: int,
    ) -> WorkerExecutionResult:
        if not card.agent_id:
            raise WorkerExecutionError("OpenClaw direct worker requires an assigned agent id")
        command = [
            self.executable,
            "agent",
            "--agent",
            card.agent_id,
            "--model",
            _runtime_model("openclaw", model),
            "--session-key",
            f"agent:{card.agent_id}:make-it-so:worker:{card.id}:{attempt_id}",
            "--message",
            prompt,
            "--json",
            "--timeout",
            str(timeout_seconds),
        ]
        try:
            result = self.runner(command, timeout=timeout_seconds + 60)
        except (OSError, TimeoutError, subprocess.SubprocessError) as exc:
            raise WorkerExecutionError(f"OpenClaw worker process failed: {exc}") from exc
        if result.returncode:
            raise WorkerExecutionError((result.stderr or result.stdout).strip()[:3000])
        try:
            envelope = decode_first_json(result.stdout)
            if not isinstance(envelope, dict):
                raise WorkerExecutionError("OpenClaw worker response was not an object")
            result_value = cast(dict[str, Any], envelope).get("result")
            result_object = cast(dict[str, Any], result_value) if isinstance(result_value, dict) else {}
            payloads_value = result_object.get("payloads")
            payloads = cast(list[Any], payloads_value) if isinstance(payloads_value, list) else []
            text = (
                cast(dict[str, Any], payloads[0]).get("text")
                if payloads and isinstance(payloads[0], dict)
                else cast(dict[str, Any], envelope).get("summary")
            )
            if not isinstance(text, str):
                raise WorkerExecutionError("OpenClaw worker response did not contain an outcome")
            return _parse_result(text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WorkerExecutionError(f"invalid OpenClaw worker outcome: {exc}") from exc


def _worker_prompt(card: QueueCard, *, attempt_id: str, workspace: Path) -> str:
    schema = json.dumps(strict_output_schema(WorkerExecutionResult), separators=(",", ":"))
    merge_rule = (
        "This is an explicitly assigned merge-stage card: you may merge only after the configured merge gate "
        "passes and its completion policy allows it. Do not release, deploy, expose secrets, force-push, or "
        "delete branches."
        if "stage:merge" in card.labels
        else "Do not merge, release, deploy, expose secrets, force-push, or delete branches."
    )
    return (
        "You are a Make It So worker in a fresh context. Execute only the assigned card.\n"
        f"Card ID: {card.id}\n"
        f"Attempt ID / idempotency key: {attempt_id}\n"
        f"Exact working directory: {workspace}\n"
        f"Assignment:\n{card.notes or card.title}\n\n"
        "This managed launcher owns Workboard claim, heartbeat, completion, and blocking. Do not call "
        "Workboard tools or lifecycle helper commands, even if the assignment text mentions them. "
        "Report the outcome only by returning the JSON object requested below.\n\n"
        "Inspect current repository state before mutating it. Keep changes inside the exact working directory. "
        f"{merge_rule} Run the checks relevant "
        "to this card. Return blocked with a TECHNICAL:, USER_SECRET:, GOAL_DIVERGENCE:, EXTERNAL_ACCESS:, or "
        "HIGH_RISK_DECISION: reason when completion is not justified. Never invent proof.\n\n"
        "Return exactly one JSON object matching this schema, with no markdown or commentary:\n"
        f"{schema}"
    )


def _parse_result(text: str) -> WorkerExecutionResult:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        last_fence = stripped.rfind("```")
        if first_newline >= 0 and last_fence > first_newline:
            stripped = stripped[first_newline + 1 : last_fence].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise WorkerExecutionError("worker response did not contain a JSON object")
    try:
        return WorkerExecutionResult.model_validate_json(stripped[start : end + 1])
    except ValueError as exc:
        raise WorkerExecutionError(f"worker outcome failed schema validation: {exc}") from exc


def _runtime_model(runtime: Literal["openclaw", "codex"], model: str) -> str:
    if runtime == "codex" and model.startswith("codex/"):
        return model.split("/", 1)[1]
    return model


def _codex_usage(stdout: str, *, prompt_bytes: int, response_bytes: int) -> ModelUsage:
    usage: dict[str, Any] = {}
    reported_model: str | None = None
    for line in stdout.splitlines():
        try:
            raw_event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw_event, dict):
            continue
        event = cast(dict[str, Any], raw_event)
        if event.get("type") != "turn.completed":
            continue
        raw_usage = event.get("usage")
        if isinstance(raw_usage, dict):
            usage = cast(dict[str, Any], raw_usage)
        if event.get("model"):
            reported_model = str(event["model"])

    def token(name: str) -> int | None:
        value = usage.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    return ModelUsage(
        reported_model=reported_model,
        input_tokens=token("input_tokens"),
        cached_input_tokens=token("cached_input_tokens"),
        cache_write_tokens=token("cache_write_tokens"),
        reasoning_tokens=token("reasoning_output_tokens") or token("reasoning_tokens"),
        output_tokens=token("output_tokens"),
        total_tokens=token("total_tokens"),
        prompt_bytes=prompt_bytes,
        response_bytes=response_bytes,
        source="codex" if usage else "unreported",
    )


__all__ = [
    "CommandWorkerExecutor",
    "WorkerExecutionError",
    "WorkerExecutionResult",
    "WorkerExecutionTelemetry",
    "WorkerExecutorAdapter",
]
