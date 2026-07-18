# Runtime Adapter Guide

MAKE_IT_SO has explicit extension surfaces. A runtime may implement the harness and queue
surfaces, while GitHub, notifications, and scheduling remain independently replaceable.
The typed configuration accepts extension-owned kinds and a small `settings` envelope;
the corresponding registry still validates the adapter before any live operation.

## Harness Adapter

`HarnessAdapter` executes one fresh model context and returns schema-validated output. Planning and review use this boundary. Implementations must:

- accept the prompt, explicit model target, role, output schema, working directory, and writable flag
- create a fresh session for each call and for each fallback attempt; fallback
  attempts share only a root correlation ID, never provider conversation state
- enforce read-only versus workspace-write isolation
- return one JSON object matching the requested schema
- preserve every failed fallback attempt and the final resolved model
- compare any provider-reported model with the requested route; unqualified
  responses and the known `codex/...` to `openai/...` route alias are allowed,
  while a concrete model/provider mismatch must fail closed
- fail closed when structured output or sandbox guarantees are unavailable

OpenClaw is the P0 harness and Codex is the P1 harness. A future harness should
subclass `HarnessAdapter` and register a builder with `HarnessAdapterRegistry`;
core planning, policy, prompts, and model-attempt auditing do not change. The
registry validates the returned object before a model invocation begins, so a
malformed packaged harness fails at construction rather than during a live cycle.
`HarnessConfig.kind` is intentionally open to extension-owned names; an unknown
kind is still unusable until a matching registry builder is installed.

Direct model admission also accepts an optional runtime telemetry synchronizer.
The OpenClaw CLI supplies one backed by `openclaw sessions`; it reconciles the
previous call before the next direct Captain call is admitted and suppresses the call
when the telemetry endpoint is degraded. A Codex or future runtime
can provide the same callback from its native usage ledger without importing
OpenClaw code into the engine.

## Worker Orchestration And Tracking

`WorkerOrchestratorAdapter` maps the portable workflow DAG onto a runtime's worker
primitives. `WorkTrackerAdapter` is a separate optional boundary for boards,
cards, and human-facing task views. The core can use `DirectOrchestrator` with
SQLite and no board; OpenClaw may add its built-in Workboard through the tracker
adapter. It receives work-item records containing stable idempotency keys, parent
item IDs, assigned roles, isolated workspace references, retry limits, source
links, and proof requirements.

An adapter must implement:

1. idempotent board and card creation
2. dependency promotion only after every parent is complete
3. role-specific worker dispatch
4. deterministic worker-model health before dispatch, when the runtime exposes agent configuration
5. claims and heartbeats or an equivalent lease mechanism
6. structured passed proof on completion
7. block, unblock, reclaim, reassign, comment, and diagnostics operations
8. durable status reads after process restart
9. reclaiming expired worker leases when heartbeats stop before completion
10. reclaiming sessions reported as ended, terminated, completed, or missing
11. bounded retries without suppressing unrelated ready work

Adapters that inspect worker sessions should expose an optional
`recover_ended_workers` hook and a `recovery_warnings()` readout. A transient
session-inspection failure must be reported as degraded evidence while leaving
unrelated ready cards eligible for dispatch; it must not be silently treated as
a healthy pass or freeze the entire queue.

The adapter must round-trip each card's isolated `WorkspaceRef`; fresh retry and
repair cards are not allowed to silently inherit the runtime's shared default
workspace. `WorkspaceRef.branch` is the local checkout branch and
`WorkspaceRef.push_branch`, when present, is the remote branch that a repair
worker must update. Adapters must preserve both values and expose them in the
worker's card context.

The worker-facing lease boundary is `WorkerLifecycleAdapter`. It carries a
runtime-issued owner/token pair through heartbeats, structured completion proof,
and explicit blockers. The queue adapter may acquire that claim during dispatch
or delegate it to a worker-session service; the deterministic workflow engine
does not need to know which mechanism was used.

Board-free runtimes implement the extended `ClaimingWorkerLifecycleAdapter` and
use `WorkerExecutorAdapter` to launch a fresh host worker. `DirectOrchestrator`
atomically claims one dependency-ready card, records an attempt id and expiring
lease, maintains heartbeats while the host process runs, and commits completion or
block evidence only while the original owner/token is still active. Cancellation
and expiry remove the live token before changing state, so a late process cannot
overwrite the newer decision. Expired leases retry only within the card's bounded
budget; restart reconciliation recovers the same durable SQLite state.

The built-in command executor supports both OpenClaw and Codex. OpenClaw receives
an isolated session key, assigned agent and exact workspace; Codex receives a fresh
`codex exec` invocation with `workspace-write`, an output schema and the same
idempotency key. Both must return the portable `WorkerExecutionResult` contract.

Runtime model mappings are intentionally separate. The portable `models` and
`harness_model_overrides.codex` sections describe direct Codex execution, where
`gpt-5.3-codex-spark` is the economical coding route. OpenClaw Workboard also
supports typed per-role execution through `worker_runtimes`: Workboard retains
the claim, heartbeat, dependency, retry, and proof lifecycle while a coding card
may run through `codex exec`. The resulting execution receipt, including the
requested model and provider token telemetry, is retained in Workboard proof and
imported idempotently into the portable usage store. OpenClaw-only roles continue
to use the configured gateway model. Direct Codex uses ChatGPT OAuth, not an
OpenAI API-key path. Runtime-specific capability checks must be performed before
dispatch and the requested, reported, provider, and authentication route must be
retained in usage evidence.
An `external` direct runtime leaves cards ready for a third-party worker to claim
through the same lifecycle API.

Final-review cards must include a passed marker anchored to the reviewed head:
`READY_FOR_OWNER:<head-sha>`, `CONTROL_PLANE_COMPLETE:<head-sha>`, or
`AUTO_MERGE_ALLOWED:<head-sha>` according to repository policy. Generic
successful prose is not completion proof. The workflow must also carry the exact
GitHub PR URL in implementation or repair proof. Runtime configurations require
a `CompletionValidator` by default. MAKE_IT_SO re-reads the live PR before treating
the workflow as complete: the head, checks, mergeability, and unresolved review
threads must still match the final-review evidence. A portable adapter may
explicitly set `require_live_completion_validation: false` for deterministic
conformance tests, but that boundary must never be used for unattended
repository operation. If a runtime preserves multiple passed proof records, the
latest passed record must carry the policy marker and is authoritative; a newer
passed record without that marker invalidates older reviewed SHAs.

Capability QA is represented by one card per selected `QAProfile`, not by generic
test prose on a shared card. Adapters must preserve `qaProfile`, `qaSurfaces`,
`qaChecks`, planned/actual path evidence, and the proof object in card metadata.
A passed QA proof has the marker `QA_PASSED:<profile-key>:<head-sha>` plus
non-empty `model`, `provider`, and `evidence` fields. Web UI evidence separately
covers accessibility, contrast, responsive behavior, flow, and cohesion. The
GitHub-backed validator compares every required profile with the current PR head;
a repair commit invalidates stale evidence and causes a fresh QA retry. If live PR
files reveal an unplanned capability, reconciliation creates the missing role card
before final completion can pass.

OpenClaw maps worker dispatch to OpenClaw workers and may map task visibility to
the built-in Workboard Gateway RPC. The P1 Codex adapter may use SQLite rows plus
disposable `codex exec` sessions. A future runtime may provide either or both
boundaries. Neither adapter may move merge policy,
blocker classification, workflow topology, GitHub gates, or proof validation
out of MAKE_IT_SO core.

Construction is centralized in `make_it_so.runtime` and the runtime-neutral
registry. The default OpenClaw path can select Workboard-backed dispatch, while
`DirectOrchestrator` is the board-free fallback. Codex and future integrations
register builders without changing the workflow engine or policy code. The
orchestrator must implement the `WorkerOrchestratorAdapter` and
`WorkerLifecycleAdapter` contracts; a tracker, when present, implements
`WorkTrackerAdapter`. The registry validates each selected surface before a live
workflow starts. Conformance fixtures remain runtime-neutral.

Packaged adapters may expose a callable registrar through the entry-point groups
`make_it_so.runtime_adapters`, `make_it_so.harness_adapters`, and
`make_it_so.scheduler_adapters`. The registrar receives the
corresponding registry and calls `register(...)`; MAKE_IT_SO discovers these plugins
only when building the default registry, loads each plugin once, and fails closed
if loading or registration fails. OpenClaw remains built in, so the core package
does not require any future runtime package to be installed.

Notifier packages use the same boundary through `NotifierAdapterRegistry` and the
`make_it_so.notifier_adapters` entry-point group. Built-in `stdout`, OpenClaw Discord,
and Discord webhook delivery remain unchanged. An extension may use
`NotificationConfig.settings` for its own validated options; an unregistered
notification kind fails closed instead of being guessed as a webhook.

`NotifierAdapter` is the runtime-neutral delivery contract; `Notifier` remains a
compatibility name. `SchedulerAdapter` is the corresponding install contract for
cron and hosted schedulers. `UsageTelemetryAdapter` synchronizes provider-native
usage before a model call, and `InteractionAdapter` owns planning handoffs and
durable readiness/checkpoint answers. OpenClaw and Codex may provide different
implementations without changing course or policy logic; the deterministic native
implementations remain the default.

Schedulers use the same registry boundary through `SchedulerAdapterRegistry` and
the `make_it_so.scheduler_adapters` entry-point group. OpenClaw cron, system cron,
systemd, and Windows Task Scheduler remain built in. A future hosted runtime can
register its own kind and reuse the same `ScheduleSpec`; the
CLI no longer needs a new hard-coded branch for that runtime. Render-only
behavior is reserved for the built-in portable renderers; registered runtime
schedulers implement `install(spec)` and must preserve idempotency and the
requested enabled state.

For example, a future adapter package can publish these entry points:

```toml
[project.entry-points."make_it_so.runtime_adapters"]
future_runtime = "make_it_so_future_runtime:register_runtime"

[project.entry-points."make_it_so.harness_adapters"]
future_runtime = "make_it_so_future_runtime:register_harness"

[project.entry-points."make_it_so.notifier_adapters"]
future_runtime = "make_it_so_future_runtime:register_notifier"

[project.entry-points."make_it_so.scheduler_adapters"]
future_runtime = "make_it_so_future_runtime:register_scheduler"
```

Each registrar receives its registry and registers only the adapter builder;
the shared `WorkflowOrchestrator`, policy engine, GitHub provider, and proof
validators remain in MAKE_IT_SO.

## GitHub Provider

`GitHubProvider` is the engine-facing protocol. `GhGitHubProvider` is the current
implementation using authenticated `gh` REST, GraphQL, and CLI operations. A future
provider can use GitHub App REST/GraphQL, an organization service, or a test double
without changing the Captain engine, baseline analysis, policy, or merge gate. It must
preserve the same snapshot, pull-request, review-thread, check-gate, issue-mutation,
and default-branch verification semantics. Issue mutation includes create, update,
add-label, retarget (milestone and/or assignees), and close operations; these are
typed deterministic actions so a runtime adapter never needs to reimplement GitHub
issue semantics.

The provider also exposes `provision_greenfield` for the greenfield course path. It is
called only after readiness and owner engagement have been recorded. The built-in
implementation uses `gh repo create --source --push` after initializing a committed
local seed, then reads the repository back to prove creation. Providers for other Git
hosts can implement the same approval boundary without importing GitHub CLI behavior
into the core.

## Runtime Mapping

| Surface | OpenClaw P0 | Codex P1 | Future runtimes |
| --- | --- | --- | --- |
| Harness | `openclaw agent --json` | `codex exec --json` | Runtime harness adapter |
| Orchestration | OpenClaw workers | `DirectOrchestrator` plus worker processes | Runtime orchestrator adapter |
| Tracking | Optional Workboard Gateway RPC | Optional tracker adapter | Optional board/tracker adapter |
| GitHub | `GhGitHubProvider` | Same provider or App client | Same provider or App client |
| Notifications | OpenClaw Discord route | Discord webhook/stdout | Runtime notifier adapter |
| Scheduler | OpenClaw cron | cron/systemd/Task Scheduler | Runtime scheduler adapter |

The Codex plugin's `scripts/mcp_server.py` and `scripts/serve_ui.py` are host
adapters, not alternate policy engines. The MCP bridge delegates to the
installed CLI, and the standalone UI delegates to the same sidecar methods used
by the OpenClaw dashboard.

The installed `make_it_so.conformance` module exposes the runtime-neutral workflow
scenario. Each new production adapter should provide its queue factory plus a
claimed-card blocker hook, call `run_runtime_conformance` against its adapter,
pass the runtime-supplied `WorkspaceRef` when the adapter materializes a
disposable checkout, and add only adapter-specific RPC/session failure tests.
The helper returns
workflow, owner-blocker, and technical-retry evidence and raises
`RuntimeConformanceError` when the adapter violates the contract; it does not
depend on OpenClaw or the repository's test fakes.
The shared conformance helpers also cover simultaneous owner and technical
blockers, proving that an owner decision does not suppress autonomous repair or
unrelated ready work.

## Shared Configuration

Runtime configuration should inherit `WorkerOrchestrationConfig`, then add only
runtime-specific connection and dispatch fields. Workboard configuration belongs
to the optional tracker adapter; it is never a required core field. The worker
roles and retry policy remain common:

- Captain recovery
- coder/repair
- independent reviewer
- tester/CI checker
- UX reviewer
- final reviewer
- deterministic merger
- post-merge verifier

## Conformance Scenario

Every production queue adapter must pass the disposable scenario in `tests/test_orchestration_e2e.py`, then repeat it against a disposable real repository:

1. enqueue implementation from an issue
2. prove isolated workspace execution
3. promote review, test, and UX in parallel
4. inject a technical review failure
5. create and complete a coder repair card
6. rerun the independent gate on new-head proof
7. produce exact current-head final-review authorization
8. pass deterministic merge policy
9. verify default-branch CI after merge
10. demonstrate that separate user-blocked and technical-repair workflows, including when both are present, do not stop ready work

Runtime success or a model's narrative is never sufficient evidence. Status transitions, proof, current GitHub state, and policy results must agree.
