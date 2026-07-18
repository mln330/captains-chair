from __future__ import annotations

from pathlib import Path

from make_it_so.command import run_command
from make_it_so.conformance import run_full_autonomous_workflow
from make_it_so.models import (
    ActionKind,
    CompletionPolicy,
    NotificationConfig,
    OperationMode,
    PlanDecision,
    RepoConfig,
)
from make_it_so.orchestration import QueueCard, WorkflowOrchestrator, WorkspaceRef
from make_it_so.worktrees import WorktreeManager
from tests.fakes import InMemoryWorkQueue, worker_policy


def git(cwd: Path | None, *args: str) -> str:
    result = run_command(["git", *args], cwd=cwd, timeout=120)
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def commit(cwd: Path, message: str) -> None:
    git(cwd, "-c", "user.name=MAKE_IT_SO Test", "-c", "user.email=make_it_so@example.test", "commit", "-m", message)


def test_real_git_worktrees_keep_parallel_lanes_isolated(tmp_path: Path) -> None:
    bare = tmp_path / "origin.git"
    repo_root = tmp_path / "repo"
    git(None, "init", "--bare", str(bare))
    git(None, "init", "--initial-branch=main", str(repo_root))
    git(repo_root, "config", "user.name", "MAKE_IT_SO Test")
    git(repo_root, "config", "user.email", "make_it_so@example.test")
    (repo_root / "README.md").write_text("# Disposable project\n", encoding="utf-8")
    (repo_root / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    git(repo_root, "add", "README.md", "ISSUES_EXECUTION_PLAN.md")
    commit(repo_root, "Seed disposable project")
    git(repo_root, "remote", "add", "origin", str(bare))
    git(repo_root, "push", "--set-upstream", "origin", "main")

    repo = RepoConfig(
        full_name="example/project",
        local_path=repo_root,
        default_branch="main",
        planning_doc="ISSUES_EXECUTION_PLAN.md",
        notification=NotificationConfig(kind="stdout"),
    )
    manager = WorktreeManager(tmp_path / "state" / "worktrees")

    implementation = manager.create(repo, "issue-39", lane="work")
    documentation = manager.create(repo, "plan-sync", lane="docs")
    (implementation.path / "feature.txt").write_text("implementation\n", encoding="utf-8")
    git(implementation.path, "add", "feature.txt")
    commit(implementation.path, "Implement issue 39")
    (documentation.path / "ISSUES_EXECUTION_PLAN.md").write_text(
        "# Plan\n\n- Track issue 39\n", encoding="utf-8"
    )
    git(documentation.path, "add", "ISSUES_EXECUTION_PLAN.md")
    commit(documentation.path, "Update durable plan")
    git(documentation.path, "push", "origin", "HEAD:refs/heads/make_it_so/docs/plan")
    review = manager.checkout_existing(
        repo,
        "pr-35-head-1",
        "make_it_so/docs/plan",
        lane="review",
    )

    assert git(repo_root, "branch", "--show-current") == "main"
    assert git(repo_root, "status", "--porcelain") == ""
    assert git(implementation.path, "branch", "--show-current") == "make_it_so/work/issue-39"
    assert git(documentation.path, "branch", "--show-current") == "make_it_so/docs/plan-sync"
    assert git(review.path, "branch", "--show-current") == "make_it_so/review/pr-35-head-1"
    assert review.push_branch == "make_it_so/docs/plan"
    assert not (repo_root / "feature.txt").exists()
    worktrees = git(repo_root, "worktree", "list", "--porcelain")
    normalized_worktrees = worktrees.replace("\\", "/")
    assert implementation.path.as_posix() in normalized_worktrees
    assert documentation.path.as_posix() in normalized_worktrees

    manager.remove(repo, implementation)
    manager.remove(repo, documentation)
    manager.remove(repo, review)

    assert not implementation.path.exists()
    assert not documentation.path.exists()
    assert not review.path.exists()
    assert git(repo_root, "status", "--porcelain") == ""


def test_disposable_autonomous_workflow_pushes_and_verifies_default_branch(tmp_path: Path) -> None:
    bare = tmp_path / "origin.git"
    repo_root = tmp_path / "repo"
    git(None, "init", "--bare", str(bare))
    git(None, "init", "--initial-branch=main", str(repo_root))
    git(repo_root, "config", "user.name", "MAKE_IT_SO Test")
    git(repo_root, "config", "user.email", "make_it_so@example.test")
    (repo_root / "README.md").write_text("# Disposable project\n", encoding="utf-8")
    (repo_root / "ISSUES_EXECUTION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    git(repo_root, "add", "README.md", "ISSUES_EXECUTION_PLAN.md")
    commit(repo_root, "Seed disposable project")
    git(repo_root, "remote", "add", "origin", str(bare))
    git(repo_root, "push", "--set-upstream", "origin", "main")

    repo = RepoConfig(
        full_name="example/project",
        local_path=repo_root,
        default_branch="main",
        operation_mode=OperationMode.AUTONOMOUS,
        completion_policy=CompletionPolicy.AUTO_MERGE,
        allow_autonomous_merge=True,
        planning_doc="ISSUES_EXECUTION_PLAN.md",
        notification=NotificationConfig(kind="stdout"),
    )
    manager = WorktreeManager(tmp_path / "state" / "worktrees")
    implementation = manager.create(repo, "39")
    workspace = WorkspaceRef(
        kind="worktree",
        path=implementation.path,
        branch=implementation.branch,
        push_branch=implementation.push_branch,
    )
    queue = InMemoryWorkQueue()
    orchestrator = WorkflowOrchestrator(queue, worker_policy())
    decision = PlanDecision(
        action=ActionKind.IMPLEMENT,
        summary="Implement a real disposable slice",
        reason="The contract fixture selected the next dependency-ready item.",
        target_issue=39,
        acceptance_criteria=("The feature is committed", "Main verifies the feature"),
    )
    current_head: str | None = None
    merged_head: str | None = None

    def complete_card(
        card_id: str,
        summary: str,
        proof: tuple[dict[str, object], ...],
    ) -> QueueCard:
        del summary, proof
        nonlocal current_head, merged_head
        card = queue.cards[card_id]
        stage = next(label.split(":", 1)[1] for label in card.labels if label.startswith("stage:"))
        if stage == "implementation":
            assert card.workspace is not None and card.workspace.path is not None
            (card.workspace.path / "feature.txt").write_text("implemented\n", encoding="utf-8")
            git(card.workspace.path, "add", "feature.txt")
            commit(card.workspace.path, "Implement disposable slice")
            git(card.workspace.path, "push", "origin", f"HEAD:refs/heads/{implementation.push_branch}")
            current_head = git(card.workspace.path, "rev-parse", "HEAD")
        elif stage == "repair":
            assert card.workspace is not None and card.workspace.path is not None
            (card.workspace.path / "repair-proof.txt").write_text("reviewed\n", encoding="utf-8")
            git(card.workspace.path, "add", "repair-proof.txt")
            commit(card.workspace.path, "Repair review finding")
            git(card.workspace.path, "push", "origin", f"HEAD:refs/heads/{implementation.push_branch}")
            current_head = git(card.workspace.path, "rev-parse", "HEAD")
        elif stage in {"review", "test", "ux_review", "final_review"}:
            assert card.workspace is not None and card.workspace.path is not None
            assert git(card.workspace.path, "status", "--porcelain") == ""
            current_head = git(card.workspace.path, "rev-parse", "HEAD")
        elif stage == "merge":
            assert current_head is not None
            git(repo_root, "checkout", "main")
            git(repo_root, "merge", "--ff-only", implementation.branch)
            git(repo_root, "push", "origin", "main")
            merged_head = git(repo_root, "rev-parse", "HEAD")
            assert merged_head == current_head
        elif stage == "post_merge":
            assert merged_head is not None
            assert git(repo_root, "rev-parse", "HEAD") == merged_head
            assert (repo_root / "feature.txt").read_text(encoding="utf-8") == "implemented\n"

        if stage == "final_review":
            assert current_head is not None
            completion_proof = ({"status": "passed", "note": f"AUTO_MERGE_ALLOWED:{current_head}"},)
        elif stage == "merge":
            assert merged_head is not None
            completion_proof = ({"status": "passed", "note": f"Merged default branch {merged_head}"},)
        elif stage == "post_merge":
            assert merged_head is not None
            completion_proof = ({"status": "passed", "note": f"Verified default branch {merged_head}"},)
        else:
            completion_proof = ({"status": "passed", "note": f"{stage} verified at {current_head}"},)
        return queue.complete_card(card_id, summary=f"{stage} completed", proof=completion_proof)

    run_full_autonomous_workflow(
        orchestrator,
        queue,
        repo,
        decision,
        "real-git-e2e",
        block_card=queue.block,
        complete_card=complete_card,
        workspace=workspace,
    )

    assert merged_head is not None
    assert git(repo_root, "rev-parse", "HEAD") == merged_head
    assert git(repo_root, "status", "--porcelain") == ""
    manager.remove(repo, implementation)
