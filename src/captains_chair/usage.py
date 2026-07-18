from __future__ import annotations

import json
from typing import Any, cast

from captains_chair.model_policy import models_match
from captains_chair.models import UsageConfig

TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "output_tokens",
    "total_tokens",
)


def build_usage_report(summary: dict[str, Any], config: UsageConfig) -> dict[str, Any]:
    """Build a token-only report from provider-reported usage.

    Component totals are never converted to billing values. A provider total is
    preferred for limit accounting; when it is absent, reported input and output
    components are summed and the source is labelled ``components``.
    """

    direct = dict(summary.get("direct_calls", {}))
    external = dict(summary.get("external_sessions", {}))
    direct_groups = [_decorate_group(group) for group in summary.get("direct_groups", [])]
    direct_attempt_groups = [
        _decorate_group(group) for group in summary.get("direct_attempt_groups", [])
    ]
    external_groups = [_decorate_group(group) for group in summary.get("external_groups", [])]
    repeated_prompts = [_decorate_group(group) for group in summary.get("repeated_prompts", [])]
    groups = [*(direct_attempt_groups or direct_groups), *external_groups]

    token_totals = _token_totals(direct, external, groups)
    model_totals = _model_totals(groups)
    unknown_records = _number(direct.get("unknown_calls")) + _number(
        external.get("unknown_sessions")
    )
    total_records = _number(direct.get("calls")) + _number(external.get("calls"))
    measured_records = max(0, total_records - unknown_records)
    aggregate_only_records = _number(direct.get("aggregate_only_calls")) + _number(
        external.get("aggregate_only_sessions")
    )
    aggregate_only_tokens = _number(direct.get("aggregate_only_tokens")) + _number(
        external.get("aggregate_only_tokens")
    )
    breakdown_records = _number(direct.get("breakdown_calls")) + _number(
        external.get("breakdown_sessions")
    )
    stale_total_sessions = _number(external.get("stale_total_sessions"))
    stale_total_tokens = _number(external.get("stale_total_tokens"))
    legacy_calls = _number(direct.get("legacy_calls"))
    telemetry_status = _telemetry_status(total_records, measured_records, breakdown_records)

    failure_hotspots = _failure_hotspots(summary.get("attempt_records", []))
    failed_attempts = sum(_number(item.get("failed_attempts")) for item in failure_hotspots)
    failed_attempt_tokens = sum(_number(item.get("accounted_tokens")) for item in failure_hotspots)
    efficiency = _efficiency_summary(
        direct,
        external,
        direct_groups,
        external_groups,
        repeated_prompts,
    )
    warnings = _warnings(
        config=config,
        unknown_records=unknown_records,
        aggregate_only_records=aggregate_only_records,
        aggregate_only_tokens=aggregate_only_tokens,
        stale_total_sessions=stale_total_sessions,
        stale_total_tokens=stale_total_tokens,
        legacy_calls=legacy_calls,
        failed_attempts=failed_attempts,
        failed_attempt_tokens=failed_attempt_tokens,
        efficiency=efficiency,
        token_totals=token_totals,
        model_totals=model_totals,
    )

    return {
        "direct_calls": direct,
        "external_sessions": external,
        "direct_groups": direct_groups,
        "direct_attempt_groups": direct_attempt_groups,
        "external_groups": external_groups,
        "token_totals": token_totals,
        "model_totals": model_totals,
        "daily_token_limit": config.daily_token_limit,
        "model_daily_token_limits": dict(config.model_daily_token_limits),
        "telemetry": {
            "status": telemetry_status,
            "total_records": total_records,
            "measured_records": measured_records,
            "unknown_records": unknown_records,
            "breakdown_records": breakdown_records,
            "aggregate_only_records": aggregate_only_records,
            "aggregate_only_tokens": aggregate_only_tokens,
            "stale_total_sessions": stale_total_sessions,
            "stale_total_tokens": stale_total_tokens,
            "legacy_direct_calls": legacy_calls,
        },
        "token_hotspots": sorted(
            groups,
            key=lambda item: (_number(item.get("accounted_tokens")), _number(item.get("calls"))),
            reverse=True,
        )[:10],
        "failure_hotspots": failure_hotspots,
        "failed_attempts": failed_attempts,
        "failed_attempt_tokens": failed_attempt_tokens,
        "efficiency": efficiency,
        "warnings": warnings,
    }


def dispatch_budget(summary: dict[str, Any], config: UsageConfig) -> dict[str, Any]:
    """Return a fail-closed decision for authoritative token safeguards."""

    report = build_usage_report(summary, config)
    telemetry = cast(dict[str, Any], report["telemetry"])
    token_totals = cast(dict[str, Any], report["token_totals"])
    unknown = _number(telemetry.get("unknown_records"))
    total_tokens = _number(token_totals.get("accounted_tokens"))
    decision: dict[str, Any] = {
        "allowed": True,
        "reason": "no token limit has been reached",
        "total_tokens": total_tokens,
        "daily_token_limit": config.daily_token_limit,
        "unknown_records": unknown,
        "telemetry_status": telemetry.get("status", "unknown"),
        "model_limits": [],
    }

    if config.block_on_unknown and unknown:
        decision.update(
            allowed=False,
            reason="usage telemetry is incomplete; new worker sessions are suppressed until it is reconciled",
        )
        return decision

    model_totals = cast(list[dict[str, Any]], report["model_totals"])
    model_limit_results: list[dict[str, Any]] = []
    exceeded_models: list[str] = []
    for configured_model, limit in config.model_daily_token_limits.items():
        consumed = sum(
            _number(group.get("accounted_tokens"))
            for group in model_totals
            if _model_matches(str(group.get("model") or ""), configured_model)
        )
        reached = consumed >= limit
        model_limit_results.append(
            {
                "model": configured_model,
                "total_tokens": consumed,
                "token_limit": limit,
                "reached": reached,
            }
        )
        if reached:
            exceeded_models.append(configured_model)
    decision["model_limits"] = model_limit_results

    if exceeded_models:
        decision.update(
            allowed=False,
            reason="configured model token limit reached for: " + ", ".join(exceeded_models),
        )
        return decision
    if config.daily_token_limit is not None and total_tokens >= config.daily_token_limit:
        decision.update(allowed=False, reason="the configured daily token limit has been reached")
        return decision
    if config.daily_token_limit is None and not config.model_daily_token_limits:
        decision["reason"] = "no token limit is configured"
    return decision


def usage_summary_text(
    report: dict[str, Any],
    *,
    repo: str | None = None,
    since: str | None = None,
) -> str:
    """Render a concise operator summary with explicit telemetry quality."""

    telemetry = cast(dict[str, Any], report.get("telemetry", {}))
    direct = cast(dict[str, Any], report.get("direct_calls", {}))
    external = cast(dict[str, Any], report.get("external_sessions", {}))
    tokens = cast(dict[str, Any], report.get("token_totals", {}))
    efficiency = cast(dict[str, Any], report.get("efficiency", {}))
    scope = repo or "all managed repositories"
    window = f" since {since}" if since else ""
    lines = [
        f"Captain's Chair token audit: {scope}{window}",
        (
            f"Tokens: {_number(tokens.get('accounted_tokens'))} accounted "
            f"({_number(tokens.get('input_tokens'))} input, "
            f"{_number(tokens.get('cached_input_tokens'))} cached input, "
            f"{_number(tokens.get('cache_write_tokens'))} cache write, "
            f"{_number(tokens.get('reasoning_tokens'))} reasoning, "
            f"{_number(tokens.get('output_tokens'))} output)"
        ),
        (
            f"Telemetry: {telemetry.get('status', 'unknown')}; "
            f"{_number(telemetry.get('unknown_records'))} unknown of "
            f"{_number(telemetry.get('total_records'))} records"
        ),
        (
            f"Runs: {_number(direct.get('calls'))} direct, "
            f"{_number(external.get('calls'))} external, "
            f"{_number(direct.get('fallback_attempts'))} fallback attempts"
        ),
    ]
    repeated_tokens = _number(efficiency.get("repeated_prompt_tokens"))
    if repeated_tokens:
        lines.append(f"Repeated prompts: {repeated_tokens} reported tokens")
    failed_tokens = _number(report.get("failed_attempt_tokens"))
    if report.get("failed_attempts"):
        lines.append(
            f"Failed attempts: {_number(report.get('failed_attempts'))}; "
            f"{failed_tokens} reported tokens"
        )
    warnings_value = report.get("warnings", [])
    if isinstance(warnings_value, list):
        warnings = cast(list[Any], warnings_value)
        lines.extend(f"Warning: {warning}" for warning in warnings[:5])
    return "\n".join(lines)


def _decorate_group(group: dict[str, Any]) -> dict[str, Any]:
    value = dict(group)
    accounted, source = _accounted_tokens(value)
    value["accounted_tokens"] = accounted
    value["token_total_source"] = source
    value["telemetry_status"] = _group_telemetry_status(value)
    calls = _number(value.get("calls"))
    if calls:
        value["average_prompt_bytes"] = round(_number(value.get("prompt_bytes")) / calls)
        value["average_response_bytes"] = round(_number(value.get("response_bytes")) / calls)
        value["average_duration_ms"] = round(_number(value.get("duration_ms")) / calls)
        value["average_accounted_tokens"] = round(accounted / calls)
    return value


def _token_totals(
    direct: dict[str, Any], external: dict[str, Any], groups: list[dict[str, Any]]
) -> dict[str, int]:
    if groups:
        result = {field: sum(_number(group.get(field)) for group in groups) for field in TOKEN_FIELDS}
        result["accounted_tokens"] = sum(_number(group.get("accounted_tokens")) for group in groups)
        return result
    result = {
        field: _number(direct.get(field)) + _number(external.get(field)) for field in TOKEN_FIELDS
    }
    direct_total, _ = _accounted_tokens(direct)
    external_total, _ = _accounted_tokens(external)
    result["accounted_tokens"] = direct_total + external_total
    return result


def _model_totals(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for group in groups:
        model = _qualified_model(group)
        item = totals.setdefault(
            model,
            {
                "model": model,
                "calls": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "output_tokens": 0,
                "accounted_tokens": 0,
                "unknown_records": 0,
            },
        )
        item["calls"] += _number(group.get("calls"))
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "output_tokens",
            "accounted_tokens",
        ):
            item[field] += _number(group.get(field))
        item["unknown_records"] += _number(group.get("unknown_calls")) + _number(
            group.get("unknown_sessions")
        )
    return sorted(totals.values(), key=lambda item: item["accounted_tokens"], reverse=True)


def _qualified_model(group: dict[str, Any]) -> str:
    """Keep provider identity visible when aggregating heterogeneous runtimes."""
    model = str(group.get("model") or "unknown")
    if "/" in model:
        return model
    provider = str(group.get("provider") or "").strip()
    if provider:
        return f"{provider}/{model}"
    if str(group.get("runtime") or "").strip().lower() == "codex":
        return f"codex/{model}"
    return model


def _failure_hotspots(records: Any) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        return []
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw_record in cast(list[Any], records):
        if not isinstance(raw_record, dict):
            continue
        record = cast(dict[str, Any], raw_record)
        try:
            attempts_value: Any = json.loads(str(record.get("attempts_json") or "[]"))
        except json.JSONDecodeError:
            continue
        if not isinstance(attempts_value, list):
            continue
        attempts = cast(list[Any], attempts_value)
        for raw_attempt in attempts:
            if not isinstance(raw_attempt, dict):
                continue
            attempt = cast(dict[str, Any], raw_attempt)
            if attempt.get("success") is True:
                continue
            model = str(attempt.get("model") or record.get("model") or "unknown")
            key = (str(record.get("repo") or ""), str(record.get("role") or ""), model)
            item = grouped.setdefault(
                key,
                {
                    "repo": key[0],
                    "role": key[1],
                    "model": key[2],
                    "failed_attempts": 0,
                    "unknown_failed_attempts": 0,
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "cache_write_tokens": 0,
                    "reasoning_tokens": 0,
                    "output_tokens": 0,
                    "accounted_tokens": 0,
                },
            )
            item["failed_attempts"] += 1
            if not _has_token_telemetry(attempt):
                item["unknown_failed_attempts"] += 1
                continue
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "output_tokens",
            ):
                item[field] += _number(attempt.get(field))
            accounted, _ = _accounted_tokens(attempt)
            item["accounted_tokens"] += accounted
    return sorted(
        grouped.values(),
        key=lambda item: (item["accounted_tokens"], item["failed_attempts"]),
        reverse=True,
    )[:10]


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
    mismatch_attempts = _number(direct.get("model_mismatch_attempts")) + _number(
        external.get("model_mismatch_attempts")
    )
    reasoning_tokens = _number(direct.get("reasoning_tokens")) + _number(
        external.get("reasoning_tokens")
    )
    output_tokens = _number(direct.get("output_tokens")) + _number(external.get("output_tokens"))
    unknown_records = _number(direct.get("unknown_calls")) + _number(
        external.get("unknown_sessions")
    )
    total_records = direct_calls + external_calls
    repeated = [
        {
            "repo": item.get("repo"),
            "role": item.get("role"),
            "model": item.get("model"),
            "prompt_fingerprint": item.get("prompt_fingerprint"),
            "calls": _number(item.get("calls")),
            "accounted_tokens": _number(item.get("accounted_tokens")),
            "prompt_bytes": _number(item.get("prompt_bytes")),
            "duration_ms": _number(item.get("duration_ms")),
            "telemetry_status": item.get("telemetry_status"),
        }
        for item in repeated_prompts
    ]
    return {
        "fallback_attempts": fallback_attempts,
        "model_mismatch_attempts": mismatch_attempts,
        "reasoning_tokens": reasoning_tokens,
        "reasoning_token_ratio_of_output": round(reasoning_tokens / output_tokens, 4)
        if output_tokens
        else 0.0,
        "fallback_attempts_per_direct_call": round(fallback_attempts / direct_calls, 4)
        if direct_calls
        else 0.0,
        "unknown_records": unknown_records,
        "unknown_record_rate": round(unknown_records / total_records, 4) if total_records else 0.0,
        "average_direct_prompt_bytes": round(_number(direct.get("prompt_bytes")) / direct_calls)
        if direct_calls
        else 0,
        "average_direct_duration_ms": round(_number(direct.get("duration_ms")) / direct_calls)
        if direct_calls
        else 0,
        "large_prompt_groups": [
            _group_identity(group, "average_prompt_bytes")
            for group in direct_groups
            if _number(group.get("average_prompt_bytes")) >= 80_000
        ],
        "large_context_window_groups": [
            _group_identity(group, "max_context_tokens")
            for group in external_groups
            if _number(group.get("max_context_tokens")) >= 100_000
        ],
        "repeated_prompt_groups": repeated,
        "repeated_prompt_calls": sum(_number(item.get("calls")) for item in repeated),
        "repeated_prompt_tokens": sum(_number(item.get("accounted_tokens")) for item in repeated),
    }


def _warnings(
    *,
    config: UsageConfig,
    unknown_records: int,
    aggregate_only_records: int,
    aggregate_only_tokens: int,
    stale_total_sessions: int,
    stale_total_tokens: int,
    legacy_calls: int,
    failed_attempts: int,
    failed_attempt_tokens: int,
    efficiency: dict[str, Any],
    token_totals: dict[str, int],
    model_totals: list[dict[str, Any]],
) -> list[str]:
    warnings: list[str] = []
    if unknown_records:
        warnings.append(f"{unknown_records} usage records have no provider token telemetry")
    if aggregate_only_records:
        warnings.append(
            f"{aggregate_only_records} records expose aggregate totals only "
            f"({aggregate_only_tokens} tokens)"
        )
    if stale_total_sessions:
        warnings.append(
            f"{stale_total_sessions} external session totals are stale "
            f"({stale_total_tokens} tokens)"
        )
    if efficiency["fallback_attempts"]:
        warnings.append(f"{efficiency['fallback_attempts']} fallback model attempts occurred")
    if efficiency["model_mismatch_attempts"]:
        warnings.append(
            f"{efficiency['model_mismatch_attempts']} attempts resolved to a model outside the requested route"
        )
    if efficiency["large_prompt_groups"]:
        warnings.append(
            f"{len(efficiency['large_prompt_groups'])} model groups average at least 80 KB of prompt input"
        )
    if efficiency["large_context_window_groups"]:
        warnings.append(
            f"{len(efficiency['large_context_window_groups'])} worker groups expose context windows of at least 100k tokens"
        )
    if efficiency["repeated_prompt_groups"]:
        warnings.append(
            f"{len(efficiency['repeated_prompt_groups'])} prompt fingerprints were sent repeatedly "
            f"using {efficiency['repeated_prompt_tokens']} reported tokens"
        )
    if failed_attempts:
        warnings.append(
            f"{failed_attempts} failed model attempts used {failed_attempt_tokens} reported tokens"
        )
    if legacy_calls:
        warnings.append(f"{legacy_calls} direct records came from a legacy or unknown runtime")
    if config.daily_token_limit is not None and token_totals["accounted_tokens"] >= config.daily_token_limit:
        warnings.append(
            f"daily token limit reached: {token_totals['accounted_tokens']} of {config.daily_token_limit}"
        )
    for model, limit in config.model_daily_token_limits.items():
        consumed = sum(
            _number(item.get("accounted_tokens"))
            for item in model_totals
            if _model_matches(str(item.get("model") or ""), model)
        )
        if consumed >= limit:
            warnings.append(f"model token limit reached for {model}: {consumed} of {limit}")
    return warnings


def _accounted_tokens(value: dict[str, Any]) -> tuple[int, str]:
    total = _optional_number(value.get("total_tokens"))
    if total is not None:
        return total, "provider_total"
    input_tokens = _optional_number(value.get("input_tokens"))
    output_tokens = _optional_number(value.get("output_tokens"))
    if input_tokens is not None or output_tokens is not None:
        return (input_tokens or 0) + (output_tokens or 0), "components"
    return 0, "unknown"


def _group_telemetry_status(value: dict[str, Any]) -> str:
    if not _has_token_telemetry(value):
        return "unknown"
    if value.get("input_tokens") is not None and value.get("output_tokens") is not None:
        return "complete"
    return "partial"


def _telemetry_status(total: int, measured: int, breakdown: int) -> str:
    if total == 0 or measured == 0:
        return "unknown"
    if measured == total and breakdown == total:
        return "complete"
    return "partial"


def _has_token_telemetry(value: dict[str, Any]) -> bool:
    return any(value.get(field) is not None for field in TOKEN_FIELDS)


def _group_identity(group: dict[str, Any], metric: str) -> dict[str, Any]:
    return {
        "repo": group.get("repo"),
        "role": group.get("role"),
        "provider": group.get("provider"),
        "model": group.get("model"),
        metric: _number(group.get(metric)),
    }


def _model_matches(actual: str, configured: str) -> bool:
    return models_match(actual, configured)


def _number(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _optional_number(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


__all__ = ["build_usage_report", "dispatch_budget", "usage_summary_text"]
