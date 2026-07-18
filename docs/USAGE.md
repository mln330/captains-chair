# Token Usage And Efficiency

Captain's Chair tracks only provider-reported token telemetry. It does not infer
prices, convert tokens into synthetic units, or treat missing data as zero.

## Recorded Data

Each model attempt may record:

- requested and resolved model
- runtime, repository, role, run, session, and attempt identity
- input, cached-input, cache-write, reasoning, output, and total tokens when exposed
- prompt and response byte counts without retaining transcript content
- duration, success, fallback order, model mismatch, and provider error
- telemetry status of `complete`, `partial`, or `unknown`

OpenClaw session imports are metadata-only and idempotent. Direct harness records
and correlated OpenClaw sessions are merged so the same invocation is not counted
twice. Stale session totals remain labelled as stale evidence.

## Reports

Run:

```bash
captains-chair --config "$CAPTAINS_CHAIR_CONFIG" usage report
captains-chair --config "$CAPTAINS_CHAIR_CONFIG" usage report --repo OWNER/REPO --summary
captains-chair --config "$CAPTAINS_CHAIR_CONFIG" usage sync-openclaw --repo OWNER/REPO
```

The report includes:

- token totals by component and model
- direct-call and external-session groups
- telemetry quality and unknown records
- token-heavy roles and models
- failed-attempt token usage
- fallback and model-route mismatch counts
- repeated prompt fingerprints and their reported tokens
- large prompt and large context-window warnings
- stale totals and legacy-runtime provenance

`accounted_tokens` uses a provider total when one is exposed. Otherwise it sums
reported input and output components and labels the source as `components`.
Cached input is a subset of input and is never added again. Missing usage remains
`unknown`; reasoning-only metadata is not silently promoted to a complete total.

## Safeguards

Optional safeguards are configured with authoritative token counts:

```yaml
usage:
  daily_token_limit: 2000000
  model_daily_token_limits:
    codex/gpt-5.6-sol: 250000
  block_on_unknown: true
  allow_incomplete_telemetry: false
  retention_days: 90
```

The daily limit is account-wide across managed repositories. Per-model limits
accept equivalent `codex/` and `openai/` route prefixes. A limit is reached when
reported consumption is greater than or equal to its configured value.

`block_on_unknown: true` suppresses new worker sessions when usage cannot be
reconciled. This is the autonomous default. `allow_incomplete_telemetry` is a
supervised escape hatch for runtimes that expose aggregate totals only; it does
not invent missing component values.

## Efficiency Review

Review these signals before increasing concurrency or using a stronger model:

1. Failed attempts with reported tokens indicate retry waste or an unhealthy route.
2. Fallback churn requires inspecting the original provider errors.
3. Repeated prompt fingerprints may indicate a missing evidence-change guard.
4. Large prompts with small output suggest context packaging can be narrowed.
5. Large context-window declarations are not consumption; compare them with actual
   input tokens.
6. Frontier-model use on bounded work should be compared with qualified Spark or
   local routes.
7. Model mismatches must be repaired before autonomous dispatch.

The CLI report is an operational telemetry view, not an invoice. Provider account
surfaces remain authoritative for quota and billing.

## Course And Model Precedence

An approved course selects one eligible work package at a time. The engine records
that selection on every runtime card and updates the durable package status as the
workflow moves through execution, review, and completion. Runtime cards are not
the source of truth for the course plan.

Model routes can be overridden at repository, course, work-package, and stage
levels. Use `stage:<name>` keys such as `stage:implementation` or
`stage:final_review` for stage-specific choices. The dashboard edits repository
routes plus global/runtime controls, course and work-package overrides, and an
effective-route preview. Economy, Balanced, Maximum quality, and Local first
presets are available as starting points; applying one does not prevent manual
role-level edits before saving. Package routes take precedence over course routes, which
take precedence over repository routes; runtime routes take precedence over global
defaults. Route qualification and runtime-specific availability should be verified
before promoting a route to autonomous use.

The dashboard also edits repository QA profiles and token safeguards. QA profiles
select the application surface, deterministic checks, required tools, and reviewer
role used for a work package. Token limits are provider-reported daily limits,
optionally scoped by model; missing telemetry remains unknown and can be configured
to block autonomous work rather than being treated as zero.

OpenClaw's native Workboard session keys are correlated to durable card IDs so
worker calls are attributed to the card's course, work package, stage, and
configured model. Deterministic merge cards record `deterministic/no-model` and
consume no model tokens.

Planning is hybrid by design. Use the dashboard's planning brief, the OpenClaw
`/captains-chair plan OWNER/REPO COURSE_KEY` command, or the Codex
`captains_chair_planning_session` MCP tool to hand durable course context to the
native host conversation. The host agent asks only unresolved questions, answers
are written through the readiness API, and course approval remains the explicit
mutation gate.
