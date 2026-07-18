# Make It So Engineering Guide

## Boundaries

- Keep the core deterministic and harness-neutral.
- GitHub, model harnesses, notifications, and schedulers are adapters.
- Managed-repository requirements and roadmaps remain in managed repositories.
- Operational events, leases, model provenance, and run artifacts belong in MAKE_IT_SO state.
- Never commit deployment credentials, live channel IDs, private paths, or runtime databases.

## Safety

- Every implementation and documentation task uses an isolated Git worktree.
- Only `AUTO_MERGE_ALLOWED` can authorize an autonomous merge.
- Release, production deployment, secrets, billing, destructive actions, force-push, and branch deletion require approval unless a validated repository policy explicitly whitelists them.
- A provider error is not an empty result. Fail closed and record the error.
- Model fallback must preserve every failed attempt and may not downgrade final review.

## Verification

Run before publishing:

    ruff check .
    pyright
    pytest
    python -m build

