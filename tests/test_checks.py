from pathlib import Path

from captains_chair.engine import select_checks, worktree_check_command


def test_check_command_replaces_shared_checkout_with_worktree() -> None:
    command = worktree_check_command(
        "docker run --rm -v /workspace/PrintHub:/src image dotnet test",
        Path("/workspace/PrintHub"),
        Path("/state/worktrees/printhub/issue-6"),
    )

    assert "/state/worktrees/printhub/issue-6:/src" in command
    assert all("/workspace/PrintHub" not in item for item in command)


def test_backend_change_skips_local_frontend_checks() -> None:
    checks = select_checks(
        (
            "dotnet test PrintHub.sln",
            "docker run node:24 npm ci",
            "npm --prefix frontend run test",
        ),
        ["src/PrintHub.Api/Phase1Api.cs"],
        ("frontend/", "*.tsx"),
    )

    assert checks == ("dotnet test PrintHub.sln",)


def test_frontend_change_runs_all_configured_checks() -> None:
    checks = ("dotnet test PrintHub.sln", "npm --prefix frontend run test")

    assert select_checks(checks, ["frontend/src/App.tsx"], ("frontend/",)) == checks
