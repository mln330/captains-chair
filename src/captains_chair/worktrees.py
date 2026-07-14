from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from captains_chair.command import CommandRunner, require_success, run_command
from captains_chair.models import RepoConfig

SAFE_BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    base: str
    push_branch: str


class WorktreeManager:
    def __init__(self, root: Path, runner: CommandRunner = run_command) -> None:
        self.root = root.resolve()
        self.runner = runner
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, repo: RepoConfig, work_id: str, lane: str = "work") -> Worktree:
        branch = f"captains_chair/{lane}/{_safe_component(work_id)}"
        if not _safe_branch(branch):
            raise ValueError(f"unsafe branch name: {branch}")
        repo_root = repo.local_path.resolve()
        require_success(
            self.runner(["git", "-C", str(repo_root), "rev-parse", "--show-toplevel"]), "find git root"
        )
        require_success(
            self.runner(
                ["git", "-C", str(repo_root), "fetch", "--quiet", "origin", repo.default_branch], timeout=180
            ),
            "fetch default branch",
        )
        path = (self.root / _safe_component(repo.full_name) / _safe_component(work_id)).resolve()
        if self.root not in path.parents:
            raise ValueError("worktree path escaped the configured root")
        if path.exists():
            top_level = require_success(
                self.runner(["git", "-C", str(path), "rev-parse", "--show-toplevel"]),
                "inspect existing worktree",
            ).strip()
            current_branch = require_success(
                self.runner(["git", "-C", str(path), "branch", "--show-current"]),
                "inspect existing worktree branch",
            ).strip()
            if Path(top_level).resolve() != path or current_branch != branch:
                raise RuntimeError(
                    f"existing worktree does not match {branch}: {path} ({current_branch})"
                )
            status = require_success(
                self.runner(["git", "-C", str(path), "status", "--porcelain"]),
                "inspect existing worktree status",
            )
            if status.strip():
                raise RuntimeError(
                    f"existing worktree is dirty; refusing to reuse it: {path}"
                )
            head = require_success(
                self.runner(["git", "-C", str(path), "rev-parse", "HEAD"]),
                "inspect existing worktree head",
            ).strip()
            base = require_success(
                self.runner(
                    ["git", "-C", str(path), "rev-parse", f"origin/{repo.default_branch}"]
                ),
                "inspect existing worktree base",
            ).strip()
            if not head or head != base:
                raise RuntimeError(
                    f"existing worktree is not at current origin/{repo.default_branch}; refusing to reuse it: {path}"
                )
            return Worktree(
                path=path,
                branch=branch,
                base=f"origin/{repo.default_branch}",
                push_branch=branch,
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        require_success(
            self.runner(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(path),
                    f"origin/{repo.default_branch}",
                ],
                timeout=180,
            ),
            "create isolated worktree",
        )
        return Worktree(path=path, branch=branch, base=f"origin/{repo.default_branch}", push_branch=branch)

    def checkout_existing(
        self,
        repo: RepoConfig,
        work_id: str,
        remote_branch: str,
        *,
        lane: str = "repair",
    ) -> Worktree:
        if not _safe_branch(remote_branch):
            raise ValueError(f"unsafe remote branch name: {remote_branch}")
        lane_component = _safe_component(lane)
        repo_root = repo.local_path.resolve()
        require_success(
            self.runner(
                ["git", "-C", str(repo_root), "fetch", "--quiet", "origin", remote_branch], timeout=180
            ),
            "fetch pull request branch",
        )
        branch = f"captains_chair/{lane_component}/{_safe_component(work_id)}"
        path = (self.root / _safe_component(repo.full_name) / _safe_component(work_id)).resolve()
        if self.root not in path.parents:
            raise ValueError("worktree path escaped the configured root")
        if path.exists():
            raise FileExistsError(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        require_success(
            self.runner(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(path),
                    f"origin/{remote_branch}",
                ],
                timeout=180,
            ),
            "create repair worktree",
        )
        return Worktree(
            path=path, branch=branch, base=f"origin/{repo.default_branch}", push_branch=remote_branch
        )

    def remove(self, repo: RepoConfig, worktree: Worktree) -> None:
        self.remove_path(repo, worktree.path)

    def discard(self, repo: RepoConfig, worktree: Worktree) -> None:
        """Force-remove a failed local worktree without deleting its branch or PR."""
        self.discard_path(repo, worktree.path)

    def remove_path(self, repo: RepoConfig, path: Path) -> bool:
        """Remove a clean managed worktree; return false when it is already absent."""
        path = path.resolve()
        if self.root not in path.parents:
            raise ValueError("refusing to remove a worktree outside the configured root")
        if not path.exists():
            return False
        status = self.runner(["git", "-C", str(path), "status", "--porcelain"])
        require_success(status, "inspect worktree status")
        if status.stdout.strip():
            raise RuntimeError("refusing to remove a dirty worktree")
        require_success(
            self.runner(
                [
                    "git",
                    "-C",
                    str(repo.local_path.resolve()),
                    "worktree",
                    "remove",
                    str(path),
                ],
                timeout=180,
            ),
            "remove worktree",
        )
        return True

    def discard_path(self, repo: RepoConfig, path: Path) -> bool:
        """Force-remove a managed local worktree after a failed ephemeral action."""
        path = path.resolve()
        if self.root not in path.parents:
            raise ValueError("refusing to discard a worktree outside the configured root")
        if not path.exists():
            return False
        require_success(
            self.runner(
                [
                    "git",
                    "-C",
                    str(repo.local_path.resolve()),
                    "worktree",
                    "remove",
                    "--force",
                    str(path),
                ],
                timeout=180,
            ),
            "discard failed worktree",
        )
        return True

    def remove_disposable(self, repo: RepoConfig, worktree: Worktree) -> None:
        path = worktree.path.resolve()
        if self.root not in path.parents:
            raise ValueError("refusing to remove a worktree outside the configured root")
        if not worktree.branch.startswith("captains_chair/repair/ux-"):
            raise ValueError("refusing to force-remove a non-UX disposable worktree")
        require_success(
            self.runner(
                ["git", "-C", str(repo.local_path.resolve()), "worktree", "remove", "--force", str(path)],
                timeout=180,
            ),
            "remove disposable UX worktree",
        )


def _safe_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    if not normalized:
        raise ValueError("identifier did not contain a safe path component")
    return normalized.lower()[:120]


def _safe_branch(value: str) -> bool:
    if not SAFE_BRANCH.fullmatch(value) or value.startswith("/") or value.endswith("/"):
        return False
    return all(part not in {"", ".", ".."} for part in value.split("/"))
