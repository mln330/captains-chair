# Usage Accounting

CAPTAINS_CHAIR records model provenance and usage in SQLite without retaining prompts or
transcripts. Direct harness calls record the role, every fallback attempt, model,
duration, prompt/response byte counts, and provider-reported token fields when the
harness exposes them. The original provider error is kept on failed attempts so a
successful fallback does not hide the waste.
All-attempts-failed calls are recorded too, with an unresolved model and the
prompt bytes that were sent, so provider outages and oversized failed contexts
remain visible in the usage audit.

OpenClaw Workboard workers run outside the direct harness process. Import their
metadata-only session records with:

```bash
captains_chair usage sync-openclaw --repo NewmanZone/PrintHub \
  --openclaw-executable /home/mln330/.npm-global/bin/openclaw
captains_chair usage report --repo NewmanZone/PrintHub
```

Add `--summary` for a compact operator report suitable for a terminal or a
Discord diagnostic message. Omit `--repo` for the portfolio-wide view:

```bash
captains_chair usage report --repo NewmanZone/PrintHub --since 2026-07-13T00:00:00Z --summary
captains_chair usage report --since 2026-07-13T00:00:00Z --summary
```

Direct records include a runtime provenance field. Rows imported from an older
Captain or an older state schema are labeled `legacy`, so they cannot be mistaken
for current CAPTAINS_CHAIR spend. The compact report calls this out separately.

The importer requests a bounded recent-session window (1000 by default) rather
than downloading the entire OpenClaw session history. Set the OpenClaw
orchestrator's `session_limit` to a larger value, or pass `--session-limit` for
one manual audit, when older session coverage is needed. The accepted range is
1-10,000; a larger value is rejected. The importer is idempotent by OpenClaw
session key. It stores model/provider,
role, token counters, and the source activity update time, but never stores session content. A record
with no provider token counters remains visibly unknown; it is not treated as a
zero-cost call. The sync result reports `session_limit_reached: true` when the
bounded window was full, so an audit can see when older sessions may be omitted.
`false` means only that the response did not fill the requested window; it does
not prove that the provider returned complete history.
For autonomous dispatch and direct model admission, a full window is treated as
degraded telemetry and fails closed until the window is expanded or reconciled.

Direct CAPTAINS_CHAIR OpenClaw calls retain only their opaque root session UUID. Each
fallback attempt receives a distinct provider session key, while the root UUID
keeps the attempts grouped as one call. Metadata-only sync merges each attempt's
counters into that one direct-call record without double-counting or letting a
later fallback overwrite an earlier attempt. Calls remain visibly unknown when
the session endpoint does not expose usable telemetry.

OpenClaw also reports whether a session's aggregate `totalTokens` value is fresh.
CAPTAINS_CHAIR preserves that flag and reports stale totals separately. Stale totals remain
useful session-history evidence, but should not be interpreted as fresh usage from
the current Captain cycle.

OpenClaw Workboard reconciliation also performs this metadata-only import after
each queue pass. If the OpenClaw session endpoint is unavailable, reconciliation
continues and reports a telemetry warning in its JSON result.

The optional `usage.rates` configuration estimates credits per million input,
cached-input, and output tokens. Estimates are labeled as estimates and must be
checked against the account's current rate card. Use `--since` for a bounded audit.
OpenAI reports cached input as a subset of total input, so CAPTAINS_CHAIR prices
`input_tokens - cached_input_tokens` at the uncached input rate and the cached
subset at the cached-input rate. It does not double-charge cached tokens.
The report also includes efficiency signals for fallback attempts, unknown-record
rate, model groups with unusually large average prompts, aggregate model latency,
reported model-route mismatches, repeated prompt fingerprints, and provider-reported
reasoning-token totals. Prompt fingerprints are one-way hashes; prompt
contents and transcripts are never persisted. Repeated fingerprints are a strong
signal that an unchanged cycle is still invoking a worker, or that a large
context package is being replayed unnecessarily.
For priced records, the report also breaks estimated credits into uncached input,
cached input, and output components, and estimates the cost attached to repeated
prompt fingerprints. This makes output-heavy review roles and duplicate context
replays visible as separate audit findings.

Deep baselines fingerprint collected evidence without the volatile generation
timestamp. When that fingerprint and a prior validated analysis match, CAPTAINS_CHAIR
reuses the analysis artifact instead of making the same baseline model calls
again. Configured CAPTAINS_CHAIR state and artifact directories are excluded from the
repository evidence scan, even when a local development configuration places
them beneath the repository root.

If a deep-baseline model call is interrupted after one or more evidence batches,
CAPTAINS_CHAIR writes a validated checkpoint in the external baseline artifact directory.
The next run with the same evidence fingerprint resumes from the completed
batches and only repeats the remaining synthesis work. A successful synthesis
removes the checkpoint; a changed evidence set or malformed checkpoint is
discarded and analyzed from the current evidence instead.

OpenClaw session imports also retain the reported context-window capacity. The
`efficiency.large_context_window_groups` signal identifies worker groups with
at least 100k tokens of available context. This is a risk signal, not proof that
the full window was consumed; actual input-token telemetry remains authoritative.

`cost_hotspots` ranks the ten measured role/model groups by estimated credits and
labels each estimate as `estimated`, `incomplete`, or `unpriced`. This makes it
possible to see which worker role is consuming the budget without treating unknown
provider telemetry as zero cost.

The report also includes `telemetry` counters for unknown records, records with a
complete input/output breakdown, and records that expose only an aggregate
`total_tokens` value, plus OpenClaw totals marked stale by the provider. Aggregate-only records are deliberately excluded from the
credit estimate and set `estimated_credits_are_lower_bound` to true. A zero
estimate for an incomplete group therefore does not mean the work was free; it
means the provider did not expose enough data to price it.

When an OpenClaw worker session is imported, CAPTAINS_CHAIR compares the observed model
with the configured model for that worker role. Unqualified provider responses
are accepted, and the known `codex/...` to `openai/...` route alias is accepted;
other provider changes or a different model are recorded as route mismatches and
appear in the efficiency warnings. This catches an agent that was left on an
old or unexpected model even when the session reports valid token counters.

`failure_hotspots` separately ranks failed direct model attempts, including failed
primary calls before a fallback succeeds. It reports the estimated credits spent on
those attempts and flags missing telemetry, so retries and provider failures are
visible as waste rather than disappearing inside the successful worker total.

Reasoning tokens are diagnostic metadata, not an additional charge line: they
help identify roles spending disproportionate compute inside the provider's
output-token total. CAPTAINS_CHAIR normalizes nested provider usage details when available;
missing values remain unknown.

When a provider reports a concrete model, CAPTAINS_CHAIR compares it with the requested
route. A mismatch fails that harness attempt closed and remains visible in the
usage report instead of silently charging or attributing work to the wrong model.

Direct model groups also expose average response bytes and duration. OpenClaw
session imports preserve provider-reported prompt/response bytes and duration when
the gateway exposes those fields; otherwise the missing values remain visible as
unknown rather than being treated as free.

## Cost controls

Set `operation_mode: disabled` when the Captain must be completely paused. This is stronger
than merely removing a schedule: cycle, watch, baseline, dispatch, reconcile,
unblock, live canary, and merge-gate mutation entry points return before model,
GitHub, or Workboard work. Read-only usage reports, status inspection, and
non-mutating gate inspection remain available.
An orchestration preflight for a disabled repository reports `status: disabled`
and keeps any underlying queue or telemetry findings in `health_status`, so a
paused project is not mistaken for a failed autonomous run.

`operation_mode: advisory` permits evidence collection, safe checks, baseline analysis,
planning, and recommendations, but it is a read-only operating mode for project
actions. Dispatch, reconciliation, Workboard lifecycle changes, live canaries,
PR recovery, and autonomous merge requests return with `status: advisory` before
constructing their runtime or provider. Move the repository to `supervised` or
`autonomous` only after reviewing the proposed action and its evidence.

- Keep planning and final review on the strongest model. Use GPT-5.3-Codex or
  GPT-5.3-Codex-Spark for bounded coding, test-repair, and UX canaries only
  when the selected adapter and auth source explicitly support the route.
  Spark is a distinct Codex model route, available in the Codex app/CLI/VS
  Code for eligible ChatGPT-authenticated users during its preview; its exact
  availability and accounting treatment must still be confirmed by the
  selected Codex runtime. Probe the actual role before enabling it:

  ```bash
  captains_chair model-check --repo NewmanZone/PrintHub --harness codex --role coder
  ```

  Use `--role tester` or `--role ux_reviewer` to probe those worker routes as
  well. Older configurations without dedicated tester or UX policies safely
  reuse their coder policy until the specialized route is declared explicitly.

  Keep Spark out of baseline synthesis, planning, independent review, and
  final review until repository-specific evaluations show that it produces
  acceptable evidence. Record the provider-reported model and auth source;
  never infer OAuth support from a route name alone. ChatGPT plan credits and
  API-key billing remain separate accounting systems.
- Keep prompts compact and evidence-based. Cache repository evidence by commit SHA
  and avoid replaying full logs into every worker context.
- Bound repair retries and suppress identical no-progress cycles. Escalate one
  actionable recovery card instead of repeatedly asking a model the same question.
- After a direct execution failure, unchanged GitHub and policy evidence suppresses
  another planner call; use `--force-replan` only after inspecting the failure or when
  an operator intentionally wants a fresh attempt.
- Planner inputs are fingerprinted before model invocation. When the repository
  snapshot and Captain policy are unchanged, the next cycle reuses the previous
  decision/stall evidence without another planner call. Use
  `cycle --force-replan` only when an operator intentionally wants one fresh
  planning attempt.
- Prefer local Ollama workers for low-risk experiments when their output quality
  is acceptable; record that provider separately rather than pretending it used
  Codex credits.
- Use the cheapest model that can produce acceptable evidence for the role:
  reserve the strongest model for baseline synthesis, planning, independent
  review, final review, and recovery after repeated failure. A coding model is
  appropriate for bounded implementation, test repair, and documentation edits;
  it is not a substitute for an independent reviewer.
- Keep background schedules disabled while auditing or set an explicit daily
  budget and alert threshold.
- Treat `telemetry.unknown_records`, `telemetry.aggregate_only_records`, and
  `efficiency.repeated_prompt_calls` as the first three leak indicators. Investigate
  those and `efficiency.large_context_window_groups` before increasing worker
  concurrency or adding more review stages.

## Model and credit boundaries

`codex/...` is a route, not proof of which model or account was billed. Every
worker record must be checked for the provider-reported model, auth source,
token counters, and fallback attempts. A route mismatch, unknown telemetry,
stale aggregate total, or unpriced model is an accounting gap and should block
new autonomous work when `usage.block_on_unknown` is enabled.

ChatGPT plan credits and API-key billing are separate accounting systems. A
model can be technically usable through the API while unavailable to a
ChatGPT-authenticated Codex session, and a locally hosted Ollama call does not
consume Codex credits. Keep those providers in separate report groups and
compare the report with the provider's official usage view before changing the
daily budget.

For a usage-efficient coding cycle, the Captain should:

1. Reuse evidence packages by commit SHA and send only the files and checks
   relevant to the current work item.
2. Suppress unchanged no-progress cycles and duplicate queue cards before any
   model call.
3. Run one bounded coder, then parallel independent review/test/UX checks only
   when the diff warrants them.
4. Record failed primary calls and fallback reasons instead of silently retrying
   with another model.
5. Stop at the configured daily budget or when telemetry is incomplete, while
   continuing deterministic reconciliation and reporting.

The following command is the starting point for a bounded audit:

```bash
captains_chair usage report --repo NewmanZone/PrintHub --since 2026-07-13T00:00:00Z
```

Review `cost_hotspots`, `failure_hotspots`, `telemetry`, route-mismatch
warnings, repeated prompt fingerprints, large-context groups, and the lower
bound flag together. The estimate is intentionally conservative when provider
data is missing; an unknown call is not a free call.

Usage accounting is telemetry, not a billing ledger. Missing or provider-specific
fields are reported as unknown so gaps remain visible.

The configured rate card is an internal estimate only. ChatGPT/Codex plan usage and
API billing can use different accounting, so compare the report with the account's
official usage view before treating estimated credits as a billing amount.

Usage reports and Workboard budget checks prune direct and imported usage rows
older than `usage.retention_days`; this bounds the local audit database without
deleting current-day evidence.

When `usage.daily_budget_credits` is configured, it is an account-wide
same-day ceiling across all managed repositories. `orchestrate reconcile`, the
direct `orchestrate dispatch` command, and every direct Captain model invocation
(planning, baseline analysis, review, repair, and model health checks) suppress
new work at or above that estimate. When `usage.block_on_unknown` is true, the
same direct-model guard also suppresses calls with unreconciled telemetry even
without a hard daily budget. The guard runs before the provider call, so a
suppressed call does not consume tokens. It emits a
`MODEL_CALL_SUPPRESSED` degraded event with the budget decision instead of
appearing as a provider failure. Independently, `usage.block_on_unknown: true` (the default)
suppresses new workers whenever provider telemetry is incomplete, even when no
hard credit ceiling is configured. Set it to `false` only when the operator
accepts that usage estimates may be incomplete. Queue recovery and state
bookkeeping still run, and every pass reports the budget decision in JSON. A
configured budget also fails closed for measured tokens whose model has no
matching rate card.
The same guard applies when a provider reports only aggregate `total_tokens`
without input/output breakdowns, because that cannot support a reliable cost
estimate.

For a bounded supervised canary only, `usage.allow_incomplete_telemetry: true`
allows aggregate or partial records to remain warnings while the configured
daily budget still applies. CAPTAINS_CHAIR rejects this setting when any repository is in
`autonomous` mode. It is an operational compromise for runtimes whose session
metadata cannot provide complete token breakdowns; it does not make the
estimated credit total a billing ledger.

After resolving a true owner blocker, resume only that Workboard card without
starting a worker immediately:

```bash
captains_chair orchestrate unblock --repo NewmanZone/PrintHub --card CARD_ID
captains_chair orchestrate reconcile --repo NewmanZone/PrintHub
```

The first command is deterministic and does not invoke a model. Reconciliation
then re-evaluates dependencies, retry policy, usage budget, and worker health.
