from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from captains_chair.command import CommandResult, run_command
from captains_chair.github import GhGitHubProvider, GitHubProviderError
from captains_chair.models import CourseStatus, NotificationConfig, RepoConfig, RepositoryProvisioningConfig
from tests.test_courses import ready_course


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


def test_readiness_evidence_collects_live_proof_without_issue_or_pr_bodies(tmp_path: Path) -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del cwd, input_text, timeout
        values = list(command)
        if values[1:3] == ["auth", "status"]:
            return CommandResult(0, "github.com\n", "")
        if values[1:3] == ["repo", "view"]:
            return json_result({"nameWithOwner": "example/project", "defaultBranchRef": {"name": "main"}})
        if values[1:3] == ["issue", "list"]:
            return json_result([{"number": 7, "title": "Gap", "body": "private planning detail"}])
        if values[1:3] == ["pr", "list"]:
            return json_result([{"number": 8, "title": "Change", "headRefOid": "head-8", "body": "omit me"}])
        if values[1:3] == ["api", "repos/example/project/branches"]:
            return json_result([{"name": "main"}])
        if values[1:3] == ["run", "list"]:
            return json_result([{"databaseId": 9, "conclusion": "success"}])
        if values[1:3] == ["api", "repos/example/project/commits/main"]:
            return json_result({"sha": "main-sha"})
        if values[1:3] == ["api", "repos/example/project/branches/main/protection/required_status_checks"]:
            return json_result({"contexts": ["build"]})
        if values[1:3] == ["api", "repos/example/project/environments"]:
            return json_result({"environments": [{"name": "prod", "protection_rules": [{"type": "required_reviewers"}]}]})
        if values[1:3] == ["pr", "view"]:
            return json_result({
                "number": 8,
                "headRefOid": "head-8",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "statusCheckRollup": [{"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"}],
            })
        if values[1:3] == ["api", "graphql"]:
            return json_result({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}})
        raise AssertionError(f"unexpected command: {values}")

    evidence = GhGitHubProvider(runner, cwd=tmp_path).readiness_evidence(repo(tmp_path))

    assert evidence["default_branch_sha"] == "main-sha"
    assert evidence["github_auth"] == {"authenticated": True}
    assert evidence["repository_lifecycle"]["snapshot_expected_before_approval"] is True
    assert evidence["required_checks"] == ["build"]
    assert evidence["pull_requests"][0]["review_threads"]["unresolved_blocking"] == 0
    serialized = json.dumps(evidence)
    assert "private planning detail" not in serialized
    assert "omit me" not in serialized
    assert evidence["collection_errors"] == {}


@pytest.mark.parametrize("include_security", (False, True))
def test_gate_requires_complete_required_check_coverage_and_counts_active_threads(
    tmp_path: Path,
    include_security: bool,
) -> None:
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
                        *(
                            [
                                {
                                    "name": "security",
                                    "status": "COMPLETED",
                                    "conclusion": "SUCCESS",
                                }
                            ]
                            if include_security
                            else []
                        ),
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
    assert gate.checks_green is include_security
    assert [(check.name, check.status) for check in gate.required_checks] == [
        ("build", "COMPLETED"),
        ("security", "COMPLETED" if include_security else "MISSING"),
    ]
    assert gate.unresolved_threads == 1
    assert gate.review_head_sha == "head-12"


def test_gate_allows_empty_rollup_when_no_checks_are_required(tmp_path: Path) -> None:
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
                    "number": 13,
                    "headRefOid": "head-13",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "isDraft": False,
                    "statusCheckRollup": [],
                }
            )
        if values[1:3] == ["api", "repos/example/project/branches/main/protection/required_status_checks"]:
            return json_result({"contexts": []})
        if values[1:3] == ["api", "graphql"]:
            return json_result(
                {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}}
            )
        raise AssertionError(f"unexpected command: {values}")

    gate = GhGitHubProvider(runner, cwd=tmp_path).gate(repo(tmp_path), 13, "head-13")

    assert gate.checks_green
    assert gate.required_checks == ()


def test_pull_request_files_are_collected_as_typed_paths(tmp_path: Path) -> None:
    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return json_result(
            {
                "number": 12,
                "headRefOid": "head-12",
                "files": [{"path": "frontend/App.tsx"}, {"path": "src/cli.py"}],
            }
        )

    provider = GhGitHubProvider(runner, cwd=tmp_path)

    assert provider.pull_request_files(repo(tmp_path), 12) == (
        "frontend/App.tsx",
        "src/cli.py",
    )


@pytest.mark.parametrize(
    ("stderr", "expected"),
    (
        ("HTTP 404: Not Found", "empty"),
        (
            "Upgrade to GitHub Pro or make this repository public to enable this feature.",
            "empty",
        ),
        ("HTTP 500: server error", "error"),
    ),
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


def test_provider_rejects_malformed_github_responses_and_transport_failures(tmp_path: Path) -> None:
    def invalid_json(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return CommandResult(0, "not-json", "")

    with pytest.raises(GitHubProviderError, match="invalid JSON"):
        GhGitHubProvider(invalid_json)._json(["repo", "view"])  # pyright: ignore[reportPrivateUsage]

    def failing_diff(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return CommandResult(1, "", "diff unavailable")

    with pytest.raises(GitHubProviderError, match="diff unavailable"):
        GhGitHubProvider(failing_diff).pull_request_diff(repo(tmp_path), 1)

    def wrong_shape(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return json_result([])

    provider = GhGitHubProvider(wrong_shape)
    with pytest.raises(GitHubProviderError, match="pull request response"):
        provider.pull_request(repo(tmp_path), 1)
    with pytest.raises(GitHubProviderError, match="review thread response"):
        provider.review_threads(repo(tmp_path), 1)

    def gate_missing_rollup(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        del command, cwd, input_text, timeout
        return json_result({"number": 1})

    with pytest.raises(GitHubProviderError, match="statusCheckRollup"):
        GhGitHubProvider(gate_missing_rollup).gate(repo(tmp_path), 1, "head-1")


def test_snapshot_rejects_unexpected_collection_shapes(tmp_path: Path) -> None:
    calls = 0

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        nonlocal calls
        del command, cwd, input_text, timeout
        calls += 1
        return json_result({"nameWithOwner": "example/project"} if calls == 1 else {"wrong": True})

    with pytest.raises(GitHubProviderError, match="unexpected shape"):
        GhGitHubProvider(runner).snapshot(repo(tmp_path))


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


def test_greenfield_provisioning_pushes_seeded_source_only_after_configured_approval(
    tmp_path: Path,
) -> None:
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
            return json_result(
                {
                    "nameWithOwner": "example/new-project",
                    "url": "https://github.test/example/new-project",
                    "visibility": "PRIVATE",
                    "defaultBranchRef": {"name": "main"},
                }
            )
        return CommandResult(0, "created\n", "")

    configured = repo(tmp_path).model_copy(
        update={
            "full_name": "example/new-project",
            "provisioning": RepositoryProvisioningConfig(
                enabled=True,
                visibility="private",
                description="A new project",
            ),
        }
    )
    result = GhGitHubProvider(runner).provision_greenfield(
        configured, ready_course().model_copy(update={"status": CourseStatus.ENGAGED})
    )

    assert result["created"] is True
    create_index = next(index for index, (values, _cwd) in enumerate(calls) if values[1:3] == ["repo", "create"])
    create = calls[create_index][0]
    assert create[1:3] == ["repo", "create"]
    assert "--source" in create and str(tmp_path) in create
    assert "--push" in create and "--private" in create
    assert calls[create_index][1] == tmp_path.parent
    assert any(values[0:2] == ["git", "init"] and "-b" in values for values, _ in calls)
    assert any(values[0:2] == ["git", "-c"] and "commit" in values for values, _ in calls)


def test_greenfield_provisioning_creates_a_real_clean_seed_commit_before_remote_creation(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("# New course\n", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int = 60,
    ) -> CommandResult:
        values = list(command)
        if values[0] == "git":
            return run_command(values, cwd=cwd, input_text=input_text, timeout=timeout)
        calls.append(values)
        if values[1:3] == ["repo", "view"]:
            return json_result(
                {
                    "nameWithOwner": "example/new-project",
                    "url": "https://github.test/example/new-project",
                    "visibility": "PRIVATE",
                    "defaultBranchRef": {"name": "main"},
                }
            )
        return CommandResult(0, "created\n", "")

    configured = repo(tmp_path).model_copy(
        update={
            "full_name": "example/new-project",
            "provisioning": RepositoryProvisioningConfig(enabled=True),
        }
    )
    GhGitHubProvider(runner).provision_greenfield(
        configured, ready_course().model_copy(update={"status": CourseStatus.ENGAGED})
    )

    assert run_command(["git", "-C", str(tmp_path), "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip() == "main"
    assert run_command(["git", "-C", str(tmp_path), "status", "--porcelain"]).stdout.strip() == ""
    assert run_command(["git", "-C", str(tmp_path), "log", "-1", "--format=%s"]).stdout.strip() == (
        "Initialize course feature-search"
    )
    assert calls[0][1:3] == ["repo", "create"]


def test_greenfield_provisioning_rejects_disabled_or_missing_sources(tmp_path: Path) -> None:
    disabled = repo(tmp_path).model_copy(update={"provisioning": RepositoryProvisioningConfig(enabled=False)})
    with pytest.raises(GitHubProviderError, match="disabled"):
        GhGitHubProvider().provision_greenfield(
            disabled, ready_course().model_copy(update={"status": CourseStatus.ENGAGED})
        )

    missing = repo(tmp_path / "missing").model_copy(
        update={"provisioning": RepositoryProvisioningConfig(enabled=True)}
    )
    with pytest.raises(GitHubProviderError, match="does not exist"):
        GhGitHubProvider().provision_greenfield(
            missing, ready_course().model_copy(update={"status": CourseStatus.ENGAGED})
        )

    with pytest.raises(GitHubProviderError, match="engaged course"):
        GhGitHubProvider().provision_greenfield(
            disabled.model_copy(update={"provisioning": RepositoryProvisioningConfig(enabled=True)}),
            ready_course(),
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
