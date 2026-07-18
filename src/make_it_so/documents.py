from __future__ import annotations

import re

VOLATILE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("generated timestamp", re.compile(r"(?im)^\s*(?:[-*]\s*)?generated:\s*\d{4}-\d{2}-\d{2}")),
    ("live PR count", re.compile(r"(?im)^\s*(?:[-*]\s*)?open PRs?:\s*(?:none|\d+)\s*$")),
    ("commit SHA", re.compile(r"\b[0-9a-f]{12,40}\b", re.IGNORECASE)),
    ("latest CI claim", re.compile(r"(?i)latest (?:observed )?CI\s*:\s*(?:pass|success|green|fail)")),
    (
        "current deploy claim",
        re.compile(r"(?i)(?:current|latest) deploy(?:ment)?\s*:\s*(?:pass|success|fail|green|red)"),
    ),
)


def durable_document_findings(text: str) -> list[str]:
    return [name for name, pattern in VOLATILE_PATTERNS if pattern.search(text)]


def assert_durable_document(text: str) -> None:
    findings = durable_document_findings(text)
    if findings:
        raise ValueError("planning document contains volatile state: " + ", ".join(findings))


def normalize_durable_document(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        if re.match(r"(?i)^\s*(?:[-*]\s*)?generated:", line):
            continue
        if re.match(r"(?i)^\s*(?:[-*]\s*)?open PRs?:", line):
            continue
        line = re.sub(r"\b[0-9a-f]{12,40}\b", "<checked-live>", line, flags=re.IGNORECASE)
        lines.append(line.rstrip())
    normalized = "\n".join(lines).strip() + "\n"
    assert_durable_document(normalized)
    return normalized
