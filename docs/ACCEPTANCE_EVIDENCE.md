# Acceptance Evidence

This matrix binds the product acceptance criteria in `PRODUCT_REORIENTATION.md` to
inspectable proof. CI publishes `python-evidence-*` and `runtime-conformance`
artifacts for every pull request.

| Acceptance area | Automated evidence | Live evidence | Status |
| --- | --- | --- | --- |
| Idempotent packaged OpenClaw installation and host registration | `openclaw-plugin/tests/host-registration.mjs`, `openclaw-plugin/tests/package-install.mjs`, `tests/test_openclaw_process_e2e.py` | None required | Automated |
| Dashboard routes, authentication, desktop/mobile layout, accessibility, and overflow | `openclaw-plugin/tests-e2e/dashboard.spec.ts`, `openclaw-plugin/tests/ui-contract.test.ts`, `tests/test_codex_plugin.py` | None required | Automated |
| Sidecar health/version negotiation and crash recovery | `tests/test_sidecar.py`, `tests/test_acceptance_sidecar.py`, `tests/test_cli_orchestration.py` | None required | Automated |
| Schedule reconciliation without duplicate jobs | `openclaw-plugin/tests/schedules.test.ts`, `tests/test_scheduler.py` | PrintHub schedule observation in `docs/canaries/PRINTHUB.md` | Automated plus pending canary |
| Workboard enabled, disabled, and unavailable | `tests/test_engine_workboard_e2e.py`, `tests/test_disabled_mode.py`, `tests/test_cli_orchestration.py`, `tests/test_openclaw_conformance.py` | None required | Automated |
| Direct execution without a kanban | `tests/test_direct_worker_execution.py`, `tests/test_codex_plugin.py` | None required | Automated |
| Greenfield, takeover, and shipped-feature courses | `tests/test_readiness_review.py`, `tests/test_codex_plugin.py`, `tests/test_sidecar.py` | PrintHub uses takeover mode | Automated |
| PR creation, independent review, repair, CI gate, merge, and post-merge verification | `tests/test_disposable_repository_e2e.py`, `tests/test_orchestration_e2e.py`, `tests/test_active_pr_flow.py`, `tests/test_completion_gate.py` | Supervised and bounded autonomous PrintHub runs in `docs/canaries/PRINTHUB.md` | Automated plus pending canary |
| No repeated no-progress cycles | `tests/test_pr34_regression.py`, `tests/test_engine_control.py`, `tests/test_queue_events.py` | PrintHub transition/event query in `docs/canaries/PRINTHUB.md` | Automated plus pending canary |
| Accurate token telemetry by model, course, package, stage, and date | `tests/test_usage.py`, `tests/test_sidecar.py`, `openclaw-plugin/tests-e2e/dashboard.spec.ts` | PrintHub token summary in `docs/canaries/PRINTHUB.md` | Automated plus pending canary |
| Configurable model and reasoning routes | `tests/test_model_policy.py`, `tests/test_sidecar.py`, `openclaw-plugin/ui-tests/app.test.tsx` | None required | Automated |
| Capability-specific UI, CLI, API, library, data, and release QA | `tests/test_capability_qa.py`, `tests/test_qa.py`, `tests/test_acceptance_edges.py` | PrintHub UI QA evidence in `docs/canaries/PRINTHUB.md` | Automated plus pending canary |
| At least 90% line and 85% branch coverage | `scripts/check_coverage.py`, `.github/workflows/ci.yml` | None required | Enforced |

The PrintHub rows remain pending until prerequisite feature PRs are merged, the
packaged plugin is installed on OpenClaw, and both bounded canaries produce durable
GitHub and event evidence. Automated fixtures are not labeled as live proof.
