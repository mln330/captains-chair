from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import pytest

from captains_chair.command import CommandResult
from captains_chair.models import OpenClawWorkboardConfig, WorkerAssignments
from captains_chair.openclaw_runtime import OpenClawRuntimeInstaller, sync_openclaw_auth_profiles
from captains_chair.openclaw_workboard import OpenClawWorkboardError


def runtime_config() -> OpenClawWorkboardConfig:
    return OpenClawWorkboardConfig(
        workers=WorkerAssignments(
            captain="captains-chair",
            coder="github-coder",
            reviewer="github-reviewer",
            tester="github-tester",
            ux_reviewer="github-ux",
            final_reviewer="github-final",
            merger="github-merge",
            verifier="github-verify",
        )
    )


def test_runtime_plan_is_portable_data_and_does_not_write(tmp_path: Path) -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return CommandResult(0, "warning\n[]", "")

    actions = OpenClawRuntimeInstaller(runtime_config(), runner).plan(tmp_path)

    assert len(actions) == 8
    assert {item.role for item in actions} == {
        "captain",
        "coder",
        "reviewer",
        "tester",
        "ux_reviewer",
        "final_reviewer",
        "merger",
        "verifier",
    }
    assert all(item.action == "create" for item in actions)
    assert not any(tmp_path.iterdir())


def test_runtime_install_creates_agents_and_role_protocols(tmp_path: Path) -> None:
    commands: list[Sequence[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(command)
        if list(command)[1:4] == ["agents", "list", "--json"]:
            return CommandResult(0, "[]", "")
        return CommandResult(0, json.dumps({"status": "created"}), "")

    OpenClawRuntimeInstaller(runtime_config(), runner).install(tmp_path)

    assert len([command for command in commands if "add" in command]) == 8
    coder = (tmp_path / "github-coder" / "AGENTS.md").read_text(encoding="utf-8")
    reviewer = (tmp_path / "github-reviewer" / "AGENTS.md").read_text(encoding="utf-8")
    assert "Never review, approve, or merge your own work" in coder
    assert "Do not edit files" in reviewer
    assert "USER_SECRET:" in coder
    assert "captains_chair worker-protocol complete" in coder
    assert "Never call `heartbeat_respond`" in coder
    merger = (tmp_path / "github-merge" / "AGENTS.md").read_text(encoding="utf-8")
    assert "merge-gate --repo <owner/repo>" in merger
    assert "Never invoke `gh pr merge` directly" in merger


def test_runtime_install_fails_closed_on_existing_model_mismatch(tmp_path: Path) -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return CommandResult(
            0,
            json.dumps([{"id": "captains-chair", "model": "unexpected/model"}]),
            "",
        )

    with pytest.raises(OpenClawWorkboardError, match="different model"):
        OpenClawRuntimeInstaller(runtime_config(), runner).install(tmp_path)


def test_runtime_plan_accepts_codex_openai_model_route_alias(tmp_path: Path) -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return CommandResult(
            0,
            json.dumps(
                [
                    {"id": "captains-chair", "model": "openai/gpt-5.5"},
                    {"id": "github-coder", "model": "openai/gpt-5.3-codex"},
                    {"id": "github-reviewer", "model": "openai/gpt-5.5"},
                    {"id": "github-tester", "model": "openai/gpt-5.3-codex"},
                    {"id": "github-ux", "model": "openai/gpt-5.3-codex"},
                    {"id": "github-final", "model": "openai/gpt-5.5"},
                    {"id": "github-merge", "model": "openai/gpt-5.5"},
                    {"id": "github-verify", "model": "openai/gpt-5.5"},
                ]
            ),
            "",
        )

    actions = OpenClawRuntimeInstaller(runtime_config(), runner).plan(tmp_path)

    assert all(item.action == "update_instructions" for item in actions)


def test_runtime_install_rejects_late_model_mismatch_before_any_mutation(tmp_path: Path) -> None:
    commands: list[Sequence[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(command)
        return CommandResult(
            0,
            json.dumps([{"id": "github-reviewer", "model": "unexpected/model"}]),
            "",
        )

    with pytest.raises(OpenClawWorkboardError, match="different model"):
        OpenClawRuntimeInstaller(runtime_config(), runner).install(tmp_path)

    assert not any("agents" in command and "add" in command for command in commands)
    assert not any(tmp_path.iterdir())


def test_auth_sync_copies_only_openclaw_auth_tables(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    target = tmp_path / "target.sqlite"
    with closing(sqlite3.connect(source)) as conn:
        conn.executescript(
            """
            CREATE TABLE auth_profile_store(profile_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE auth_profile_state(profile_id TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE unrelated_memory(id TEXT PRIMARY KEY, content TEXT NOT NULL);
            INSERT INTO auth_profile_store VALUES('working', 'credential-payload');
            INSERT INTO auth_profile_state VALUES('working', 'healthy');
            INSERT INTO unrelated_memory VALUES('memory', 'must-not-copy');
            """
        )

    sync_openclaw_auth_profiles(source, target)

    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("SELECT profile_id FROM auth_profile_store").fetchall() == [("working",)]
        assert conn.execute("SELECT state FROM auth_profile_state").fetchall() == [("healthy",)]
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='unrelated_memory'"
            ).fetchone()
            is None
        )


def test_auth_sync_fails_when_source_schema_is_incomplete(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("CREATE TABLE auth_profile_store(profile_id TEXT PRIMARY KEY)")

    with pytest.raises(OpenClawWorkboardError, match="auth_profile_state"):
        sync_openclaw_auth_profiles(source, tmp_path / "target.sqlite")
