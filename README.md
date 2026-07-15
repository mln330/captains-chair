# Captain's Chair

_Set the course. Engage the crew._

[![Python 3.11-3.13](https://img.shields.io/badge/python-3.11--3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![OpenClaw P0](https://img.shields.io/badge/OpenClaw-P0-0B7285)](https://docs.openclaw.ai/plugins/sdk-overview)
[![Codex P1](https://img.shields.io/badge/Codex-P1-5B5BD6)](https://developers.openai.com/codex/noninteractive)
[![License Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-D22128)](LICENSE)

![Captain's Chair command deck](docs/assets/captains-chair-hero.png)

Captain's Chair is an open-source, harness-neutral SDLC control plane that puts the builder in command of an agent crew. It separates deterministic state, policy, GitHub operations, workflow DAGs, worktrees, and evidence gates from the runtime that queues and executes workers.

OpenClaw Workboard is the P0 worker runtime. The built-in SQLite `DirectOrchestrator` provides a board-free path for Codex and portable integrations, while extension-owned adapter kinds support additional harnesses without moving GitHub policy or workflow logic into them. Repository requirements and implementation plans remain in each managed repository. Captain's Chair stores leases, event history, model provenance, baselines, and run artifacts outside those repositories.

## Safety model

- Advisory mode inspects and recommends only.
- Supervised mode requires exact approval for each mutation.
- Autonomous mode may execute validated repository workflows.
- Only `AUTO_MERGE_ALLOWED` can authorize autonomous merge.
- Every code or documentation task starts in an isolated worktree from current `origin/main`.
- Coder, reviewer, tester, UX reviewer, final reviewer, merger, and verifier are role-separated workers.
- Technical blockers retry or route to Captain recovery while unrelated ready work continues.
- Only explicitly tagged secrets, access, goal divergence, and high-risk decisions require owner intervention.
- Provider, model, notification, and check failures produce blocked or degraded health instead of empty success.
- Unchanged planner inputs do not trigger repeated model calls; `--force-replan` is an explicit recovery override.

## Quick start

1. Install with `python -m pip install -e ".[dev]"`.
2. Copy `config/config.example.yaml` outside the repository and configure local paths and harnesses.
3. Add `.captains-chair/project.yaml` to a managed repository using `examples/project.yaml` as a starting point.
4. Run `captains-chair --config /path/to/config.yaml doctor`.
5. Run `captains-chair --config /path/to/config.yaml baseline --repo OWNER/REPO --harness openclaw --analyze --run-checks`.
6. Run three `shadow-canary` cycles before enabling a live cycle.

## Commands

- `captains-chair schema` writes the strict configuration JSON Schema.
- `captains-chair doctor` validates configuration, authentication, paths, and harness executables.
- `captains-chair model-check` validates the configured model, harness route, exact JSON schema, and fallback provenance.
- The OpenClaw dashboard validates edited model routes before saving and flags routes whose runtime capability is still unverified; use `model-check` before autonomous promotion.
- `captains-chair baseline` collects GitHub state, all canonical docs, source/dependency inventory, CI, tests, branches, worktrees, and configured checks.
- `captains-chair cycle` runs one bounded state-machine transition in shadow or live mode.
- `captains-chair cycle --continue-run --live` chains safe immediate transitions for up to six steps or 30 minutes.
- `captains-chair cycle --watch --live` advances only active PR and post-merge work without selecting new tasks.
- `captains-chair shadow-canary` runs repeated non-mutating cycles.
- `captains-chair status` reports state and recent events from SQLite.
- `captains-chair usage report` shows provider-reported tokens, fallback attempts, and telemetry quality.
- `captains-chair usage sync-openclaw` imports metadata-only OpenClaw worker session usage by repository.
- `captains-chair approve` records approval for one exact supervised action ID.
- `captains-chair schedule` installs an OpenClaw command cron or renders system cron, systemd, and Windows Task Scheduler definitions. Schedules are disabled and shadow-only by default.
- `captains-chair runtime-install` plans or installs isolated OpenClaw worker agents and their role protocols.
- `captains-chair orchestrate status|dispatch|reconcile` operates the configured runtime queue.
- `captains-chair orchestrate health` checks configured worker-agent model routes without invoking a model.
- `captains-chair orchestrate preflight` checks the adapter, model routes, Workboard, queue, and usage guard without dispatching workers.
- `captains-chair orchestrate canary` plans, explicitly dispatches, or checks a no-repository-mutation Workboard runtime canary.

With a Workboard orchestrator configured, the Captain cycle enqueues a policy-approved worker DAG instead of executing the implementation itself. Run reconciliation/dispatch frequently so ready cards are claimed promptly, and keep the slower Captain schedule for repository review and queue replenishment. Frontend-impacting PRs receive a dedicated UX worker covering flows, contrast, responsive behavior, accessibility, and visual cohesion before final Captain review.

See `docs/ARCHITECTURE.md` and `docs/TESTING.md` for runtime ownership and conformance requirements.
See `docs/PRODUCT_REORIENTATION.md` for the approved OpenClaw-first product direction and delivery plan.
See `docs/ADAPTERS.md` for the GitHub provider, harness, orchestration, tracking, notification, and scheduler boundaries used by Codex and future adapters.
See `docs/FUTURE_RUNTIME_ADAPTERS.md` for the implementation checklist and verification contract for those future runtimes.
See `docs/OPENCLAW_OPERATIONS.md` for the pause, resume, migration, canary, and autonomous-promotion runbook.
Runtime authors can run `captains_chair.conformance.run_runtime_conformance` against a
new queue adapter before adding runtime-specific integration tests.
Adapter packages can register queue, harness, and notifier builders through the
documented `captains_chair.runtime_adapters`, `captains_chair.harness_adapters`, and
`captains_chair.notifier_adapters` entry-point groups.

The CLI returns 0 for healthy/progress states, 2 for blocked or degraded states, and 3 for execution failures.

## The operating model

Captain's Chair gives the builder a durable course, a bounded crew, and evidence
gates around every meaningful transition. The repository owns goals, plans,
acceptance criteria, and checks. Captain's Chair owns leases, events, model
provenance, schedules, and operational history. The host runtime only supplies
workers and delivery surfaces through adapters.

```mermaid
flowchart LR
    human[Builder in the Chair] --> course[Course and exit criteria]
    course --> readiness[Readiness review]
    readiness -->|approved| packages[Dependency-aware work packages]
    packages --> workers[Role-separated crew]
    workers --> pr[Pull request]
    pr --> review[Independent review and QA]
    review -->|repair needed| workers
    review --> gate[Final evidence gate]
    gate -->|policy allows| merge[Merge and post-merge verification]
    gate -->|owner decision| human
    merge --> status[Useful summary and durable event history]
```

The OpenClaw plugin provides the P0 dashboard tab, sidecar, Gateway methods,
and idempotent schedules. Workboard is the preferred OpenClaw worker adapter,
but it is optional. When no board is configured, the same workflow contract is
backed by the SQLite `DirectOrchestrator`.

```mermaid
flowchart TB
    ui[OpenClaw dashboard or standalone UI]
    ui --> sidecar[Versioned JSON-RPC sidecar]
    sidecar --> core[Python control-plane core]
    core --> github[GitHub adapter]
    core --> state[(SQLite operational state)]
    core --> work[WorkerOrchestratorAdapter]
    work --> openclaw[OpenClaw Workboard]
    work --> direct[DirectOrchestrator]
    core --> harness[HarnessAdapter]
    harness --> openclaw_h[OpenClaw]
    harness --> codex[Codex P1]
    core --> notify[NotifierAdapter]
```

## Why this is different from `/goal`

`/goal` is useful for an interactive, single-threaded coding session. Captain's
Chair is for a repository or portfolio that must keep moving between sessions:

- a course preserves the approved goal, scope, prerequisites, checkpoints, and
  exit criteria;
- each work package gets a fresh context package instead of inheriting an
  unbounded conversation;
- implementation, review, QA, repair, final review, merge, and verification are
  separate auditable stages;
- unchanged evidence suppresses duplicate planning calls and repeated no-progress
  loops;
- every model attempt records requested and resolved model, role, stage, and
  provider-reported token counts;
- expensive models are reserved for planning, independent review, and final
  decisions, while routine coding and deterministic checks can use cheaper or
  local workers.

There is no synthetic billing calculation. Use `captains-chair usage report` to
inspect tokens by model, incomplete telemetry, fallback attempts,
failed calls, repeated prompt fingerprints, and high-consumption workflow stages.

## OpenClaw installation

Build the plugin and install it into the OpenClaw plugin directory from
`openclaw-plugin/`. Configure the Python sidecar path and a config file in the
OpenClaw host, then use the Captain's Chair tab to register repositories and
courses. The plugin reconciles the five-minute worker schedule and two-hour
course-review schedule without creating duplicate cron jobs. Schedule commands
are verified before an existing job is reused.

```text
cd openclaw-plugin
npm ci
npm run check
npm test
npm run build
```

The first course remains in readiness review until the builder approves it.
Autonomy and merge policy are configured separately, so a course can continue
safe independent work while a dependency-scoped checkpoint waits for a human.
Planning is hybrid: use the dashboard's planning brief or ask the native host
conversation for `/captains-chair plan OWNER/REPO COURSE_KEY` in OpenClaw (or the
equivalent Codex MCP tool). The handoff is deterministic and token-free; only
the host model's questions and readiness answers consume model usage.

For a brand-new repository, **Create from the Chair** records the course first. Remote
repository creation is deliberately held until readiness is complete and the owner
engages the course, so a half-formed idea cannot silently become a public repository.

## Codex P1 host

`codex-plugin/captains-chair/` is the Codex boundary. Its MCP bridge exposes
doctor, baseline, status, bounded-cycle, and token-report tools, while the
`DirectOrchestrator` keeps workflow state durable without requiring a kanban
board. The plugin's `serve_ui.py` hosts the same React build used by OpenClaw
and proxies its API to the Python sidecar. Codex-specific process details stay
inside this plugin; the core remains usable by another harness adapter.
