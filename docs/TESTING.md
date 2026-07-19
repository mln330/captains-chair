# Testing Strategy

MAKE_IT_SO tests are organized around evidence, not implementation shape.

## Unit Tests

- every Number 1 mode and completion policy
- every approval invariant and high-risk action
- DAG topology and role separation
- no autonomous merge cards under owner-only policies
- explicit user-blocker classification
- technical retry and Number 1 recovery routing
- review-failure repair creation and gate rerun
- unrelated dispatch while another card is blocked
- OpenClaw RPC payloads, noisy output, malformed output, and permission failures
- a process-level fake OpenClaw executable covering JSON-RPC persistence, dispatch,
  completion proof, and model-health checks without invoking a model
- a process-level fake Codex executable covering `codex exec` structured output,
  read-only/workspace-write sandbox selection, model provenance, and usage capture
- preflight validation of the exact worker lifecycle helper command used by agents
- configuration schema and unknown-field rejection
- state transitions, leases, worktree containment, and secret scanning
- failed direct implementation/repair cleanup force-discards only the local worktree,
  while successful PR creation remains resumable when clean removal needs fallback
- UX review worker failures preserve the original error while discarding the disposable
  review worktree, and successful UX cleanup warnings remain visible in review evidence
- OpenClaw worker recovery recognizes failed, crashed, aborted, and killed terminal
  sessions as retryable missing-proof work rather than leaving cards running
- a real disposable Git repository with parallel implementation/docs worktrees
- a real disposable Git repository workflow that pushes an implementation branch,
  fast-forwards the default branch, and verifies the resulting merge commit
- workspace references round-trip through OpenClaw cards and survive retry/repair creation
- empty courses cannot be approved, and cyclic work-package graphs are rejected
- planning-session handoffs preserve unresolved questions and the course-approval mutation gate
- greenfield repository registration remains local-only until course approval, then seeds
  and commits the plan/manifest before the GitHub provider creates and verifies the remote

## Integration Tests

- idempotent workflow enqueue after retry or crash
- dependency keys resolved to runtime card IDs
- implementation completion promotes independent gates
- failed independent gate creates repair work
- completed repair reopens the original gate
- stale/current-head review behavior
- Workboard capacity waits, changed-capacity replanning, and owner-blocker isolation
- simultaneous owner and technical blockers while unrelated work continues
- completed workflow worktree cleanup, dirty-worktree refusal, failed direct-worktree
  recovery, and cleanup failure isolation
- pending and failing CI
- autonomous merge and post-merge verification
- notification delivery failure, provenance preservation, and degraded scheduler exit
- restart with persisted MAKE_IT_SO state and runtime queue state; a fresh orchestrator must
  promote completed work's dependent gates without creating duplicate cards
- negative runtime conformance: an adapter that promotes dependent cards before
  their parent completes must fail the shared contract scenario

## Disposable End-To-End Tests

Each supported production runtime must eventually pass the same conformance scenario in a disposable GitHub repository:

1. create an issue from a documented gap
2. enqueue and dispatch a worker workflow
3. implement in an isolated worktree
4. open a PR
5. run independent review and checks
6. repair an injected review failure
7. rerun current-head evidence
8. auto-merge under policy
9. verify the actual default-branch merge commit
10. prove that technical recovery, a separate user-blocked card, and their coexistence do not stop unblocked work

The OpenClaw plugin CI job also installs the built plugin into an isolated OpenClaw
profile, inspects the live registration, runs plugin doctor, and removes that profile.
OpenClaw is the required P0 runtime implementation and Codex is the P1 host
boundary. The Codex plugin delegates to the frozen Python core through its MCP
bridge and uses `DirectOrchestrator` without requiring a task board. Both must
pass the adapter contract tests; future runtimes should reuse the same core
fixtures and conformance suite without importing runtime-specific assumptions
into the core.

CI enforces at least 85% package-wide line coverage and a branch-coverage gate
for the portable policy/orchestration/state/runtime boundary. New adapter code
must add its own failure-path tests without lowering either gate. The product
target remains 90% line and 85% branch coverage; the gap is reported explicitly
by the verification job rather than hidden by synthetic exclusions. Linux CI also
runs a focused mutation suite over model routing, merge gates, policy, state, and
orchestration. Native Windows mutation runs are reported as unsupported by mutmut;
the normal unit and integration suite remains required on Windows.

## Current Verification Status

The local implementation currently verifies 832 Python tests, OpenClaw plugin
typecheck/tests/build, repository QA and token-safeguard dashboard controls, explicit
intelligence-level and UI-acceptance dashboard controls, four Playwright dashboard
tests across desktop and mobile Chromium with keyboard, accessibility, and visual
snapshot checks, Codex plugin manifest validation, Python package build,
zero high-severity frontend audit findings, and an isolated OpenClaw host
registration check.

The full July 18 verification run reached 93.07% lines and 85.33% branches,
meeting the product target. Broader Playwright accessibility and visual coverage and
the live PrintHub canary remain explicit operational acceptance work; passing local
coverage never substitutes for a clean host-side canary.
Mutation testing is configured for Linux CI and remains environment-limited on
this Windows development host; disposable fixtures must pass before any live run
is treated as validated.

## Live Canary Gates

PrintHub may use the Workboard runtime only after:

- all unit and static checks pass
- `make_it_so orchestrate preflight` reports ready without starting a model or worker
- `make_it_so orchestrate canary --run` dispatches a no-op Workboard card, and `--check` verifies claim/completion proof
- a supervised code card opens a valid PR
- independent review and repair are visibly separate workers
- one bounded autonomous PR merges and passes post-merge verification
- Discord summaries identify work completed, proof, current blocker, and next action
- no routine technical failure asks the owner for approval
