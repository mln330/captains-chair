from __future__ import annotations

from collections.abc import Callable

from captains_chair.command import CommandRunner, run_command
from captains_chair.models import OpenClawWorkboardConfig, OrchestratorConfig
from captains_chair.openclaw_workboard import OpenClawWorkboardAdapter
from captains_chair.orchestration import (
    CompletionValidator,
    WorkerLifecycleAdapter,
    WorkflowOrchestrator,
    WorkQueueAdapter,
    WorkspaceCleanup,
)
from captains_chair.plugins import EntryPointProvider, load_entrypoint_plugins


class RuntimeAdapterUnavailable(RuntimeError):
    """Raised when a validated runtime is configured before its adapter is installed."""


class RuntimeAdapterContractError(RuntimeError):
    """Raised when an installed runtime does not implement the portable contract."""


RUNTIME_ADAPTER_ENTRYPOINT_GROUP = "captains_chair.runtime_adapters"


QueueAdapterBuilder = Callable[[OrchestratorConfig, CommandRunner], WorkQueueAdapter]

_QUEUE_METHODS = (
    "ensure_board",
    "list_cards",
    "create_card",
    "complete_card",
    "unblock_card",
    "reclaim_card",
    "reassign_card",
    "comment",
    "dispatch",
    "diagnostics",
)


def validate_work_queue_adapter(adapter: WorkQueueAdapter) -> WorkQueueAdapter:
    missing = [name for name in _QUEUE_METHODS if not callable(getattr(adapter, name, None))]
    if not isinstance(adapter, WorkerLifecycleAdapter):
        missing.extend(
            name
            for name in ("heartbeat_card", "complete_claimed_card", "block_claimed_card")
            if not callable(getattr(adapter, name, None))
        )
    if missing:
        raise RuntimeAdapterContractError(
            "runtime adapter is missing required operations: " + ", ".join(sorted(set(missing)))
        )
    return adapter


class RuntimeAdapterRegistry:
    """Explicit queue-adapter registry that keeps future runtimes out of core policy."""

    def __init__(self) -> None:
        self._builders: dict[str, QueueAdapterBuilder] = {}
        self._loaded_plugins: set[str] = set()

    def register(self, kind: str, builder: QueueAdapterBuilder, *, replace: bool = False) -> None:
        normalized = kind.strip()
        if not normalized:
            raise ValueError("runtime adapter kind must not be empty")
        if normalized in self._builders and not replace:
            raise ValueError(f"runtime adapter is already registered: {normalized}")
        self._builders[normalized] = builder

    def discover(self, *, provider: EntryPointProvider | None = None) -> tuple[str, ...]:
        """Discover packaged queue adapters without moving policy into plugins."""
        if provider is None:
            return load_entrypoint_plugins(
                self,
                group=RUNTIME_ADAPTER_ENTRYPOINT_GROUP,
                loaded=self._loaded_plugins,
            )
        return load_entrypoint_plugins(
            self,
            group=RUNTIME_ADAPTER_ENTRYPOINT_GROUP,
            provider=provider,
            loaded=self._loaded_plugins,
        )

    def build(self, config: OrchestratorConfig, runner: CommandRunner) -> WorkQueueAdapter:
        builder = self._builders.get(config.kind)
        if builder is None:
            raise RuntimeAdapterUnavailable(
                f"orchestrator kind {config.kind} has no installed queue adapter; "
                "register a WorkQueueAdapter with RuntimeAdapterRegistry"
            )
        return validate_work_queue_adapter(builder(config, runner))


def _build_openclaw_queue(config: OrchestratorConfig, runner: CommandRunner) -> WorkQueueAdapter:
    if not isinstance(config, OpenClawWorkboardConfig):
        raise TypeError("openclaw_workboard adapter requires OpenClawWorkboardConfig")
    return OpenClawWorkboardAdapter(config, runner)


DEFAULT_RUNTIME_ADAPTERS = RuntimeAdapterRegistry()
DEFAULT_RUNTIME_ADAPTERS.register("openclaw_workboard", _build_openclaw_queue)


def register_work_queue_adapter(
    kind: str,
    builder: QueueAdapterBuilder,
    *,
    replace: bool = False,
) -> None:
    """Register a production queue adapter in the process-wide default registry."""
    DEFAULT_RUNTIME_ADAPTERS.register(kind, builder, replace=replace)


def build_work_queue_adapter(
    config: OrchestratorConfig,
    runner: CommandRunner = run_command,
    *,
    registry: RuntimeAdapterRegistry = DEFAULT_RUNTIME_ADAPTERS,
) -> WorkQueueAdapter:
    """Build the queue adapter for one runtime configuration.

    The default registry contains OpenClaw. Tests and host integrations can pass
    an instance-local registry so future runtimes do not mutate global state.
    """
    if registry is DEFAULT_RUNTIME_ADAPTERS:
        registry.discover()
    return registry.build(config, runner)


def build_work_queue_orchestrator(
    config: OrchestratorConfig,
    runner: CommandRunner = run_command,
    *,
    registry: RuntimeAdapterRegistry = DEFAULT_RUNTIME_ADAPTERS,
    workspace_cleanup: WorkspaceCleanup | None = None,
    completion_validator: CompletionValidator | None = None,
) -> WorkflowOrchestrator:
    """Build the runtime-neutral orchestrator around one queue adapter."""
    return WorkflowOrchestrator(
        build_work_queue_adapter(config, runner, registry=registry),
        config,
        workspace_cleanup=workspace_cleanup,
        completion_validator=completion_validator,
    )


__all__ = [
    "RuntimeAdapterUnavailable",
    "RuntimeAdapterContractError",
    "RuntimeAdapterRegistry",
    "RUNTIME_ADAPTER_ENTRYPOINT_GROUP",
    "validate_work_queue_adapter",
    "register_work_queue_adapter",
    "build_work_queue_adapter",
    "build_work_queue_orchestrator",
]
