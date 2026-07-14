# Captain's Chair

_Set the course. Engage the crew._

Captain's Chair is an open-source, harness-neutral SDLC control plane that puts the builder in command of an agent crew. It separates deterministic state, policy, GitHub operations, workflow DAGs, worktrees, and evidence gates from the runtime that queues and executes workers.

OpenClaw Workboard is the production V1 runtime. The portable queue contract is designed for later Hermes and standalone Codex adapters without moving GitHub policy or workflow logic into those runtimes. Repository requirements and implementation plans remain in each managed repository. CAPTAINS_CHAIR stores leases, event history, model provenance, baselines, and run artifacts outside those repositories.

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
- `captains-chair baseline` collects GitHub state, all canonical docs, source/dependency inventory, CI, tests, branches, worktrees, and configured checks.
- `captains-chair cycle` runs one bounded state-machine transition in shadow or live mode.
- `captains-chair cycle --continue-run --live` chains safe immediate transitions for up to six steps or 30 minutes.
- `captains-chair cycle --watch --live` advances only active PR and post-merge work without selecting new tasks.
- `captains-chair shadow-canary` runs repeated non-mutating cycles.
- `captains-chair status` reports state and recent events from SQLite.
- `captains-chair usage report` shows measured model usage, fallback attempts, and configured credit estimates.
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
See `docs/ADAPTERS.md` for the GitHub provider, harness, queue, notification, and scheduler boundaries used by future Hermes and standalone Codex adapters.
See `docs/FUTURE_RUNTIME_ADAPTERS.md` for the implementation checklist and verification contract for those future runtimes.
See `docs/OPENCLAW_OPERATIONS.md` for the pause, resume, migration, canary, and autonomous-promotion runbook.
Runtime authors can run `captains_chair.conformance.run_runtime_conformance` against a
new queue adapter before adding runtime-specific integration tests.
Adapter packages can register queue, harness, and notifier builders through the
documented `captains_chair.runtime_adapters`, `captains_chair.harness_adapters`, and
`captains_chair.notifier_adapters` entry-point groups.

The CLI returns 0 for healthy/progress states, 2 for blocked or degraded states, and 3 for execution failures.
