---
name: make-it-so
description: Baseline a GitHub repository, select its next work item, and drive policy-gated PR workflows through Make It So.
---

# Make It So

Use the Make It So CLI as the deterministic control plane. Do not recreate its state machine in chat.

1. Run `make-it-so doctor` before a first baseline or after configuration changes.
2. Run a deep baseline before enabling live cycles.
3. Use shadow cycles to validate decisions without mutations.
4. Use live cycles only after repository mode and completion policy are intentionally configured.
5. Treat MAKE_IT_SO events, GitHub live state, and repository-owned plans as explicit context. Do not rely on remembered chat state.

Never bypass a Make It So policy result. Never treat `READY_FOR_OWNER` as autonomous merge permission.
