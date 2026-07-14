# Testing Strategy

CAPTAINS_CHAIR tests are organized around evidence, not implementation shape.

## Unit Tests

- every Captain mode and completion policy
- every approval invariant and high-risk action
- DAG topology and role separation
- no autonomous merge cards under owner-only policies
- explicit user-blocker classification
- technical retry and Captain recovery routing
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
- restart with persisted CAPTAINS_CHAIR state and runtime queue state; a fresh orchestrator must
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

OpenClaw is the only required V1 runtime implementation. Hermes and standalone Codex adapters must pass adapter contract tests when added; they should reuse the same core fixtures and conformance suite.

CI enforces at least 75% package-wide coverage and at least 80% aggregate coverage across the portable policy/orchestration/state/runtime boundary. New adapter code must add its own failure-path tests without lowering either gate.

## Live Canary Gates

PrintHub may use the Workboard runtime only after:

- all unit and static checks pass
- `captains_chair orchestrate preflight` reports ready without starting a model or worker
- `captains_chair orchestrate canary --run` dispatches a no-op Workboard card, and `--check` verifies claim/completion proof
- a supervised code card opens a valid PR
- independent review and repair are visibly separate workers
- one bounded autonomous PR merges and passes post-merge verification
- Discord summaries identify work completed, proof, current blocker, and next action
- no routine technical failure asks the owner for approval
