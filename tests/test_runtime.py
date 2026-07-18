from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

import make_it_so.runtime as runtime
from make_it_so.command import CommandResult, CommandRunner
from make_it_so.conformance import run_runtime_conformance
from make_it_so.models import (
    ActionKind,
    CompletionPolicy,
    ExternalWorkboardConfig,
    OpenClawWorkboardConfig,
    OperationMode,
    OrchestratorConfig,
    PlanDecision,
    WorkerAssignments,
)
from make_it_so.orchestration import (
    NullWorkTracker,
    QueueCard,
    WorkerLifecycleAdapter,
    WorkQueueAdapter,
    WorkspaceRef,
    WorkTrackerAdapter,
)
from make_it_so.plugins import PluginDiscoveryError
from make_it_so.runtime import (
    RuntimeAdapterContractError,
    RuntimeAdapterRegistry,
    RuntimeAdapterUnavailable,
    build_work_queue_adapter,
    build_work_queue_orchestrator,
)
from tests.fakes import InMemoryWorkQueue, worker_policy
from tests.helpers import repo_config


class ContractAdapter:
    def ensure_board(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def list_cards(self, *args: object, **kwargs: object) -> list[object]:
        del args, kwargs
        return []

    def create_card(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        return object()

    def complete_card(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        return object()

    def unblock_card(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        return object()

    def reclaim_card(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        return object()

    def reassign_card(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        return object()

    def comment(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        return object()

    def dispatch(self, *args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        return {}

    def diagnostics(self, *args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        return {}

    def heartbeat_card(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        return object()

    def complete_claimed_card(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        return object()

    def block_claimed_card(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        return object()


class FakeEntryPoint:
    def __init__(self, name: str, group: str, registrar: Callable[[Any], None]) -> None:
        self.name = name
        self.group = group
        self._registrar = registrar

    def load(self) -> Callable[[Any], None]:
        return self._registrar


def workers() -> WorkerAssignments:
    return WorkerAssignments(
        captain="captain",
        coder="coder",
        reviewer="reviewer",
        tester="tester",
        ux_reviewer="ux",
        final_reviewer="final",
        merger="merger",
        verifier="verifier",
    )


def no_rpc(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: int = 60,
) -> CommandResult:
    del command, cwd, input_text, timeout
    raise AssertionError("runtime factory test should not call the gateway")


def test_openclaw_runtime_factory_builds_queue_and_orchestrator() -> None:
    config = OpenClawWorkboardConfig(
        workers=workers(),
        require_live_completion_validation=False,
    )

    adapter = build_work_queue_adapter(config, no_rpc)
    orchestrator = build_work_queue_orchestrator(config, no_rpc)

    assert adapter.__class__.__name__ == "OpenClawWorkboardAdapter"
    assert isinstance(adapter, WorkerLifecycleAdapter)
    assert orchestrator.config is config


def test_openclaw_runtime_factory_requires_live_completion_validator_by_default() -> None:
    config = OpenClawWorkboardConfig(workers=workers())

    with pytest.raises(ValueError, match="live completion validation is required"):
        build_work_queue_orchestrator(config, no_rpc)


@pytest.mark.parametrize(
    "config",
    (
        ExternalWorkboardConfig(kind="future_a", executable="future-a", workers=workers()),
        ExternalWorkboardConfig(kind="future_b", executable="future-b", workers=workers()),
    ),
)
def test_future_runtime_configs_fail_at_adapter_boundary(config: OrchestratorConfig) -> None:
    with pytest.raises(RuntimeAdapterUnavailable, match="no installed queue adapter"):
        build_work_queue_adapter(config)


def test_future_queue_runtime_can_register_without_mutating_workflow_core() -> None:
    registry = RuntimeAdapterRegistry()
    marker = ContractAdapter()

    def build_future(config: OrchestratorConfig, runner: CommandRunner) -> WorkQueueAdapter:
        del config, runner
        return cast(WorkQueueAdapter, marker)

    registry.register("future_runtime", build_future)
    adapter = build_work_queue_adapter(
        ExternalWorkboardConfig(kind="future_runtime", executable="future", workers=workers()),
        no_rpc,
        registry=registry,
    )

    assert adapter is marker
    with pytest.raises(ValueError, match="already registered"):
        registry.register("future_runtime", build_future)


def test_runtime_registry_discovers_packaged_adapter_once() -> None:
    registry = RuntimeAdapterRegistry()
    marker = ContractAdapter()

    def build_future(config: OrchestratorConfig, runner: CommandRunner) -> WorkQueueAdapter:
        del config, runner
        return cast(WorkQueueAdapter, marker)

    def register_future(target: Any) -> None:
        target.register("future_runtime", build_future)

    entry_point = FakeEntryPoint("future", "make_it_so.runtime_adapters", register_future)
    assert registry.discover(provider=lambda: [entry_point]) == ("future",)
    assert registry.discover(provider=lambda: [entry_point]) == ()
    assert (
        build_work_queue_adapter(
            ExternalWorkboardConfig(kind="future_runtime", executable="future", workers=workers()),
            no_rpc,
            registry=registry,
        )
        is marker
    )


def test_runtime_plugin_registration_failure_is_explicit() -> None:
    registry = RuntimeAdapterRegistry()

    def broken_plugin(target: Any) -> None:
        del target
        raise RuntimeError("plugin setup failed")

    with pytest.raises(PluginDiscoveryError, match="failed during registration"):
        registry.discover(
            provider=lambda: [FakeEntryPoint("broken", "make_it_so.runtime_adapters", broken_plugin)]
        )


@pytest.mark.parametrize(
    ("config", "runtime_kind"),
    (
        (
            ExternalWorkboardConfig(
                kind="future_a",
                executable="future-a",
                workers=worker_policy().workers,
                require_live_completion_validation=False,
            ),
            "future_a",
        ),
        (
            ExternalWorkboardConfig(
                kind="future_b",
                executable="future-b",
                workers=worker_policy().workers,
                require_live_completion_validation=False,
            ),
            "future_b",
        ),
    ),
)
def test_future_runtime_shape_runs_the_shared_workflow_conformance_fixture(
    tmp_path: Path,
    config: OrchestratorConfig,
    runtime_kind: str,
) -> None:
    registry = RuntimeAdapterRegistry()
    queue = InMemoryWorkQueue()

    def build_future(config: OrchestratorConfig, runner: CommandRunner) -> WorkQueueAdapter:
        del config, runner
        return queue

    registry.register(runtime_kind, build_future)
    orchestrator = build_work_queue_orchestrator(config, no_rpc, registry=registry)
    repo = repo_config(
        tmp_path,
        mode=OperationMode.AUTONOMOUS,
        completion=CompletionPolicy.AUTO_MERGE,
    )
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement a future-runtime slice",
        reason="The shared conformance fixture selected it.",
        target_issue=39,
        acceptance_criteria=("Scope is correct", "Checks pass"),
    )

    assert isinstance(orchestrator.adapter, WorkerLifecycleAdapter)
    workspace = WorkspaceRef(
        kind="runtime-test",
        path=tmp_path / "workspace",
        branch="make_it_so/test/work",
        push_branch="remote/test/work",
    )
    lifecycle_completions: list[str] = []

    def complete_claimed(
        card_id: str,
        summary: str,
        proof: tuple[dict[str, object], ...],
    ) -> QueueCard:
        lifecycle_completions.append(card_id)
        return queue.complete_claimed_card(
            card_id,
            owner_id="test-worker",
            token="test-token",
            summary=summary,
            proof=proof,
        )

    report = run_runtime_conformance(
        orchestrator,
        orchestrator.adapter,
        repo,
        decision,
        action_id="future-runtime-conformance",
        block_card=lambda card_id, reason: queue.block_claimed_card(
            card_id,
            owner_id="test-worker",
            token="test-token",
            reason=reason,
        ),
        complete_card=complete_claimed,
        workspace=workspace,
    )
    assert report.workflow_id == "future-runtime-conformance"
    assert report.owner_blocked_card_id.startswith("card-")
    assert report.technical_retry_card_id.startswith("card-")
    assert report.mixed_owner_blocked_card_id.startswith("card-")
    assert report.mixed_technical_retry_card_id.startswith("card-")
    assert report.mixed_unrelated_card_id.startswith("card-")
    assert len(lifecycle_completions) == 7
    workflow_cards = [
        card
        for card in queue.list_cards("make-it-so-example-project")
        if "workflow:future-runtime-conformance" in card.labels
    ]
    assert workflow_cards
    assert all(
        card.workspace == workspace
        for card in workflow_cards
        if all(
            stage not in card.labels for stage in ("stage:orchestration", "stage:merge", "stage:post_merge")
        )
    )

    repeated = run_runtime_conformance(
        orchestrator,
        orchestrator.adapter,
        repo,
        decision,
        action_id="future-runtime-conformance-repeat",
        block_card=lambda card_id, reason: queue.block_claimed_card(
            card_id,
            owner_id="test-worker",
            token="test-token",
            reason=reason,
        ),
        complete_card=complete_claimed,
        workspace=workspace,
    )
    assert repeated.workflow_id == "future-runtime-conformance-repeat"
    assert repeated.workflow_id != report.workflow_id
    assert len(lifecycle_completions) == 14


def test_future_queue_runtime_rejects_incomplete_adapter_at_construction() -> None:
    registry = RuntimeAdapterRegistry()

    def build_incomplete(config: OrchestratorConfig, runner: CommandRunner) -> WorkQueueAdapter:
        del config, runner
        return cast(WorkQueueAdapter, object())

    registry.register("future_runtime", build_incomplete)

    with pytest.raises(RuntimeAdapterContractError, match="required operations"):
        build_work_queue_adapter(
            ExternalWorkboardConfig(kind="future_runtime", executable="future", workers=workers()),
            no_rpc,
            registry=registry,
        )


def test_runtime_registry_rejects_empty_kind_and_allows_explicit_replacement() -> None:
    registry = RuntimeAdapterRegistry()
    marker = ContractAdapter()

    def build(config: OrchestratorConfig, runner: CommandRunner) -> WorkQueueAdapter:
        del config, runner
        return cast(WorkQueueAdapter, marker)

    with pytest.raises(ValueError, match="must not be empty"):
        registry.register("   ", build)
    registry.register("replaceable", build)
    registry.register("replaceable", build, replace=True)


def test_runtime_registry_rejects_openclaw_and_direct_config_type_mismatches() -> None:
    registry = RuntimeAdapterRegistry()
    registry.register("openclaw_workboard", runtime._build_openclaw_queue)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(TypeError, match="OpenClawWorkboardConfig"):
        registry.build(
            ExternalWorkboardConfig(kind="openclaw_workboard", executable="openclaw", workers=workers()),
            no_rpc,
        )
    registry = RuntimeAdapterRegistry()
    registry.register("direct", runtime._build_direct_orchestrator)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(TypeError, match="DirectOrchestratorConfig"):
        registry.build(
            ExternalWorkboardConfig(kind="direct", executable="direct", workers=workers()),
            no_rpc,
        )


def test_null_work_tracker_satisfies_optional_tracker_contract() -> None:
    tracker = NullWorkTracker()

    assert isinstance(tracker, WorkTrackerAdapter)
    assert (
        tracker.mirror_work(
            "course-1/package-1",
            title="Implement bounded package",
            summary="No external tracker is configured.",
            status="ready",
            source_url="https://example.test/package-1",
            metadata={"course_id": "course-1"},
        )
        is None
    )
    tracker.update_work("unused", status="done", summary="Complete")
    tracker.remove_work("unused")
    assert tracker.diagnostics() == {"status": "healthy", "kind": "null", "enabled": False}
