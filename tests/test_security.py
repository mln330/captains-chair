from pathlib import Path

from make_it_so.security import safe_changed_paths, scan_secrets


def test_safe_changed_paths_excludes_escape_sensitive_and_generated_paths(tmp_path: Path) -> None:
    safe, excluded = safe_changed_paths(
        tmp_path,
        ["src/app.py", "../outside.txt", ".env", "obj/app.dll", ".git/config"],
    )

    assert safe == ["src/app.py"]
    assert excluded == ["../outside.txt", ".env", "obj/app.dll", ".git/config"]


def test_scan_secrets_detects_credentials_and_private_keys(tmp_path: Path) -> None:
    config = tmp_path / "config.py"
    config.write_text("client_secret = 'not-a-real-but-long-secret-value'", encoding="utf-8")
    key = tmp_path / "key.txt"
    key.write_text("-----BEGIN PRIVATE KEY-----", encoding="utf-8")
    ignored = tmp_path / "missing.txt"

    assert scan_secrets(tmp_path, ["config.py", "key.txt", "missing.txt"]) == [
        "config.py",
        "key.txt",
    ]
    assert scan_secrets(tmp_path, [str(ignored)]) == []
