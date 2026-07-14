from __future__ import annotations

import re
from pathlib import Path

SENSITIVE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "secrets.json",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".sqlite", ".db"}
SECRET_PATTERNS = (
    re.compile(r"(?i)(?:api[_-]?key|client_secret|password|access_token)\s*[:=]\s*['\"]?[^\s'\"]{12,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"gh[opsu]_[A-Za-z0-9_]{20,}"),
)


def safe_changed_paths(repo_path: Path, paths: list[str]) -> tuple[list[str], list[str]]:
    safe: list[str] = []
    excluded: list[str] = []
    root = repo_path.resolve()
    for relative in paths:
        candidate = (root / relative).resolve()
        if root != candidate and root not in candidate.parents:
            excluded.append(relative)
            continue
        path = Path(relative)
        if path.name in SENSITIVE_NAMES or path.suffix.lower() in SENSITIVE_SUFFIXES:
            excluded.append(relative)
            continue
        if any(part in {".git", "node_modules", "bin", "obj", ".venv", "__pycache__"} for part in path.parts):
            excluded.append(relative)
            continue
        safe.append(path.as_posix())
    return safe, excluded


def scan_secrets(repo_path: Path, paths: list[str]) -> list[str]:
    findings: list[str] = []
    for relative in paths:
        path = repo_path / relative
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            findings.append(relative)
    return findings
