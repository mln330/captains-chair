# OpenClaw Operations Runbook

This is the V1 operating sequence for CAPTAINS_CHAIR on OpenClaw. Repository plans and
requirements stay in the managed repository. CAPTAINS_CHAIR state, queue evidence, usage
records, and leases stay in the configured state directory. OpenClaw Workboard
is the preferred P0 worker tracker and orchestration adapter, but the core can
fall back to `DirectOrchestrator` with no board.

## Pause Guarantee

Set the managed repository to `operation_mode: disabled` before pausing work. Disabled
mode returns before model calls, GitHub mutations, Workboard mutations,
dispatch, reconciliation, live canary creation, and merge-gate mutation. The
read-only commands `status`, `usage report`, `orchestrate health`, and
`orchestrate preflight` remain available.

```bash
captains_chair --config "$CAPTAINS_CHAIR_CONFIG" status --repo OWNER/REPO
captains_chair --config "$CAPTAINS_CHAIR_CONFIG" usage report --repo OWNER/REPO --since 2026-01-01T00:00:00Z
captains_chair --config "$CAPTAINS_CHAIR_CONFIG" orchestrate health --repo OWNER/REPO
captains_chair --config "$CAPTAINS_CHAIR_CONFIG" orchestrate preflight --repo OWNER/REPO
```

The preflight may report preserved queue findings while the repository is
disabled. Those findings are evidence, not permission to dispatch. Do not
delete, complete, or rewrite old cards merely to make the dashboard green;
inspect their source links and preserve migration evidence first.

Advisory mode is also read-only for project execution. It permits baselines,
checks, status, planning, and recommendations, but dispatch, reconciliation,
worker lifecycle changes, live canaries, PR recovery, and merge requests return
before constructing a runtime or provider. Promote to `supervised` or
`autonomous` only after reviewing the proposed action and evidence.

Every cycle and deep-baseline command takes the repository lease before doing
expensive evidence collection or model work. If another pass is active, the
command reports `busy` and exits successfully; the next scheduled pass retries
without duplicating model calls.

## Greenfield Onboarding

Use **Create from the Chair** in the dashboard for a repository that does not exist
yet. This creates a local readiness review only. It does not call GitHub, create a
remote, or push source. The owner answers the course questions and explicitly engages
the course; only then does `course.approve` seed the README, implementation plan, and
`.captains-chair/project.yaml`, initialize a committed local source, and ask the GitHub
provider to create and verify the remote repository. A failed provider call is reported
as a greenfield provisioning error and the course remains unengaged for retry or repair.

## Resume Gates

Resume one repository at a time. The following sequence is the minimum gate
before unattended work:

1. Reconcile usage metadata and inspect `token_hotspots`, unknown telemetry,
   stale aggregate totals, route mismatches, failed attempts, repeated prompts,
   and large-context warnings. Configure authoritative daily or per-model token
   limits before autonomous mode when the runtime exposes complete telemetry.
2. Confirm `orchestrate health` reports every configured worker model correctly and
   `orchestrate preflight` confirms the configured worker-protocol helper can start.
   This check does not invoke a model. Run `model-check` separately only when
   usage is approved because it performs a real harness call.
3. Set the repository to `advisory` and run a deep baseline. Review the
   repository-owned plan and the resulting evidence before allowing mutations.
4. Run three shadow cycles. Shadow cycles may inspect and plan but must not
   create branches, PRs, comments, issues, or Workboard worker sessions.
5. Run the no-repository-mutation Workboard canary. The plan phase is safe;
   `--run` dispatches a real worker and consumes runtime usage.

```bash
captains_chair --config "$CAPTAINS_CHAIR_CONFIG" orchestrate canary --repo OWNER/REPO
captains_chair --config "$CAPTAINS_CHAIR_CONFIG" orchestrate canary --repo OWNER/REPO --run
captains_chair --config "$CAPTAINS_CHAIR_CONFIG" orchestrate canary --repo OWNER/REPO --check
```

6. Run one supervised documentation task and one supervised implementation
   task. Confirm isolated workspaces, direct PR links, independent review,
   targeted checks, repair handling, and useful Discord summaries.
7. Promote only that repository to `autonomous`, run one bounded implementation
   PR with the configured completion policy, and verify the actual merge and
   post-merge default-branch checks.
8. Install or restore the two-hour schedule only after the canary and bounded
   autonomous PR pass. Keep worker reconciliation frequent enough to claim
   ready cards; the Captain cycle is for planning and state review.

## Existing Queue Migration

When adopting CAPTAINS_CHAIR over an older OpenClaw queue, preserve existing branches,
PRs, and cards as evidence. A stale card is not proof that its work is safe to
repeat. Before resuming:

- map every ready, running, blocked, and done card to its GitHub issue or PR;
- identify cards with missing proof, expired claims, or ended sessions;
- let reconciliation reclaim technical failures into fresh retry cards;
- reserve `unblock` for a confirmed owner blocker, never for a technical
  failure;
- keep preserved migration PRs explicitly protected by repository policy;
- dispatch only after current worker-model health and usage admission pass.

Technical failures should become bounded retries or Captain recovery. Only
`USER_SECRET:`, `GOAL_DIVERGENCE:`, `EXTERNAL_ACCESS:`, and
`HIGH_RISK_DECISION:` are owner-attention categories. A user-blocked card must
not suppress unrelated ready work.

## Runtime Portability

OpenClaw maps this sequence to built-in Workboard Gateway calls when enabled;
otherwise it uses the board-free direct adapter. Direct Codex and
future adapters must implement the same orchestration and claimed-worker
contracts, then reuse the shared conformance scenario. They must not copy
OpenClaw policy into the adapter or change workflow, GitHub gates, blocker
classification, proof validation, or usage accounting.

Before enabling a future adapter, run its contract tests and the full shared
workflow scenario, including technical recovery, owner-blocker isolation,
simultaneous blockers, restart recovery, and current-head merge proof.
