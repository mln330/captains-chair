import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

import captains_chair.openclaw_usage as openclaw_usage
from captains_chair.command import CommandResult
from captains_chair.harness import CodexAdapter
from captains_chair.models import HarnessConfig, ModelTarget, RoleModels, UsageConfig
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
            '{"type":"turn.completed","model":"gpt-5.3-codex-spark",'
            '"usage":{"input_tokens":100,"cached_input_tokens":20,'
            '"output_tokens_details":{"reasoning_tokens":8},'
            '"output_tokens":30,"total_tokens":130}}',
            "",
        )

    result = CodexAdapter(
        HarnessConfig(kind="codex", executable="codex", timeout_seconds=30), runner
    ).run(
        prompt="Return structured output.",
        models=RoleModels(primary=ModelTarget(model="gpt-5.3-codex-spark")),
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
    assert attempt.reported_model == "gpt-5.3-codex-spark"
    assert attempt.prompt_bytes > 0
    assert attempt.response_bytes > 0


def test_openclaw_sessions_are_imported_idempotently(tmp_path: Path) -> None:
    output = '{"sessions":[{"key":"agent:github-coder:subagent:workboard-printhub-1",' \
        '"agentId":"github-coder","model":"gpt-5.3-codex-spark","modelProvider":"codex",' \
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
        expected_models={"coder": "codex/gpt-5.3-codex-spark"},
    )
    second = sync_openclaw_sessions(
        state,
        repo="NewmanZone/PrintHub",
        executable="openclaw",
        runner=runner,
        expected_models={"coder": "codex/gpt-5.3-codex-spark"},
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
            "model": "gpt-5.3-codex-spark",
            "max_context_tokens": 272000,
        }
    ]
    assert report["telemetry"]["stale_total_sessions"] == 1
    assert report["telemetry"]["stale_total_tokens"] == 130
    assert any("totals are stale" in warning for warning in report["warnings"])
    assert any("context windows" in warning for warning in report["warnings"])


def test_openclaw_usage_accepts_codex_openai_route_alias(tmp_path: Path) -> None:
    output = (
        '{"sessions":[{"key":"agent:github-coder:subagent:workboard-printhub-alias",'
        '"agentId":"github-coder","model":"openai/gpt-5.3-codex-spark",'
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
        expected_models={"coder": "codex/gpt-5.3-codex-spark"},
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
        expected_models={"coder": "codex/gpt-5.3-codex-spark"},
    )

    summary = state.usage_summary(repo="NewmanZone/PrintHub")
    assert summary["external_sessions"]["model_mismatch_attempts"] == 1
    report = build_usage_report(summary, UsageConfig())
    assert report["efficiency"]["model_mismatch_attempts"] == 1
    assert any("outside the requested route" in warning for warning in report["warnings"])


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
        expected_models={"ux_reviewer": "codex/gpt-5.3-codex-spark"},
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


def test_openclaw_fallback_sessions_merge_into_one_call_without_double_counting_tokens(
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
                "model": "gpt-5.3-codex-spark",
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
        '"agentId":"codex-harness","model":"gpt-5.3-codex-spark",'
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
        '"agentId":"github-coder","model":"gpt-5.3-codex-spark","updatedAt":0,' \
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


def test_openclaw_usage_handles_prefixed_json_items_and_filters_rows(tmp_path: Path) -> None:
    output = "warning from openclaw\n" + json.dumps(
        {
            "items": [
                None,
                {"agentId": "github-final"},
                {
                    "key": "agent:github-final:session-1",
                    "agentId": "github-final",
                    "model": "gpt-5.5",
                    "modelProvider": "codex",
                    "input_tokens": 12.0,
                    "cached_input_tokens": 3,
                    "reasoning_tokens": 2,
                    "output_tokens": 5,
                    "total_tokens": 17,
                    "total_tokens_fresh": True,
                    "prompt_bytes": 10,
                    "response_bytes": 20,
                    "duration_ms": 30,
                },
                {"key": "agent:other:session-2", "agentId": "other"},
            ]
        }
    )

    def runner(command: Sequence[str], *, timeout: int = 60, **_: object) -> CommandResult:
        del command, timeout
        return CommandResult(0, output, "")

    state = StateStore(tmp_path / "state.db")
    result = sync_openclaw_sessions(
        state,
        repo="repo/project",
        runner=runner,
        expected_models={"final_reviewer": "codex/gpt-5.5"},
        session_filter="session-1",
    )

    assert result["sessions_seen"] == 4
    assert result["sessions_imported"] == 1
    assert result["sessions_with_usage"] == 1
    assert state.usage_summary(repo="repo/project")["external_sessions"]["calls"] == 1


def test_openclaw_usage_fails_closed_on_command_or_json_errors(tmp_path: Path) -> None:
    def failed_runner(command: Sequence[str], *, timeout: int = 60, **_: object) -> CommandResult:
        del command, timeout
        return CommandResult(1, "partial output", "gateway unavailable")

    with pytest.raises(RuntimeError, match="gateway unavailable"):
        sync_openclaw_sessions(StateStore(tmp_path / "failed.db"), repo="repo/project", runner=failed_runner)

    def invalid_runner(command: Sequence[str], *, timeout: int = 60, **_: object) -> CommandResult:
        del command, timeout
        return CommandResult(0, "no JSON here", "")

    with pytest.raises(RuntimeError, match="did not contain JSON"):
        sync_openclaw_sessions(StateStore(tmp_path / "invalid.db"), repo="repo/project", runner=invalid_runner)


def test_openclaw_usage_scalar_parsers_reject_invalid_values() -> None:
    assert openclaw_usage._integer(True) is None  # pyright: ignore[reportPrivateUsage]
    assert openclaw_usage._integer(-1) is None  # pyright: ignore[reportPrivateUsage]
    assert openclaw_usage._integer(3.5) is None  # pyright: ignore[reportPrivateUsage]
    assert openclaw_usage._integer(3.0) == 3  # pyright: ignore[reportPrivateUsage]
    assert openclaw_usage._integer("3") is None  # pyright: ignore[reportPrivateUsage]
    assert openclaw_usage._boolean(True) is True  # pyright: ignore[reportPrivateUsage]
    assert openclaw_usage._boolean("true") is None  # pyright: ignore[reportPrivateUsage]


def test_usage_report_tracks_tokens_and_warns_about_unknown_records(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.record_model_call(
        "repo/project",
        "run-1",
        "coder",
        "gpt-5.3-codex-spark",
        [{"model": "gpt-5.3-codex-spark", "success": True, "input_tokens": 1_000_000, "output_tokens": 10}],
    )
    state.record_model_call(
        "repo/project",
        "run-2",
        "reviewer",
        "gpt-5.5",
        [{"model": "gpt-5.5", "success": False, "error": "provider unavailable"}],
    )
    report = build_usage_report(state.usage_summary(repo="repo/project"), UsageConfig())

    assert report["token_totals"]["accounted_tokens"] == 1_000_010
    assert report["token_hotspots"][0]["role"] == "coder"
    assert report["token_hotspots"][0]["token_total_source"] == "components"
    assert report["failed_attempts"] == 1
    assert report["failure_hotspots"][0]["unknown_failed_attempts"] == 1
    assert any("no provider token telemetry" in warning for warning in report["warnings"])
    assert "credit" not in json.dumps(report).lower()


def test_usage_report_keeps_cached_input_as_a_non_additive_component() -> None:
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
        UsageConfig(),
    )

    assert report["token_totals"]["accounted_tokens"] == 1_100
    assert report["direct_groups"][0]["accounted_tokens"] == 1_100
    assert report["direct_groups"][0]["cached_input_tokens"] == 400
    assert report["direct_groups"][0]["accounted_tokens"] != 1_500


def test_usage_report_quantifies_failed_fallback_attempt_tokens(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.record_model_call(
        "repo/project",
        "run-1",
        "coder",
        "gpt-5.3-codex-spark",
        [
            {
                "model": "gpt-5.5",
                "success": False,
                "input_tokens": 1_000_000,
                "output_tokens": 100,
                "error": "provider unavailable",
            },
            {
                "model": "gpt-5.3-codex-spark",
                "success": True,
                "input_tokens": 100,
                "output_tokens": 10,
            },
        ],
    )

    report = build_usage_report(state.usage_summary(repo="repo/project"), UsageConfig())

    assert report["failed_attempts"] == 1
    assert report["failed_attempt_tokens"] == 1_000_100
    assert report["failure_hotspots"][0]["role"] == "coder"
    assert report["failure_hotspots"][0]["model"] == "gpt-5.5"
    assert report["failure_hotspots"][0]["accounted_tokens"] == 1_000_100


def test_usage_report_ranks_external_token_hotspots_and_marks_partial_telemetry() -> None:
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
                    "model": "gpt-5.3-codex-spark",
                    "calls": 1,
                    "input_tokens": 1_000_000,
                    "output_tokens": 0,
                },
                {
                    "repo": "repo/project",
                    "role": "coder",
                    "provider": "codex",
                    "model": "gpt-5.3-codex-spark",
                    "calls": 1,
                    "total_tokens": 2_000_000,
                },
            ],
        },
        UsageConfig(),
    )

    assert [item["role"] for item in report["token_hotspots"]] == ["coder", "reviewer"]
    assert report["token_hotspots"][0]["telemetry_status"] == "partial"
    assert report["token_hotspots"][0]["accounted_tokens"] == 2_000_000
    assert {item["model"] for item in report["model_totals"]} == {"codex/gpt-5.3-codex-spark"}


def test_usage_report_keeps_openai_and_codex_model_totals_separate() -> None:
    report = build_usage_report(
        {
            "direct_calls": {"calls": 0},
            "direct_groups": [],
            "external_sessions": {"calls": 2, "unknown_sessions": 0},
            "external_groups": [
                {"provider": "openai", "model": "gpt-5.5", "calls": 1, "total_tokens": 3_500_000},
                {"provider": "codex", "model": "gpt-5.5", "calls": 1, "total_tokens": 108_660},
            ],
        },
        UsageConfig(),
    )

    totals = {item["model"]: item["accounted_tokens"] for item in report["model_totals"]}
    assert totals == {"openai/gpt-5.5": 3_500_000, "codex/gpt-5.5": 108_660}


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

    assert report["efficiency"]["fallback_attempts"] == 1
    assert report["efficiency"]["fallback_attempts_per_direct_call"] == 0.5
    assert report["efficiency"]["unknown_record_rate"] == 0.3333
    assert report["efficiency"]["average_direct_prompt_bytes"] == 90_000
    assert report["efficiency"]["large_prompt_groups"][0]["role"] == "planner"
    assert any("fallback model attempts" in warning for warning in report["warnings"])
    assert any("80 KB" in warning for warning in report["warnings"])


def test_usage_report_surfaces_repeated_prompt_fingerprints_and_latency(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    attempts = [{
        "model": "gpt-5.3-codex-spark",
        "success": True,
        "input_tokens": 100,
        "output_tokens": 20,
        "duration_ms": 1250,
        "prompt_bytes": 400,
    }]
    state.record_model_call("repo/project", "run-1", "planner", "gpt-5.3-codex-spark", attempts, prompt="same context")
    state.record_model_call("repo/project", "run-2", "planner", "gpt-5.3-codex-spark", attempts, prompt="same context")

    report = build_usage_report(state.usage_summary(repo="repo/project"), UsageConfig())

    assert report["efficiency"]["average_direct_duration_ms"] == 1250
    assert report["efficiency"]["repeated_prompt_calls"] == 2
    assert report["efficiency"]["repeated_prompt_groups"][0]["calls"] == 2
    assert report["efficiency"]["repeated_prompt_tokens"] == 240
    assert report["efficiency"]["repeated_prompt_groups"][0]["telemetry_status"] == "complete"
    assert any("prompt fingerprints" in warning for warning in report["warnings"])


def test_usage_report_surfaces_model_route_mismatches(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.record_model_call(
        "repo/project",
        "run-1",
        "coder",
        "codex/gpt-5.3-codex-spark",
        [{
            "model": "codex/gpt-5.3-codex-spark",
            "success": False,
            "error": "model route mismatch: requested codex/gpt-5.3-codex-spark, provider reported gpt-5.6-sol",
        }],
    )

    report = build_usage_report(state.usage_summary(repo="repo/project"), UsageConfig())

    assert report["efficiency"]["model_mismatch_attempts"] == 1
    assert any("outside the requested route" in warning for warning in report["warnings"])


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
    assert report["token_totals"]["accounted_tokens"] == 0
    decision = dispatch_budget(summary, UsageConfig(block_on_unknown=True))
    assert decision["allowed"] is False
    assert "telemetry is incomplete" in decision["reason"]


def test_dispatch_budget_allows_without_limit_and_blocks_at_daily_limit() -> None:
    summary: dict[str, Any] = {
        "direct_calls": {"calls": 1, "unknown_calls": 0, "input_tokens": 1_000_000, "output_tokens": 0},
        "direct_groups": [
            {"model": "gpt-5.3-codex-spark", "input_tokens": 1_000_000, "output_tokens": 0}
        ],
        "external_sessions": {"unknown_sessions": 0},
        "external_groups": [],
    }
    assert dispatch_budget(summary, UsageConfig())["allowed"] is True
    decision = dispatch_budget(summary, UsageConfig(daily_token_limit=1_000_000))
    assert decision["allowed"] is False
    assert "token limit" in decision["reason"]
    assert decision["total_tokens"] == 1_000_000


def test_dispatch_budget_enforces_per_model_limit_with_route_aliases() -> None:
    summary: dict[str, Any] = {
        "direct_calls": {"calls": 1, "unknown_calls": 0, "input_tokens": 1_000_000, "output_tokens": 0},
        "direct_groups": [
            {"model": "openai/gpt-5.3-codex-spark", "input_tokens": 1_000_000, "output_tokens": 0}
        ],
        "external_sessions": {"unknown_sessions": 0},
        "external_groups": [],
    }

    decision = dispatch_budget(
        summary,
        UsageConfig(model_daily_token_limits={"codex/gpt-5.3-codex-spark": 1_000_000}),
    )

    assert decision["allowed"] is False
    assert decision["model_limits"][0]["reached"] is True
    assert "gpt-5.3-codex-spark" in decision["reason"]


def test_dispatch_budget_accepts_authoritative_total_only_usage() -> None:
    summary: dict[str, Any] = {
        "direct_calls": {"calls": 1, "unknown_calls": 0, "total_tokens": 1_000_000},
        "direct_groups": [{"model": "gpt-5.3-codex-spark", "total_tokens": 1_000_000}],
        "external_sessions": {"unknown_sessions": 0},
        "external_groups": [],
    }

    decision = dispatch_budget(summary, UsageConfig(daily_token_limit=1_000_001))

    assert decision["allowed"] is True
    assert decision["total_tokens"] == 1_000_000
    assert decision["telemetry_status"] == "partial"


def test_legacy_usage_config_keys_are_discarded_without_inventing_token_limits() -> None:
    config = UsageConfig.model_validate(
        {
            "daily_budget_credits": 10,
            "rates": {"gpt-5.5": {"input_credits_per_million": 125}},
            "block_on_unknown": False,
        }
    )

    assert config.daily_token_limit is None
    assert config.model_daily_token_limits == {}
    assert config.model_dump() == {
        "daily_token_limit": None,
        "model_daily_token_limits": {},
        "block_on_unknown": False,
        "allow_incomplete_telemetry": False,
        "retention_days": 90,
    }


def test_dispatch_budget_blocks_unknown_telemetry_when_configured() -> None:
    summary: dict[str, Any] = {
        "direct_calls": {"unknown_calls": 0},
        "direct_groups": [],
        "external_sessions": {"calls": 1, "unknown_sessions": 1},
        "external_groups": [],
    }

    decision = dispatch_budget(summary, UsageConfig())

    assert decision["allowed"] is False
    assert decision["daily_token_limit"] is None
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
    assert decision["reason"] == "no token limit is configured"


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
        "gpt-5.3-codex-spark",
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
        "gpt-5.3-codex-spark",
        [{"input_tokens": 200, "output_tokens": 40, "total_tokens": 240}],
        runtime="codex",
    )

    summary = state.usage_summary(repo="repo/project")
    report = build_usage_report(summary, UsageConfig())

    assert report["telemetry"]["legacy_direct_calls"] == 1
    assert {item["runtime"] for item in report["token_hotspots"]} == {"legacy", "codex"}
    assert report["token_totals"]["accounted_tokens"] == 360
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
                    "model": "gpt-5.3-codex-spark",
                    "calls": 1,
                    "input_tokens": 1000,
                    "output_tokens": 100,
                }
            ],
            "external_sessions": {"calls": 1, "unknown_sessions": 0},
            "external_groups": [],
        },
        UsageConfig(),
    )

    text = usage_summary_text(report, repo="repo/project", since="2026-07-13T00:00:00Z")

    assert "Captain's Chair token audit: repo/project since 2026-07-13T00:00:00Z" in text
    assert "Tokens: 1100 accounted" in text
    assert "Telemetry: partial" in text
    assert "fallback attempts" in text
    assert "Warning:" in text


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
        "gpt-5.3-codex-spark",
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
