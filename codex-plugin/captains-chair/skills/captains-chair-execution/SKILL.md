---
name: captains-chair-execution
description: Execute Captain's Chair work packages through the runtime-neutral direct worker lifecycle in Codex.
---

# Captain's Chair Execution

Use `captains_chair_ready_work` and `captains_chair_worker_discover` to select only
dependency-ready work. A kanban board is not required.

1. Claim one ready card with a unique owner ID and opaque claim token.
2. Work only in the supplied workspace and within the card's acceptance scope.
3. Heartbeat meaningful progress; do not use heartbeats to conceal stalled work.
4. Complete with a concise summary and verifiable proof, or block with a precise
   technical or owner reason.
5. Let Captain's Chair dispatch independent review, QA, repair, final review,
   merge policy, and post-merge verification. A coder cannot supply its own
   review evidence.

Never bypass course state, approval mode, current-head review, required checks,
or protected-action gates.
