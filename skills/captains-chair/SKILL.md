---
name: captains-chair
description: Baseline a GitHub repository, select its next work item, and drive policy-gated PR workflows through Captain's Chair.
---

# Captain's Chair

Use the Captain's Chair CLI as the deterministic control plane. Do not recreate its state machine in chat.

1. Run `captains-chair doctor` before a first baseline or after configuration changes.
2. Run a deep baseline before enabling live cycles.
3. Use shadow cycles to validate decisions without mutations.
4. Use live cycles only after repository mode and completion policy are intentionally configured.
5. Treat CAPTAINS_CHAIR events, GitHub live state, and repository-owned plans as explicit context. Do not rely on remembered chat state.

Never bypass a Captain's Chair policy result. Never treat `READY_FOR_OWNER` as autonomous merge permission.
