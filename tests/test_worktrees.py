from collections.abc import Sequence
from pathlib import Path

import pytest

from make_it_so.command import CommandResult
from make_it_so.models import NotificationConfig, RepoConfig
from make_it_so.worktrees import Worktree, WorktreeManager


def test_create_resumes_matching_existing_worktree(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    root = tmp_path / "state" / "worktrees"
    existing = root / "example-project" / "issue-6"
    repo_path.mkdir()
    existing.mkdir(parents=True)
    commands: list[list[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        args = list(command)
        commands.append(args)
        if args[-2:] == ["branch", "--show-current"]:
            return CommandResult(0, "make_it_so/work/issue-6\n", "")
        if args[-2:] == ["rev-parse", "--show-toplevel"] and str(existing) in args:
            return CommandResult(0, str(existing) + "\n", "")
        if args[-2:] in (["rev-parse", "HEAD"], ["rev-parse", "origin/main"]):
            return CommandResult(0, "same-head\n", "")
        return CommandResult(0, "", "")

    repo = RepoConfig(
        full_name="example/project",
        local_path=repo_path,
        planning_doc="ISSUES_EXECUTION_PLAN.md",
        notification=NotificationConfig(kind="stdout"),
    )

    worktree = WorktreeManager(root, runner).create(repo, "issue-6")

    assert worktree.path == existing.resolve()
    assert worktree.branch == "make_it_so/work/issue-6"
    assert not any("worktree" in command and "add" in command for command in commands)


def test_create_uses_origin_default_branch_and_isolated_path(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    commands: list[list[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(list(command))
        return CommandResult(0, "repo\n", "")

    repo = RepoConfig(
        full_name="example/project",
        local_path=repo_path,
        planning_doc="ISSUES_EXECUTION_PLAN.md",
        notification=NotificationConfig(kind="stdout"),
    )

    worktree = WorktreeManager(tmp_path / "state" / "worktrees", runner).create(repo, "issue-6")

    assert worktree.branch == "make_it_so/work/issue-6"
    add = next(command for command in commands if "worktree" in command and "add" in command)
    assert "origin/main" in add
    assert worktree.path.is_relative_to((tmp_path / "state" / "worktrees").resolve())


def test_checkout_existing_rejects_unsafe_remote_branch(tmp_path: Path) -> None:
    repo = RepoConfig(
        full_name="example/project",
        local_path=tmp_path / "repo",
        planning_doc="ISSUES_EXECUTION_PLAN.md",
        notification=NotificationConfig(kind="stdout"),
    )

    with pytest.raises(ValueError, match="unsafe remote branch"):
        WorktreeManager(tmp_path / "state" / "worktrees").checkout_existing(repo, "repair-1", "../main")


def test_checkout_existing_keeps_local_repair_branch_separate_from_pr_push_branch(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    commands: list[list[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(list(command))
        return CommandResult(0, "", "")

    repo = RepoConfig(
        full_name="example/project",
        local_path=repo_path,
        planning_doc="ISSUES_EXECUTION_PLAN.md",
        notification=NotificationConfig(kind="stdout"),
    )

    worktree = WorktreeManager(tmp_path / "state" / "worktrees", runner).checkout_existing(
        repo, "repair-1", "feature/current-pr"
    )

    add = next(command for command in commands if "worktree" in command and "add" in command)
    assert worktree.branch == "make_it_so/repair/repair-1"
    assert worktree.push_branch == "feature/current-pr"
    assert worktree.base == "origin/main"
    assert "make_it_so/repair/repair-1" in add
    assert "origin/feature/current-pr" in add


def test_checkout_existing_supports_a_review_lane_without_changing_pr_push_branch(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    commands: list[list[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(list(command))
        return CommandResult(0, "", "")

    repo = RepoConfig(
        full_name="example/project",
        local_path=repo_path,
        planning_doc="ISSUES_EXECUTION_PLAN.md",
        notification=NotificationConfig(kind="stdout"),
    )

    worktree = WorktreeManager(tmp_path / "state" / "worktrees", runner).checkout_existing(
        repo,
        "pr-35-head-1",
        "make_it_so/docs/plan",
        lane="review",
    )

    add = next(command for command in commands if "worktree" in command and "add" in command)
    assert worktree.branch == "make_it_so/review/pr-35-head-1"
    assert worktree.push_branch == "make_it_so/docs/plan"
    assert "make_it_so/review/pr-35-head-1" in add


def test_create_rejects_existing_worktree_with_wrong_branch(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    root = tmp_path / "state" / "worktrees"
    existing = root / "example-project" / "issue-6"
    repo_path.mkdir()
    existing.mkdir(parents=True)

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        args = list(command)
        if args[-2:] == ["rev-parse", "--show-toplevel"]:
            return CommandResult(0, str(existing) + "\n", "") if str(existing) in args else CommandResult(0, "repo\n", "")
        if args[-2:] == ["branch", "--show-current"]:
            return CommandResult(0, "make_it_so/work/different\n", "")
        return CommandResult(0, "", "")

    repo = RepoConfig(
        full_name="example/project",
        local_path=repo_path,
        planning_doc="ISSUES_EXECUTION_PLAN.md",
        notification=NotificationConfig(kind="stdout"),
    )

    with pytest.raises(RuntimeError, match="does not match"):
        WorktreeManager(root, runner).create(repo, "issue-6")


def test_create_rejects_matching_existing_worktree_when_dirty(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    root = tmp_path / "state" / "worktrees"
    existing = root / "example-project" / "issue-6"
    repo_path.mkdir()
    existing.mkdir(parents=True)

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        args = list(command)
        if args[-2:] == ["rev-parse", "--show-toplevel"]:
            return CommandResult(0, str(existing) + "\n", "") if str(existing) in args else CommandResult(0, "repo\n", "")
        if args[-2:] == ["branch", "--show-current"]:
            return CommandResult(0, "make_it_so/work/issue-6\n", "")
        if args[-2:] == ["status", "--porcelain"]:
            return CommandResult(0, " M unfinished.txt\n", "")
        return CommandResult(0, "", "")

    repo = RepoConfig(
        full_name="example/project",
        local_path=repo_path,
        planning_doc="ISSUES_EXECUTION_PLAN.md",
        notification=NotificationConfig(kind="stdout"),
    )

    with pytest.raises(RuntimeError, match="existing worktree is dirty"):
        WorktreeManager(root, runner).create(repo, "issue-6")


def test_create_rejects_clean_existing_worktree_on_stale_head(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    root = tmp_path / "state" / "worktrees"
    existing = root / "example-project" / "issue-6"
    repo_path.mkdir()
    existing.mkdir(parents=True)

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        args = list(command)
        if args[-2:] == ["rev-parse", "--show-toplevel"]:
            return CommandResult(0, str(existing) + "\n", "") if str(existing) in args else CommandResult(0, "repo\n", "")
        if args[-2:] == ["branch", "--show-current"]:
            return CommandResult(0, "make_it_so/work/issue-6\n", "")
        if args[-2:] == ["status", "--porcelain"]:
            return CommandResult(0, "", "")
        if args[-2:] == ["rev-parse", "HEAD"]:
            return CommandResult(0, "stale-head\n", "")
        if args[-2:] == ["rev-parse", "origin/main"]:
            return CommandResult(0, "current-main\n", "")
        return CommandResult(0, "", "")

    repo = RepoConfig(
        full_name="example/project",
        local_path=repo_path,
        planning_doc="ISSUES_EXECUTION_PLAN.md",
        notification=NotificationConfig(kind="stdout"),
    )

    with pytest.raises(RuntimeError, match="not at current origin/main"):
        WorktreeManager(root, runner).create(repo, "issue-6")


def test_remove_refuses_dirty_worktree_and_does_not_remove_it(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    root = tmp_path / "state" / "worktrees"
    path = root / "example-project" / "issue-6"
    repo_path.mkdir()
    path.mkdir(parents=True)
    commands: list[list[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        args = list(command)
        commands.append(args)
        if args[-2:] == ["status", "--porcelain"]:
            return CommandResult(0, " M file.txt\n", "")
        return CommandResult(0, "", "")

    repo = RepoConfig(
        full_name="example/project",
        local_path=repo_path,
        planning_doc="ISSUES_EXECUTION_PLAN.md",
        notification=NotificationConfig(kind="stdout"),
    )

    with pytest.raises(RuntimeError, match="dirty worktree"):
        WorktreeManager(root, runner).remove_path(repo, path)
    assert not any("worktree" in command and "remove" in command for command in commands)


def test_discard_force_removes_failed_worktree_without_branch_deletion(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    root = tmp_path / "state" / "worktrees"
    path = root / "example-project" / "issue-6"
    repo_path.mkdir()
    path.mkdir(parents=True)
    commands: list[list[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(list(command))
        return CommandResult(0, "", "")

    repo = RepoConfig(
        full_name="example/project",
        local_path=repo_path,
        planning_doc="ISSUES_EXECUTION_PLAN.md",
        notification=NotificationConfig(kind="stdout"),
    )
    manager = WorktreeManager(root, runner)

    assert manager.discard_path(repo, path) is True
    remove = next(command for command in commands if "worktree" in command and "remove" in command)
    assert remove[-2:] == ["--force", str(path.resolve())]
    assert "branch" not in remove


def test_remove_disposable_only_force_removes_ux_worktrees(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    root = tmp_path / "state" / "worktrees"
    path = root / "example-project" / "ux-1"
    repo_path.mkdir()
    path.mkdir(parents=True)
    commands: list[list[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        commands.append(list(command))
        return CommandResult(0, "", "")

    repo = RepoConfig(
        full_name="example/project",
        local_path=repo_path,
        planning_doc="ISSUES_EXECUTION_PLAN.md",
        notification=NotificationConfig(kind="stdout"),
    )
    manager = WorktreeManager(root, runner)

    with pytest.raises(ValueError, match="non-UX"):
        manager.remove_disposable(
            repo,
            Worktree(path=path, branch="make_it_so/work/ux-1", base="origin/main", push_branch="make_it_so/work/ux-1"),
        )
    manager.remove_disposable(
        repo,
        Worktree(path=path, branch="make_it_so/repair/ux-1", base="origin/main", push_branch="make_it_so/repair/ux-1"),
    )
    assert any("--force" in command for command in commands)
