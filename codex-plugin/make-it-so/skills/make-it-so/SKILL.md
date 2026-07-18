---
name: make-it-so
description: Use Make It So to baseline repositories, inspect course readiness, and advance policy-gated SDLC work through Codex.
---

# Make It So for Codex

Make It So is the deterministic control plane. Use its CLI or MCP tools for
state, policy, GitHub evidence, course readiness, work-package transitions, and
token telemetry. Do not recreate the state machine in conversation.

## Operating sequence

1. Run `make-it-so doctor` before first use or after configuration changes.
2. Run a deep baseline before engaging a repository course.
3. Review readiness requirements, exit criteria, permissions, secrets, and
   checkpoints with the builder.
4. Approve the course before repository mutation begins.
5. Use shadow cycles and bounded live cycles; every cycle must leave durable
   evidence and a short actionable summary.
6. Use `make-it-so usage report` to inspect provider-reported tokens by model,
   role, and stage.

The Codex host uses `DirectOrchestrator` by default and may add a custom tracker
adapter later. A board is never required. Implementation uses workspace-write
isolation; planning, review, and QA use read-only contexts unless the configured
adapter explicitly supplies a stricter boundary.

Never bypass a policy result, treat a narrative as completion proof, or interpret
`READY_FOR_OWNER` as autonomous merge permission. Protected actions remain
approval-gated.
