# Architecture

Captain's Chair separates SDLC control policy from the runtime that executes worker stages.

## Ownership

- Managed repositories own product truth: goals, requirements, architecture, plans, code, tests, issues, and pull requests.
- CAPTAINS_CHAIR core owns deterministic policy, workflow DAG construction, GitHub gates, worktree safety, evidence validation, and portable event/state records.
- Runtime adapters own queue persistence, worker launch, claims, heartbeats, retries, and runtime-specific observability.
- GitHub remains the durable external work contract. A runtime queue is an operating view, not a replacement for issues or repository plans.

## Portable Boundary

`WorkerOrchestratorAdapter` is the runtime boundary; `WorkQueueAdapter` remains a compatibility alias. `WorkTrackerAdapter` is an optional external-project mirror and `NullWorkTracker` keeps tracking disabled. `OrchestrationPolicy` is the structural policy boundary, and `WorkerOrchestrationConfig` provides a reusable typed configuration base. The core emits `WorkflowSpec` and `QueueCardSpec` values with stable idempotency keys, role assignments, dependencies, workspace metadata, retry budgets, and proof requirements.

`WorkerLifecycleAdapter` is the separate claimed-worker boundary. It standardizes
heartbeats, passed proof, and explicit blockers while leaving claim acquisition
and lease storage to the runtime.

OpenClaw maps this contract to the bundled Workboard Gateway RPC API. `DirectOrchestrator` maps it to durable SQLite workflow state without a board. Future integrations use the generic adapter registry without changing workflow construction, policy, GitHub gates, or blocker rules.

Course-scoped workflow cards carry `courseKey` and `workPackageKey` metadata. The
durable course file remains canonical for package status; Workboard or a direct
runtime is only the worker execution view. Queue admission moves an approved
package to `executing`, review moves it to `reviewing`, and successful completion
or post-merge verification moves it to `complete`.

Model profiles resolve in this order: global/runtime policy, repository profile,
engaged course profile, selected work-package profile, then stage profile. Stage
profiles use the explicit `stage:<stage-name>` key (for example,
`stage:implementation`), which keeps provider names out of workflow code while
allowing a course to route implementation and review differently.

Runtime adapters must provide:

- board/project creation
- idempotent card creation
- dependency-aware status progression
- worker dispatch
- completion proof and comments
- unblock, reclaim, and Captain recovery routing
- diagnostics

Queue cards preserve the `WorkspaceRef` supplied by the core. Retry and repair cards
must carry that same isolated workspace (or an adapter-created equivalent) rather than
falling back to a board's shared default checkout. For a repair checkout, the local
CAPTAINS_CHAIR branch and the original PR push branch are separate fields so a worker can commit
safely in isolation and update the intended PR without guessing branch identity.

The queue remains the source of worker truth. After every card in a workflow has
completed with proof, the host-provided workspace cleaner may remove the clean local
worktree. This is idempotent and never deletes the remote CAPTAINS_CHAIR branch or pull request;
inconsistent references or dirty worktrees are reported as technical health events and
do not suppress unrelated Workboard dispatch.

Issue reconciliation is also a first-class deterministic boundary. The planner can
create, update, add labels to, retarget, or close a GitHub issue through the portable
`GitHubProvider`; the engine applies the same operation mode and approval policy before any
mutation. Runtime adapters only queue non-direct work and do not implement issue
semantics themselves.

Planning uses the managed repository's current default branch as its durable source of
truth. A clean checkout is fast-forwarded before a cycle reads the planning document;
if the checkout is dirty, on another branch, or cannot be synchronized, CAPTAINS_CHAIR reads the
document from `origin/<default-branch>` instead. If that remote document cannot be
read from a real Git checkout, the cycle records degraded planning context and makes
no model call. This prevents stale local plans from silently driving new work.

Greenfield onboarding is an approval-gated exception to the normal existing-repository
flow. The dashboard's `repo.create` action records a local course intent and readiness
questions without calling GitHub. After the course is approved, the GitHub adapter seeds
the README, durable implementation plan, and `.captains-chair/project.yaml`, initializes
and commits the local source, then invokes the provider's repository-provisioning method.
The core depends only on that provider method; GitHub CLI flags, visibility, and remote
creation remain outside the workflow engine.

## OpenClaw Runtime

OpenClaw Workboard is the production V1 orchestrator. CAPTAINS_CHAIR creates a parent work contract and role-separated child cards. Workboard dispatch starts isolated agents and supplies bounded card context, claim tokens, parent results, workspace metadata, and retry limits.

The default implementation workflow is:

1. implementation
2. independent review, test/CI, and optional UX review
3. Captain final review
4. policy-gated merge
5. post-merge verification

When an active PR enters Workboard review, CAPTAINS_CHAIR creates a fresh isolated checkout
of the exact PR head and passes that `WorkspaceRef` to the reviewer, tester, UX,
final-review, and any repair cards. The local review branch is disposable; the
original PR branch remains the only push target. Merge and post-merge cards do not
inherit that PR branch workspace, so their gates use live GitHub/default-branch
evidence instead.

The direct UX-review fallback follows the same disposable-worktree rule. A failed
browser or model invocation force-discards only the local review checkout while
preserving the original error for retry classification. A successful review remains
valid when clean local removal needs a force-discard fallback; that cleanup warning
is retained in the final review evidence.

The coder cannot satisfy review or final-review dependencies. Auto-merge workflows are not created unless repository completion policy is `auto_merge`.
Final-review completion is policy-specific and requires a passed current-head
marker; a generic worker success message cannot promote a workflow to completion.
Configuration also rejects duplicate worker agent IDs, so coder, reviewer, tester,
final reviewer, merger, and verifier identities cannot collapse into one runtime
principal by accident.

## Blockers And Recovery

Only four explicit blocker prefixes are treated as requiring owner input:

- `USER_SECRET:`
- `GOAL_DIVERGENCE:`
- `EXTERNAL_ACCESS:`
- `HIGH_RISK_DECISION:`

Planner decisions use the same contract: `requires_owner_approval` is not enough
to page the owner by itself. A planner-requested approval must include an
`owner_blocker` with one of the prefixes above. A bare or malformed approval
request is treated as a technical planning failure and can be replanned
autonomously.

Sensitive direct actions such as merge, release, deployment, secrets, billing,
destructive changes, force-push, and branch deletion remain owner-gated unless
the exact action is explicitly approved or a validated repository whitelist
authorizes it. Routine autonomous implementation and repair do not require
approval merely because they eventually produce a PR.

An autonomous merge is never assigned to a model worker. Its Workboard card is
unassigned and the OpenClaw adapter executes it through the deterministic merge
gate. The gate requires current-head `AUTO_MERGE_ALLOWED` proof, green required
checks, clean mergeability, resolved blocking threads, autonomous repository
mode, and an explicit `auto_merge` policy before it calls GitHub.

The direct Captain final-review contract also has an `owner_blocker` field. It must use
one of those prefixes to move an autonomous PR to `blocked`; an unclassified final
review blocker is treated as technical repair work instead of silently paging the
owner. Ordinary review findings remain autonomous repair work.

Direct coding and repair workers use the same distinction: a structured worker
blocker is classified before the engine records the result. Owner-prefixed blockers
page the owner without marking the proposal executed; technical blockers become
degraded handling events and are eligible for the normal evidence-change retry
path. An owner-attention event is never treated as an autonomous proposal to resume.

Everything else is technical. Technical implementation failures are retried within the card budget. Review, test, UX, and final-review failures create coder repair cards, then rerun the independent gate. Exhausted retries route to the Captain recovery agent for autonomous replanning. Reconciliation always dispatches unrelated ready work.

Direct issue mutations follow the same autonomy rule without spending another
planner call: update, label, retarget, and close operations receive one bounded
automatic retry on the unchanged decision. Issue creation is deliberately not
replayed after an ambiguous provider failure, because the first request may have
created the issue before its response was lost. After the safe retry is exhausted,
the Captain reports a stalled technical state and waits for changed evidence or an
explicit forced replan.

Notifications follow the same boundary: approval requests and explicitly classified
owner blockers use the attention ladder; baseline, queue, worker, review, and deploy-
health failures are reported as `Captain HANDLING` with the automatic next action. A
technical failure must not look like a request for owner approval.
An owner-blocked Workboard card is checked on every reconciliation, not only when
its status first changes. It re-notifies at ladder levels 1, 2, 3, 4, 8, and 16;
an `captains_chair ack` acknowledgement resets that card's ladder so the next unresolved
decision starts at level 1. This keeps unattended blockers visible without sending
the same ping on every scheduled pass.
Queue reconciliation also reports automatic technical retries, repair-card creation,
and Captain-recovery routing as idempotent `Captain HANDLING` events, so autonomous progress is
visible even when no owner decision is needed. Event fingerprints include the card
and retry evidence; repeated reconciliation does not resend the same transition.
Notification delivery failures are persisted as linked `NOTIFICATION_FAILED` events,
move repository state to `degraded`, and return a nonzero scheduled-command result;
queue reconciliation still attempts every event in the batch so one broken route does
not hide other delivery failures.
If a queue/provider failure prevents reconciliation from producing a normal result,
the CLI persists a deduplicated `QUEUE_DEGRADED` event with the failure detail and
the recovery command, sends it through the configured route, and suppresses repeat
notifications until the failure evidence changes.

Notification failures remain visible in status and history without becoming new
work evidence: the next cycle reads the latest operational event beneath the
delivery failure. This prevents a Discord outage from replaying planner, review,
or post-merge model calls against unchanged GitHub state.

Active PR waiting evidence is fingerprinted from the current head, required
checks, mergeability, unresolved-thread count, and review-head SHA. A waiting
completion state is reused while that evidence is unchanged, so autonomous watch
cycles do not spend new reviewer calls without a new fact. A changed head or gate
evidence automatically permits a fresh review. Live completion validation is
cached only within one reconciliation pass; every later scheduled pass rereads
the current PR gate, so a PR that changes after final review cannot inherit stale
approval evidence.

Workboard admission also includes the configured `max_parallel_prs` capacity in
the planning fingerprint. A full queue waits without repeatedly replanning;
completed workflows reopen capacity. A workflow whose remaining work is fully
blocked on an explicit owner blocker does not consume that slot, so unrelated
unblocked work can continue. A mixed workflow with other active cards still
counts until those cards finish.

## State

CAPTAINS_CHAIR SQLite remains the portable audit and policy store. OpenClaw Workboard stores runtime-local cards, claims, attempts, worker logs, proof, and diagnostics. Stable workflow and card idempotency keys make reconciliation safe after crashes or duplicate scheduled runs. The `recover-pr` crash-after-push path is also idempotent: an already executed action with the same active PR returns its existing recovery state, while a mismatched or non-proposed action fails closed. Runtime configurations require a `CompletionValidator` by default; the OpenClaw adapter uses it to validate the exact PR URL, current head, checks, mergeability, and review threads against live GitHub state. Only explicitly marked portable conformance tests may disable that requirement.

OpenClaw V1 uses only OpenClaw's built-in Workboard Gateway as its queue and
worker orchestration layer. CAPTAINS_CHAIR does not depend on a custom task system or a
second OpenClaw task board. Future runtimes may map the same contract to their
own native task/session primitives, but core correctness cannot depend on any
runtime-specific grouping feature.

Every scheduled queue mutation (`dispatch`, `reconcile`, `unblock`, and live
canary creation) takes the repository's SQLite lease before it can call usage
sync, Workboard, or model-health code. If another scheduled pass owns that
lease, the command emits a concise `busy` result and exits successfully so the
next scheduled pass can retry. Worker card claims remain a separate Workboard
lease: worker heartbeats and completions can proceed while the Captain reconciler is
running.

See `docs/ADAPTERS.md` for the conformance path for Codex and future runtimes.
