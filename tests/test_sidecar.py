import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

import captains_chair.sidecar as sidecar_module
from captains_chair.config import load_config
from captains_chair.github import GitHubProvider, RepositorySnapshot
from captains_chair.models import (
    CourseKind,
    HarnessConfig,
    OpenClawWorkboardConfig,
    OperationMode,
    RepoConfig,
    RepositoryProvisioningConfig,
    WorkerAssignments,
)
from captains_chair.orchestration import QueueCard, QueueStatus
from captains_chair.sidecar import SidecarError, SidecarServer
from tests.helpers import app_config, repo_config
from tests.test_courses import ready_course, rebind_readiness_review


class GreenfieldProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def provision_greenfield(self, repo: RepoConfig, course: Any) -> dict[str, Any]:
        self.calls.append((repo.full_name, course.key))
        return {
            "created": True,
            "nameWithOwner": repo.full_name,
            "url": f"https://github.test/{repo.full_name}",
        }


def _workboard_card(stage: str, status: QueueStatus, timestamp: int) -> QueueCard:
    return QueueCard(
        id=f"{stage}-{status.value}",
        title=f"{stage} card",
        status=status,
        labels=("workflow:test-workflow", f"stage:{stage}"),
        metadata={"comments": [{"createdAt": timestamp}]},
    )


def test_sidecar_projects_terminal_workboard_proof_into_completed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_config(tmp_path).model_copy(
        update={"orchestrator": "openclaw", "orchestration_board": "test-board"}
    )
    workers = WorkerAssignments(
        captain="captain",
        coder="coder",
        reviewer="reviewer",
        tester="tester",
        ux_reviewer="ux",
        final_reviewer="final",
        merger="merger",
        verifier="verifier",
    )
    orchestrator = OpenClawWorkboardConfig(
        workers=workers,
        require_live_completion_validation=False,
    )
    config = app_config(tmp_path, repo_config(tmp_path)).model_copy(
        update={"repos": (repo,), "orchestrators": {"openclaw": orchestrator}}
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")

    cards = [
        _workboard_card("review", QueueStatus.DONE, 1),
        _workboard_card("merge", QueueStatus.DONE, 2),
        _workboard_card("post_merge", QueueStatus.DONE, 3),
        _workboard_card("repair", QueueStatus.BLOCKED, 4),
    ]

    class Adapter:
        def list_cards(self, board_id: str) -> list[QueueCard]:
            assert board_id == "test-board"
            return cards

    monkeypatch.setattr(sidecar_module, "build_work_queue_adapter", lambda _config: Adapter())
    server = SidecarServer(config_path)

    result = server.request("portfolio.status")["repos"][0]

    assert result["state"] == "merged"
    assert result["state_source"] == "workboard"
    assert result["workboard_status"]["status"] == "completed"
    assert result["workboard_status"]["active_cards"] == 0


def test_sidecar_does_not_mark_workboard_with_active_cards_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_config(tmp_path).model_copy(
        update={"orchestrator": "openclaw", "orchestration_board": "test-board"}
    )
    workers = WorkerAssignments(
        captain="captain",
        coder="coder",
        reviewer="reviewer",
        tester="tester",
        ux_reviewer="ux",
        final_reviewer="final",
        merger="merger",
        verifier="verifier",
    )
    orchestrator = OpenClawWorkboardConfig(
        workers=workers,
        require_live_completion_validation=False,
    )
    config = app_config(tmp_path, repo_config(tmp_path)).model_copy(
        update={"repos": (repo,), "orchestrators": {"openclaw": orchestrator}}
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")

    cards = [
        _workboard_card("merge", QueueStatus.RUNNING, 2),
        _workboard_card("post_merge", QueueStatus.TODO, 3),
    ]

    class Adapter:
        def list_cards(self, board_id: str) -> list[QueueCard]:
            assert board_id == "test-board"
            return cards

    monkeypatch.setattr(sidecar_module, "build_work_queue_adapter", lambda _config: Adapter())
    server = SidecarServer(config_path)

    result = server.request("portfolio.status")["repos"][0]

    assert result["state"] == "unbaselined"
    assert result["workboard_status"]["status"] == "in_progress"
    assert result["workboard_status"]["active_cards"] == 2


def test_sidecar_correlates_workboard_sessions_and_reports_execution_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_config(tmp_path).model_copy(
        update={"orchestrator": "openclaw", "orchestration_board": "test-board"}
    )
    workers = WorkerAssignments(
        captain="captain",
        coder="coder",
        reviewer="reviewer",
        tester="tester",
        ux_reviewer="ux",
        final_reviewer="final",
        merger="merger",
        verifier="verifier",
    )
    orchestrator = OpenClawWorkboardConfig(
        workers=workers,
        require_live_completion_validation=False,
    )
    config = app_config(tmp_path, repo_config(tmp_path)).model_copy(
        update={"repos": (repo,), "orchestrators": {"openclaw": orchestrator}}
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")

    cards = [
        QueueCard(
            id="merge-done",
            title="Merge implementation PR",
            status=QueueStatus.DONE,
            labels=("workflow:routing-canary", "stage:merge"),
            agent_id="merger",
            source_url="https://github.com/mln330/captains-chair/pull/42",
            metadata={"comments": [{"createdAt": 2, "body": "Merged after green gates."}]},
        ),
        QueueCard(
            id="post-merge-done",
            title="Verify merged change",
            status=QueueStatus.DONE,
            labels=("workflow:routing-canary", "stage:post_merge"),
            agent_id="verifier",
            metadata={"comments": [{"createdAt": 3, "body": "Post-merge check passed."}]},
        ),
    ]

    class Adapter:
        def list_cards(self, board_id: str) -> list[QueueCard]:
            assert board_id == "test-board"
            return cards

    class Github:
        def snapshot(self, _repo: RepoConfig) -> RepositorySnapshot:
            return RepositorySnapshot(
                repo={},
                issues=[{"number": 7}],
                pull_requests=[
                    {
                        "number": 42,
                        "title": "Routing canary",
                        "url": "https://github.com/mln330/captains-chair/pull/42",
                        "isDraft": False,
                        "reviewDecision": "APPROVED",
                    }
                ],
                branches=["main", "feature/routing"],
                workflow_runs=[{"status": "completed", "conclusion": "success"}],
            )

    def sync(state: Any, **kwargs: Any) -> dict[str, Any]:
        assert "merge-done" in kwargs["session_context"]
        state.record_external_usage(
            {
                "source": "openclaw-session",
                "external_id": "agent:github-merge:captains-chair:worker:merge-done:managed:1",
                "repo": kwargs["repo"],
                "role": "merger",
                "stage": "merge",
                "provider": "codex",
                "model": "codex/gpt-5.6-terra",
                "input_tokens": 20,
                "output_tokens": 7,
                "total_tokens": 27,
                "total_tokens_fresh": True,
            }
        )
        return {
            "repo": kwargs["repo"],
            "source": "openclaw-session",
            "sessions_seen": 1,
            "sessions_imported": 1,
            "sessions_with_usage": 1,
        }

    monkeypatch.setattr(sidecar_module, "build_work_queue_adapter", lambda _config: Adapter())
    monkeypatch.setattr(sidecar_module, "sync_openclaw_sessions", sync)
    server = SidecarServer(config_path, github=cast(GitHubProvider, Github()))

    result = server.request("portfolio.status")["repos"][0]

    assert result["tokens"]["accounted_tokens"] == 27
    assert result["workboard_status"]["usage_sync"]["status"] == "ok"
    assert result["workboard_status"]["loop_count"] == 0
    assert result["workboard_status"]["pr_count"] == 1
    assert result["workboard_status"]["pr_numbers"] == [42]
    assert result["workboard_status"]["pr_urls"] == [
        "https://github.com/mln330/captains-chair/pull/42"
    ]
    assert result["github_status"]["open_prs"] == 1
    assert result["github_status"]["checks"] == {"recent": 1, "failed": 0, "pending": 0, "passed": 1}


def test_sidecar_reports_health_portfolio_and_schedule_contract(tmp_path: Path) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)

    health = server.request("health")
    assert health["status"] == "healthy"
    assert health["version"] == "0.2.0"
    assert health["protocol_version"] == 1
    status = server.request("portfolio.status")
    assert status["repos"][0]["full_name"] == "example/project"
    assert status["repos"][0]["state"] == "unbaselined"
    assert status["repos"][0]["events"] == []
    assert "model_totals" in status["repos"][0]["usage_detail"]
    assert status["repos"][0]["usage_detail"]["dimensions"] == []
    server.state.record_model_call(
        "example/project",
        "run-1",
        "coder",
        "test-model",
        [{"total_tokens": 42, "success": True}],
        course_key="feature-search",
        work_package_key="api",
        stage="implementation",
    )
    dimension = server.request("portfolio.status")["repos"][0]["usage_detail"]["dimensions"][0]
    assert dimension["course_key"] == "feature-search"
    assert dimension["work_package_key"] == "api"
    assert dimension["stage"] == "implementation"
    assert dimension["tokens"] == 42
    schedule = server.request("schedule.describe")
    assert schedule["source_of_truth"] == "openclaw_gateway_cron"
    assert [job["name"] for job in schedule["jobs"]] == [
        "captains-chair-reconcile",
        "captains-chair-course-review",
    ]
    assert [job["kind"] for job in schedule["jobs"]] == ["reconcile", "review"]
    assert schedule["repository_enablement"] == {"example/project": True}

    configured = server.request(
        "schedule.configure",
        {"reconcile_every": "10m", "review_every": "4h"},
    )
    assert [job["every"] for job in configured["jobs"]] == ["10m", "4h"]
    assert load_config(config_path).schedules.review_every == "4h"

    server.state.note_attention("example/project", "decision-1", "ATTENTION_REQUIRED")
    acknowledged = server.request(
        "attention.ack",
        {"full_name": "example/project", "fingerprint": "decision-1"},
    )
    assert acknowledged["count"] == 1


def test_sidecar_registers_and_updates_repositories_atomically(tmp_path: Path) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)

    registered = server.request(
        "repo.register",
        {
            "full_name": "example/second",
            "local_path": str(tmp_path / "second"),
            "planning_doc": "PLAN.md",
            "checks": ["pytest"],
        },
    )
    assert registered["status"] == "registered"
    assert len(load_config(config_path).repos) == 2

    updated = server.request(
        "repo.update",
        {
            "full_name": "example/second",
            "local_path": str(tmp_path / "second-clean-worktree"),
            "operation_mode": "supervised",
        },
    )
    assert updated["repo"]["operation_mode"] == "supervised"
    persisted = load_config(config_path).repo("example/second")
    assert persisted.operation_mode.value == "supervised"
    assert persisted.local_path == tmp_path / "second-clean-worktree"

    scheduled = server.request(
        "repo.update",
        {"full_name": "example/second", "schedule_enabled": False},
    )
    assert scheduled["repo"]["schedule_enabled"] is False


def test_sidecar_validates_model_routes_without_spending_model_tokens(tmp_path: Path) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)

    unverified = server.request(
        "models.validate",
        {
            "full_name": "example/project",
            "model_profiles": {"coder": {"primary": {"model": "codex/gpt-5.3-codex-spark"}}},
        },
    )
    assert unverified["status"] == "unverified"
    assert unverified["can_save"] is True
    assert "route test" in unverified["warnings"][0]["warning"]

    invalid = server.request(
        "models.validate",
        {"full_name": "example/project", "model_profiles": {"coder": {"primary": {}}}},
    )
    assert invalid["status"] == "invalid"
    assert invalid["can_save"] is False


def test_sidecar_reads_and_updates_global_and_runtime_model_layers(tmp_path: Path) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)

    current = server.request("models.config")
    assert "baseline" in current["global_profiles"]
    assert "test" in current["runtimes"]
    assert current["usage"]["block_on_unknown"] is False

    global_update = server.request(
        "models.update",
        {
            "scope": "global",
            "model_profiles": {"coder": {"primary": {"model": "global-coder", "thinking": "medium"}}},
        },
    )
    assert global_update["status"] == "updated"
    assert global_update["global_profiles"]["coder"]["primary"]["model"] == "global-coder"

    runtime_update = server.request(
        "models.update",
        {
            "scope": "runtime",
            "runtime": "test",
            "model_profiles": {"coder": {"primary": {"model": "runtime-coder", "thinking": "low"}}},
        },
    )
    assert runtime_update["runtime_profiles"]["test"]["coder"]["primary"]["model"] == "runtime-coder"
    assert load_config(config_path).harness_model_overrides["test"].profiles["coder"].primary.model == "runtime-coder"

    usage_update = server.request(
        "usage.update",
        {
            "daily_token_limit": 1000,
            "model_daily_token_limits": {"codex/gpt-5.3-codex-spark": 600},
            "block_on_unknown": True,
        },
    )
    assert usage_update["usage"] == {
        "daily_token_limit": 1000,
        "model_daily_token_limits": {"codex/gpt-5.3-codex-spark": 600},
        "block_on_unknown": True,
        "allow_incomplete_telemetry": False,
        "retention_days": 90,
    }
    assert load_config(config_path).usage.daily_token_limit == 1000

    with pytest.raises(SidecarError, match="invalid usage configuration"):
        server.request("usage.update", {"model_daily_token_limits": {"": -1}})

    with pytest.raises(SidecarError, match="unknown model runtime"):
        server.request(
            "models.update",
            {"scope": "runtime", "runtime": "missing", "model_profiles": {}},
        )


def test_sidecar_revalidates_cross_field_policy_before_persisting(tmp_path: Path) -> None:
    repo = repo_config(tmp_path, mode=OperationMode.AUTONOMOUS)
    config = app_config(tmp_path, repo)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)

    with pytest.raises(SidecarError, match="invalid application configuration"):
        server.request("usage.update", {"allow_incomplete_telemetry": True})

    assert load_config(config_path).usage.allow_incomplete_telemetry is False


def test_sidecar_exposes_course_creation_readiness_approval_and_ready_work(tmp_path: Path) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)
    course = ready_course().model_dump(mode="json")

    created = server.request(
        "course.create",
        {"full_name": "example/project", "course": course},
    )
    assert created["status"] == "created"
    assert created["readiness"]["ready"] is True
    assert server.request("courses.list")["courses"][0]["course"]["key"] == "feature-search"

    approved = server.request(
        "course.approve",
        {"full_name": "example/project", "course_key": "feature-search", "approved_by": "owner"},
    )
    assert approved["course"]["status"] == "engaged"
    ready = server.request(
        "course.ready_work",
        {"full_name": "example/project", "course_key": "feature-search"},
    )
    assert {item["key"] for item in ready["work_packages"]} == {"index", "docs"}


def test_greenfield_repo_creation_waits_for_course_approval_and_seeds_durable_files(
    tmp_path: Path,
) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    provider = GreenfieldProvider()
    server = SidecarServer(config_path, github=cast(Any, provider))
    local_path = tmp_path / "new-project"
    course = rebind_readiness_review(
        ready_course().model_copy(
            update={
                "key": "first-course",
                "repository": "example/new-project",
                "kind": CourseKind.GREENFIELD,
                "title": "New project",
            }
        )
    ).model_dump(mode="json")

    pending = server.request(
        "repo.create",
        {
            "full_name": "example/new-project",
            "local_path": str(local_path),
            "description": "A new project",
            "visibility": "private",
            "course": course,
        },
    )

    assert pending["status"] == "awaiting_course_approval"
    assert provider.calls == []
    assert (local_path / ".captains-chair" / "courses" / "first-course.yaml").is_file()

    engaged = server.request(
        "course.approve",
        {"full_name": "example/new-project", "course_key": "first-course", "approved_by": "owner"},
    )

    assert engaged["status"] == "engaged"
    assert engaged["provisioning"]["created"] is True
    assert provider.calls == [("example/new-project", "first-course")]
    assert (local_path / "README.md").is_file()
    assert (local_path / "docs" / "IMPLEMENTATION_PLAN.md").is_file()
    assert (local_path / ".captains-chair" / "project.yaml").is_file()
    assert load_config(config_path).repo("example/new-project").provisioning.enabled is True


def test_greenfield_repo_creation_defaults_to_configured_openclaw_orchestrator(
    tmp_path: Path,
) -> None:
    workers = WorkerAssignments(
        captain="captain",
        coder="coder",
        reviewer="reviewer",
        tester="tester",
        ux_reviewer="ux",
        final_reviewer="final",
        merger="merger",
        verifier="verifier",
    )
    root_config = app_config(tmp_path, repo_config(tmp_path))
    config = root_config.model_copy(
        update={
            "orchestrators": {
                "openclaw-workers": OpenClawWorkboardConfig(workers=workers)
            }
        }
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path, github=cast(Any, GreenfieldProvider()))
    course = rebind_readiness_review(
        ready_course().model_copy(
            update={
                "key": "openclaw-course",
                "repository": "example/openclaw-project",
                "kind": CourseKind.GREENFIELD,
            }
        )
    ).model_dump(mode="json")

    server.request(
        "repo.create",
        {
            "full_name": "example/openclaw-project",
            "local_path": str(tmp_path / "openclaw-project"),
            "course": course,
        },
    )

    assert load_config(config_path).repo("example/openclaw-project").orchestrator == "openclaw-workers"


@pytest.mark.parametrize("kind", tuple(CourseKind))
def test_sidecar_supports_all_onboarding_course_kinds(tmp_path: Path, kind: CourseKind) -> None:
    repo = repo_config(tmp_path)
    if kind == CourseKind.GREENFIELD:
        repo = repo.model_copy(update={"provisioning": RepositoryProvisioningConfig(enabled=True)})
    config = app_config(tmp_path, repo)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path, github=cast(Any, GreenfieldProvider()))
    course_key = f"{kind.value}-course"
    course = rebind_readiness_review(
        ready_course().model_copy(update={"key": course_key, "kind": kind})
    ).model_dump(mode="json")

    created = server.request("course.create", {"full_name": "example/project", "course": course})
    assert created["course"]["kind"] == kind.value
    planning = server.request(
        "course.planning_session",
        {"full_name": "example/project", "course_key": course_key},
    )
    assert planning["next_questions"] == []
    assert planning["mutation_requires_course_approval"] is True
    approved = server.request(
        "course.approve",
        {"full_name": "example/project", "course_key": course_key, "approved_by": "owner"},
    )
    assert approved["course"]["status"] == "engaged"
    ready = server.request(
        "course.ready_work",
        {"full_name": "example/project", "course_key": course_key},
    )
    assert ready["work_packages"]


def test_sidecar_run_once_executes_the_bounded_review_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "review complete", "")

    monkeypatch.setattr("captains_chair.sidecar.subprocess.run", fake_run)
    result = server.request("run.once", {"kind": "review"})

    assert result["status"] == "completed"
    assert result["model_invocations"] is None
    assert calls[0][-2:] == ["--live", "--continue-run"]
    assert result["execution"][0]["output"] == "review complete"


def test_sidecar_reconcile_does_not_skip_board_free_repositories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "reconcile complete", "")

    monkeypatch.setattr("captains_chair.sidecar.subprocess.run", fake_run)
    result = server.request("run.once", {"kind": "reconcile"})

    assert result["status"] == "completed"
    assert calls[0][-2:] == ["--repo", "example/project"]
    assert result["execution"][0]["output"] == "reconcile complete"


def test_sidecar_course_lifecycle_and_surface_configuration(tmp_path: Path) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)
    course = ready_course().model_dump(mode="json")

    server.request("course.create", {"full_name": "example/project", "course": course})
    params = {"full_name": "example/project", "course_key": "feature-search"}
    assert server.request("course.get", params)["course"]["key"] == "feature-search"
    assert server.request("course.readiness", params)["readiness"]["ready"] is True
    planning = server.request("course.planning_session", params)
    assert planning["interaction"] == "host_agent_conversation"
    assert planning["next_questions"] == []
    with pytest.raises(SidecarError, match="cannot self-verify"):
        server.request(
            "course.requirement",
            {
                **params,
                "requirement_key": "success",
                "status": "verified",
                "answer": "The search flow is fast and ranked.",
                "evidence": ["owner"],
                "verified_by": "owner",
                "verification_model": "test-model",
            },
        )
    approved = server.request("course.approve", {**params, "approved_by": "owner"})
    assert approved["course"]["status"] == "engaged"
    assert server.request("course.ready_work", params)["work_packages"]
    resolved = server.request(
        "course.checkpoint",
        {
            **params,
            "checkpoint_key": "ui-demo",
            "status": "resolved",
            "resolved_by": "owner",
            "evidence": ["demo.png"],
        },
    )
    assert resolved["status"] == "resolved"
    assert server.request("course.pause", params)["status"] == "paused"
    assert server.request("course.resume", params)["status"] == "engaged"

    updated = server.request(
        "repo.update",
        {"full_name": "example/project", "surfaces": ["cli"], "notification_route": "notifications"},
    )
    assert updated["repo"]["surfaces"] == ["cli"]
    assert updated["repo"]["orchestrator"] == "direct"


def test_sidecar_readiness_review_uses_the_durable_cli_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)
    server.request(
        "course.create",
        {"full_name": "example/project", "course": ready_course().model_dump(mode="json")},
    )
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"status":"awaiting_approval","repository":"example/project"}',
            stderr="",
        )

    monkeypatch.setattr("captains_chair.sidecar.subprocess.run", fake_run)
    result = server.request(
        "course.readiness_review",
        {"full_name": "example/project", "course_key": "feature-search", "harness": "test"},
    )

    assert result["status"] == "awaiting_approval"
    assert commands[0][0:3] == [sys.executable, "-m", "captains_chair.cli"]
    assert commands[0][-6:] == [
        "--repo",
        "example/project",
        "--course-key",
        "feature-search",
        "--harness",
        "test",
    ]

    with pytest.raises(SidecarError, match="requires a harness"):
        server.request(
            "course.readiness_review",
            {"full_name": "example/project", "course_key": "feature-search"},
        )

    with pytest.raises(SidecarError, match="unknown sidecar method"):
        server.request("unknown")
    with pytest.raises(SidecarError, match="unsupported one-shot"):
        server.request("run.once", {"kind": "invalid"})


def test_sidecar_updates_course_and_work_package_model_routes(tmp_path: Path) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)
    course = ready_course().model_dump(mode="json")
    params = {"full_name": "example/project", "course_key": "feature-search"}
    server.request("course.create", {"full_name": "example/project", "course": course})

    course_update = server.request(
        "course.models",
        {
            **params,
            "layer": "course",
            "model_profiles": {
                "coder": {"primary": {"model": "codex/course-coder", "thinking": "medium"}},
            },
        },
    )
    assert course_update["status"] == "updated"
    assert course_update["layer"] == "course"
    assert course_update["course"]["model_profiles"]["coder"]["primary"]["model"] == "codex/course-coder"

    package_update = server.request(
        "course.models",
        {
            **params,
            "layer": "work_package",
            "work_package_key": "index",
            "model_profiles": {
                "coder": {"primary": {"model": "codex/package-coder", "thinking": "low"}},
            },
        },
    )
    assert package_update["work_package_key"] == "index"
    index = next(item for item in package_update["course"]["work_packages"] if item["key"] == "index")
    assert index["model_profiles"]["coder"]["primary"]["model"] == "codex/package-coder"
    assert package_update["course"]["model_profiles"]["coder"]["primary"]["model"] == "codex/course-coder"

    stage_update = server.request(
        "course.models",
        {
            **params,
            "layer": "stage",
            "stage_name": "implementation",
            "stage_scope": "course",
            "stage_profile": {"primary": {"model": "codex/stage-coder", "thinking": "medium"}},
        },
    )
    assert stage_update["stage_name"] == "implementation"
    assert stage_update["course"]["model_profiles"]["stage:implementation"]["primary"]["model"] == "codex/stage-coder"

    package_stage = server.request(
        "course.models",
        {
            **params,
            "layer": "stage",
            "stage_name": "review",
            "stage_scope": "work_package",
            "work_package_key": "index",
            "stage_profile": {"primary": {"model": "codex/stage-reviewer", "thinking": "high"}},
        },
    )
    index = next(item for item in package_stage["course"]["work_packages"] if item["key"] == "index")
    assert index["model_profiles"]["stage:review"]["primary"]["model"] == "codex/stage-reviewer"

    with pytest.raises(SidecarError, match="requires work_package_key"):
        server.request(
            "course.models",
            {**params, "layer": "work_package", "model_profiles": {}},
        )
    with pytest.raises(SidecarError, match="not defined"):
        server.request(
            "course.models",
            {**params, "layer": "package", "work_package_key": "missing", "model_profiles": {}},
        )
    with pytest.raises(SidecarError, match="requires stage_name"):
        server.request(
            "course.models",
            {**params, "layer": "stage", "stage_profile": {"primary": {"model": "stage"}}},
        )


def _sidecar(
    tmp_path: Path,
    *,
    repo: RepoConfig | None = None,
    harnesses: dict[str, HarnessConfig] | None = None,
) -> SidecarServer:
    config = app_config(tmp_path, repo_config(tmp_path))
    if repo is not None:
        config = config.model_copy(update={"repos": (repo,)})
    if harnesses is not None:
        config = config.model_copy(update={"harnesses": harnesses})
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    return SidecarServer(config_path)


def test_sidecar_rejects_invalid_repository_and_model_requests(tmp_path: Path) -> None:
    server = _sidecar(tmp_path)

    invalid_requests: tuple[tuple[str, dict[str, Any], str], ...] = (
        ("repo.register", {}, "requires full_name and local_path"),
        ("repo.update", {}, "requires full_name"),
        ("repo.update", {"full_name": "missing/repo"}, "not registered"),
        ("models.validate", {}, "requires full_name or model_profiles"),
        ("models.validate", {"full_name": "missing/repo"}, "not registered"),
        ("models.update", {"scope": "global", "model_profiles": []}, "requires a model_profiles object"),
        (
            "models.update",
            {"scope": "invalid", "model_profiles": {}},
            "scope must be global or runtime",
        ),
        ("models.update", {"scope": "runtime", "model_profiles": {}}, "requires runtime"),
    )
    for method, params, message in invalid_requests:
        with pytest.raises(SidecarError, match=message):
            server.request(method, params)

    with pytest.raises(SidecarError, match="already registered"):
        server.request("repo.register", {"full_name": "example/project", "local_path": str(tmp_path)})

    invalid_route = server.request(
        "models.validate",
        {
            "full_name": "example/project",
            "model_profiles": {
                "bad_shape": "not-an-object",
                "bad_profile": {"primary": {}},
            },
        },
    )
    assert invalid_route["status"] == "invalid"
    assert {item["role"] for item in invalid_route["errors"]} == {"bad_shape", "bad_profile"}
    fallback_route = server.request("models.validate", {"full_name": "example/project"})
    assert fallback_route["repository"] == "example/project"

    with pytest.raises(SidecarError, match="invalid model profile"):
        server.request("models.update", {"scope": "global", "model_profiles": {"coder": {"primary": {}}}})


def test_sidecar_course_validation_errors_are_actionable(tmp_path: Path) -> None:
    server = _sidecar(tmp_path)

    with pytest.raises(SidecarError, match="requires full_name and course_key"):
        server.request("course.get", {})
    with pytest.raises(SidecarError, match="missing/repo"):
        server.request("course.get", {"full_name": "missing/repo", "course_key": "course"})
    with pytest.raises(SidecarError, match="requires full_name and a course object"):
        server.request("course.create", {})
    with pytest.raises(SidecarError, match="requires full_name and a course object"):
        server.request("course.create", {"full_name": "example/project", "course": []})
    with pytest.raises(SidecarError):
        server.request("course.create", {"full_name": "example/project", "course": {"key": "bad"}})

    server.request(
        "course.create",
        {"full_name": "example/project", "course": ready_course().model_dump(mode="json")},
    )
    with pytest.raises(SidecarError, match="requires requirement_key and status"):
        server.request(
            "course.requirement", {"full_name": "example/project", "course_key": "feature-search"}
        )
    with pytest.raises(SidecarError, match="requires checkpoint_key and status"):
        server.request(
            "course.checkpoint", {"full_name": "example/project", "course_key": "feature-search"}
        )
    with pytest.raises(SidecarError):
        server.request(
            "course.requirement",
            {"full_name": "example/project", "course_key": "feature-search", "requirement_key": "success", "status": "invalid"},
        )
    with pytest.raises(SidecarError):
        server.request(
            "course.checkpoint",
            {"full_name": "example/project", "course_key": "feature-search", "checkpoint_key": "ui-demo", "status": "invalid"},
        )


def test_sidecar_covers_stage_and_package_route_validation_errors(tmp_path: Path) -> None:
    server = _sidecar(tmp_path)
    course = ready_course().model_dump(mode="json")
    params = {"full_name": "example/project", "course_key": "feature-search"}
    server.request("course.create", {"full_name": "example/project", "course": course})

    with pytest.raises(SidecarError, match="stage_scope must be"):
        server.request(
            "course.models",
            {
                **params,
                "layer": "stage",
                "stage_name": "review",
                "stage_scope": "invalid",
                "stage_profile": {"primary": {"model": "stage"}},
            },
        )
    with pytest.raises(SidecarError, match="invalid stage model profile"):
        server.request(
            "course.models",
            {**params, "layer": "stage", "stage_name": "review", "stage_profile": {"primary": {}}},
        )
    with pytest.raises(SidecarError, match="stage work-package scope requires"):
        server.request(
            "course.models",
            {
                **params,
                "layer": "stage",
                "stage_name": "review",
                "stage_scope": "work_package",
                "stage_profile": {"primary": {"model": "stage"}},
            },
        )
    with pytest.raises(SidecarError, match="layer must be"):
        server.request("course.models", {**params, "layer": "unknown", "model_profiles": {}})
    with pytest.raises(SidecarError, match="work package is not defined"):
        server.request(
            "course.models",
            {**params, "layer": "work_package", "work_package_key": "missing", "model_profiles": {}},
        )


def test_sidecar_reports_dirty_git_and_one_shot_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    server = _sidecar(tmp_path, repo=repo_config(repo_path))

    def dirty_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(args, 0, " M README.md\n", "")

    monkeypatch.setattr("captains_chair.sidecar.subprocess.run", dirty_run)
    assert server.request("repos.list")["repos"][0]["dirty"] is True

    def unavailable_git(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise OSError("git unavailable")

    monkeypatch.setattr("captains_chair.sidecar.subprocess.run", unavailable_git)
    assert server.request("repos.list")["repos"][0]["dirty"] is True

    def fail_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise subprocess.TimeoutExpired("review", 1)

    monkeypatch.setattr("captains_chair.sidecar.subprocess.run", fail_run)
    result = server.request("run.once", {"kind": "review"})
    assert result["status"] == "degraded"
    assert result["execution"][0]["status"] == "failed"


def test_sidecar_run_once_skips_disabled_repositories_and_requires_review_harness(tmp_path: Path) -> None:
    disabled = repo_config(tmp_path / "disabled", mode=OperationMode.DISABLED)
    server = _sidecar(tmp_path, repo=disabled, harnesses={})

    with pytest.raises(SidecarError, match="requires at least one configured harness"):
        server.request("run.once", {"kind": "review"})

    result = server.request("run.once", {"kind": "reconcile"})
    assert result["status"] == "completed"
    assert result["execution"] == [{"repo": "example/project", "status": "disabled", "exit_code": 0}]


def test_sidecar_stdio_protocol_returns_jsonrpc_errors_without_stopping(tmp_path: Path) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).parents[1] / "src"), environment.get("PYTHONPATH", "")]
    )
    completed = subprocess.run(
        [sys.executable, "-m", "captains_chair.sidecar", "--config", str(config_path)],
        input=(
            '{"jsonrpc":"2.0","id":1,"method":"health","params":{}}\n'
            "not-json\n"
            "[]\n"
            '{"jsonrpc":"2.0","id":2,"method":"missing","params":{}}\n'
        ),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
        timeout=20,
    )

    responses = [yaml.safe_load(line) for line in completed.stdout.splitlines()]
    assert completed.returncode == 0
    assert responses[0]["result"]["status"] == "healthy"
    assert responses[1]["error"]["code"] == "SIDECAR_ERROR"
    assert responses[2]["error"]["code"] == "SIDECAR_ERROR"
    assert responses[3]["error"]["code"] == "SIDECAR_ERROR"


def test_one_shot_process_exit_code_reflects_degraded_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeServer:
        def __init__(self, _config_path: Path) -> None:
            pass

        def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            assert method == "run.once"
            assert params == {"kind": "review"}
            return {"status": "degraded"}

    monkeypatch.setattr(sidecar_module, "SidecarServer", FakeServer)
    monkeypatch.setattr(
        sys,
        "argv",
        ["captains-chair-sidecar", "--config", str(tmp_path / "config.yaml"), "--once", "review"],
    )

    assert sidecar_module.main() == 2
    assert '"status": "degraded"' in capsys.readouterr().out
