from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from captains_chair.command import CommandResult, CommandRunner
from captains_chair.conformance import run_runtime_conformance
from captains_chair.models import (
    ActionKind,
    CodexWorkboardConfig,
    CompletionPolicy,
    HermesWorkboardConfig,
    OpenClawWorkboardConfig,
    OperationMode,
    OrchestratorConfig,
    PlanDecision,
    WorkerAssignments,
)
from captains_chair.orchestration import QueueCard, WorkerLifecycleAdapter, WorkQueueAdapter, WorkspaceRef
from captains_chair.plugins import PluginDiscoveryError
from captains_chair.runtime import (
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
        HermesWorkboardConfig(workers=workers()),
        CodexWorkboardConfig(workers=workers()),
    ),
)
def test_future_runtime_configs_fail_at_adapter_boundary(config: OrchestratorConfig) -> None:
    with pytest.raises(RuntimeAdapterUnavailable, match="no installed queue adapter"):
        build_work_queue_adapter(config)


def test_future_queue_runtime_can_register_without_mutating_workflow_core() -> None:
    registry = RuntimeAdapterRegistry()
    marker = ContractAdapter()

    def build_hermes(config: OrchestratorConfig, runner: CommandRunner) -> WorkQueueAdapter:
        del config, runner
        return cast(WorkQueueAdapter, marker)

    registry.register("hermes_workboard", build_hermes)
    adapter = build_work_queue_adapter(
        HermesWorkboardConfig(workers=workers()),
        no_rpc,
        registry=registry,
    )

    assert adapter is marker
    with pytest.raises(ValueError, match="already registered"):
        registry.register("hermes_workboard", build_hermes)


def test_runtime_registry_discovers_packaged_adapter_once() -> None:
    registry = RuntimeAdapterRegistry()
    marker = ContractAdapter()

    def build_hermes(config: OrchestratorConfig, runner: CommandRunner) -> WorkQueueAdapter:
        del config, runner
        return cast(WorkQueueAdapter, marker)

    def register_hermes(target: Any) -> None:
        target.register("hermes_workboard", build_hermes)

    entry_point = FakeEntryPoint("hermes", "captains_chair.runtime_adapters", register_hermes)
    assert registry.discover(provider=lambda: [entry_point]) == ("hermes",)
    assert registry.discover(provider=lambda: [entry_point]) == ()
    assert build_work_queue_adapter(HermesWorkboardConfig(workers=workers()), no_rpc, registry=registry) is marker


def test_runtime_plugin_registration_failure_is_explicit() -> None:
    registry = RuntimeAdapterRegistry()

    def broken_plugin(target: Any) -> None:
        del target
        raise RuntimeError("plugin setup failed")

    with pytest.raises(PluginDiscoveryError, match="failed during registration"):
        registry.discover(
            provider=lambda: [FakeEntryPoint("broken", "captains_chair.runtime_adapters", broken_plugin)]
        )


@pytest.mark.parametrize(
    ("config", "runtime_kind"),
    (
        (
            HermesWorkboardConfig(
                workers=worker_policy().workers,
                require_live_completion_validation=False,
            ),
            "hermes_workboard",
        ),
        (
            CodexWorkboardConfig(
                workers=worker_policy().workers,
                require_live_completion_validation=False,
            ),
            "codex_workboard",
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

    def build_hermes(config: OrchestratorConfig, runner: CommandRunner) -> WorkQueueAdapter:
        del config, runner
        return queue

    registry.register(runtime_kind, build_hermes)
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
        branch="captains_chair/test/work",
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
    assert len(lifecycle_completions) == 8
    workflow_cards = [
        card
        for card in queue.list_cards("captains-chair-example-project")
        if "workflow:future-runtime-conformance" in card.labels
    ]
    assert workflow_cards
    assert all(
        card.workspace == workspace
        for card in workflow_cards
        if all(
            stage not in card.labels
            for stage in ("stage:orchestration", "stage:merge", "stage:post_merge")
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
    assert len(lifecycle_completions) == 16


def test_future_queue_runtime_rejects_incomplete_adapter_at_construction() -> None:
    registry = RuntimeAdapterRegistry()

    def build_incomplete(config: OrchestratorConfig, runner: CommandRunner) -> WorkQueueAdapter:
        del config, runner
        return cast(WorkQueueAdapter, object())

    registry.register("hermes_workboard", build_incomplete)

    with pytest.raises(RuntimeAdapterContractError, match="required operations"):
        build_work_queue_adapter(
            HermesWorkboardConfig(workers=workers()),
            no_rpc,
            registry=registry,
        )
