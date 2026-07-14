from __future__ import annotations

import json
from typing import Any, cast

from captains_chair.models import UsageConfig, UsageRate


def build_usage_report(summary: dict[str, Any], config: UsageConfig) -> dict[str, Any]:
    direct_groups = [_with_estimate(group, config.rates) for group in summary.get("direct_groups", [])]
    external_groups = [_with_estimate(group, config.rates) for group in summary.get("external_groups", [])]
    repeated_prompt_groups = [
        _with_estimate(group, config.rates) for group in summary.get("repeated_prompts", [])
    ]
    total_credits = sum(float(group["estimated_credits"]) for group in (*direct_groups, *external_groups))
    direct = dict(summary.get("direct_calls", {}))
    external = dict(summary.get("external_sessions", {}))
    unknown = int(direct.get("unknown_calls") or 0) + int(external.get("unknown_sessions") or 0)
    unpriced = sum(
        1
        for group in (*direct_groups, *external_groups)
        if _has_tokens(group) and not group["rate_card_model"]
    )
    incomplete_cost = sum(
        1
        for group in (*direct_groups, *external_groups)
        if _cost_breakdown_incomplete(group)
    )
    aggregate_only_records = _number(direct.get("aggregate_only_calls")) + _number(
        external.get("aggregate_only_sessions")
    )
    aggregate_only_tokens = _number(direct.get("aggregate_only_tokens")) + _number(
        external.get("aggregate_only_tokens")
    )
    stale_total_sessions = _number(external.get("stale_total_sessions"))
    stale_total_tokens = _number(external.get("stale_total_tokens"))
    if not aggregate_only_records:
        aggregate_only_records = sum(_aggregate_only_records(group) for group in (*direct_groups, *external_groups))
        aggregate_only_tokens = sum(_aggregate_only_tokens(group) for group in (*direct_groups, *external_groups))
    efficiency = _efficiency_summary(
        direct,
        external,
        direct_groups,
        external_groups,
        repeated_prompt_groups,
    )
    cost_hotspots = _cost_hotspots(direct_groups, external_groups)
    failure_hotspots = _failure_hotspots(summary.get("attempt_records", []), config.rates)
    failed_attempts = sum(int(item["failed_attempts"]) for item in failure_hotspots)
    failed_attempt_credits = sum(float(item["estimated_credits"]) for item in failure_hotspots)
    token_cost_breakdown = _token_cost_breakdown((*direct_groups, *external_groups))
    repeated_prompt_credits = sum(
        float(item.get("estimated_credits") or 0) for item in repeated_prompt_groups
    )
    warnings: list[str] = []
    if unknown:
        warnings.append(f"{unknown} usage records have no provider token telemetry")
    if not config.rates:
        warnings.append("No credit rate card is configured; token totals are measured but cost is not estimated")
    elif unpriced:
        warnings.append(f"{unpriced} measured usage groups have no matching credit rate card")
    if incomplete_cost:
        warnings.append(f"{incomplete_cost} usage groups lack input/output breakdown for cost estimation")
    if aggregate_only_records:
        warnings.append(
            f"{aggregate_only_records} records expose only aggregate token totals "
            f"({aggregate_only_tokens} tokens); estimated credits exclude them"
        )
    if stale_total_sessions:
        warnings.append(
            f"{stale_total_sessions} OpenClaw session totals are marked stale "
            f"({stale_total_tokens} tokens); treat them as session-history evidence, not fresh-run usage"
        )
    if efficiency["fallback_attempts"]:
        warnings.append(
            f"{efficiency['fallback_attempts']} fallback model attempts occurred; inspect provider failures before increasing concurrency"
        )
    if efficiency["model_mismatch_attempts"]:
        warnings.append(
            f"{efficiency['model_mismatch_attempts']} model attempts reported a route different from the requested policy"
        )
    if efficiency["large_prompt_groups"]:
        warnings.append(
            f"{len(efficiency['large_prompt_groups'])} model groups average at least 80 KB of prompt input; review context packing"
        )
    if efficiency["large_context_window_groups"]:
        warnings.append(
            f"{len(efficiency['large_context_window_groups'])} worker groups report context windows of at least 100k tokens; compare actual input telemetry before increasing context"
        )
    if efficiency["repeated_prompt_groups"]:
        warnings.append(
            f"{len(efficiency['repeated_prompt_groups'])} prompt fingerprints were sent repeatedly; inspect for missing evidence-change suppression"
        )
    if repeated_prompt_credits:
        warnings.append(
            f"Repeated prompts consumed approximately {repeated_prompt_credits:.4f} estimated credits; inspect repeated_prompt_groups"
        )
    if efficiency["reasoning_tokens"]:
        warnings.append(
            f"{efficiency['reasoning_tokens']} reasoning tokens were reported; compare reasoning-heavy roles against their output and success rate"
        )
    if failed_attempts:
        warnings.append(
            f"{failed_attempts} failed model attempts consumed approximately {failed_attempt_credits:.4f} estimated credits; inspect failure_hotspots"
        )
    legacy_calls = _number(direct.get("legacy_calls"))
    if legacy_calls:
        warnings.append(
            f"{legacy_calls} direct records came from a legacy or unknown runtime; do not mix them with CAPTAINS_CHAIR spend"
        )
    if config.daily_budget_credits is not None and total_credits > config.daily_budget_credits:
        warnings.append(
            f"Estimated credits {total_credits:.2f} exceed daily budget {config.daily_budget_credits:.2f}"
        )
    return {
        "direct_calls": direct,
        "external_sessions": external,
        "direct_groups": direct_groups,
        "external_groups": external_groups,
        "estimated_credits": round(total_credits, 4),
        "budget_credits": config.daily_budget_credits,
        "token_cost_breakdown": token_cost_breakdown,
        "repeated_prompt_estimated_credits": round(repeated_prompt_credits, 4),
        "incomplete_cost_groups": incomplete_cost,
        "telemetry": {
            "total_records": _number(direct.get("calls")) + _number(external.get("calls")),
            "measured_records": _number(direct.get("measured_calls")) + _number(external.get("calls")) - _number(external.get("unknown_sessions")),
            "unknown_records": unknown,
            "breakdown_records": _number(direct.get("breakdown_calls")) + _number(external.get("breakdown_sessions")),
            "aggregate_only_records": aggregate_only_records,
            "aggregate_only_tokens": aggregate_only_tokens,
            "stale_total_sessions": stale_total_sessions,
            "stale_total_tokens": stale_total_tokens,
            "estimated_credits_are_lower_bound": bool(unknown or aggregate_only_records or incomplete_cost),
            "legacy_direct_calls": legacy_calls,
        },
        "cost_hotspots": cost_hotspots,
        "failure_hotspots": failure_hotspots,
        "failed_attempts": failed_attempts,
        "failed_attempt_estimated_credits": round(failed_attempt_credits, 4),
        "efficiency": efficiency,
        "warnings": warnings,
    }


def dispatch_budget(summary: dict[str, Any], config: UsageConfig) -> dict[str, Any]:
    """Return a fail-closed decision for starting new worker sessions."""
    report = build_usage_report(summary, config)
    unknown = int(report["direct_calls"].get("unknown_calls") or 0) + int(
        report["external_sessions"].get("unknown_sessions") or 0
    )
    unpriced = sum(
        1
        for group in (*report["direct_groups"], *report["external_groups"])
        if group.get("usage_measured") and not group.get("rate_card_model")
    )
    incomplete_cost = int(report.get("incomplete_cost_groups") or 0)
    budget = config.daily_budget_credits
    if incomplete_cost and not config.allow_incomplete_telemetry:
        return {
            "allowed": False,
            "reason": "measured usage lacks an input/output breakdown; new worker sessions are suppressed",
            "estimated_credits": report["estimated_credits"],
            "budget_credits": budget,
            "unknown_records": unknown,
            "unpriced_groups": unpriced,
            "incomplete_cost_groups": incomplete_cost,
        }
    if unpriced:
        return {
            "allowed": False,
            "reason": "measured usage has no matching credit rate card; new worker sessions are suppressed",
            "estimated_credits": report["estimated_credits"],
            "budget_credits": budget,
            "unknown_records": unknown,
            "unpriced_groups": unpriced,
            "incomplete_cost_groups": incomplete_cost,
        }
    if config.block_on_unknown and unknown:
        return {
            "allowed": False,
            "reason": "usage telemetry is incomplete; new worker sessions are suppressed until it is reconciled",
            "estimated_credits": report["estimated_credits"],
            "budget_credits": budget,
            "unknown_records": unknown,
            "unpriced_groups": unpriced,
            "incomplete_cost_groups": incomplete_cost,
        }
    if budget is None:
        return {
            "allowed": True,
            "reason": "no daily usage budget is configured",
            "estimated_credits": report["estimated_credits"],
            "budget_credits": None,
            "unknown_records": unknown,
            "unpriced_groups": unpriced,
            "incomplete_cost_groups": incomplete_cost,
        }
    if report["estimated_credits"] >= budget:
        return {
            "allowed": False,
            "reason": "the configured daily usage budget has been reached",
            "estimated_credits": report["estimated_credits"],
            "budget_credits": budget,
            "unknown_records": unknown,
            "unpriced_groups": unpriced,
            "incomplete_cost_groups": incomplete_cost,
        }
    return {
        "allowed": True,
        "reason": "usage is below the configured daily budget",
        "estimated_credits": report["estimated_credits"],
        "budget_credits": budget,
        "unknown_records": unknown,
        "unpriced_groups": unpriced,
        "incomplete_cost_groups": incomplete_cost,
    }


def _with_estimate(group: dict[str, Any], rates: dict[str, UsageRate]) -> dict[str, Any]:
    value = dict(group)
    rate = _rate_for(str(value.get("model") or ""), rates)
    input_tokens = _number(value.get("input_tokens"))
    cached_tokens = _number(value.get("cached_input_tokens"))
    output_tokens = _number(value.get("output_tokens"))
    input_credits = _uncached_input_tokens(input_tokens, cached_tokens) * rate.input_credits_per_million / 1_000_000
    cached_input_credits = cached_tokens * rate.cached_input_credits_per_million / 1_000_000
    output_credits = output_tokens * rate.output_credits_per_million / 1_000_000
    estimated = input_credits + cached_input_credits + output_credits
    value["estimated_credits"] = round(estimated, 4)
    value["estimated_input_credits"] = round(input_credits, 4)
    value["estimated_cached_input_credits"] = round(cached_input_credits, 4)
    value["estimated_output_credits"] = round(output_credits, 4)
    value["uncached_input_tokens"] = _uncached_input_tokens(input_tokens, cached_tokens)
    value["rate_card_model"] = rate is not _ZERO_RATE
    value["usage_measured"] = _has_tokens(value)
    calls = _number(value.get("calls"))
    prompt_bytes = _number(value.get("prompt_bytes"))
    if calls:
        value["average_prompt_bytes"] = round(prompt_bytes / calls)
        value["average_response_bytes"] = round(_number(value.get("response_bytes")) / calls)
        value["average_duration_ms"] = round(_number(value.get("duration_ms")) / calls)
    return value


_ZERO_RATE = UsageRate()


def _rate_for(model: str, rates: dict[str, UsageRate]) -> UsageRate:
    candidates = (model, model.removeprefix("codex/"), model.removeprefix("openai/"))
    for candidate in candidates:
        if candidate in rates:
            return rates[candidate]
    return _ZERO_RATE


def _number(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _has_tokens(group: dict[str, Any]) -> bool:
    return any(group.get(key) is not None for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"))


def _cost_breakdown_incomplete(group: dict[str, Any]) -> bool:
    return _has_tokens(group) and (
        group.get("input_tokens") is None or group.get("output_tokens") is None
    )


def _aggregate_only_records(group: dict[str, Any]) -> int:
    explicit = _number(group.get("aggregate_only_calls")) + _number(group.get("aggregate_only_sessions"))
    if explicit:
        return explicit
    if group.get("total_tokens") is not None and (
        group.get("input_tokens") is None or group.get("output_tokens") is None
    ):
        return _number(group.get("calls"))
    return 0


def _aggregate_only_tokens(group: dict[str, Any]) -> int:
    explicit = _number(group.get("aggregate_only_tokens"))
    if explicit:
        return explicit
    if _aggregate_only_records(group):
        return _number(group.get("total_tokens"))
    return 0


def _efficiency_summary(
    direct: dict[str, Any],
    external: dict[str, Any],
    direct_groups: list[dict[str, Any]],
    external_groups: list[dict[str, Any]],
    repeated_prompts: list[dict[str, Any]],
) -> dict[str, Any]:
    direct_calls = _number(direct.get("calls"))
    external_calls = _number(external.get("calls"))
    fallback_attempts = _number(direct.get("fallback_attempts"))
    model_mismatch_attempts = _number(direct.get("model_mismatch_attempts")) + _number(
        external.get("model_mismatch_attempts")
    )
    reasoning_tokens = _number(direct.get("reasoning_tokens")) + _number(
        external.get("reasoning_tokens")
    )
    output_tokens = _number(direct.get("output_tokens")) + _number(external.get("output_tokens"))
    unknown_records = _number(direct.get("unknown_calls")) + _number(external.get("unknown_sessions"))
    total_records = direct_calls + external_calls
    large_prompt_groups = [
        {
            "repo": group.get("repo"),
            "role": group.get("role"),
            "model": group.get("model"),
            "average_prompt_bytes": group.get("average_prompt_bytes"),
        }
        for group in direct_groups
        if _number(group.get("average_prompt_bytes")) >= 80_000
    ]
    large_context_window_groups = [
        {
            "repo": group.get("repo"),
            "role": group.get("role"),
            "provider": group.get("provider"),
            "model": group.get("model"),
            "max_context_window_tokens": _number(group.get("max_context_tokens")),
            "average_context_window_tokens": _number(group.get("average_context_tokens")),
        }
        for group in external_groups
        if _number(group.get("max_context_tokens")) >= 100_000
    ]
    prompt_bytes = _number(direct.get("prompt_bytes"))
    duration_ms = _number(direct.get("duration_ms"))
    repeated = [
        {
            "repo": item.get("repo"),
            "role": item.get("role"),
            "model": item.get("model"),
            "prompt_fingerprint": item.get("prompt_fingerprint"),
            "calls": _number(item.get("calls")),
            "estimated_credits": float(item.get("estimated_credits") or 0),
            "estimated_input_credits": float(item.get("estimated_input_credits") or 0),
            "estimated_cached_input_credits": float(item.get("estimated_cached_input_credits") or 0),
            "estimated_output_credits": float(item.get("estimated_output_credits") or 0),
            "cost_status": _cost_status(item),
            "prompt_bytes": _number(item.get("prompt_bytes")),
            "duration_ms": _number(item.get("duration_ms")),
        }
        for item in repeated_prompts
    ]
    return {
        "fallback_attempts": fallback_attempts,
        "model_mismatch_attempts": model_mismatch_attempts,
        "reasoning_tokens": reasoning_tokens,
        "reasoning_token_ratio_of_output": round(reasoning_tokens / output_tokens, 4)
        if output_tokens
        else 0.0,
        "fallback_attempts_per_direct_call": round(fallback_attempts / direct_calls, 4)
        if direct_calls
        else 0.0,
        "unknown_records": unknown_records,
        "unknown_record_rate": round(unknown_records / total_records, 4) if total_records else 0.0,
        "direct_prompt_bytes": prompt_bytes,
        "direct_response_bytes": _number(direct.get("response_bytes")),
        "average_direct_prompt_bytes": round(prompt_bytes / direct_calls) if direct_calls else 0,
        "direct_duration_ms": duration_ms,
        "average_direct_duration_ms": round(duration_ms / direct_calls) if direct_calls else 0,
        "large_prompt_groups": large_prompt_groups,
        "large_context_window_groups": large_context_window_groups,
        "repeated_prompt_groups": repeated,
        "repeated_prompt_calls": sum(_number(item.get("calls")) for item in repeated),
        "repeated_prompt_estimated_credits": round(
            sum(float(item.get("estimated_credits") or 0) for item in repeated), 4
        ),
    }


def usage_summary_text(
    report: dict[str, Any],
    *,
    repo: str | None = None,
    since: str | None = None,
) -> str:
    """Render a compact operator view without hiding accounting uncertainty."""
    telemetry = cast(dict[str, Any], report.get("telemetry", {}))
    direct = cast(dict[str, Any], report.get("direct_calls", {}))
    external = cast(dict[str, Any], report.get("external_sessions", {}))
    scope = repo or "all managed repositories"
    window = f" since {since}" if since else ""
    lines = [
        f"CAPTAINS_CHAIR usage audit: {scope}{window}",
        (
            "Estimated credits (lower bound): "
            f"{float(report.get('estimated_credits') or 0):.4f}"
        ),
        (
            "Records: "
            f"{_number(telemetry.get('total_records'))} total, "
            f"{_number(telemetry.get('unknown_records'))} unknown, "
            f"{_number(telemetry.get('aggregate_only_records'))} aggregate-only, "
            f"{_number(telemetry.get('legacy_direct_calls'))} legacy direct"
        ),
        (
            "Calls: "
            f"{_number(direct.get('calls'))} direct, "
            f"{_number(external.get('calls'))} OpenClaw sessions, "
            f"{_number(direct.get('fallback_attempts'))} fallback attempts"
        ),
    ]
    token_cost = cast(dict[str, Any], report.get("token_cost_breakdown", {}))
    lines.append(
        "Token cost: "
        f"input {float(token_cost.get('input_credits') or 0):.4f}, "
        f"cached {float(token_cost.get('cached_input_credits') or 0):.4f}, "
        f"output {float(token_cost.get('output_credits') or 0):.4f} credits"
    )
    repeated_credits = float(report.get("repeated_prompt_estimated_credits") or 0)
    if repeated_credits:
        lines.append(
            f"Repeated-prompt cost: {repeated_credits:.4f} estimated credits; inspect duplicate fingerprints"
        )
    hotspots_value = report.get("cost_hotspots", [])
    if isinstance(hotspots_value, list) and hotspots_value:
        lines.append("Top cost hotspots:")
        for raw_item in cast(list[Any], hotspots_value)[:5]:
            if not isinstance(raw_item, dict):
                continue
            item = cast(dict[str, Any], raw_item)
            lines.append(
                "- "
                f"{item.get('role') or 'unknown'} / {item.get('model') or 'unknown'}: "
                f"{float(item.get('estimated_credits') or 0):.4f} credits, "
                f"{_number(item.get('calls'))} calls, {item.get('cost_status') or 'unknown'}"
            )
    failed = _number(report.get("failed_attempts"))
    if failed:
        lines.append(
            f"Failed attempts: {failed} ({float(report.get('failed_attempt_estimated_credits') or 0):.4f} estimated credits)"
        )
    warnings_value = report.get("warnings", [])
    if isinstance(warnings_value, list) and warnings_value:
        lines.append("Warnings:")
        warnings = cast(list[Any], warnings_value)
        lines.extend(f"- {str(warning)}" for warning in warnings[:6])
    else:
        lines.append("Warnings: none")
    return "\n".join(lines)


def _cost_hotspots(
    direct_groups: list[dict[str, Any]],
    external_groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rank measured model groups so expensive roles are visible without manual math."""
    hotspots: list[dict[str, Any]] = []
    for source, groups in (("direct", direct_groups), ("external", external_groups)):
        for group in groups:
            if not group.get("usage_measured"):
                continue
            calls = _number(group.get("calls"))
            unknown_records = (
                _number(group.get("unknown_sessions"))
                if source == "external"
                else max(0, calls - _number(group.get("measured_calls")))
            )
            if _cost_breakdown_incomplete(group):
                cost_status = "incomplete"
            elif not group.get("rate_card_model"):
                cost_status = "unpriced"
            else:
                cost_status = "estimated"
            hotspots.append(
                {
                    "source": source,
                    "repo": group.get("repo"),
                    "runtime": group.get("runtime"),
                    "role": group.get("role"),
                    "provider": group.get("provider"),
                    "model": group.get("model"),
                    "calls": calls,
                    "unknown_records": unknown_records,
                    "estimated_credits": float(group.get("estimated_credits") or 0),
                    "estimated_input_credits": float(group.get("estimated_input_credits") or 0),
                    "estimated_cached_input_credits": float(group.get("estimated_cached_input_credits") or 0),
                    "estimated_output_credits": float(group.get("estimated_output_credits") or 0),
                    "cost_status": cost_status,
                    "input_tokens": group.get("input_tokens"),
                    "cached_input_tokens": group.get("cached_input_tokens"),
                    "output_tokens": group.get("output_tokens"),
                    "total_tokens": group.get("total_tokens"),
                }
            )
    hotspots.sort(
        key=lambda item: (
            float(item["estimated_credits"]),
            _number(item.get("total_tokens")),
            _number(item.get("calls")),
        ),
        reverse=True,
    )
    return hotspots[:10]


def _cost_status(group: dict[str, Any]) -> str:
    if _cost_breakdown_incomplete(group):
        return "incomplete"
    if not group.get("rate_card_model"):
        return "unpriced"
    return "estimated"


def _token_cost_breakdown(groups: tuple[dict[str, Any], ...]) -> dict[str, float]:
    """Expose which token type contributes to the estimate."""
    return {
        "input_credits": round(
            sum(float(group.get("estimated_input_credits") or 0) for group in groups), 4
        ),
        "cached_input_credits": round(
            sum(float(group.get("estimated_cached_input_credits") or 0) for group in groups), 4
        ),
        "output_credits": round(
            sum(float(group.get("estimated_output_credits") or 0) for group in groups), 4
        ),
        "estimated_credits": round(
            sum(float(group.get("estimated_credits") or 0) for group in groups), 4
        ),
    }


def _failure_hotspots(
    records: Any,
    rates: dict[str, UsageRate],
) -> list[dict[str, Any]]:
    """Estimate tokens spent on failed direct attempts, grouped by role and model."""
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    if not isinstance(records, list):
        return []
    for raw_record in cast(list[Any], records):
        if not isinstance(raw_record, dict):
            continue
        record = cast(dict[str, Any], raw_record)
        try:
            attempts = json.loads(str(record.get("attempts_json") or "[]"))
        except json.JSONDecodeError:
            continue
        if not isinstance(attempts, list):
            continue
        for raw_attempt in cast(list[Any], attempts):
            if not isinstance(raw_attempt, dict) or cast(dict[str, Any], raw_attempt).get("success") is True:
                continue
            attempt = cast(dict[str, Any], raw_attempt)
            model = str(attempt.get("model") or record.get("model") or "unresolved")
            key = (str(record.get("repo") or ""), str(record.get("role") or ""), model)
            group = groups.setdefault(
                key,
                {
                    "repo": key[0],
                    "role": key[1],
                    "model": key[2],
                    "failed_attempts": 0,
                    "unknown_failed_attempts": 0,
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "estimated_credits": 0.0,
                    "rate_card_model": False,
                },
            )
            group["failed_attempts"] += 1
            input_tokens = _optional_number(attempt.get("input_tokens"))
            cached_tokens = _optional_number(attempt.get("cached_input_tokens"))
            output_tokens = _optional_number(attempt.get("output_tokens"))
            rate = _rate_for(model, rates)
            if input_tokens is None or output_tokens is None:
                group["unknown_failed_attempts"] += 1
                continue
            group["input_tokens"] += input_tokens
            group["cached_input_tokens"] += cached_tokens or 0
            group["output_tokens"] += output_tokens
            group["rate_card_model"] = rate is not _ZERO_RATE
            group["estimated_credits"] += (
                _uncached_input_tokens(input_tokens, cached_tokens or 0)
                * rate.input_credits_per_million
                + (cached_tokens or 0) * rate.cached_input_credits_per_million
                + output_tokens * rate.output_credits_per_million
            ) / 1_000_000
    hotspots: list[dict[str, Any]] = []
    for group in groups.values():
        if group["unknown_failed_attempts"]:
            status = "incomplete"
        elif not group["rate_card_model"]:
            status = "unpriced"
        else:
            status = "estimated"
        hotspots.append(
            {
                **group,
                "estimated_credits": round(float(group["estimated_credits"]), 4),
                "cost_status": status,
            }
        )
    hotspots.sort(
        key=lambda item: (
            float(item["estimated_credits"]),
            int(item["failed_attempts"]),
        ),
        reverse=True,
    )
    return hotspots[:10]


def _optional_number(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _uncached_input_tokens(input_tokens: int, cached_input_tokens: int) -> int:
    """Return billable uncached input when cached tokens are a total-input subset."""
    return max(0, input_tokens - cached_input_tokens)


__all__ = ["build_usage_report", "dispatch_budget", "usage_summary_text"]
