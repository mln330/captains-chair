"""Structured milestone test evidence and deterministic validation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from make_it_so.models import TestEvidencePolicy


@dataclass(frozen=True)
class EvidenceValidation:
    allowed: bool
    reason: str
    summary: dict[str, Any]


def _int_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _float_value(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        normalized = value.strip().removesuffix("%").strip()
        try:
            return float(normalized)
        except ValueError:
            return None
    return None


def _first_value(raw: Mapping[str, object], *keys: str) -> object | None:
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return None


def _string_list(value: object) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in cast(list[object], value) if str(item).strip()]


def _artifact(value: object, *, default_kind: str = "other") -> dict[str, Any] | None:
    if isinstance(value, str) and value.strip():
        return {"kind": default_kind, "title": value.strip(), "url": value.strip()}
    if not isinstance(value, Mapping):
        return None
    raw = cast(Mapping[str, object], value)
    url = str(_first_value(raw, "url", "href", "path") or "").strip()
    if not url:
        return None
    item: dict[str, Any] = {
        "kind": str(_first_value(raw, "kind", "type") or default_kind).strip() or default_kind,
        "title": str(_first_value(raw, "title", "name", "label") or url).strip(),
        "url": url,
    }
    for source, target in (
        ("path", "path"),
        ("mime_type", "mime_type"),
        ("mimeType", "mime_type"),
        ("viewport", "viewport"),
        ("description", "description"),
    ):
        raw_value = raw.get(source)
        if raw_value not in (None, ""):
            item[target] = raw_value
    return item


def _artifacts(raw: Mapping[str, object]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for key, default_kind in (("artifacts", "other"), ("screenshots", "screenshot")):
        value = raw.get(key)
        if isinstance(value, list):
            candidates = cast(list[object], value)
        elif value is None:
            candidates = []
        else:
            candidates = [value]
        for candidate in candidates:
            item = _artifact(candidate, default_kind=default_kind)
            if item is not None:
                values.append(item)
    for url in _string_list(raw.get("screenshot_urls")):
        values.append({"kind": "screenshot", "title": url, "url": url})
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in values:
        key = (str(item.get("kind") or "other"), str(item.get("url") or ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _looks_like_test_evidence(raw: Mapping[str, object]) -> bool:
    keys = set(raw)
    count_keys = {
        "tests_total",
        "total_tests",
        "total",
        "tests_passed",
        "passed_tests",
        "passed",
        "pass_rate",
        "passRate",
    }
    identity_keys = {"head_sha", "headSha", "command", "commands", "test_command"}
    return bool(keys.intersection(count_keys) and keys.intersection(identity_keys))


def _marked_test_evidence(value: object) -> dict[str, object] | None:
    if not isinstance(value, str):
        return None
    match = re.search(r"MAKE_IT_SO_TEST_EVIDENCE_JSON:(\{[^\r\n]+\})", value)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return cast(dict[str, object], parsed) if isinstance(parsed, dict) else None


def _candidate_mappings(value: object, depth: int = 0) -> list[Mapping[str, object]]:
    if depth > 5:
        return []
    if isinstance(value, Mapping):
        raw = cast(Mapping[str, object], value)
        candidates: list[Mapping[str, object]] = [raw]
        for key in ("test_evidence", "testEvidence", "test_evidence_record", "testEvidenceRecord"):
            if key in raw:
                candidates.extend(_candidate_mappings(raw[key], depth + 1))
        for key in ("evidence", "proof"):
            nested = raw.get(key)
            if isinstance(nested, Mapping):
                candidates.extend(_candidate_mappings(cast(Mapping[str, object], nested), depth + 1))
            elif isinstance(nested, list):
                candidates.extend(_candidate_mappings(cast(list[object], nested), depth + 1))
        marked = _marked_test_evidence(raw.get("note"))
        if marked is not None:
            candidates.extend(_candidate_mappings(marked, depth + 1))
        return candidates
    if isinstance(value, (list, tuple)):
        candidates = []
        for item in value:
            candidates.extend(_candidate_mappings(item, depth + 1))
        return candidates
    return []


def extract_test_evidence(value: object) -> dict[str, Any] | None:
    """Extract and normalize the newest structured test-evidence object."""
    selected: Mapping[str, object] | None = None
    for candidate in _candidate_mappings(value):
        if _looks_like_test_evidence(candidate):
            selected = candidate
            break
    if selected is None:
        return None

    total = _int_value(_first_value(selected, "tests_total", "total_tests", "total"))
    passed = _int_value(_first_value(selected, "tests_passed", "passed_tests", "passed"))
    failed = _int_value(_first_value(selected, "tests_failed", "failed_tests", "failed"))
    skipped = _int_value(_first_value(selected, "tests_skipped", "skipped_tests", "skipped")) or 0
    commands = _string_list(_first_value(selected, "commands", "test_commands"))
    command = _first_value(selected, "command", "test_command")
    if command is not None:
        commands = [str(command).strip(), *commands]
    commands = list(dict.fromkeys(item for item in commands if item))
    if total is None and passed is not None and failed is not None:
        total = passed + failed + skipped
    executed = (passed or 0) + (failed or 0)
    supplied_rate = _float_value(_first_value(selected, "pass_rate", "passRate"))
    calculated_rate = round((passed or 0) * 100 / executed, 2) if executed else None
    artifacts = _artifacts(selected)
    screenshots = [item for item in artifacts if item.get("kind") == "screenshot"]
    return {
        "status": str(selected.get("status") or "passed").strip().lower(),
        "head_sha": str(_first_value(selected, "head_sha", "headSha", "sha") or "").strip(),
        "command": commands[0] if commands else None,
        "commands": commands,
        "tests_total": total,
        "tests_passed": passed,
        "tests_failed": failed,
        "tests_skipped": skipped,
        "executed_tests": executed,
        "pass_rate": supplied_rate if supplied_rate is not None else calculated_rate,
        "calculated_pass_rate": calculated_rate,
        "artifacts": artifacts,
        "screenshots": screenshots,
        "model": str(selected.get("model") or "").strip() or None,
        "provider": str(selected.get("provider") or "").strip() or None,
        "captured_at": str(selected.get("captured_at") or selected.get("capturedAt") or "").strip() or None,
        "summary": str(selected.get("summary") or selected.get("notes") or "").strip() or None,
    }


def validate_test_evidence(
    value: object,
    policy: TestEvidencePolicy,
    current_head_sha: str | None = None,
    *,
    require_screenshot: bool = False,
) -> EvidenceValidation:
    """Validate evidence without trusting a worker's prose status."""
    required_screenshots = max(
        policy.minimum_screenshots,
        1 if policy.require_screenshot or require_screenshot else 0,
    )
    parsed = extract_test_evidence(value)
    base = {
        "required": policy.required,
        "minimum_pass_rate": policy.minimum_pass_rate,
        "required_screenshots": required_screenshots,
    }
    if not policy.required:
        return EvidenceValidation(
            True, "test evidence is optional for this milestone", {**base, "status": "optional"}
        )
    if parsed is None:
        return EvidenceValidation(False, "structured test evidence is missing", {**base, "status": "missing"})
    summary = {**base, **parsed}
    if parsed["status"] != "passed":
        return EvidenceValidation(
            False, "test evidence is not marked passed", {**summary, "status": "failed"}
        )
    head_sha = str(parsed.get("head_sha") or "")
    if current_head_sha and head_sha.lower() != current_head_sha.lower():
        return EvidenceValidation(
            False,
            f"test evidence is stale for current head {current_head_sha}",
            {**summary, "status": "stale"},
        )
    total = parsed.get("tests_total")
    passed = parsed.get("tests_passed")
    failed = parsed.get("tests_failed")
    executed = parsed.get("executed_tests")
    if not isinstance(total, int) or total <= 0 or not isinstance(passed, int) or not isinstance(failed, int):
        return EvidenceValidation(
            False,
            "test evidence needs positive totals and passed/failed counts",
            {**summary, "status": "invalid"},
        )
    if (
        not isinstance(executed, int)
        or executed != passed + failed
        or executed <= 0
        or passed + failed > total
    ):
        return EvidenceValidation(
            False, "test evidence counts are inconsistent", {**summary, "status": "invalid"}
        )
    calculated_rate = parsed.get("calculated_pass_rate")
    supplied_rate = parsed.get("pass_rate")
    if not isinstance(calculated_rate, (int, float)):
        return EvidenceValidation(
            False, "test evidence cannot calculate a pass rate", {**summary, "status": "invalid"}
        )
    if isinstance(supplied_rate, (int, float)) and abs(float(supplied_rate) - float(calculated_rate)) > 0.01:
        return EvidenceValidation(
            False, "reported pass rate does not match test counts", {**summary, "status": "invalid"}
        )
    if float(calculated_rate) < policy.minimum_pass_rate:
        return EvidenceValidation(
            False,
            f"pass rate {calculated_rate:g}% is below {policy.minimum_pass_rate:g}%",
            {**summary, "status": "failed"},
        )
    if policy.require_command and not parsed.get("commands"):
        return EvidenceValidation(
            False, "test evidence does not include a command", {**summary, "status": "incomplete"}
        )
    screenshots = parsed.get("screenshots")
    if not isinstance(screenshots, list):
        return EvidenceValidation(
            False,
            f"test evidence needs at least {required_screenshots} screenshot(s)",
            {**summary, "status": "incomplete"},
        )
    if len(cast(list[object], screenshots)) < required_screenshots:
        return EvidenceValidation(
            False,
            f"test evidence needs at least {required_screenshots} screenshot(s)",
            {**summary, "status": "incomplete"},
        )
    return EvidenceValidation(
        True, "test evidence passed", {**summary, "status": "passed", "pass_rate": calculated_rate}
    )


__all__ = ["EvidenceValidation", "extract_test_evidence", "validate_test_evidence"]
