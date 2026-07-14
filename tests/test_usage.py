import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from captains_chair.command import CommandResult
from captains_chair.harness import CodexAdapter
from captains_chair.models import HarnessConfig, ModelTarget, RoleModels, UsageConfig, UsageRate
from captains_chair.openclaw_usage import sync_openclaw_sessions
from captains_chair.state import StateStore
from captains_chair.usage import build_usage_report, dispatch_budget, usage_summary_text


def test_codex_usage_is_recorded_without_retaining_response_content(tmp_path: Path) -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        output_path = Path(command[list(command).index("--output-last-message") + 1])
        output_path.write_text('{"value":"ok"}', encoding="utf-8")
        return CommandResult(
            0,
            '{"type":"turn.completed","model":"gpt-5.3-codex",'
            '"usage":{"input_tokens":100,"cached_input_tokens":20,'
            '"output_tokens_details":{"reasoning_tokens":8},'
            '"output_tokens":30,"total_tokens":130}}',
            "",
        )

    result = CodexAdapter(
        HarnessConfig(kind="codex", executable="codex", timeout_seconds=30), runner
    ).run(
        prompt="Return structured output.",
        models=RoleModels(primary=ModelTarget(model="gpt-5.3-codex")),
        role="coder",
        output_model=_Output,
        cwd=tmp_path,
        writable=True,
    )
    attempt = result.attempts[0]
    assert attempt.input_tokens == 100
    assert attempt.cached_input_tokens == 20
    assert attempt.reasoning_tokens == 8
    assert attempt.output_tokens == 30
    assert attempt.total_tokens == 130
    assert attempt.reported_model == "gpt-5.3-codex"
    assert attempt.prompt_bytes > 0
    assert attempt.response_bytes > 0


def test_openclaw_sessions_are_imported_idempotently(tmp_path: Path) -> None:
    output = '{"sessions":[{"key":"agent:github-coder:subagent:workboard-printhub-1",' \
        '"agentId":"github-coder","model":"gpt-5.3-codex","modelProvider":"codex",' \
        '"inputTokens":100,"reasoningTokens":8,"outputTokens":30,"totalTokens":130,' \
        '"totalTokensFresh":false,' \
        '"contextTokens":272000}]}'

    def runner(command: Sequence[str], *, timeout: int = 60, **_: object) -> CommandResult:
        del command, timeout
        return CommandResult(0, output, "")

    state = StateStore(tmp_path / "state.db")
    first = sync_openclaw_sessions(
        state,
        repo="NewmanZone/PrintHub",
        executable="openclaw",
        runner=runner,
        expected_models={"coder": "codex/gpt-5.3-codex"},
    )
    second = sync_openclaw_sessions(
        state,
        repo="NewmanZone/PrintHub",
        executable="openclaw",
        runner=runner,
        expected_models={"coder": "codex/gpt-5.3-codex"},
    )
    assert first["sessions_imported"] == second["sessions_imported"] == 1
    assert first["session_limit"] == 1000
    assert first["session_limit_reached"] is False
    summary = state.usage_summary(repo="NewmanZone/PrintHub")
    assert summary["external_sessions"]["calls"] == 1
    assert summary["external_sessions"]["total_tokens"] == 130
    assert summary["external_sessions"]["reasoning_tokens"] == 8
    assert summary["external_sessions"]["max_context_tokens"] == 272000
    assert summary["external_sessions"]["stale_total_sessions"] == 1
    assert summary["external_sessions"]["stale_total_tokens"] == 130
    assert summary["external_sessions"]["model_mismatch_attempts"] == 0
    report = build_usage_report(summary, UsageConfig())
    assert report["efficiency"]["large_context_window_groups"] == [
        {
            "repo": "NewmanZone/PrintHub",
            "role": "coder",
            "provider": "codex",
            "model": "gpt-5.3-codex",
            "max_context_window_tokens": 272000,
            "average_context_window_tokens": 272000,
        }
    ]
    assert report["telemetry"]["stale_total_sessions"] == 1
    assert report["telemetry"]["stale_total_tokens"] == 130
    assert any("marked stale" in warning for warning in report["warnings"])
    assert any("context windows" in warning for warning in report["warnings"])


def test_openclaw_usage_accepts_codex_openai_route_alias(tmp_path: Path) -> None:
    output = (
        '{"sessions":[{"key":"agent:github-coder:subagent:workboard-printhub-alias",'
        '"agentId":"github-coder","model":"openai/gpt-5.3-codex",'
        '"inputTokens":100,"outputTokens":30,"totalTokens":130}]}'
    )

    def runner(command: Sequence[str], *, timeout: int = 60, **_: object) -> CommandResult:
        del command, timeout
        return CommandResult(0, output, "")

    state = StateStore(tmp_path / "state.db")
    sync_openclaw_sessions(
        state,
        repo="NewmanZone/PrintHub",
        runner=runner,
        expected_models={"coder": "codex/gpt-5.3-codex"},
    )

    assert state.usage_summary(repo="NewmanZone/PrintHub")["external_sessions"][
        "model_mismatch_attempts"
    ] == 0


def test_openclaw_session_import_uses_a_bounded_limit(tmp_path: Path) -> None:
    commands: list[Sequence[str]] = []

    def runner(command: Sequence[str], *, timeout: int = 60, **_: object) -> CommandResult:
        del timeout
        commands.append(command)
        limit = int(command[-1])
        rows = [{"key": f"session-{index}"} for index in range(limit)]
        return CommandResult(0, json.dumps({"sessions": rows}), "")

    state = StateStore(tmp_path / "state.db")
    first = sync_openclaw_sessions(state, repo="repo/project", runner=runner)
    second = sync_openclaw_sessions(state, repo="repo/project", runner=runner, session_limit=37)

    assert commands[0][-2:] == ["--limit", "1000"]
    assert commands[1][-2:] == ["--limit", "37"]
    assert first["session_limit_reached"] is True
    assert second["session_limit_reached"] is True

    with pytest.raises(ValueError, match="session_limit"):
        sync_openclaw_sessions(state, repo="repo/project", runner=runner, session_limit=10001)


def test_openclaw_usage_reports_worker_model_route_mismatch(tmp_path: Path) -> None:
    output = '{"sessions":[{"key":"agent:github-coder:subagent:workboard-printhub-mismatch",' \
        '"agentId":"github-coder","model":"gpt-5.6-sol","modelProvider":"codex",' \
        '"inputTokens":100,"outputTokens":30,"totalTokens":130}]}'

    def runner(command: Sequence[str], *, timeout: int = 60, **_: object) -> CommandResult:
        del command, timeout
        return CommandResult(0, output, "")

    state = StateStore(tmp_path / "state.db")
    sync_openclaw_sessions(
        state,
        repo="NewmanZone/PrintHub",
        executable="openclaw",
        runner=runner,
        expected_models={"coder": "codex/gpt-5.3-codex"},
    )

    summary = state.usage_summary(repo="NewmanZone/PrintHub")
    assert summary["external_sessions"]["model_mismatch_attempts"] == 1
    report = build_usage_report(summary, UsageConfig())
    assert report["efficiency"]["model_mismatch_attempts"] == 1
    assert any("route different" in warning for warning in report["warnings"])


def test_openclaw_usage_matches_configured_long_role_names_to_agent_ids(tmp_path: Path) -> None:
    output = '{"sessions":[{"key":"agent:github-ux:subagent:workboard-printhub-ux",' \
        '"agentId":"github-ux","model":"gpt-5.5","modelProvider":"codex",' \
        '"inputTokens":100,"outputTokens":30,"totalTokens":130}]}'

    def runner(command: Sequence[str], *, timeout: int = 60, **_: object) -> CommandResult:
        del command, timeout
        return CommandResult(0, output, "")

    state = StateStore(tmp_path / "state.db")
    sync_openclaw_sessions(
        state,
        repo="NewmanZone/PrintHub",
        executable="openclaw",
        runner=runner,
        expected_models={"ux_reviewer": "codex/gpt-5.3-codex"},
    )

    assert state.usage_summary(repo="NewmanZone/PrintHub")["external_sessions"][
        "model_mismatch_attempts"
    ] == 1


def test_openclaw_direct_session_usage_enriches_without_double_counting(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.record_model_call(
        "repo/project",
        "run-1",
        "planner",
        "gpt-5.5",
        [{"model": "gpt-5.5", "success": True}],
        session_id="session-123",
    )
    output = (
        '{"sessions":[{"key":"agent:codex-harness:captains_chair:planner:session-123",'
        '"agentId":"codex-harness","model":"gpt-5.5",'
        '"modelProvider":"codex","inputTokens":100,"outputTokens":30,'
        '"totalTokens":130,"totalTokensFresh":true}]}'
    )

    def runner(command: Sequence[str], *, timeout: int = 60, **_: object) -> CommandResult:
        del command, timeout
        return CommandResult(0, output, "")

    result = sync_openclaw_sessions(
        state,
        repo="repo/project",
        executable="openclaw",
        runner=runner,
    )

    assert result["sessions_imported"] == 1
    summary = state.usage_summary(repo="repo/project")
    assert summary["direct_calls"]["measured_calls"] == 1
    assert summary["direct_calls"]["total_tokens"] == 130
    assert summary["external_sessions"]["calls"] == 0


def test_openclaw_fallback_sessions_merge_into_one_call_without_overwriting_costs(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.db")
    state.record_model_call(
        "repo/project",
        "run-1",
        "planner",
        "gpt-5.5",
        [
            {
                "model": "gpt-5.5",
                "session_id": "attempt-0:session-root",
                "success": False,
            },
            {
                "model": "gpt-5.3-codex",
                "session_id": "attempt-1:session-root",
                "success": True,
            },
        ],
        session_id="session-root",
    )
    output = (
        '{"sessions":['
        '{"key":"agent:codex-harness:captains_chair:planner:attempt-0:session-root",'
        '"agentId":"codex-harness","model":"gpt-5.5",'
        '"inputTokens":100,"outputTokens":30,"totalTokens":130},'
        '{"key":"agent:codex-harness:captains_chair:planner:attempt-1:session-root",'
        '"agentId":"codex-harness","model":"gpt-5.3-codex",'
        '"inputTokens":200,"outputTokens":40,"totalTokens":240}'
        ']}'
    )

    def runner(command: Sequence[str], *, timeout: int = 60, **_: object) -> CommandResult:
        del command, timeout
        return CommandResult(0, output, "")

    sync_openclaw_sessions(state, repo="repo/project", runner=runner)
    sync_openclaw_sessions(state, repo="repo/project", runner=runner)

    summary = state.usage_summary(repo="repo/project")
    assert summary["direct_calls"]["calls"] == 1
    assert summary["direct_calls"]["input_tokens"] == 300
    assert summary["direct_calls"]["output_tokens"] == 70
    assert summary["direct_calls"]["total_tokens"] == 370
    assert summary["external_sessions"]["calls"] == 0


def test_openclaw_usage_window_uses_source_activity_time(tmp_path: Path) -> None:
    output = '{"sessions":[{"key":"agent:github-coder:subagent:workboard-printhub-old",' \
        '"agentId":"github-coder","model":"gpt-5.3-codex","updatedAt":0,' \
        '"inputTokens":100,"outputTokens":30,"totalTokens":130}]}'

    def runner(command: Sequence[str], *, timeout: int = 60, **_: object) -> CommandResult:
        del command, timeout
        return CommandResult(0, output, "")

    state = StateStore(tmp_path / "state.db")
    sync_openclaw_sessions(
        state,
        repo="NewmanZone/PrintHub",
        executable="openclaw",
        runner=runner,
    )

    assert state.usage_summary(repo="NewmanZone/PrintHub", since="2000-01-01T00:00:00+00:00")[
        "external_sessions"
    ]["calls"] == 0


def test_usage_report_estimates_credits_and_warns_about_unknown_records(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.record_model_call(
        "repo/project",
        "run-1",
        "coder",
        "gpt-5.3-codex",
        [{"model": "gpt-5.3-codex", "success": True, "input_tokens": 1_000_000, "output_tokens": 10}],
    )
    state.record_model_call(
        "repo/project",
        "run-2",
        "reviewer",
        "gpt-5.5",
        [{"model": "gpt-5.5", "success": False, "error": "provider unavailable"}],
    )
    report = build_usage_report(
        state.usage_summary(repo="repo/project"),
        UsageConfig(
            rates={
                "gpt-5.3-codex": UsageRate(
                    input_credits_per_million=43.75,
                    output_credits_per_million=350,
                )
            }
        ),
    )
    assert report["estimated_credits"] == 43.7535
    assert report["cost_hotspots"][0]["role"] == "coder"
    assert report["cost_hotspots"][0]["estimated_credits"] == 43.7535
    assert report["cost_hotspots"][0]["cost_status"] == "estimated"
    assert report["failed_attempts"] == 1
    assert report["failure_hotspots"][0]["cost_status"] == "incomplete"
    assert any("no provider token telemetry" in warning for warning in report["warnings"])


def test_usage_report_does_not_charge_cached_input_at_the_uncached_rate() -> None:
    report = build_usage_report(
        {
            "direct_calls": {"calls": 1},
            "direct_groups": [
                {
                    "repo": "repo/project",
                    "role": "planner",
                    "model": "gpt-5.5",
                    "calls": 1,
                    "input_tokens": 1_000,
                    "cached_input_tokens": 400,
                    "output_tokens": 100,
                }
            ],
            "external_sessions": {"calls": 0},
            "external_groups": [],
        },
        UsageConfig(
            rates={
                "gpt-5.5": UsageRate(
                    input_credits_per_million=100,
                    cached_input_credits_per_million=10,
                    output_credits_per_million=200,
                )
            }
        ),
    )

    # input_tokens includes cached_input_tokens; only the remaining 600 input
    # tokens use the uncached rate.
    assert report["estimated_credits"] == 0.084
    assert report["direct_groups"][0]["uncached_input_tokens"] == 600


def test_usage_report_quantifies_failed_fallback_attempt_cost(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.record_model_call(
        "repo/project",
        "run-1",
        "coder",
        "gpt-5.3-codex",
        [
            {
                "model": "gpt-5.5",
                "success": False,
                "input_tokens": 1_000_000,
                "output_tokens": 100,
                "error": "provider unavailable",
            },
            {
                "model": "gpt-5.3-codex",
                "success": True,
                "input_tokens": 100,
                "output_tokens": 10,
            },
        ],
    )

    report = build_usage_report(
        state.usage_summary(repo="repo/project"),
        UsageConfig(
            rates={
                "gpt-5.5": UsageRate(input_credits_per_million=125, output_credits_per_million=750),
                "gpt-5.3-codex": UsageRate(input_credits_per_million=43.75, output_credits_per_million=350),
            }
        ),
    )

    assert report["failed_attempts"] == 1
    assert report["failed_attempt_estimated_credits"] == 125.075
    assert report["failure_hotspots"][0]["role"] == "coder"
    assert report["failure_hotspots"][0]["model"] == "gpt-5.5"
    assert report["failure_hotspots"][0]["cost_status"] == "estimated"


def test_usage_report_ranks_external_cost_hotspots_and_marks_incomplete_cost() -> None:
    report = build_usage_report(
        {
            "direct_calls": {"calls": 0},
            "direct_groups": [],
            "external_sessions": {"calls": 2, "unknown_sessions": 0},
            "external_groups": [
                {
                    "repo": "repo/project",
                    "role": "reviewer",
                    "provider": "codex",
                    "model": "gpt-5.3-codex",
                    "calls": 1,
                    "input_tokens": 1_000_000,
                    "output_tokens": 0,
                    "estimated_credits": 1.75,
                    "rate_card_model": True,
                    "usage_measured": True,
                },
                {
                    "repo": "repo/project",
                    "role": "coder",
                    "provider": "codex",
                    "model": "gpt-5.3-codex",
                    "calls": 1,
                    "total_tokens": 2_000_000,
                    "estimated_credits": 0.0,
                    "rate_card_model": True,
                    "usage_measured": True,
                },
            ],
        },
        UsageConfig(
            rates={
                "gpt-5.3-codex": UsageRate(input_credits_per_million=1.75),
            }
        ),
    )

    assert [item["role"] for item in report["cost_hotspots"]] == ["reviewer", "coder"]
    assert report["cost_hotspots"][1]["cost_status"] == "incomplete"
    assert report["telemetry"]["aggregate_only_records"] == 1
    assert report["telemetry"]["aggregate_only_tokens"] == 2_000_000
    assert report["telemetry"]["estimated_credits_are_lower_bound"] is True


def test_usage_report_surfaces_fallbacks_and_large_prompt_groups() -> None:
    report = build_usage_report(
        {
            "direct_calls": {
                "calls": 2,
                "fallback_attempts": 1,
                "unknown_calls": 0,
                "prompt_bytes": 180_000,
                "response_bytes": 4_000,
            },
            "direct_groups": [
                {
                    "repo": "repo/project",
                    "role": "planner",
                    "model": "gpt-5.5",
                    "calls": 2,
                    "fallback_attempts": 1,
                    "prompt_bytes": 180_000,
                    "response_bytes": 4_000,
                }
            ],
            "external_sessions": {"calls": 1, "unknown_sessions": 1},
            "external_groups": [],
        },
        UsageConfig(),
    )

    assert report["efficiency"] == {
        "fallback_attempts": 1,
        "model_mismatch_attempts": 0,
        "reasoning_tokens": 0,
        "reasoning_token_ratio_of_output": 0.0,
        "fallback_attempts_per_direct_call": 0.5,
        "unknown_records": 1,
        "unknown_record_rate": 0.3333,
        "direct_prompt_bytes": 180_000,
        "direct_response_bytes": 4_000,
        "average_direct_prompt_bytes": 90_000,
        "direct_duration_ms": 0,
        "average_direct_duration_ms": 0,
        "large_prompt_groups": [
            {
                "repo": "repo/project",
                "role": "planner",
                "model": "gpt-5.5",
                "average_prompt_bytes": 90_000,
            }
        ],
        "large_context_window_groups": [],
        "repeated_prompt_groups": [],
        "repeated_prompt_calls": 0,
        "repeated_prompt_estimated_credits": 0,
    }
    assert any("fallback model attempts" in warning for warning in report["warnings"])
    assert any("80 KB" in warning for warning in report["warnings"])


def test_usage_report_surfaces_repeated_prompt_fingerprints_and_latency(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    attempts = [{
        "model": "gpt-5.3-codex",
        "success": True,
        "input_tokens": 100,
        "output_tokens": 20,
        "duration_ms": 1250,
        "prompt_bytes": 400,
    }]
    state.record_model_call("repo/project", "run-1", "planner", "gpt-5.3-codex", attempts, prompt="same context")
    state.record_model_call("repo/project", "run-2", "planner", "gpt-5.3-codex", attempts, prompt="same context")

    report = build_usage_report(
        state.usage_summary(repo="repo/project"),
        UsageConfig(
            rates={
                "gpt-5.3-codex": UsageRate(
                    input_credits_per_million=43.75,
                    cached_input_credits_per_million=4.375,
                    output_credits_per_million=350,
                )
            }
        ),
    )

    assert report["efficiency"]["direct_duration_ms"] == 2500
    assert report["efficiency"]["repeated_prompt_calls"] == 2
    assert report["efficiency"]["repeated_prompt_groups"][0]["calls"] == 2
    assert report["repeated_prompt_estimated_credits"] == 0.0227
    assert report["token_cost_breakdown"] == {
        "input_credits": 0.0088,
        "cached_input_credits": 0.0,
        "output_credits": 0.014,
        "estimated_credits": 0.0227,
    }
    assert report["efficiency"]["repeated_prompt_groups"][0]["cost_status"] == "estimated"
    assert any("prompt fingerprints" in warning for warning in report["warnings"])
    assert any("Repeated prompts consumed" in warning for warning in report["warnings"])


def test_usage_report_surfaces_model_route_mismatches(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.record_model_call(
        "repo/project",
        "run-1",
        "coder",
        "codex/gpt-5.3-codex",
        [{
            "model": "codex/gpt-5.3-codex",
            "success": False,
            "error": "model route mismatch: requested codex/gpt-5.3-codex, provider reported gpt-5.6-sol",
        }],
    )

    report = build_usage_report(state.usage_summary(repo="repo/project"), UsageConfig())

    assert report["efficiency"]["model_mismatch_attempts"] == 1
    assert any("route different" in warning for warning in report["warnings"])


def test_usage_report_surfaces_reasoning_tokens(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.record_model_call(
        "repo/project",
        "run-1",
        "planner",
        "gpt-5.5",
        [{
            "model": "gpt-5.5",
            "success": True,
            "reasoning_tokens": 80,
            "output_tokens": 100,
        }],
    )

    report = build_usage_report(state.usage_summary(repo="repo/project"), UsageConfig())

    assert report["efficiency"]["reasoning_tokens"] == 80
    assert report["efficiency"]["reasoning_token_ratio_of_output"] == 0.8
    assert any("reasoning tokens" in warning for warning in report["warnings"])


def test_reasoning_only_direct_usage_remains_unknown_and_blocks_dispatch(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.record_model_call(
        "repo/project",
        "run-reasoning-only",
        "reviewer",
        "gpt-5.5",
        [{
            "model": "gpt-5.5",
            "success": True,
            "reasoning_tokens": 500,
        }],
    )

    summary = state.usage_summary(repo="repo/project")
    report = build_usage_report(summary, UsageConfig())

    assert summary["direct_calls"]["unknown_calls"] == 1
    assert report["telemetry"]["unknown_records"] == 1
    assert report["estimated_credits"] == 0
    decision = dispatch_budget(summary, UsageConfig(block_on_unknown=True))
    assert decision["allowed"] is False
    assert "telemetry is incomplete" in decision["reason"]


def test_dispatch_budget_allows_without_ceiling_and_blocks_at_ceiling() -> None:
    summary: dict[str, Any] = {
        "direct_calls": {"unknown_calls": 0},
        "direct_groups": [
            {"model": "gpt-5.3-codex", "input_tokens": 1_000_000, "output_tokens": 0}
        ],
        "external_sessions": {"unknown_sessions": 0},
        "external_groups": [],
    }
    rates = {"gpt-5.3-codex": UsageRate(input_credits_per_million=10)}

    assert dispatch_budget(summary, UsageConfig(rates=rates))["allowed"] is True
    decision = dispatch_budget(
        summary,
        UsageConfig(rates=rates, daily_budget_credits=10),
    )
    assert decision["allowed"] is False
    assert "budget" in decision["reason"]


def test_dispatch_budget_fails_closed_for_unpriced_measured_usage() -> None:
    summary: dict[str, Any] = {
        "direct_calls": {"unknown_calls": 0},
        "direct_groups": [
            {"model": "future-model", "input_tokens": 1_000_000, "output_tokens": 0}
        ],
        "external_sessions": {"unknown_sessions": 0},
        "external_groups": [],
    }

    decision = dispatch_budget(summary, UsageConfig(daily_budget_credits=10))

    assert decision["allowed"] is False
    assert decision["unpriced_groups"] == 1
    assert "rate card" in decision["reason"]


def test_dispatch_budget_fails_closed_for_total_only_usage() -> None:
    summary: dict[str, Any] = {
        "direct_calls": {"unknown_calls": 0},
        "direct_groups": [{"model": "gpt-5.3-codex", "total_tokens": 1_000_000}],
        "external_sessions": {"unknown_sessions": 0},
        "external_groups": [],
    }

    decision = dispatch_budget(
        summary,
        UsageConfig(
            rates={"gpt-5.3-codex": UsageRate(input_credits_per_million=10)},
            daily_budget_credits=10,
        ),
    )

    assert decision["allowed"] is False
    assert decision["incomplete_cost_groups"] == 1
    assert "input/output" in decision["reason"]


def test_dispatch_budget_can_allow_incomplete_telemetry_for_bounded_canary() -> None:
    summary: dict[str, Any] = {
        "direct_calls": {"unknown_calls": 0},
        "direct_groups": [{"model": "gpt-5.3-codex", "total_tokens": 1_000_000}],
        "external_sessions": {"unknown_sessions": 0},
        "external_groups": [],
    }

    decision = dispatch_budget(
        summary,
        UsageConfig(
            rates={"gpt-5.3-codex": UsageRate(input_credits_per_million=10)},
            daily_budget_credits=10,
            allow_incomplete_telemetry=True,
        ),
    )

    assert decision["allowed"] is True
    assert decision["incomplete_cost_groups"] == 1
    assert decision["reason"] == "usage is below the configured daily budget"


def test_dispatch_budget_blocks_unknown_telemetry_when_hard_budget_is_enabled() -> None:
    summary: dict[str, Any] = {
        "direct_calls": {"unknown_calls": 0},
        "direct_groups": [],
        "external_sessions": {"unknown_sessions": 1},
        "external_groups": [],
    }

    decision = dispatch_budget(
        summary,
        UsageConfig(rates={"gpt-5.3-codex": UsageRate(input_credits_per_million=10)}, daily_budget_credits=10),
    )

    assert decision == {
        "allowed": False,
        "reason": "usage telemetry is incomplete; new worker sessions are suppressed until it is reconciled",
        "estimated_credits": 0.0,
        "budget_credits": 10.0,
        "unknown_records": 1,
        "unpriced_groups": 0,
        "incomplete_cost_groups": 0,
    }


def test_dispatch_budget_blocks_unknown_telemetry_without_hard_budget() -> None:
    summary: dict[str, Any] = {
        "direct_calls": {"unknown_calls": 0},
        "direct_groups": [],
        "external_sessions": {"unknown_sessions": 1},
        "external_groups": [],
    }

    decision = dispatch_budget(summary, UsageConfig())

    assert decision["allowed"] is False
    assert decision["budget_credits"] is None
    assert "incomplete" in decision["reason"]


def test_dispatch_budget_can_allow_unknown_telemetry_when_explicitly_configured() -> None:
    summary: dict[str, Any] = {
        "direct_calls": {"unknown_calls": 0},
        "direct_groups": [],
        "external_sessions": {"unknown_sessions": 1},
        "external_groups": [],
    }

    decision = dispatch_budget(summary, UsageConfig(block_on_unknown=False))

    assert decision["allowed"] is True
    assert decision["reason"] == "no daily usage budget is configured"


def test_usage_schema_migrates_an_existing_state_database(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE model_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, "
            "run_id TEXT NOT NULL, role TEXT NOT NULL, resolved_model TEXT, attempts_json TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
    state = StateStore(path)
    state.record_model_call(
        "repo/project",
        "run-1",
        "coder",
        "gpt-5.3-codex",
        [{"input_tokens": 12, "output_tokens": 4, "total_tokens": 16}],
    )

    summary = state.usage_summary(repo="repo/project")

    assert summary["direct_calls"]["total_tokens"] == 16


def test_usage_provenance_separates_legacy_records_from_current_runtime(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.record_model_call(
        "repo/project",
        "legacy-run",
        "planner",
        "gpt-5.6-sol",
        [{"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}],
        runtime="legacy",
    )
    state.record_model_call(
        "repo/project",
        "captains_chair-run",
        "coder",
        "gpt-5.3-codex",
        [{"input_tokens": 200, "output_tokens": 40, "total_tokens": 240}],
        runtime="codex",
    )

    summary = state.usage_summary(repo="repo/project")
    report = build_usage_report(
        summary,
        UsageConfig(
            rates={
                "gpt-5.6-sol": UsageRate(input_credits_per_million=125, output_credits_per_million=750),
                "gpt-5.3-codex": UsageRate(input_credits_per_million=43.75, output_credits_per_million=350),
            }
        ),
    )

    assert report["telemetry"]["legacy_direct_calls"] == 1
    assert {item["runtime"] for item in report["cost_hotspots"]} == {"legacy", "codex"}
    assert any("legacy or unknown runtime" in warning for warning in report["warnings"])


def test_usage_summary_text_keeps_uncertainty_and_hotspots_visible() -> None:
    report = build_usage_report(
        {
            "direct_calls": {"calls": 2, "legacy_calls": 1, "fallback_attempts": 1, "unknown_calls": 1},
            "direct_groups": [
                {
                    "repo": "repo/project",
                    "runtime": "codex",
                    "role": "coder",
                    "model": "gpt-5.3-codex",
                    "calls": 1,
                    "input_tokens": 1000,
                    "output_tokens": 100,
                }
            ],
            "external_sessions": {"calls": 1, "unknown_sessions": 0},
            "external_groups": [],
        },
        UsageConfig(rates={"gpt-5.3-codex": UsageRate(input_credits_per_million=43.75)}),
    )

    text = usage_summary_text(report, repo="repo/project", since="2026-07-13T00:00:00Z")

    assert "CAPTAINS_CHAIR usage audit: repo/project since 2026-07-13T00:00:00Z" in text
    assert "legacy direct" in text
    assert "Top cost hotspots:" in text
    assert "Warnings:" in text


def test_usage_schema_migrates_context_tokens_on_existing_external_table(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE external_usage (source TEXT NOT NULL, external_id TEXT NOT NULL, "
            "repo TEXT NOT NULL, role TEXT NOT NULL, provider TEXT, model TEXT, "
            "input_tokens INTEGER, cached_input_tokens INTEGER, reasoning_tokens INTEGER, "
            "output_tokens INTEGER, total_tokens INTEGER, prompt_bytes INTEGER NOT NULL DEFAULT 0, "
            "response_bytes INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER NOT NULL DEFAULT 0, "
            "updated_at TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(source, external_id))"
        )
    state = StateStore(path)
    state.record_external_usage(
        {
            "source": "openclaw-session",
            "external_id": "session-1",
            "repo": "repo/project",
            "role": "coder",
            "context_tokens": 128_000,
            "updated_at": "2026-07-13T00:00:00+00:00",
        }
    )

    assert state.usage_summary(repo="repo/project")["external_sessions"]["max_context_tokens"] == 128_000


def test_usage_retention_prunes_old_direct_and_external_records(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.record_model_call(
        "repo/project",
        "old-direct",
        "coder",
        "gpt-5.3-codex",
        [{"input_tokens": 12, "output_tokens": 4, "total_tokens": 16}],
    )
    state.record_external_usage(
        {
            "source": "openclaw-session",
            "external_id": "old-session",
            "repo": "repo/project",
            "role": "coder",
            "updated_at": "2020-01-01T00:00:00+00:00",
            "input_tokens": 12,
            "output_tokens": 4,
            "total_tokens": 16,
        }
    )
    with closing(sqlite3.connect(tmp_path / "state.db")) as connection, connection:
        connection.execute(
            "UPDATE model_calls SET created_at=? WHERE run_id=?",
            ("2020-01-01T00:00:00+00:00", "old-direct"),
        )

    deleted = state.prune_usage(30, now=datetime(2025, 1, 1, tzinfo=UTC))

    assert deleted == {"model_calls": 1, "external_usage": 1}
    summary = state.usage_summary(repo="repo/project")
    assert summary["direct_calls"]["calls"] == 0
    assert summary["external_sessions"]["calls"] == 0

class _Output(BaseModel):
    value: str
