from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from captains_chair.command import CommandRunner, run_command
from captains_chair.model_policy import models_match
from captains_chair.state import StateStore

_OPENCLAW_ROLE_ALIASES = {
    "ux": "ux_reviewer",
    "final": "final_reviewer",
    "merge": "merger",
    "verify": "verifier",
}
DEFAULT_SESSION_LIMIT = 1000
MAX_SESSION_LIMIT = 10000


def sync_openclaw_sessions(
    state: StateStore,
    *,
    repo: str,
    executable: str = "openclaw",
    runner: CommandRunner = run_command,
    session_filter: str | None = None,
    expected_models: Mapping[str, str] | None = None,
    session_limit: int = DEFAULT_SESSION_LIMIT,
) -> dict[str, Any]:
    """Import OpenClaw's metadata-only session usage into portable CAPTAINS_CHAIR state."""
    if not 1 <= session_limit <= MAX_SESSION_LIMIT:
        raise ValueError(f"session_limit must be between 1 and {MAX_SESSION_LIMIT}")
    result = runner(
        [executable, "sessions", "--all-agents", "--json", "--limit", str(session_limit)],
        timeout=120,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[:3000])
    value = _parse_json(result.stdout)
    rows_value: Any = value
    if isinstance(value, dict):
        mapping = cast(dict[str, Any], value)
        rows_value = mapping.get("sessions")
        if not isinstance(rows_value, list):
            rows_value = mapping.get("items", [])
    rows = cast(list[Any], rows_value) if isinstance(rows_value, list) else []
    repo_token = repo.rsplit("/", 1)[-1].lower()
    selected = 0
    measured = 0
    direct_session_ids = state.direct_session_ids(repo)
    for item in rows:
        if not isinstance(item, dict):
            continue
        row = cast(dict[str, Any], item)
        external_id = str(row.get("key") or row.get("sessionKey") or "").strip()
        if not external_id:
            continue
        haystack = external_id.lower()
        if (session_filter and session_filter.lower() not in haystack) or (
            not session_filter
            and repo_token not in haystack
            and external_id.rsplit(":", 1)[-1] not in direct_session_ids
        ):
            continue
        agent = str(row.get("agentId") or "unknown")
        role = agent.removeprefix("github-") or agent
        observed_model = row.get("model")
        expected_model = (
            (expected_models or {}).get(agent)
            or (expected_models or {}).get(role)
            or (expected_models or {}).get(_OPENCLAW_ROLE_ALIASES.get(role, ""))
        )
        model_mismatch = int(
            bool(expected_model)
            and bool(observed_model)
            and not models_match(str(expected_model), str(observed_model))
        )
        record = {
            "source": "openclaw-session",
            "external_id": external_id,
            "repo": repo,
            "role": role,
            "provider": row.get("modelProvider"),
            "model": observed_model,
            "expected_model": expected_model,
            "model_mismatch_count": model_mismatch,
            "input_tokens": _integer(row.get("inputTokens", row.get("input_tokens"))),
            "cached_input_tokens": _integer(
                row.get("cachedInputTokens", row.get("cached_input_tokens"))
            ),
            "reasoning_tokens": _integer(
                row.get("reasoningTokens", row.get("reasoning_tokens"))
            ),
            "output_tokens": _integer(row.get("outputTokens", row.get("output_tokens"))),
            "total_tokens": _integer(row.get("totalTokens", row.get("total_tokens"))),
            "total_tokens_fresh": _boolean(
                row.get("totalTokensFresh", row.get("total_tokens_fresh"))
            ),
            "context_tokens": _integer(row.get("contextTokens", row.get("context_tokens"))),
            "prompt_bytes": _integer(row.get("promptBytes", row.get("prompt_bytes"))) or 0,
            "response_bytes": _integer(row.get("responseBytes", row.get("response_bytes"))) or 0,
            "duration_ms": _integer(row.get("durationMs", row.get("duration_ms"))) or 0,
            "updated_at_ms": row.get("updatedAt"),
        }
        session_id = external_id.rsplit(":", 1)[-1]
        if session_id in direct_session_ids:
            state.enrich_model_call_usage(session_id, record, external_id=external_id)
        else:
            state.record_external_usage(record)
        selected += 1
        measured += int(any(record[key] is not None for key in ("input_tokens", "output_tokens", "total_tokens")))
    return {
        "repo": repo,
        "source": "openclaw-session",
        "sessions_seen": len(rows),
        "session_limit": session_limit,
        "session_limit_reached": len(rows) >= session_limit,
        "sessions_imported": selected,
        "sessions_with_usage": measured,
    }


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        object_start = text.find("{")
        if object_start >= 0 and (start < 0 or object_start < start):
            start = object_start
        if start < 0:
            raise RuntimeError("OpenClaw sessions output did not contain JSON") from None
        return json.loads(text[start:])


__all__ = ["DEFAULT_SESSION_LIMIT", "MAX_SESSION_LIMIT", "sync_openclaw_sessions"]
