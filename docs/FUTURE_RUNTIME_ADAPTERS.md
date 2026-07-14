# Future Runtime Adapters

OpenClaw is the only production runtime in V1. Hermes and standalone Codex do
not need working queue implementations yet, but they must be able to reuse the
same policy, workflow, GitHub, proof, and usage code when implemented.

New runtime names do not require a core-model edit: use `HarnessConfig.kind` for
an extension harness and `ExternalWorkboardConfig` for an extension queue. The
adapter package owns validation of its `settings` mapping, then registers the
builder at the corresponding boundary.

## Boundary

Keep these decisions in CAPTAINS_CHAIR core:

- Captain modes, approval rules, blocker classification, and high-risk actions
- workflow DAG topology, retry/recovery transitions, and idempotency keys
- isolated `WorkspaceRef` and branch/push-branch semantics
- GitHub checks, review threads, current-head validation, and merge gates
- structured worker proof and final-review authorization markers
- SQLite leases, event history, model provenance, and usage admission

Keep these decisions in the runtime adapter:

- queue/card persistence and board or task identifiers
- worker launch, claims, heartbeats, and session inspection
- runtime-specific retries, diagnostics, and delivery metadata
- mapping a card's workspace and parent results into the worker context

An adapter must not reimplement policy or infer completion from a worker's
narrative response.

Notifier implementations follow the same rule. Use `NotifierAdapterRegistry` and
the `captains_chair.notifier_adapters` entry-point group for runtime-specific delivery. The
core event renderer, owner-attention classification, and notification-failure
accounting remain shared.

## Queue Adapter Steps

1. Subclass or implement the typed runtime configuration shape in `models.py`.
2. Implement `WorkQueueAdapter` and `WorkerLifecycleAdapter` operations.
3. Register a builder with `RuntimeAdapterRegistry`, or publish an
   `captains_chair.runtime_adapters` entry point from a separate package.
4. Preserve `QueueCardSpec.key` as the idempotency key across process restarts.
5. Round-trip `WorkspaceRef.path`, `branch`, and `push_branch` without falling
   back to a shared checkout on retries or repairs.
6. Return structured proof, claims, and diagnostics in the adapter's native
   format mapped to the portable card model.
7. Run `run_runtime_conformance` unchanged before adding runtime-specific tests;
   pass the runtime's disposable `WorkspaceRef` so the shared fixture verifies
   branch and push-branch propagation too.

The conformance scenario must prove dependency ordering, independent review and
test lanes, repair after a technical failure, current-head final review, merge,
post-merge verification, owner-blocker isolation, technical recovery, and
unrelated work continuing.

## Hermes Shape

A Hermes adapter should map:

- Workboard boards and cards to Hermes tasks or projects
- `ownerId` and claim tokens to Hermes task leases
- heartbeats to Hermes lease renewal
- session inspection to Hermes worker lifecycle events
- `WorkspaceRef` to a Hermes-provided disposable checkout

Hermes-specific task metadata may be retained for diagnostics, but the durable
CAPTAINS_CHAIR card status and proof must remain reconstructible after an CAPTAINS_CHAIR restart.

## Standalone Codex Shape

A standalone Codex adapter can use SQLite queue rows and worker processes:

- one durable row per `QueueCardSpec.key`
- a lease table with owner, token, expiry, and heartbeat timestamps
- a worker process per claimed card using a fresh `codex exec` session
- `--sandbox read-only` for reviewers and `--sandbox workspace-write` only for
  coding or repair worktrees
- `--json --output-schema` for every structured worker result
- a metadata-only usage ledger associated with the CAPTAINS_CHAIR root call ID

The standalone adapter should use the same `CodexAdapter` harness boundary when
possible. It must record failed provider attempts and resolved model routes rather
than hiding fallback usage.

## Harness Adapter Steps

For Hermes or another model runtime, implement `HarnessAdapter.invoke`, return
`HarnessInvocation` when provider usage is available, and register a builder with
`captains_chair.harness_adapters`. Every invocation must create a fresh provider session,
enforce the requested sandbox, validate the requested output model, and fail
closed on a reported model mismatch or unusable structured output.

## Required Verification

Before enabling a future runtime:

- pass the adapter contract validator at construction
- pass the shared runtime conformance fixture
- pass disposable real-Git worktree tests
- test restart recovery, expired leases, duplicate enqueue, provider failure,
  missing proof, stale heads, unresolved review threads, and notification failure
- run the full package and portable-core coverage gates

Only after those tests pass should a runtime receive a no-op canary. The runtime
canary is a deployment check for the adapter; it is not a substitute for the
shared workflow and failure-isolation tests.
