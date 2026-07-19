from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from make_it_so.command import CommandResult, CommandRunner, run_command
from make_it_so.models import CheckResult, Course, CourseStatus, PullRequestGate, RepoConfig


class GitHubProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class RepositorySnapshot:
    repo: dict[str, Any]
    issues: list[dict[str, Any]]
    pull_requests: list[dict[str, Any]]
    branches: list[str]
    workflow_runs: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "issues": self.issues,
            "pull_requests": self.pull_requests,
            "branches": self.branches,
            "workflow_runs": self.workflow_runs,
        }


class GitHubProvider(Protocol):
    """Portable GitHub boundary used by the Number 1 engine and baseline collector."""

    def snapshot(self, repo: RepoConfig) -> RepositorySnapshot: ...

    def readiness_evidence(self, repo: RepoConfig) -> dict[str, Any]: ...

    def pull_request(self, repo: RepoConfig, number: int) -> dict[str, Any]: ...

    def pull_request_diff(self, repo: RepoConfig, number: int) -> str: ...

    def pull_request_files(self, repo: RepoConfig, number: int) -> tuple[str, ...]: ...

    def review_threads(self, repo: RepoConfig, number: int) -> list[dict[str, Any]]: ...

    def required_check_names(self, repo: RepoConfig) -> set[str]: ...

    def gate(self, repo: RepoConfig, number: int, review_head_sha: str | None) -> PullRequestGate: ...

    def create_pull_request(
        self, repo: RepoConfig, *, branch: str, title: str, body: str, draft: bool = True
    ) -> dict[str, Any]: ...

    def mark_ready(self, repo: RepoConfig, number: int) -> None: ...

    def comment_pull_request(self, repo: RepoConfig, number: int, body: str) -> None: ...

    def merge(self, repo: RepoConfig, number: int) -> None: ...

    def default_branch_sha(self, repo: RepoConfig) -> str: ...

    def create_issue(self, repo: RepoConfig, title: str, body: str) -> dict[str, Any]: ...

    def update_issue(self, repo: RepoConfig, number: int, title: str | None, body: str | None) -> None: ...

    def label_issue(self, repo: RepoConfig, number: int, labels: tuple[str, ...]) -> None: ...

    def retarget_issue(
        self,
        repo: RepoConfig,
        number: int,
        milestone: str | None,
        assignees: tuple[str, ...],
    ) -> None: ...

    def close_issue(self, repo: RepoConfig, number: int, reason: str) -> None: ...

    def provision_greenfield(self, repo: RepoConfig, course: Course) -> dict[str, Any]: ...


class GhGitHubProvider:
    def __init__(self, runner: CommandRunner = run_command, cwd: Path | None = None) -> None:
        self.runner = runner
        self.cwd = cwd

    def _json(self, args: list[str], *, timeout: int = 90) -> object:
        result = self.runner(["gh", *args], cwd=self.cwd, timeout=timeout)
        if result.returncode:
            raise GitHubProviderError((result.stderr or result.stdout).strip()[:3000])
        try:
            return cast(object, json.loads(result.stdout))
        except json.JSONDecodeError as exc:
            raise GitHubProviderError(f"invalid JSON from gh: {exc}") from exc

    def snapshot(self, repo: RepoConfig) -> RepositorySnapshot:
        full = repo.full_name
        repo_data = self._json(
            [
                "repo",
                "view",
                full,
                "--json",
                "nameWithOwner,description,defaultBranchRef,isArchived,isPrivate,pushedAt,url,primaryLanguage,visibility",
            ]
        )
        issues = self._json(
            [
                "issue",
                "list",
                "--repo",
                full,
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "number,title,body,labels,updatedAt,assignees,url",
            ]
        )
        prs = self._json(
            [
                "pr",
                "list",
                "--repo",
                full,
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "number,title,body,headRefName,headRefOid,baseRefName,isDraft,mergeStateStatus,updatedAt,reviewDecision,url",
            ]
        )
        branches_raw = self._json(["api", f"repos/{full}/branches", "--paginate"])
        runs = self._json(
            [
                "run",
                "list",
                "--repo",
                full,
                "--limit",
                "50",
                "--json",
                "databaseId,workflowName,headBranch,headSha,status,conclusion,createdAt,updatedAt,url,displayTitle,event",
            ]
        )
        if not isinstance(repo_data, dict):
            raise GitHubProviderError("GitHub repository response was not an object")
        if not all(isinstance(item, list) for item in (issues, prs, branches_raw, runs)):
            raise GitHubProviderError("GitHub collection returned an unexpected shape")
        repo_object = cast(dict[str, Any], repo_data)
        issue_objects = _object_list(issues, "issues")
        pr_objects = _object_list(prs, "pull requests")
        branch_objects = _object_list(branches_raw, "branches")
        run_objects = _object_list(runs, "workflow runs")
        branches = [str(item["name"]) for item in branch_objects if item.get("name")]
        return RepositorySnapshot(repo_object, issue_objects, pr_objects, branches, run_objects)

    def readiness_evidence(self, repo: RepoConfig) -> dict[str, Any]:
        """Collect sanitized live control-plane evidence for independent planning review."""
        evidence: dict[str, Any] = {
            "collected_at": datetime.now(UTC).isoformat(),
            "repository": repo.full_name,
            "collector": "gh",
            "collection_errors": {},
        }

        # A greenfield course is reviewed before its approval-gated repository
        # exists. Capture the capability to provision it separately from the
        # snapshot that is only available after approval.
        evidence["repository_lifecycle"] = {
            "provisioning_enabled": repo.provisioning.enabled,
            "visibility": repo.provisioning.visibility,
            "remote_creation_is_approval_gated": repo.provisioning.enabled,
            "snapshot_expected_before_approval": not repo.provisioning.enabled,
        }

        def collect(key: str, operation: Any) -> None:
            try:
                evidence[key] = operation()
            except (GitHubProviderError, KeyError, TypeError, ValueError) as exc:
                cast(dict[str, str], evidence["collection_errors"])[key] = str(exc)[:1000]

        def collect_authentication() -> dict[str, Any]:
            result = self.runner(["gh", "auth", "status"], cwd=self.cwd, timeout=30)
            if result.returncode:
                raise GitHubProviderError((result.stderr or result.stdout).strip()[:1000])
            return {"authenticated": True}

        snapshot_holder: dict[str, RepositorySnapshot] = {}

        def collect_snapshot() -> dict[str, Any]:
            snapshot = self.snapshot(repo)
            snapshot_holder["value"] = snapshot
            return {
                "repository": snapshot.repo,
                "open_issues": [
                    {key: item.get(key) for key in ("number", "title", "updatedAt", "url")}
                    for item in snapshot.issues
                ],
                "open_pull_requests": [
                    {
                        key: item.get(key)
                        for key in (
                            "number", "title", "headRefName", "headRefOid", "baseRefName",
                            "isDraft", "mergeStateStatus", "reviewDecision", "updatedAt", "url",
                        )
                    }
                    for item in snapshot.pull_requests
                ],
                "branches": snapshot.branches,
                "recent_workflow_runs": snapshot.workflow_runs[:20],
            }

        collect("github_auth", collect_authentication)
        collect("snapshot", collect_snapshot)
        collect("default_branch_sha", lambda: self.default_branch_sha(repo))
        collect("required_checks", lambda: sorted(self.required_check_names(repo)))
        collect("environments", lambda: self._json(["api", f"repos/{repo.full_name}/environments"]))

        snapshot = snapshot_holder.get("value")
        if snapshot is not None:
            pull_requests: list[dict[str, Any]] = []
            for summary in snapshot.pull_requests:
                number = summary.get("number")
                if not isinstance(number, int):
                    continue
                row: dict[str, Any] = {"number": number, "collection_errors": {}}
                try:
                    detail = self.pull_request(repo, number)
                    row["state"] = {
                        key: detail.get(key)
                        for key in (
                            "number", "title", "headRefName", "headRefOid", "baseRefName",
                            "isDraft", "mergeable", "mergeStateStatus", "reviewDecision",
                            "statusCheckRollup", "updatedAt", "url",
                        )
                    }
                except GitHubProviderError as exc:
                    cast(dict[str, str], row["collection_errors"])["state"] = str(exc)[:1000]
                try:
                    threads = self.review_threads(repo, number)
                    row["review_threads"] = {
                        "total": len(threads),
                        "unresolved_blocking": sum(
                            1 for thread in threads
                            if not thread.get("isResolved") and not thread.get("isOutdated")
                        ),
                    }
                except GitHubProviderError as exc:
                    cast(dict[str, str], row["collection_errors"])["review_threads"] = str(exc)[:1000]
                pull_requests.append(row)
            evidence["pull_requests"] = pull_requests
        return evidence

    def pull_request(self, repo: RepoConfig, number: int) -> dict[str, Any]:
        value = self._json(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                repo.full_name,
                "--json",
                "number,title,body,headRefName,headRefOid,baseRefName,isDraft,mergeable,mergeStateStatus,"
                "reviewDecision,statusCheckRollup,files,commits,updatedAt,url,author",
            ]
        )
        if not isinstance(value, dict):
            raise GitHubProviderError("pull request response was not an object")
        return cast(dict[str, Any], value)

    def pull_request_diff(self, repo: RepoConfig, number: int) -> str:
        result = self.runner(
            ["gh", "pr", "diff", str(number), "--repo", repo.full_name],
            cwd=self.cwd,
            timeout=180,
        )
        if result.returncode:
            raise GitHubProviderError((result.stderr or result.stdout).strip()[:3000])
        return result.stdout

    def pull_request_files(self, repo: RepoConfig, number: int) -> tuple[str, ...]:
        value = self.pull_request(repo, number).get("files")
        if not isinstance(value, list):
            raise GitHubProviderError("pull request files are missing or unreadable")
        rows = _object_list(cast(object, value), "pull request files")
        paths = tuple(str(item.get("path") or "").strip() for item in rows)
        if any(not path for path in paths):
            raise GitHubProviderError("pull request files contain a missing path")
        return paths

    def review_threads(self, repo: RepoConfig, number: int) -> list[dict[str, Any]]:
        owner, name = repo.full_name.split("/", 1)
        query = """
        query($owner:String!,$name:String!,$number:Int!){
          repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100){nodes{
            id isResolved isOutdated path line comments(first:50){nodes{id body createdAt url author{login}}}
          }}}}
        }
        """
        value = self._json(
            [
                "api",
                "graphql",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
                "-F",
                f"number={number}",
                "-f",
                f"query={query}",
            ]
        )
        try:
            root = cast(dict[str, Any], value)
            nodes = root["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        except (KeyError, TypeError) as exc:
            raise GitHubProviderError("review thread response was incomplete") from exc
        if not isinstance(nodes, list):
            raise GitHubProviderError("review thread nodes were not a list")
        return _object_list(cast(object, nodes), "review thread nodes")

    def required_check_names(self, repo: RepoConfig) -> set[str]:
        result = self.runner(
            [
                "gh",
                "api",
                f"repos/{repo.full_name}/branches/{repo.default_branch}/protection/required_status_checks",
            ],
            cwd=self.cwd,
            timeout=90,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            if "404" in detail or "not found" in detail.lower():
                return set()
            if (
                "upgrade to github pro" in detail.lower()
                and "enable this feature" in detail.lower()
            ):
                # GitHub Free private repositories return this product-limit
                # response for a branch-protection endpoint even when the
                # caller can read and administer the repository. No required
                # checks can be configured in that state.
                return set()
            raise GitHubProviderError(f"required check query failed: {detail[:3000]}")
        try:
            payload = cast(object, json.loads(result.stdout))
        except json.JSONDecodeError:
            raise GitHubProviderError("required check response was not valid JSON") from None
        if not isinstance(payload, dict):
            raise GitHubProviderError("required check response was not an object")
        payload_object = cast(dict[str, Any], payload)
        contexts_value = payload_object.get("contexts")
        checks_value = payload_object.get("checks")
        contexts = cast(list[Any], contexts_value) if isinstance(contexts_value, list) else []
        checks = (
            _object_list(cast(object, checks_value), "required checks")
            if isinstance(checks_value, list)
            else []
        )
        names = {str(item) for item in contexts}
        names.update(str(item.get("context")) for item in checks if item.get("context"))
        return names

    def gate(self, repo: RepoConfig, number: int, review_head_sha: str | None) -> PullRequestGate:
        pr = self.pull_request(repo, number)
        rollup = pr.get("statusCheckRollup")
        if not isinstance(rollup, list):
            raise GitHubProviderError("statusCheckRollup is missing or unreadable")
        required_names = self.required_check_names(repo)
        check_rows: list[CheckResult] = []
        for item in _object_list(cast(object, rollup), "status check rollup"):
            name = str(item.get("name") or item.get("workflowName") or item.get("context") or "unknown")
            if required_names and name not in required_names:
                continue
            check_rows.append(
                CheckResult(
                    name=name,
                    status=str(item.get("status") or "UNKNOWN").upper(),
                    conclusion=str(item.get("conclusion") or "") or None,
                    url=cast(Any, item.get("detailsUrl") or item.get("url")),
                )
            )
        observed_names = {check.name for check in check_rows}
        for missing_name in sorted(required_names - observed_names):
            check_rows.append(
                CheckResult(
                    name=missing_name,
                    status="MISSING",
                    conclusion=None,
                    url=None,
                )
            )
        # An empty rollup is acceptable when the branch has no required
        # checks. Otherwise every required check must be present, complete,
        # and successful; missing, pending, or failed checks remain fail-closed.
        checks_green = (not required_names and not check_rows) or (
            bool(check_rows)
            and all(
                check.status == "COMPLETED"
                and (check.conclusion or "").upper() in {"SUCCESS", "NEUTRAL", "SKIPPED"}
                for check in check_rows
            )
        )
        active_threads = [
            thread
            for thread in self.review_threads(repo, number)
            if not thread.get("isResolved") and not thread.get("isOutdated")
        ]
        return PullRequestGate(
            number=number,
            head_sha=str(pr.get("headRefOid") or ""),
            mergeable=bool(pr.get("mergeable") in {True, "MERGEABLE"}),
            merge_state=str(pr.get("mergeStateStatus") or "UNKNOWN"),
            draft=bool(pr.get("isDraft")),
            checks_green=checks_green,
            required_checks=tuple(check_rows),
            unresolved_threads=len(active_threads),
            review_head_sha=review_head_sha,
        )

    def create_pull_request(
        self, repo: RepoConfig, *, branch: str, title: str, body: str, draft: bool = True
    ) -> dict[str, Any]:
        args = [
            "pr",
            "create",
            "--repo",
            repo.full_name,
            "--base",
            repo.default_branch,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        ]
        if draft:
            args.append("--draft")
        result = self.runner(["gh", *args], cwd=self.cwd, timeout=180)
        if result.returncode:
            raise GitHubProviderError((result.stderr or result.stdout).strip()[:3000])
        prs = self._json(
            [
                "pr",
                "list",
                "--repo",
                repo.full_name,
                "--state",
                "open",
                "--head",
                branch,
                "--json",
                "number,title,url,headRefName,isDraft",
            ]
        )
        if not isinstance(prs, list) or not prs:
            raise GitHubProviderError("pull request creation succeeded but the PR could not be read back")
        return _object_list(cast(object, prs), "created pull requests")[0]

    def provision_greenfield(self, repo: RepoConfig, course: Course) -> dict[str, Any]:
        """Create and push a seeded local greenfield repository after approval."""
        if not repo.provisioning.enabled:
            raise GitHubProviderError("greenfield repository provisioning is disabled")
        if course.status != CourseStatus.ENGAGED:
            raise GitHubProviderError("greenfield repository provisioning requires an engaged course")
        if not repo.local_path.is_dir():
            raise GitHubProviderError(f"greenfield source directory does not exist: {repo.local_path}")

        # `gh repo create --source --push` expects a committed local repository.
        # Keep this bootstrap inside the GitHub adapter so the core never assumes
        # a particular Git CLI or remote-host implementation.
        if not (repo.local_path / ".git").exists():
            self._run_git(repo, ["init", "-b", repo.default_branch])
        self._run_git(repo, ["add", "--all"])
        status = self._run_git(repo, ["status", "--porcelain"])
        if status.stdout.strip():
            self._run_git(
                repo,
                [
                    "-c",
                    "user.name=Make It So",
                    "-c",
                    "user.email=make-it-so@localhost",
                    "commit",
                    "-m",
                    f"Initialize course {course.key}",
                ],
            )
        visibility_flag = f"--{repo.provisioning.visibility}"
        args = [
            "repo",
            "create",
            repo.full_name,
            "--source",
            str(repo.local_path),
            "--push",
            visibility_flag,
            "--description",
            repo.provisioning.description or course.title,
        ]
        result = self.runner(["gh", *args], cwd=repo.local_path.parent, timeout=300)
        if result.returncode:
            raise GitHubProviderError((result.stderr or result.stdout).strip()[:3000])
        value = self._json(
            [
                "repo",
                "view",
                repo.full_name,
                "--json",
                "nameWithOwner,url,visibility,defaultBranchRef",
            ]
        )
        if not isinstance(value, dict):
            raise GitHubProviderError("created repository response was not an object")
        return {"created": True, **cast(dict[str, Any], value)}

    def _run_git(self, repo: RepoConfig, args: list[str]) -> CommandResult:
        result = self.runner(["git", *args], cwd=repo.local_path, timeout=180)
        if result.returncode:
            raise GitHubProviderError((result.stderr or result.stdout).strip()[:3000])
        return result

    def mark_ready(self, repo: RepoConfig, number: int) -> None:
        result = self.runner(
            ["gh", "pr", "ready", str(number), "--repo", repo.full_name],
            cwd=self.cwd,
            timeout=90,
        )
        if result.returncode:
            raise GitHubProviderError((result.stderr or result.stdout).strip()[:3000])

    def comment_pull_request(self, repo: RepoConfig, number: int, body: str) -> None:
        result = self.runner(
            [
                "gh",
                "pr",
                "comment",
                str(number),
                "--repo",
                repo.full_name,
                "--body",
                body,
            ],
            cwd=self.cwd,
            timeout=120,
        )
        if result.returncode:
            raise GitHubProviderError((result.stderr or result.stdout).strip()[:3000])

    def merge(self, repo: RepoConfig, number: int) -> None:
        result = self.runner(
            ["gh", "pr", "merge", str(number), "--repo", repo.full_name, "--squash"],
            cwd=self.cwd,
            timeout=300,
        )
        if result.returncode:
            raise GitHubProviderError((result.stderr or result.stdout).strip()[:3000])

    def default_branch_sha(self, repo: RepoConfig) -> str:
        value = self._json(["api", f"repos/{repo.full_name}/commits/{repo.default_branch}"])
        if not isinstance(value, dict):
            raise GitHubProviderError("default branch commit response was not an object")
        commit = cast(dict[str, Any], value)
        if not commit.get("sha"):
            raise GitHubProviderError("default branch commit response did not include a SHA")
        return str(commit["sha"])

    def create_issue(self, repo: RepoConfig, title: str, body: str) -> dict[str, Any]:
        result = self.runner(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                repo.full_name,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=self.cwd,
            timeout=120,
        )
        if result.returncode:
            raise GitHubProviderError((result.stderr or result.stdout).strip()[:3000])
        return {"url": result.stdout.strip(), "title": title}

    def update_issue(self, repo: RepoConfig, number: int, title: str | None, body: str | None) -> None:
        args = ["gh", "issue", "edit", str(number), "--repo", repo.full_name]
        if title is not None:
            args.extend(["--title", title])
        if body is not None:
            args.extend(["--body", body])
        _require_success(self.runner(args, cwd=self.cwd, timeout=120), "update issue")

    def label_issue(self, repo: RepoConfig, number: int, labels: tuple[str, ...]) -> None:
        args = ["gh", "issue", "edit", str(number), "--repo", repo.full_name]
        for label in labels:
            args.extend(["--add-label", label])
        _require_success(self.runner(args, cwd=self.cwd, timeout=120), "label issue")

    def retarget_issue(
        self,
        repo: RepoConfig,
        number: int,
        milestone: str | None,
        assignees: tuple[str, ...],
    ) -> None:
        args = ["gh", "issue", "edit", str(number), "--repo", repo.full_name]
        if milestone is not None:
            args.extend(["--milestone", milestone])
        for assignee in assignees:
            args.extend(["--add-assignee", assignee])
        _require_success(self.runner(args, cwd=self.cwd, timeout=120), "retarget issue")

    def close_issue(self, repo: RepoConfig, number: int, reason: str) -> None:
        _require_success(
            self.runner(
                [
                    "gh",
                    "issue",
                    "close",
                    str(number),
                    "--repo",
                    repo.full_name,
                    "--comment",
                    reason,
                ],
                cwd=self.cwd,
                timeout=120,
            ),
            "close issue",
        )


def _require_success(result: Any, operation: str) -> None:
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise GitHubProviderError(f"{operation} failed: {detail[:3000]}")


def _object_list(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise GitHubProviderError(f"{label} response was not a list of objects")
    items = cast(list[object], value)
    if not all(isinstance(item, dict) for item in items):
        raise GitHubProviderError(f"{label} response was not a list of objects")
    return cast(list[dict[str, Any]], items)
