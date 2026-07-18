---
name: make-it-so-planning
description: Collaboratively create and engage a Make It So course in Codex for greenfield, takeover, or feature work.
---

# Make It So Planning

Use the Make It So MCP tools as the durable project record. Do not keep the
only copy of a requirement or decision in conversation.

1. Read repository status and run the deep baseline when adopting existing code.
2. Ask only the unresolved questions needed to define goal, scope, acceptance
   criteria, exit criteria, permissions, secrets, risks, checks, checkpoints,
   application surfaces, and dependency-ordered work packages.
3. Create the course with `make_it_so_course_create` using `greenfield`,
   `takeover`, or `feature` as appropriate.
4. Record answers through `make_it_so_course_answer`. A capable independent
   reviewer, not the planning conversation itself, must verify readiness evidence.
5. Show the builder the final charter and material changes. Call
   `make_it_so_course_approve` only after explicit approval.

Keep checkpoints dependency-scoped so unrelated ready packages can continue.
Never infer approval from silence.
