import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

import make_it_so.sidecar as sidecar_module
from make_it_so import __version__
from make_it_so.config import load_config
from make_it_so.courses import CourseStore
from make_it_so.github import GitHubProvider, RepositorySnapshot
from make_it_so.models import (
    CourseKind,
    CourseStatus,
    HarnessConfig,
    OpenClawWorkboardConfig,
    OperationMode,
    RepoConfig,
    RepositoryProvisioningConfig,
    WorkerAssignments,
)
from make_it_so.orchestration import QueueCard, QueueStatus
from make_it_so.sidecar import SidecarError, SidecarServer
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
        _workboard_card("final_review", QueueStatus.BLOCKED, 4),
        _workboard_card("repair", QueueStatus.BLOCKED, 5),
    ]

    class Adapter:
        def list_cards(self, board_id: str) -> list[QueueCard]:
            assert board_id == "test-board"
            return cards

    def build_adapter(_config: object) -> Adapter:
        return Adapter()

    monkeypatch.setattr(sidecar_module, "build_work_queue_adapter", build_adapter)
    server = SidecarServer(config_path)

    result = server.request("portfolio.status")["repos"][0]

    assert result["state"] == "merged"
    assert result["state_source"] == "workboard"
    assert result["allow_autonomous_merge"] is False
    assert result["workboard_status"]["status"] == "completed"
    assert result["workboard_status"]["active_cards"] == 0
    assert result["workboard_status"]["current_stage"] == "post_merge"
    assert result["workboard_status"]["review_cycles"] == 2
    assert result["workboard_status"]["reviews_passed"] == 1
    assert result["workboard_status"]["review_status"] == "passed"
    assert result["workboard_status"]["test_status"] == "not_run"
    assert result["workboard_status"]["blockers"] == 0
    assert result["workboard_status"]["current_blockers"] == 0
    assert result["workboard_status"]["historical_blockers"] == 2
    assert result["workboard_status"]["historical_review_blockers"] == 1
    assert result["workboard_status"]["superseded_retries"] == 0
    assert result["workboard_status"]["completion_status"] == "verified"
    assert result["workboard_status"]["workflow_runs"][0]["status"] == "completed"
    assert {row["stage"] for row in result["workboard_status"]["stage_history"]} >= {
        "review",
        "repair",
        "merge",
        "post_merge",
    }
    assert result["workboard_status"]["total_loop_count"] == 1
    assert "Historical blockers" in result["workboard_status"]["message"]


def test_sidecar_hides_historical_terminal_workboard_for_new_readiness_course(
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
    CourseStore(repo.local_path).save(
        sidecar_module.Course(
            key="takeover",
            repository=repo.full_name,
            kind=CourseKind.TAKEOVER,
            title="Fresh takeover",
            goal="Understand and stabilize the repository before implementation begins.",
            readiness=(
                sidecar_module.ReadinessRequirement(
                    key="goals",
                    category="goals",
                    question="What outcome should this course achieve?",
                ),
            ),
            status=CourseStatus.READINESS_REVIEW,
        )
    )

    class Adapter:
        def list_cards(self, board_id: str) -> list[QueueCard]:
            assert board_id == "test-board"
            return [
                _workboard_card("review", QueueStatus.DONE, 1),
                _workboard_card("merge", QueueStatus.DONE, 2),
                _workboard_card("post_merge", QueueStatus.DONE, 3),
            ]

    monkeypatch.setattr(sidecar_module, "build_work_queue_adapter", lambda _config: Adapter())
    result = SidecarServer(config_path).request("portfolio.status")["repos"][0]

    assert result["course_key"] == "takeover"
    assert result["course_status"] == "readiness_review"
    assert result["state"] == "baseline_review"
    assert result["state_source"] == "state_store"
    assert result["workboard_status"] is None


def test_sidecar_separates_superseded_retry_cards_from_historical_blockers() -> None:
    def retry_card(card_id: str, stage: str, status: QueueStatus, timestamp: int) -> QueueCard:
        return QueueCard(
            id=card_id,
            title=f"Retry {stage}",
            status=status,
            labels=("workflow:retry-workflow", f"stage:{stage}", f"retry:{card_id}"),
            metadata={"comments": [{"createdAt": timestamp, "body": "CANCELLED: superseded by successful path"}]},
        )

    summary = sidecar_module._summarize_workboard(  # pyright: ignore[reportPrivateUsage]
        [
            _workboard_card("review", QueueStatus.DONE, 1),
            retry_card("review-retry", "review", QueueStatus.BLOCKED, 2),
            _workboard_card("final_review", QueueStatus.DONE, 3),
            retry_card("final-retry", "final_review", QueueStatus.BLOCKED, 4),
            _workboard_card("merge", QueueStatus.DONE, 5),
            _workboard_card("post_merge", QueueStatus.DONE, 6),
        ],
        "board",
    )

    assert summary["status"] == "completed"
    assert summary["historical_blockers"] == 0
    assert summary["historical_review_blockers"] == 0
    assert summary["current_blockers"] == 0
    assert summary["superseded_retries"] == 2
    assert all(item["superseded_retry"] for item in summary["timeline"] if item["status"] == "blocked")
    review = next(row for row in summary["stage_history"] if row["stage"] == "review")
    assert review["superseded_retries"] == 1
    assert review["historical_blockers"] == 0
    run = summary["workflow_runs"][0]
    assert run["superseded_retries"] == 2
    assert run["historical_blockers"] == 0


def test_workboard_history_preserves_build_review_and_completion_runs() -> None:
    def card(
        card_id: str,
        workflow: str,
        stage: str,
        status: QueueStatus,
        timestamp: int,
        agent: str,
        *,
        attempts: int = 0,
    ) -> QueueCard:
        return QueueCard(
            id=card_id,
            title=f"{stage} evidence",
            status=status,
            labels=(f"workflow:{workflow}", f"stage:{stage}"),
            agent_id=agent,
            source_url="https://github.com/example/project/pull/7",
            metadata={
                "comments": [{"createdAt": timestamp, "body": f"{stage} finished"}],
                "attempts": [{"createdAt": timestamp - index} for index in range(attempts)],
            },
        )

    summary = sidecar_module._summarize_workboard(  # pyright: ignore[reportPrivateUsage]
        [
            card("build", "build-run", "implementation", QueueStatus.DONE, 1_000, "coder"),
            card(
                "stale-build-card",
                "build-run",
                "implementation",
                QueueStatus.TODO,
                1_100,
                "coder",
            ),
            card("review", "review-run", "review", QueueStatus.DONE, 2_000, "reviewer"),
            card(
                "repair",
                "review-run",
                "repair",
                QueueStatus.DONE,
                2_100,
                "coder",
                attempts=2,
            ),
            card("merge", "completion-run", "merge", QueueStatus.DONE, 3_000, "merger"),
            card(
                "verify",
                "completion-run",
                "post_merge",
                QueueStatus.DONE,
                3_100,
                "verifier",
            ),
        ],
        "board",
        worker_models={
            "coder": "codex/gpt-5.6-terra",
            "reviewer": "codex/gpt-5.6-terra",
            "merger": "codex/gpt-5.6-terra",
            "verifier": "codex/gpt-5.6-terra",
        },
    )

    assert [run["kind"] for run in summary["workflow_runs"]] == [
        "build",
        "review",
        "completion",
    ]
    assert [run["status"] for run in summary["workflow_runs"]] == [
        "superseded",
        "superseded",
        "completed",
    ]
    implementation = next(row for row in summary["stage_history"] if row["stage"] == "implementation")
    assert implementation["models"] == ["codex/gpt-5.6-terra"]
    assert implementation["active"] == 0
    assert summary["total_loop_count"] == 1
    assert summary["pr_count"] == 1


def test_sidecar_imports_direct_codex_workboard_usage(tmp_path: Path) -> None:
    state = sidecar_module.StateStore(tmp_path / "state.db")
    card = QueueCard(
        id="spark-card",
        title="Implement with Spark",
        status=QueueStatus.DONE,
        labels=("workflow:spark", "stage:implementation"),
        agent_id="coder",
        metadata={
            "comments": [
                {"createdAt": 1_784_408_000_000},
                {
                    "body": "MAKE_IT_SO_WORKER_EXECUTION:"
                    + json.dumps(
                        {
                            "runtime": "codex",
                            "requested_model": "gpt-5.3-codex-spark",
                            "attempt_id": "attempt-1",
                            "duration_ms": 1234,
                            "usage": {
                                "input_tokens": 100,
                                "cached_input_tokens": 25,
                                "output_tokens": 10,
                                "prompt_bytes": 500,
                                "response_bytes": 100,
                            },
                        }
                    ),
                },
            ],
            "proof": [
                {
                    "status": "passed",
                    "note": "tests passed",
                }
            ],
        },
    )

    first = sidecar_module._sync_workboard_worker_usage(  # pyright: ignore[reportPrivateUsage]
        state, repo="mln330/example", cards=[card]
    )
    second = sidecar_module._sync_workboard_worker_usage(  # pyright: ignore[reportPrivateUsage]
        state, repo="mln330/example", cards=[card]
    )
    summary = state.usage_summary(repo="mln330/example")

    assert first == {"imported": 1}
    assert second == {"imported": 1}
    assert summary["external_sessions"]["calls"] == 1
    assert summary["external_sessions"]["input_tokens"] == 100
    assert summary["external_sessions"]["output_tokens"] == 10
    assert summary["external_groups"][0]["model"] == "gpt-5.3-codex-spark"


def test_sidecar_worker_receipt_parser_ignores_malformed_comments() -> None:
    card = QueueCard(
        id="malformed-receipts",
        title="Malformed receipts",
        status=QueueStatus.DONE,
        metadata={
            "proof": "not-a-list",
            "comments": [
                None,
                {"body": 123},
                {"body": "ordinary comment"},
                {"body": "MAKE_IT_SO_WORKER_EXECUTION:{"},
                {"body": "MAKE_IT_SO_WORKER_EXECUTION:[]"},
            ],
        },
    )

    assert sidecar_module._card_execution(card) is None  # pyright: ignore[reportPrivateUsage]


def test_sidecar_worker_receipt_parser_prefers_proof_execution() -> None:
    card = QueueCard(
        id="proof-receipt",
        title="Proof receipt",
        status=QueueStatus.DONE,
        metadata={
            "proof": [None, {"execution": "invalid"}, {"execution": {"runtime": "codex"}}],
            "comments": [{"body": "MAKE_IT_SO_WORKER_EXECUTION:{}"}],
        },
    )

    assert sidecar_module._card_execution(card) == {  # pyright: ignore[reportPrivateUsage]
        "runtime": "codex"
    }


def test_sidecar_worker_receipt_parser_handles_absent_collections() -> None:
    no_comments = QueueCard(
        id="no-comments",
        title="No comments",
        status=QueueStatus.DONE,
        metadata={"proof": [], "comments": "not-a-list"},
    )
    empty_comments = no_comments.model_copy(
        update={"id": "empty-comments", "metadata": {"proof": [], "comments": []}}
    )

    assert sidecar_module._card_execution(no_comments) is None  # pyright: ignore[reportPrivateUsage]
    assert sidecar_module._card_execution(empty_comments) is None  # pyright: ignore[reportPrivateUsage]


def test_sidecar_card_model_prefers_receipt_then_configured_fallbacks() -> None:
    receipt = QueueCard(
        id="receipt-model",
        title="Receipt model",
        status=QueueStatus.DONE,
        metadata={
            "comments": [
                {
                    "body": "MAKE_IT_SO_WORKER_EXECUTION:"
                    + json.dumps({"runtime": "codex", "requested_model": "gpt-5.3-codex-spark"})
                }
            ]
        },
    )
    configured = receipt.model_copy(update={"id": "configured", "agent_id": "coder", "metadata": {}})
    deterministic = receipt.model_copy(
        update={"id": "merge", "agent_id": "make-it-so-managed:deterministic-merge:1", "metadata": {}}
    )
    unknown = receipt.model_copy(update={"id": "unknown", "metadata": {}})

    assert sidecar_module._card_model(receipt, {}) == "gpt-5.3-codex-spark"  # pyright: ignore[reportPrivateUsage]
    assert sidecar_module._card_model(configured, {"coder": "codex/gpt-5.6-terra"}) == (  # pyright: ignore[reportPrivateUsage]
        "codex/gpt-5.6-terra"
    )
    assert sidecar_module._card_model(deterministic, {}) == "deterministic gate"  # pyright: ignore[reportPrivateUsage]
    assert sidecar_module._card_model(unknown, {}) is None  # pyright: ignore[reportPrivateUsage]


def test_sidecar_card_activity_handles_mixed_workboard_timestamps() -> None:
    empty = QueueCard(id="empty", title="Empty", status=QueueStatus.TODO)
    active = QueueCard(
        id="active",
        title="Active",
        status=QueueStatus.RUNNING,
        metadata={
            "automation": {"createdAt": 1000, "lastDispatchAt": "invalid"},
            "attempts": "not-a-list",
            "comments": [None, {"createdAt": 2000, "startedAt": "invalid", "endedAt": 3000}],
        },
    )

    assert sidecar_module._card_activity_timestamp(empty) == 0  # pyright: ignore[reportPrivateUsage]
    assert sidecar_module._card_activity_time(empty) is None  # pyright: ignore[reportPrivateUsage]
    assert sidecar_module._card_activity_timestamp(active) == 3000  # pyright: ignore[reportPrivateUsage]
    assert sidecar_module._card_activity_time(active) == "1970-01-01T00:00:03+00:00"  # pyright: ignore[reportPrivateUsage]


def test_sidecar_card_summary_uses_automation_then_comments_then_title() -> None:
    automation = QueueCard(
        id="automation",
        title="Title",
        status=QueueStatus.DONE,
        metadata={"automation": {"summary": "  automated  "}},
    )
    comment = automation.model_copy(
        update={
            "id": "comment",
            "metadata": {"automation": {"summary": 123}, "comments": [None, {"body": "  comment  "}]},
        }
    )
    title = automation.model_copy(
        update={"id": "title", "metadata": {"comments": [{"body": "   "}, None]}}
    )

    assert sidecar_module._card_summary(automation) == "automated"  # pyright: ignore[reportPrivateUsage]
    assert sidecar_module._card_summary(comment) == "comment"  # pyright: ignore[reportPrivateUsage]
    assert sidecar_module._card_summary(title) == "Title"  # pyright: ignore[reportPrivateUsage]


def test_sidecar_bootstraps_a_missing_configuration_and_provisions_the_crew(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config" / "config.yaml"
    calls: list[str] = []

    class FakeInstaller:
        def __init__(self, configured: OpenClawWorkboardConfig) -> None:
            self.configured = configured

        def agent_inventory(self) -> tuple[dict[str, str], ...]:
            return ({"id": "main", "model": "ollama/test", "workspace": "/workspace"},)

        def plan(self, workspace_root: Path) -> tuple[SimpleNamespace, ...]:
            calls.append(f"plan:{workspace_root}")
            return tuple(
                SimpleNamespace(
                    role=role,
                    agent_id=getattr(self.configured.workers, role),
                    model=getattr(self.configured.worker_models, role),
                    workspace=str(workspace_root / getattr(self.configured.workers, role)),
                    action="create",
                )
                for role in (
                    "captain",
                    "coder",
                    "reviewer",
                    "tester",
                    "ux_reviewer",
                    "final_reviewer",
                    "merger",
                    "verifier",
                )
            )

        def install(self, workspace_root: Path) -> tuple[SimpleNamespace, ...]:
            calls.append(f"install:{workspace_root}")
            return self.plan(workspace_root)

    monkeypatch.setattr(sidecar_module, "OpenClawRuntimeInstaller", FakeInstaller)
    server = SidecarServer(config_path)

    health = server.request("health")
    status = server.request("bootstrap.status", {"openclaw_executable": "openclaw"})
    assert health["status"] == "healthy"
    assert health["setup_required"] is True
    assert status["setup_required"] is True
    assert status["agents"][0]["id"] == "main"

    result = server.request(
        "bootstrap.apply",
        {
            "openclaw_executable": "openclaw",
            "workspace_root": str(tmp_path / "workers"),
            "reconcile_every": "10m",
            "review_every": "4h",
        },
    )

    persisted = load_config(config_path)
    assert result["configured"] is True
    assert result["automation_enabled"] is False
    assert config_path.is_file()
    assert persisted.repos == ()
    assert persisted.schedules.reconcile_every == "10m"
    assert persisted.orchestrators["openclaw-workers"].workers.captain == "github-captain"  # type: ignore[union-attr]
    assert any(value.startswith("install:") for value in calls)


def test_bootstrap_model_conflict_does_not_persist_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.yaml"

    class ConflictingInstaller:
        def __init__(self, _config: OpenClawWorkboardConfig) -> None:
            pass

        def plan(self, workspace_root: Path) -> tuple[SimpleNamespace, ...]:
            return (
                SimpleNamespace(
                    role="coder",
                    agent_id="github-coder",
                    model="codex/gpt-5.3-codex-spark",
                    workspace=str(workspace_root / "github-coder"),
                    action="model_mismatch",
                ),
            )

        def install(self, _workspace_root: Path) -> tuple[SimpleNamespace, ...]:
            raise AssertionError("conflicts must fail before installation")

    monkeypatch.setattr(sidecar_module, "OpenClawRuntimeInstaller", ConflictingInstaller)
    server = SidecarServer(config_path)

    with pytest.raises(SidecarError, match="different model"):
        server.request("bootstrap.apply", {"openclaw_executable": "openclaw"})

    assert not config_path.exists()
    assert server.request("health")["setup_required"] is True


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

    def build_adapter(_config: object) -> Adapter:
        return Adapter()

    monkeypatch.setattr(sidecar_module, "build_work_queue_adapter", build_adapter)
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
            source_url="https://github.com/mln330/make-it-so/pull/42",
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
                        "url": "https://github.com/mln330/make-it-so/pull/42",
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
                "external_id": "agent:github-merge:make-it-so:worker:merge-done:managed:1",
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

    def build_adapter(_config: object) -> Adapter:
        return Adapter()

    monkeypatch.setattr(sidecar_module, "build_work_queue_adapter", build_adapter)
    monkeypatch.setattr(sidecar_module, "sync_openclaw_sessions", sync)
    server = SidecarServer(config_path, github=cast(GitHubProvider, Github()))

    result = server.request("portfolio.status")["repos"][0]

    assert result["tokens"]["accounted_tokens"] == 27
    assert result["workboard_status"]["usage_sync"]["status"] == "ok"
    assert result["workboard_status"]["loop_count"] == 0
    assert result["workboard_status"]["pr_count"] == 1
    assert result["workboard_status"]["pr_numbers"] == [42]
    assert result["workboard_status"]["pr_urls"] == ["https://github.com/mln330/make-it-so/pull/42"]
    assert result["github_status"]["open_prs"] == 1
    assert result["github_status"]["checks"] == {"recent": 1, "failed": 0, "pending": 0, "passed": 1}


def test_sidecar_reports_health_portfolio_and_schedule_contract(tmp_path: Path) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)

    health = server.request("health")
    assert health["status"] == "healthy"
    assert health["version"] == __version__
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
        "make-it-so-reconcile",
        "make-it-so-course-review",
    ]
    assert [job["kind"] for job in schedule["jobs"]] == ["reconcile", "review"]
    assert [job["timeout_seconds"] for job in schedule["jobs"]] == [3900, 3900]
    assert all("--background" in job["command"] for job in schedule["jobs"])
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


def test_sidecar_collects_multi_repo_portfolio_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = repo_config(tmp_path)
    repos = tuple(
        base.model_copy(
            update={
                "full_name": f"example/project-{index}",
                "local_path": tmp_path / f"repo-{index}",
            }
        )
        for index in range(3)
    )
    config = app_config(tmp_path, base).model_copy(update={"repos": repos})
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)
    barrier = threading.Barrier(len(repos))

    def synchronized_status(repo: RepoConfig) -> dict[str, str]:
        barrier.wait(timeout=2)
        return {"full_name": repo.full_name}

    monkeypatch.setattr(server, "_repo_status", synchronized_status)

    result = server.request("portfolio.status")

    assert [row["full_name"] for row in result["repos"]] == [repo.full_name for repo in repos]


def test_sidecar_collects_github_and_workboard_status_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_config(tmp_path)
    config = app_config(tmp_path, repo)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)
    barrier = threading.Barrier(2)

    def workboard_status(_repo: RepoConfig) -> dict[str, str]:
        barrier.wait(timeout=2)
        return {"status": "ready"}

    def github_status(_repo: RepoConfig) -> dict[str, str]:
        barrier.wait(timeout=2)
        return {"status": "available"}

    monkeypatch.setattr(server, "_workboard_status", workboard_status)
    monkeypatch.setattr(server, "_github_status", github_status)

    result = server.request("portfolio.status")

    repo_result = result["repos"][0]
    assert repo_result["workboard_status"]["status"] == "ready"
    assert repo_result["github_status"]["status"] == "available"


def test_sidecar_cached_workboard_status_projects_the_durable_card_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_config(tmp_path).model_copy(
        update={"orchestrator": "openclaw", "orchestration_board": "cached-board"}
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
    config = app_config(tmp_path, repo_config(tmp_path)).model_copy(
        update={
            "repos": (repo,),
            "orchestrators": {
                "openclaw": OpenClawWorkboardConfig(
                    workers=workers,
                    require_live_completion_validation=False,
                )
            },
        }
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)
    card = _workboard_card("review", QueueStatus.DONE, 1).model_dump(mode="json")

    def cached_cards(_repo: str) -> list[dict[str, Any]]:
        return [card]

    monkeypatch.setattr(server.state, "orchestration_card_payloads", cached_cards)

    result = server._cached_workboard_status(repo)  # pyright: ignore[reportPrivateUsage]

    assert result is not None
    assert result["status"] == "blocked"
    assert result["review_cycles"] == 1
    assert result["review_status"] == "passed"
    assert result["usage_sync"]["status"] == "cached"


def test_sidecar_fast_portfolio_status_skips_expensive_usage_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_config(tmp_path)
    config = app_config(tmp_path, repo)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)
    sync_flags: list[bool] = []

    def repo_status(
        _repo: RepoConfig, *, sync_usage: bool = True, cached_workboard: bool = False
    ) -> dict[str, Any]:
        sync_flags.append(sync_usage)
        assert cached_workboard is True
        return {"full_name": _repo.full_name}

    monkeypatch.setattr(server, "_repo_status", repo_status)

    result = server.request("portfolio.status", {"fast": True})

    assert result["freshness"] == "github_workboard_live_usage_cached"
    assert sync_flags == [False]


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


def test_sidecar_registration_discovers_clone_and_plan_without_ui_paths(tmp_path: Path) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    clone = tmp_path.parent / "second"
    (clone / ".git").mkdir(parents=True)
    (clone / "docs").mkdir()
    (clone / "docs" / "IMPLEMENTATION_ROADMAP.md").write_text("# Roadmap\n", encoding="utf-8")
    server = SidecarServer(config_path)

    registered = server.request(
        "repo.register",
        {"full_name": "example/second", "notification_route": "project-room"},
    )

    assert registered["status"] == "registered"
    assert registered["discovery"]["local_clone"]["path"] == str(clone)
    assert registered["discovery"]["local_clone"]["cloned"] is True
    assert registered["discovery"]["planning_document"]["path"] == "docs/IMPLEMENTATION_ROADMAP.md"
    assert registered["discovery"]["planning_document"]["found"] is True
    assert registered["follow_up_required"] is True
    assert "Number One" in registered["follow_up_message"]
    assert "NUMBER ONE | INITIAL PLANNING" in registered["number_one_prompt"]
    assert "Ask exactly this one readiness question" in registered["number_one_prompt"]
    assert "Ask these questions in a concise numbered list" not in registered["number_one_prompt"]
    assert registered["number_one_session_key"] == "make-it-so:number-one:example-second"
    assert registered["course_created"] is True
    assert registered["course_key"] == "takeover"
    course = CourseStore(clone).load("takeover")
    assert course.kind == CourseKind.TAKEOVER
    assert course.status == CourseStatus.READINESS_REVIEW
    assert {item.key for item in course.readiness} >= {"goals", "permissions", "exit-criteria"}
    assert course.pending_readiness_key == "goals"
    assert course.pending_readiness_question
    assert "provisional, paused takeover course" in registered["number_one_prompt"]
    persisted = load_config(config_path).repo("example/second")
    assert persisted.local_path == clone
    assert persisted.planning_doc == "docs/IMPLEMENTATION_ROADMAP.md"
    assert persisted.notification.route == "project-room"


def test_sidecar_registration_options_list_verified_github_clones(tmp_path: Path) -> None:
    configured = repo_config(tmp_path / "configured")
    config = app_config(tmp_path, configured)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    clone = tmp_path / "local-project"
    subprocess.run(["git", "init", "-b", "main", str(clone)], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(clone), "remote", "add", "origin", "git@github.com:example/local-project.git"],
        check=True,
        capture_output=True,
        text=True,
    )
    (tmp_path / "not-a-repository").mkdir()

    result = SidecarServer(config_path).request("registration.options")

    assert result["warnings"] == []
    assert result["local_clones"] == [
        {
            "full_name": "example/local-project",
            "local_path": str(clone),
            "branch": "main",
            "dirty": False,
            "registered": False,
        }
    ]


def test_sidecar_inspects_an_explicit_verified_local_clone(tmp_path: Path) -> None:
    configured = repo_config(tmp_path / "configured")
    config = app_config(tmp_path, configured)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    clone = tmp_path / "selected-project"
    subprocess.run(["git", "init", "-b", "main", str(clone)], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(clone), "remote", "add", "origin", "https://github.com/example/selected-project.git"],
        check=True,
        capture_output=True,
        text=True,
    )
    (clone / "docs").mkdir()
    (clone / "docs" / "IMPLEMENTATION_PLAN.md").write_text("# Plan\n", encoding="utf-8")
    server = SidecarServer(config_path)

    inspected = server.request(
        "repo.inspect",
        {"full_name": "example/selected-project", "local_path": str(clone)},
    )

    assert inspected["discovery"]["local_clone"]["source"] == "explicit"
    assert inspected["discovery"]["local_clone"]["remote_matches"] is True
    assert inspected["discovery"]["planning_document"]["path"] == "docs/IMPLEMENTATION_PLAN.md"

    with pytest.raises(SidecarError, match="does not match"):
        server.request("repo.inspect", {"full_name": "example/other", "local_path": str(clone)})


def test_sidecar_persists_exact_pending_question_and_appends_follow_up_answers(tmp_path: Path) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)
    course = ready_course().model_copy(
        update={
            "status": CourseStatus.READINESS_REVIEW,
            "readiness_review": None,
            "pending_readiness_key": "success",
            "pending_readiness_question": "What observable success is required?",
        }
    )
    CourseStore(config.repos[0].local_path).save(course)
    params = {"full_name": "example/project", "course_key": "feature-search"}

    first = server.request(
        "course.requirement",
        {
            **params,
            "requirement_key": "success",
            "status": "answered",
            "answer": "Search returns ranked results.",
            "evidence": ["discord-owner-answer"],
            "append_answer": True,
        },
    )
    assert first["course"]["pending_readiness_key"] is None

    server.request(
        "course.pending_question",
        {
            **params,
            "requirement_key": "success",
            "question": "What response-time target should the search meet?",
        },
    )
    second = server.request(
        "course.requirement",
        {
            **params,
            "requirement_key": "success",
            "status": "answered",
            "answer": "The p95 response time must remain under 500 ms.",
            "evidence": ["discord-owner-answer"],
            "append_answer": True,
        },
    )

    requirement = next(item for item in second["course"]["readiness"] if item["key"] == "success")
    assert "Search returns ranked results." in requirement["answer"]
    assert "Follow-up answer:" in requirement["answer"]
    assert "under 500 ms" in requirement["answer"]
    reloaded = CourseStore(config.repos[0].local_path).load("feature-search")
    assert reloaded.pending_readiness_key is None


def test_sidecar_registration_defaults_to_configured_openclaw_workboard(
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
        update={"orchestrators": {"openclaw-workers": OpenClawWorkboardConfig(workers=workers)}}
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)

    registered = server.request(
        "repo.register",
        {"full_name": "example/openclaw-registration", "notification_route": "notifications"},
    )

    assert registered["status"] == "registered"
    persisted = load_config(config_path).repo("example/openclaw-registration")
    assert persisted.orchestrator == "openclaw-workers"
    assert persisted.orchestration_board is None


def test_sidecar_derives_default_workboard_for_dashboard_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_config(tmp_path).model_copy(update={"orchestrator": "openclaw"})
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
    config = app_config(tmp_path, repo_config(tmp_path)).model_copy(
        update={
            "repos": (repo,),
            "orchestrators": {
                "openclaw": OpenClawWorkboardConfig(
                    workers=workers,
                    require_live_completion_validation=False,
                )
            }
        }
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)
    observed: list[str] = []

    class EmptyAdapter:
        def list_cards(self, board_id: str) -> list[Any]:
            observed.append(board_id)
            return []

    monkeypatch.setattr("make_it_so.sidecar.build_work_queue_adapter", lambda _config: EmptyAdapter())
    status = server._workboard_status(repo, sync_usage=False)  # pyright: ignore[reportPrivateUsage]
    assert status is not None
    assert status["board"] == "make-it-so-example-project"
    assert observed == ["make-it-so-example-project"]


def test_sidecar_inspection_is_read_only_and_registration_persists_onboarding_choices(tmp_path: Path) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    clone = tmp_path.parent / "onboarding-second"
    (clone / ".git").mkdir(parents=True)
    (clone / "README.md").write_text("# Example\n", encoding="utf-8")
    server = SidecarServer(config_path)

    inspected = server.request("repo.inspect", {"full_name": "example/onboarding-second"})

    assert inspected["status"] == "inspected"
    assert inspected["mutation_started"] is False
    assert inspected["discovery"]["local_clone"]["cloned"] is True
    assert len(load_config(config_path).repos) == 1

    registered = server.request(
        "repo.register",
        {
            "full_name": "example/onboarding-second",
            "notification_route": "project-room",
            "phase": "feature",
            "goal": "Make search easier for customers.",
            "clone_allowed": True,
            "operation_mode": "autonomous",
            "completion_policy": "auto_merge",
            "allow_autonomous_merge": True,
            "checkpoint_policy": "updates_only",
            "milestone_approval": "none",
            "detected_surface": "web_ui",
            "surfaces": ["web_ui"],
            "uat_required": True,
            "screenshots_required": True,
            "deployment_required": False,
            "intelligence_level": "balanced",
        },
    )

    persisted = load_config(config_path).repo("example/onboarding-second")
    assert registered["repo"]["onboarding"]["phase"] == "feature"
    assert persisted.onboarding.goal == "Make search easier for customers."
    assert persisted.onboarding.checkpoint_policy == "updates_only"
    assert persisted.onboarding.intelligence_level == "balanced"
    assert "Make search easier for customers." in registered["number_one_prompt"]
    assert "course type selected in the dashboard: feature" in registered["number_one_prompt"]


def test_sidecar_registration_persists_runtime_discord_delivery_settings(tmp_path: Path) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)

    registered = server.request(
        "repo.register",
        {
            "full_name": "example/second",
            "notification_route": "channel:111111111111111111",
            "notification_kind": "openclaw_discord",
            "notification_executable": "openclaw",
        },
    )

    assert registered["notification_route"] == "channel:111111111111111111"
    persisted = load_config(config_path).repo("example/second")
    assert persisted.notification.kind == "openclaw_discord"
    assert persisted.notification.executable == "openclaw"
    assert persisted.notification.route == "channel:111111111111111111"


def test_sidecar_registration_reinitializes_existing_repo_without_active_course(tmp_path: Path) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)
    full_name = f"example/{tmp_path.name}"

    first = server.request(
        "repo.register",
        {
            "full_name": full_name,
            "notification_route": "notifications",
            "phase": "takeover",
            "goal": "Understand the current repository before implementation begins.",
        },
    )
    course_path = tmp_path.parent / tmp_path.name / ".make-it-so" / "courses" / f"{first['course_key']}.yaml"
    course_path.unlink()

    second = server.request(
        "repo.register",
        {
            "full_name": full_name,
            "notification_route": "project-room",
            "phase": "takeover",
            "goal": "Restart the course from a clean readiness review.",
            "operation_mode": "autonomous",
            "completion_policy": "auto_merge",
            "allow_autonomous_merge": True,
        },
    )

    assert second["status"] == "registered"
    assert second["course_created"] is True
    assert load_config(config_path).repo(full_name).notification.route == "project-room"


def test_sidecar_exposes_durable_discord_number_one_bindings(tmp_path: Path) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)

    registered = server.request(
        "repo.register",
        {
            "full_name": "example/second",
            "notification_route": "channel:123",
            "notification_kind": "openclaw_discord",
            "notification_executable": "openclaw",
        },
    )

    bindings = server.request("discord.planning_bindings")["bindings"]

    assert registered["number_one_session_key"] == "make-it-so:number-one:example-second"
    assert bindings == [
        {
            "repository": "example/second",
            "route": "channel:123",
            "session_key": "make-it-so:number-one:example-second",
        }
    ]


def test_sidecar_registration_normalizes_github_url(tmp_path: Path) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)

    registered = server.request(
        "repo.register",
        {"full_name": "https://github.com/example/second/", "notification_route": "notifications"},
    )

    assert registered["status"] == "registered"
    assert registered["repo"]["full_name"] == "example/second"
    assert load_config(config_path).repo("example/second").full_name == "example/second"


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
    assert (
        load_config(config_path).harness_model_overrides["test"].profiles["coder"].primary.model
        == "runtime-coder"
    )

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
    assert (local_path / ".make-it-so" / "courses" / "first-course.yaml").is_file()

    engaged = server.request(
        "course.approve",
        {"full_name": "example/new-project", "course_key": "first-course", "approved_by": "owner"},
    )

    assert engaged["status"] == "engaged"
    assert engaged["provisioning"]["created"] is True
    assert provider.calls == [("example/new-project", "first-course")]
    assert (local_path / "README.md").is_file()
    assert (local_path / "docs" / "IMPLEMENTATION_PLAN.md").is_file()
    assert (local_path / ".make-it-so" / "project.yaml").is_file()
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
        update={"orchestrators": {"openclaw-workers": OpenClawWorkboardConfig(workers=workers)}}
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
        assert kwargs["timeout"] == 3900
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "review complete", "")

    monkeypatch.setattr("make_it_so.sidecar.subprocess.run", fake_run)
    result = server.request("run.once", {"kind": "review"})

    assert result["status"] == "completed"
    assert result["model_invocations"] is None
    assert calls[0][-2:] == ["--live", "--continue-run"]
    assert result["execution"][0]["output"] == "review complete"


def test_sidecar_background_entrypoint_detaches_long_running_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    class Child:
        pid = 4242

    def fake_popen(command: list[str], **kwargs: Any) -> Child:
        calls.append((command, kwargs))
        return Child()

    monkeypatch.setattr(sidecar_module.subprocess, "Popen", fake_popen)
    result = server._spawn_background_once("reconcile")  # pyright: ignore[reportPrivateUsage]

    assert result["status"] == "started"
    assert result["pid"] == 4242
    assert calls[0][0][-4:] == ["--once", "reconcile", "--config", str(config_path)]
    assert calls[0][1]["start_new_session"] is True
    assert Path(str(result["log"])).parent.name == "run-logs"


def test_sidecar_run_start_uses_background_review_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)

    class Child:
        pid = 5252

    calls: list[list[str]] = []

    def fake_popen(command: list[str], **kwargs: Any) -> Child:
        del kwargs
        calls.append(command)
        return Child()

    monkeypatch.setattr(sidecar_module.subprocess, "Popen", fake_popen)
    result = server.request("run.start", {"kind": "review", "force_replan": True})

    assert result["status"] == "started"
    assert result["kind"] == "review"
    assert result["pid"] == 5252
    assert calls[0][-5:] == ["--once", "review", "--config", str(config_path), "--force-replan"]


def test_sidecar_reconcile_does_not_skip_board_free_repositories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    server = SidecarServer(config_path)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] == 3900
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "reconcile complete", "")

    monkeypatch.setattr("make_it_so.sidecar.subprocess.run", fake_run)
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

    monkeypatch.setattr("make_it_so.sidecar.subprocess.run", fake_run)
    result = server.request(
        "course.readiness_review",
        {"full_name": "example/project", "course_key": "feature-search", "harness": "test"},
    )

    assert result["status"] == "awaiting_approval"
    assert commands[0][0:3] == [sys.executable, "-m", "make_it_so.cli"]
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
    assert (
        stage_update["course"]["model_profiles"]["stage:implementation"]["primary"]["model"]
        == "codex/stage-coder"
    )

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
        ("repo.register", {}, "requires full_name"),
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

    reinitialized = server.request(
        "repo.register", {"full_name": "example/project", "local_path": str(tmp_path)}
    )
    assert reinitialized["status"] == "registered"
    assert reinitialized["course_created"] is True
    assert len(load_config(tmp_path / "config.yaml").repos) == 1

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
        server.request("course.requirement", {"full_name": "example/project", "course_key": "feature-search"})
    with pytest.raises(SidecarError, match="requires checkpoint_key and status"):
        server.request("course.checkpoint", {"full_name": "example/project", "course_key": "feature-search"})
    with pytest.raises(SidecarError):
        server.request(
            "course.requirement",
            {
                "full_name": "example/project",
                "course_key": "feature-search",
                "requirement_key": "success",
                "status": "invalid",
            },
        )
    with pytest.raises(SidecarError):
        server.request(
            "course.checkpoint",
            {
                "full_name": "example/project",
                "course_key": "feature-search",
                "checkpoint_key": "ui-demo",
                "status": "invalid",
            },
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


def test_sidecar_reports_dirty_git_and_one_shot_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    server = _sidecar(tmp_path, repo=repo_config(repo_path))

    def dirty_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(args, 0, " M README.md\n", "")

    monkeypatch.setattr("make_it_so.sidecar.subprocess.run", dirty_run)
    assert server.request("repos.list")["repos"][0]["dirty"] is True

    def unavailable_git(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise OSError("git unavailable")

    monkeypatch.setattr("make_it_so.sidecar.subprocess.run", unavailable_git)
    assert server.request("repos.list")["repos"][0]["dirty"] is True

    def fail_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise subprocess.TimeoutExpired("review", 1)

    monkeypatch.setattr("make_it_so.sidecar.subprocess.run", fail_run)
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
        [sys.executable, "-m", "make_it_so.sidecar", "--config", str(config_path)],
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
        ["make-it-so-sidecar", "--config", str(tmp_path / "config.yaml"), "--once", "review"],
    )

    assert sidecar_module.main() == 2
    assert '"status": "degraded"' in capsys.readouterr().out
