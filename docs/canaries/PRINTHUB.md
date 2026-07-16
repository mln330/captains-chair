# PrintHub Canary Evidence

Status: **pending prerequisite PR merge and OpenClaw deployment**

Repository: `NewmanZone/PrintHub`

## Entry Gates

- PRs #9 through #13 are merged and their CI is green.
- The packaged OpenClaw plugin reports a compatible sidecar version.
- PrintHub has a current takeover course and reviewed readiness record.
- The selected package is bounded, reversible, and excludes release, secrets,
  billing, force-push, destructive operations, and production deployment.
- Token limits and model routes are visible before dispatch.

## Supervised Canary

Record the course/package, proposed action, owner approval, worker claim IDs, PR,
current-head independent review, checks, repair evidence, final verdict, merge actor,
post-merge verification, token totals by model, and Discord transition messages.

## Bounded Autonomous Canary

Select a separate low-risk package. Record the same evidence and verify that no
per-action approval was requested, only `AUTO_MERGE_ALLOWED:<head-sha>` authorized
merge, and the post-merge verifier completed on the merged default-branch SHA.

## Pass Criteria

- One PrintHub package completes from planning through post-merge verification.
- No identical no-progress transition repeats.
- Work runs in isolated worktrees and no shared checkout changes branch.
- Model provenance and token counts are present for every model call.
- Discord summaries state what changed, why, proof, link, and next action concisely.
- Any Azure deployment failure is reported as a separate release blocker unless the
  PrintHub policy explicitly makes deployment a merge gate.

## Evidence Record

Replace this section after each canary. Do not mark the canary passed from simulated
or disposable-repository evidence.

```yaml
run_id: pending
mode: supervised-or-autonomous
course: pending
work_package: pending
started_at: pending
completed_at: pending
pull_request: pending
base_sha: pending
head_sha: pending
merge_sha: pending
post_merge_event: pending
review_evidence: pending
check_evidence: pending
qa_evidence: pending
token_summary: pending
discord_event_ids: pending
result: pending
```
