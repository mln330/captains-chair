# Future Runtime Adapters

OpenClaw is the P0 runtime and Codex is P1. New runtime names never require a
core-model edit: use `HarnessConfig.kind`, `ExternalWorkboardConfig`, and the
adapter registries. An extension package owns validation of its `settings`
mapping and registers builders at the relevant boundaries.

## Core Ownership

Keep these decisions in Make It So core:

- modes, approvals, checkpoints, blocker classification, and high-risk actions
- workflow topology, retries, recovery transitions, and idempotency keys
- isolated `WorkspaceRef` branch and push-branch semantics
- GitHub checks, review threads, current-head validation, and merge gates
- structured proof and final-review authorization markers
- durable courses, work packages, events, model provenance, and token telemetry

Runtime adapters own worker launch, claims, heartbeats, session inspection,
runtime diagnostics, and translation of portable work context into native tools.
Work-tracker adapters may mirror packages into a board, but tracking is optional
and never becomes the source of policy or completion truth.

## Adapter Steps

1. Use the generic extension configuration envelope; do not add a runtime-specific
   class to the core package.
2. Implement `WorkerOrchestratorAdapter` and `WorkerLifecycleAdapter` operations.
3. Implement `WorkTrackerAdapter` only when external task mirroring is desired.
4. Register builders through `make_it_so.runtime_adapters` or the appropriate
   harness, notifier, scheduler, telemetry, or interaction entry-point group.
5. Preserve `QueueCardSpec.key` as the idempotency key across process restarts.
6. Round-trip `WorkspaceRef.path`, `branch`, and `push_branch` on retries and repairs.
7. Return structured proof, claims, telemetry status, and diagnostics.
8. Run `run_runtime_conformance` unchanged before adapter-specific tests.

## Direct Codex Shape

`DirectOrchestrator` already supplies durable SQLite workflow state without a
kanban requirement. A Codex host adds worker processes that:

- claim one ready card and use a fresh `codex exec` session
- use read-only isolation for review and workspace-write only for implementation
- request structured output for every worker result
- record failed attempts, resolved model provenance, and provider-reported tokens
- heartbeat and complete through the shared lifecycle contract

The Codex adapter must not infer completion from narrative output or move GitHub
policy into the host integration.

## Required Verification

Before enabling any extension runtime:

- pass adapter validation at construction
- pass the shared runtime conformance fixture
- pass disposable real-Git worktree tests
- test restart recovery, expired claims, duplicate enqueue, provider failure,
  missing proof, stale heads, unresolved threads, and notification failure
- test that owner blockers do not suppress unrelated ready work
- run the complete portable-core quality gates

A no-op canary checks deployment only; it never substitutes for conformance and
failure-isolation tests.
