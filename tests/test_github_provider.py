from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from captains_chair.command import CommandResult
from captains_chair.github import GhGitHubProvider, GitHubProviderError
from captains_chair.models import NotificationConfig, RepoConfig


def repo(tmp_path: Path) -> RepoConfig:
    return RepoConfig(
        full_name="example/project",
        local_path=tmp_path,
        default_branch="main",
        planning_doc="ISSUES_EXECUTION_PLAN.md",
        notification=NotificationConfig(kind="stdout"),
    )


def json_result(value: object) -> CommandResult:
    return CommandResult(0, json.dumps(value), "")


def test_snapshot_collects_and_normalizes_all_github_collections(tmp_path: Path) -> None:
    calls: list[tuple[list[str], Path | None]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del input_text, timeout
        values = list(command)
        calls.append((values, cwd))
        if values[1:3] == ["repo", "view"]:
            return json_result({"nameWithOwner": "example/project", "defaultBranchRef": {"name": "main"}})
        if values[1:3] == ["issue", "list"]:
            return json_result([{"number": 7, "title": "Gap"}])
        if values[1:3] == ["pr", "list"]:
            return json_result([{"number": 8, "headRefOid": "head-8"}])
        if values[1:3] == ["api", "repos/example/project/branches"]:
            return json_result([{"name": "main"}, {"name": "captains_chair/work/8"}])
        if values[1:3] == ["run", "list"]:
            return json_result([{"databaseId": 9, "conclusion": "success"}])
        raise AssertionError(f"unexpected command: {values}")

    provider = GhGitHubProvider(runner, cwd=tmp_path)
    snapshot = provider.snapshot(repo(tmp_path))

    assert snapshot.repo["nameWithOwner"] == "example/project"
    assert snapshot.issues == [{"number": 7, "title": "Gap"}]
    assert snapshot.pull_requests == [{"number": 8, "headRefOid": "head-8"}]
    assert snapshot.branches == ["main", "captains_chair/work/8"]
    assert snapshot.workflow_runs == [{"databaseId": 9, "conclusion": "success"}]
    assert all(cwd == tmp_path for _, cwd in calls)


def test_gate_filters_required_checks_and_counts_only_active_review_threads(tmp_path: Path) -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        values = list(command)
        if values[1:3] == ["pr", "view"]:
            return json_result(
                {
                    "number": 12,
                    "headRefOid": "head-12",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "isDraft": False,
                    "statusCheckRollup": [
                        {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
                        {"name": "optional", "status": "COMPLETED", "conclusion": "FAILURE"},
                    ],
                }
            )
        if values[1:3] == ["api", "repos/example/project/branches/main/protection/required_status_checks"]:
            return json_result({"contexts": ["build"], "checks": [{"context": "security"}]})
        if values[1:3] == ["api", "graphql"]:
            return json_result(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [
                                        {"id": "active", "isResolved": False, "isOutdated": False},
                                        {"id": "resolved", "isResolved": True, "isOutdated": False},
                                        {"id": "outdated", "isResolved": False, "isOutdated": True},
                                    ]
                                }
                            }
                        }
                    }
                }
            )
        raise AssertionError(f"unexpected command: {values}")

    gate = GhGitHubProvider(runner, cwd=tmp_path).gate(repo(tmp_path), 12, "head-12")

    assert gate.head_sha == "head-12"
    assert gate.checks_green
    assert [check.name for check in gate.required_checks] == ["build"]
    assert gate.unresolved_threads == 1
    assert gate.review_head_sha == "head-12"


@pytest.mark.parametrize(
    ("stderr", "expected"),
    (("HTTP 404: Not Found", "empty"), ("HTTP 500: server error", "error")),
)
def test_required_check_lookup_distinguishes_unprotected_branch_from_provider_failure(
    tmp_path: Path, stderr: str, expected: str
) -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return CommandResult(1, "", stderr)

    provider = GhGitHubProvider(runner)
    if expected == "empty":
        assert provider.required_check_names(repo(tmp_path)) == set()
    else:
        with pytest.raises(GitHubProviderError, match="required check query failed"):
            provider.required_check_names(repo(tmp_path))


def test_provider_rejects_malformed_required_check_json(tmp_path: Path) -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return CommandResult(0, "not-json", "")

    with pytest.raises(GitHubProviderError, match="required check response was not valid JSON"):
        GhGitHubProvider(runner).required_check_names(repo(tmp_path))


def test_mutations_use_provider_cwd_and_return_read_back_pr(tmp_path: Path) -> None:
    calls: list[tuple[list[str], Path | None]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del input_text, timeout
        values = list(command)
        calls.append((values, cwd))
        if values[1:3] == ["pr", "list"]:
            return json_result([{"number": 13, "url": "https://github.test/pr/13"}])
        if values[1:3] == ["api", "repos/example/project/commits/main"]:
            return json_result({"sha": "main-13"})
        if values[1:3] == ["issue", "create"]:
            return CommandResult(0, "https://github.test/issues/14\n", "")
        return CommandResult(0, "", "")

    provider = GhGitHubProvider(runner, cwd=tmp_path)
    configured = repo(tmp_path)
    created = provider.create_pull_request(
        configured, branch="captains_chair/work/13", title="Implement issue 13", body="Details"
    )
    provider.mark_ready(configured, 13)
    provider.comment_pull_request(configured, 13, "Review this")
    provider.merge(configured, 13)
    assert provider.default_branch_sha(configured) == "main-13"
    assert provider.create_issue(configured, "Issue 14", "Details")["url"].endswith("/14")
    provider.update_issue(configured, 14, "Updated", None)
    provider.label_issue(configured, 14, ("bug", "captains_chair"))
    provider.retarget_issue(configured, 14, "Sprint 2", ("octocat",))
    provider.close_issue(configured, 14, "Completed")

    assert created["number"] == 13
    assert all(cwd == tmp_path for _, cwd in calls)
    assert any(values[1:3] == ["pr", "merge"] and "--squash" in values for values, _ in calls)
    assert any(
        values[1:3] == ["issue", "edit"]
        and "--add-label" in values
        and values[values.index("--add-label") + 1] == "bug"
        and values[values.index("--add-label", values.index("--add-label") + 1) + 1] == "captains_chair"
        for values, _ in calls
    )
    assert any(
        values[1:3] == ["issue", "edit"]
        and "--milestone" in values
        and values[values.index("--milestone") + 1] == "Sprint 2"
        and values[values.index("--add-assignee") + 1] == "octocat"
        for values, _ in calls
    )


def test_issue_mutation_failures_use_typed_provider_error(tmp_path: Path) -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return CommandResult(1, "", "permission denied")

    provider = GhGitHubProvider(runner)
    configured = repo(tmp_path)
    with pytest.raises(GitHubProviderError, match="update issue failed"):
        provider.update_issue(configured, 14, None, "Details")
    with pytest.raises(GitHubProviderError, match="label issue failed"):
        provider.label_issue(configured, 14, ("bug",))
    with pytest.raises(GitHubProviderError, match="retarget issue failed"):
        provider.retarget_issue(configured, 14, "Sprint 2", ())
    with pytest.raises(GitHubProviderError, match="close issue failed"):
        provider.close_issue(configured, 14, "Completed")
