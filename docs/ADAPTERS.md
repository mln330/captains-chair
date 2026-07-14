# Runtime Adapter Guide

CAPTAINS_CHAIR has explicit extension surfaces. A runtime may implement the harness and queue
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

OpenClaw and Codex harness adapters are implemented. A Hermes harness should
subclass `HarnessAdapter` and register a builder with `HarnessAdapterRegistry`;
core planning, policy, prompts, and model-attempt auditing do not change. The
registry validates the returned object before a model invocation begins, so a
malformed packaged harness fails at construction rather than during a live cycle.
`HarnessConfig.kind` is intentionally open to extension-owned names; an unknown
kind is still unusable until a matching registry builder is installed.

Direct model admission also accepts an optional runtime telemetry synchronizer.
The OpenClaw CLI supplies one backed by `openclaw sessions`; it reconciles the
previous call before the next direct Captain call is admitted and suppresses the call
when the telemetry endpoint is degraded. A Hermes or standalone Codex runtime
can provide the same callback from its native usage ledger without importing
OpenClaw code into the engine.

## Work Queue Adapter

`WorkQueueAdapter` maps the portable workflow DAG onto a runtime's task primitives. It receives `QueueCardSpec` records containing stable idempotency keys, parent card IDs, assigned roles, isolated workspace references, retry limits, source links, and proof requirements.

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

Final-review cards must include a passed marker anchored to the reviewed head:
`READY_FOR_OWNER:<head-sha>`, `CONTROL_PLANE_COMPLETE:<head-sha>`, or
`AUTO_MERGE_ALLOWED:<head-sha>` according to repository policy. Generic
successful prose is not completion proof. The workflow must also carry the exact
GitHub PR URL in implementation or repair proof. Runtime configurations require
a `CompletionValidator` by default. CAPTAINS_CHAIR re-reads the live PR before treating
the workflow as complete: the head, checks, mergeability, and unresolved review
threads must still match the final-review evidence. A portable adapter may
explicitly set `require_live_completion_validation: false` for deterministic
conformance tests, but that boundary must never be used for unattended
repository operation. If a runtime preserves multiple passed proof records, the
latest passed record must carry the policy marker and is authoritative; a newer
passed record without that marker invalidates older reviewed SHAs.

OpenClaw maps these operations only to the built-in Workboard Gateway RPC and
its worker sessions. A future Hermes adapter may map them to Hermes tasks and
worker sessions. A standalone Codex adapter may use SQLite queue rows plus
disposable `codex exec` sessions. Neither adapter may move merge policy,
blocker classification, workflow topology, GitHub gates, or proof validation
out of CAPTAINS_CHAIR core.

Queue construction is centralized in `captains_chair.runtime.build_work_queue_adapter`
and `build_work_queue_orchestrator`. The default registry contains OpenClaw;
an integration can register a Hermes or standalone Codex builder with
`RuntimeAdapterRegistry` (or `register_work_queue_adapter`) without changing
the workflow engine or policy code. The adapter must implement the same
`WorkQueueAdapter` and `WorkerLifecycleAdapter` contracts. The registry validates
both surfaces when the adapter is constructed, so missing queue or claimed-worker
operations fail before a live workflow starts. Its conformance fixtures should
remain unchanged.

Packaged adapters may expose a callable registrar through the entry-point groups
`captains_chair.runtime_adapters`, `captains_chair.harness_adapters`, and
`captains_chair.scheduler_adapters`. The registrar receives the
corresponding registry and calls `register(...)`; CAPTAINS_CHAIR discovers these plugins
only when building the default registry, loads each plugin once, and fails closed
if loading or registration fails. OpenClaw remains built in, so the core package
does not require any future runtime package to be installed.

Notifier packages use the same boundary through `NotifierAdapterRegistry` and the
`captains_chair.notifier_adapters` entry-point group. Built-in `stdout`, OpenClaw Discord,
and Discord webhook delivery remain unchanged. An extension may use
`NotificationConfig.settings` for its own validated options; an unregistered
notification kind fails closed instead of being guessed as a webhook.

Schedulers use the same registry boundary through `SchedulerAdapterRegistry` and
the `captains_chair.scheduler_adapters` entry-point group. OpenClaw cron, system cron,
systemd, and Windows Task Scheduler remain built in. A future Hermes or hosted
Codex scheduler can register its own kind and reuse the same `ScheduleSpec`; the
CLI no longer needs a new hard-coded branch for that runtime. Render-only
behavior is reserved for the built-in portable renderers; registered runtime
schedulers implement `install(spec)` and must preserve idempotency and the
requested enabled state.

For example, a future adapter package can publish these entry points:

```toml
[project.entry-points."captains_chair.runtime_adapters"]
hermes = "captains_chair_hermes:register_runtime"

[project.entry-points."captains_chair.harness_adapters"]
hermes = "captains_chair_hermes:register_harness"

[project.entry-points."captains_chair.notifier_adapters"]
hermes = "captains_chair_hermes:register_notifier"

[project.entry-points."captains_chair.scheduler_adapters"]
hermes = "captains_chair_hermes:register_scheduler"
```

Each registrar receives its registry and registers only the adapter builder;
the shared `WorkflowOrchestrator`, policy engine, GitHub provider, and proof
validators remain in CAPTAINS_CHAIR.

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

## Runtime Mapping

| Surface | OpenClaw V1 | Future Hermes | Standalone Codex |
| --- | --- | --- | --- |
| Harness | `openclaw agent --json` | Hermes session/task runner | `codex exec --json` |
| Queue | Built-in Workboard Gateway RPC | Hermes task board/session leases | SQLite queue with worker processes |
| GitHub | `GhGitHubProvider` | Same provider or App client | Same provider or App client |
| Notifications | OpenClaw Discord route | Hermes/Discord adapter | Discord webhook/stdout |
| Scheduler | OpenClaw cron | Hermes scheduler | cron/systemd/Task Scheduler |

The installed `captains_chair.conformance` module exposes the runtime-neutral workflow
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

Runtime configuration should inherit `WorkerOrchestrationConfig`, then add only runtime-specific connection and dispatch fields. The schema already reserves typed `hermes_workboard` and `codex_workboard` entries, but the CLI fails clearly if either is selected until its adapter is installed. The worker roles and retry policy remain common:

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
