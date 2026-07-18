"""Small JSON-RPC sidecar used by the OpenClaw plugin.

The sidecar deliberately keeps the plugin boundary boring: one request per line,
one response per line, no transcript or credential handling, and all durable
project state remains in the Python core's existing stores.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import yaml

from captains_chair import SIDECAR_PROTOCOL_VERSION, __version__
from captains_chair.adapters import InteractionAdapter, NativeInteractionAdapter
from captains_chair.config import load_config
from captains_chair.courses import (
    CourseError,
    CourseStore,
    approve_course,
    eligible_work_packages,
    pause_course,
    readiness_report,
    resume_course,
)
from captains_chair.github import GhGitHubProvider, GitHubProvider
from captains_chair.models import (
    AppConfig,
    ApplicationSurface,
    CheckpointStatus,
    CompletionPolicy,
    Course,
    ModelPolicy,
    ModelProfile,
    NotificationConfig,
    OpenClawWorkboardConfig,
    OperationMode,
    ProjectManifest,
    RepoConfig,
    RepositoryProvisioningConfig,
    RequirementStatus,
    ScheduleConfig,
    UsageConfig,
)
from captains_chair.openclaw_usage import sync_openclaw_sessions
from captains_chair.orchestration import QueueCard, QueueStatus
from captains_chair.runtime import build_work_queue_adapter
from captains_chair.state import StateStore
from captains_chair.usage import build_usage_report


class SidecarError(RuntimeError):
    """A request failed with an operator-actionable error."""


_WORKBOARD_ACTIVE_STATUSES = frozenset(
    {
        QueueStatus.TODO,
        QueueStatus.READY,
        QueueStatus.RUNNING,
        QueueStatus.REVIEW,
    }
)


def _workflow_label(card: QueueCard) -> str | None:
    return next((label for label in card.labels if label.startswith("workflow:")), None)


def _workflow_stage(card: QueueCard) -> str | None:
    return next((label.split(":", 1)[1] for label in card.labels if label.startswith("stage:")), None)


def _card_activity_timestamp(card: QueueCard) -> int:
    """Return the newest Workboard timestamp available in normalized metadata."""
    timestamps: list[int] = []
    metadata = card.metadata
    automation = metadata.get("automation")
    if isinstance(automation, dict):
        automation = cast(dict[str, Any], automation)
        for key in ("createdAt", "lastDispatchAt"):
            value = automation.get(key)
            if isinstance(value, (int, float)):
                timestamps.append(int(value))
    for key in ("attempts", "comments", "notifications", "proof", "workerLogs"):
        values = metadata.get(key)
        if not isinstance(values, list):
            continue
        for raw_item in cast(list[object], values):
            if not isinstance(raw_item, dict):
                continue
            item = cast(dict[str, Any], raw_item)
            for timestamp_key in ("createdAt", "startedAt", "endedAt"):
                value = item.get(timestamp_key)
                if isinstance(value, (int, float)):
                    timestamps.append(int(value))
    return max(timestamps, default=0)


def _card_activity_time(card: QueueCard) -> str | None:
    timestamp = _card_activity_timestamp(card)
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp / 1000, UTC).isoformat()


def _card_summary(card: QueueCard) -> str:
    automation = card.metadata.get("automation")
    if isinstance(automation, dict):
        value = cast(dict[str, Any], automation).get("summary")
        if isinstance(value, str) and value.strip():
            return value.strip()
    comments = card.metadata.get("comments")
    if isinstance(comments, list):
        for raw_comment in reversed(cast(list[object], comments)):
            if isinstance(raw_comment, dict):
                body = cast(dict[str, Any], raw_comment).get("body")
                if isinstance(body, str) and body.strip():
                    return body.strip()
    return card.title


def _attempt_count(card: QueueCard) -> int:
    attempts = card.metadata.get("attempts")
    return len(cast(list[Any], attempts)) if isinstance(attempts, list) else 0


def _card_context_rows(cards: list[QueueCard]) -> list[dict[str, Any]]:
    return [
        {
            "id": card.id,
            "title": card.title,
            "status": card.status.value,
            "labels": list(card.labels),
            "agent_id": card.agent_id,
            "source_url": card.source_url,
            "metadata": card.metadata,
        }
        for card in cards
    ]


def _expected_worker_models(configured: OpenClawWorkboardConfig) -> dict[str, str]:
    roles = ("captain", "coder", "reviewer", "tester", "ux_reviewer", "final_reviewer", "merger", "verifier")
    return {
        str(getattr(configured.workers, role)): str(getattr(configured.worker_models, role))
        for role in roles
    }


def _summarize_workboard(
    cards: list[QueueCard],
    board_id: str,
    *,
    usage_sync: dict[str, Any] | None = None,
    repo_full_name: str | None = None,
) -> dict[str, Any]:
    """Project Workboard cards into a read-only dashboard status.

    Blocked retry artifacts are deliberately ignored for terminal detection. A
    workflow is complete only when its merge and post-merge stages are done and
    no active stage card remains.
    """
    workflows: dict[str, list[QueueCard]] = {}
    for card in cards:
        if card.metadata.get("archivedAt"):
            continue
        label = _workflow_label(card)
        if label is not None:
            workflows.setdefault(label, []).append(card)

    if not workflows:
        return {
            "status": "unknown",
            "board": board_id,
            "cards": 0,
            "counts": {},
        }

    latest_label, latest_cards = max(
        workflows.items(),
        key=lambda item: (
            max((_card_activity_timestamp(card) for card in item[1]), default=0),
            int(any(card.status in _WORKBOARD_ACTIVE_STATUSES for card in item[1])),
        ),
    )
    counts: dict[str, int] = {}
    for card in latest_cards:
        counts[card.status.value] = counts.get(card.status.value, 0) + 1
    active_cards = [card for card in latest_cards if card.status in _WORKBOARD_ACTIVE_STATUSES]
    done_stages = {
        stage
        for card in latest_cards
        if card.status == QueueStatus.DONE
        for stage in (_workflow_stage(card),)
        if stage is not None
    }
    terminal = not active_cards and {"merge", "post_merge"}.issubset(done_stages)
    status = "completed" if terminal else "in_progress" if active_cards else "blocked"
    stage_names = (
        "orchestration",
        "implementation",
        "repair",
        "review",
        "test",
        "ux_review",
        "final_review",
        "merge",
        "post_merge",
    )
    stage_rows: list[dict[str, Any]] = []
    for stage_name in stage_names:
        stage_cards = [card for card in latest_cards if _workflow_stage(card) == stage_name]
        if not stage_cards:
            continue
        stage_rows.append(
            {
                "stage": stage_name,
                "total": len(stage_cards),
                "done": sum(card.status == QueueStatus.DONE for card in stage_cards),
                "active": sum(card.status in _WORKBOARD_ACTIVE_STATUSES for card in stage_cards),
                "blocked": sum(card.status == QueueStatus.BLOCKED for card in stage_cards),
                "loops": sum(
                    stage_name == "repair"
                    or any(label.startswith("retry:") for label in card.labels)
                    or _attempt_count(card) > 1
                    for card in stage_cards
                ),
            }
        )
    timeline = sorted(
        (
            {
                "stage": _workflow_stage(card) or "unknown",
                "status": card.status.value,
                "title": card.title,
                "summary": _card_summary(card),
                "agent": card.agent_id,
                "pr_url": card.source_url if "/pull/" in str(card.source_url or "") else None,
                "updated_at": _card_activity_time(card),
                "loop": (
                    _workflow_stage(card) == "repair"
                    or any(label.startswith("retry:") for label in card.labels)
                    or _attempt_count(card) > 1
                ),
            }
            for card in latest_cards
        ),
        key=lambda item: str(item["updated_at"] or ""),
    )[-16:]
    loop_count = sum(int(bool(item["loop"])) for item in timeline)
    current_stage = next(
        (
            _workflow_stage(card) or "unknown"
            for card in sorted(latest_cards, key=_card_activity_timestamp, reverse=True)
            if card.status in _WORKBOARD_ACTIVE_STATUSES
        ),
        timeline[-1]["stage"] if timeline else None,
    )
    pr_numbers: set[int] = set()
    explicit_pr_numbers: set[int] = set()
    pr_urls_set: set[str] = set()
    for card in latest_cards:
        source_url = str(card.source_url or "")
        source_match = re.search(r"https?://[^\s]+/pull/(\d+)", source_url)
        if source_match:
            pr_urls_set.add(source_url)
            number = int(source_match.group(1))
            pr_numbers.add(number)
            explicit_pr_numbers.add(number)
        text = f"{card.title} {_card_summary(card)}"
        pr_numbers.update(int(value) for value in re.findall(r"\bPR\s*#?\s*(\d+)\b", text, re.IGNORECASE))
    pr_urls = sorted(
        pr_urls_set
        | (
            {
                f"https://github.com/{repo_full_name}/pull/{number}"
                for number in pr_numbers - explicit_pr_numbers
            }
            if repo_full_name
            else set()
        )
    )
    summary: dict[str, Any] = {
        "status": status,
        "board": board_id,
        "workflow": latest_label.removeprefix("workflow:"),
        "cards": len(latest_cards),
        "counts": counts,
        "active_cards": len(active_cards),
        "terminal_stages": sorted(done_stages & {"merge", "post_merge"}),
        "current_stage": current_stage,
        "stages": stage_rows,
        "timeline": timeline,
        "loop_count": loop_count,
        "pr_count": max(len(pr_numbers), len(pr_urls)),
        "pr_numbers": sorted(pr_numbers),
        "pr_urls": pr_urls,
    }
    if usage_sync is not None:
        summary["usage_sync"] = usage_sync
    if terminal:
        summary["message"] = "Workboard merge and post-merge verification completed."
    return summary


class SidecarServer:
    def __init__(
        self,
        config_path: Path,
        interaction: InteractionAdapter | None = None,
        github: GitHubProvider | None = None,
    ) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        self.state = StateStore(self.config.state_dir / "state.db")
        self.interaction = interaction or NativeInteractionAdapter()
        self.github = github or GhGitHubProvider()

    def _workboard_status(self, repo: RepoConfig) -> dict[str, Any] | None:
        """Read Workboard progress and reconcile worker telemetry for the dashboard."""
        if not repo.orchestrator or not repo.orchestration_board:
            return None
        configured = self.config.orchestrators.get(repo.orchestrator)
        if not isinstance(configured, OpenClawWorkboardConfig):
            return None
        try:
            adapter = build_work_queue_adapter(configured)
            cards = adapter.list_cards(repo.orchestration_board)
            self.state.sync_orchestration_cards(repo.full_name, _card_context_rows(cards))
            try:
                usage_sync = {
                    "status": "ok",
                    **sync_openclaw_sessions(
                        self.state,
                        repo=repo.full_name,
                        executable=configured.executable,
                        expected_models=_expected_worker_models(configured),
                        session_context=self.state.openclaw_session_context(repo.full_name),
                        session_limit=configured.session_limit,
                    ),
                }
            except Exception as exc:
                usage_sync = {"status": "unavailable", "error": str(exc)[:500]}
            return _summarize_workboard(
                cards,
                repo.orchestration_board,
                usage_sync=usage_sync,
                repo_full_name=repo.full_name,
            )
        except Exception as exc:
            return {
                "status": "unavailable",
                "board": repo.orchestration_board,
                "error": str(exc)[:500],
            }

    def _github_status(self, repo: RepoConfig) -> dict[str, Any]:
        """Collect the small GitHub slice needed for a portfolio card."""
        try:
            snapshot = self.github.snapshot(repo)
            runs = snapshot.workflow_runs
            failed_conclusions = {"failure", "cancelled", "timed_out", "action_required", "stale"}
            failed_runs = sum(
                str(run.get("conclusion") or "").lower() in failed_conclusions for run in runs
            )
            pending_runs = sum(str(run.get("status") or "").lower() != "completed" for run in runs)
            return {
                "status": "available",
                "open_prs": len(snapshot.pull_requests),
                "open_issues": len(snapshot.issues),
                "branches": len(snapshot.branches),
                "checks": {
                    "recent": len(runs),
                    "failed": failed_runs,
                    "pending": pending_runs,
                    "passed": max(0, len(runs) - failed_runs - pending_runs),
                },
                "prs": [
                    {
                        key: item.get(key)
                        for key in (
                            "number",
                            "title",
                            "url",
                            "headRefName",
                            "isDraft",
                            "mergeStateStatus",
                            "reviewDecision",
                            "updatedAt",
                        )
                    }
                    for item in snapshot.pull_requests[:8]
                ],
            }
        except Exception as exc:
            return {"status": "unavailable", "error": str(exc)[:500]}

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = params or {}
        if method == "health":
            return {
                "status": "healthy",
                "service": "captains-chair-sidecar",
                "version": __version__,
                "protocol_version": SIDECAR_PROTOCOL_VERSION,
                "config": str(self.config_path),
                "timestamp": datetime.now(UTC).isoformat(),
            }
        if method in {"portfolio.status", "repos.list"}:
            return {"repos": [self._repo_status(repo) for repo in self.config.repos]}
        if method == "repo.register":
            return self._register_repo(payload)
        if method == "repo.create":
            return self._create_greenfield_repo(payload)
        if method == "repo.update":
            return self._update_repo(payload)
        if method == "models.validate":
            return self._validate_model_routes(payload)
        if method == "models.config":
            return self._model_config_payload()
        if method == "models.update":
            return self._update_model_config(payload)
        if method == "usage.config":
            return self._usage_config_payload()
        if method == "usage.update":
            return self._update_usage_config(payload)
        if method == "courses.list":
            return self._list_courses()
        if method == "course.get":
            return self._course_status(payload)
        if method == "course.create":
            return self._create_course(payload)
        if method == "course.readiness":
            return self._course_readiness(payload)
        if method == "course.planning_session":
            return self._planning_session(payload)
        if method == "course.readiness_review":
            return self._review_course_readiness(payload)
        if method == "course.models":
            return self._update_course_models(payload)
        if method == "course.requirement":
            return self._resolve_requirement(payload)
        if method == "course.approve":
            return self._approve_course(payload)
        if method == "course.ready_work":
            return self._ready_work(payload)
        if method == "course.checkpoint":
            return self._resolve_checkpoint(payload)
        if method == "course.pause":
            return self._set_course_status(payload, pause_course)
        if method == "course.resume":
            return self._set_course_status(payload, resume_course)
        if method == "schedule.describe":
            return self._schedule_description()
        if method == "schedule.configure":
            return self._configure_schedules(payload)
        if method == "attention.ack":
            return self._acknowledge_attention(payload)
        if method == "run.once":
            return self._run_once(str(payload.get("kind") or "reconcile"))
        raise SidecarError(f"unknown sidecar method: {method}")

    def _repo_status(self, repo: RepoConfig) -> dict[str, Any]:
        workboard = self._workboard_status(repo)
        summary = self.state.usage_summary(repo=repo.full_name)
        usage = build_usage_report(summary, self.config.usage)
        github = self._github_status(repo)
        state = self.state.current_state(repo.full_name).value
        if workboard is not None and workboard.get("status") == "completed":
            state = "merged"
        events = [
            event.model_dump(mode="json")
            for event in self.state.recent_events(repo.full_name, limit=12)
        ]
        local_path = repo.local_path
        dirty = False
        if local_path.is_dir() and (local_path / ".git").exists():
            try:
                result = subprocess.run(
                    ["git", "-C", str(local_path), "status", "--porcelain"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                dirty = bool(result.stdout.strip())
            except (OSError, subprocess.TimeoutExpired):
                dirty = True
        return {
            "full_name": repo.full_name,
            "local_path": str(local_path),
            "exists": local_path.exists(),
            "dirty": dirty,
            "default_branch": repo.default_branch,
            "operation_mode": repo.operation_mode.value,
            "completion_policy": repo.completion_policy.value,
            "state": state,
            "state_source": "workboard" if workboard is not None else "state_store",
            "workboard_status": workboard,
            "github_status": github,
            "orchestrator": repo.orchestrator or "direct",
            "orchestration_board": repo.orchestration_board,
            "schedule_enabled": repo.schedule_enabled,
            "notification_route": repo.notification.route,
            "surfaces": sorted(surface.value for surface in repo.surfaces),
            "qa_profiles": [profile.model_dump(mode="json") for profile in repo.qa_profiles],
            "model_profiles": {
                key: profile.model_dump(mode="json") for key, profile in repo.model_profiles.items()
            },
            "tokens": usage["token_totals"],
            "usage_detail": {
                "model_totals": usage["model_totals"],
                "token_hotspots": usage["token_hotspots"][:5],
                "efficiency": usage["efficiency"],
                "failed_attempts": usage["failed_attempts"],
                "warnings": usage["warnings"][:5],
                "dimensions": self.state.usage_dimensions(repo.full_name)[:50],
            },
            "telemetry": usage["telemetry"],
            "warnings": usage["warnings"][:5],
            "active_work": self.state.active_work(repo.full_name),
            "events": events,
        }

    def _register_repo(self, payload: dict[str, Any]) -> dict[str, Any]:
        full_name = str(payload.get("full_name") or "").strip()
        local_path = str(payload.get("local_path") or "").strip()
        if not full_name or not local_path:
            raise SidecarError("repo.register requires full_name and local_path")
        if any(repo.full_name == full_name for repo in self.config.repos):
            raise SidecarError(f"repository is already registered: {full_name}")
        repo = RepoConfig(
            full_name=full_name,
            local_path=Path(local_path),
            default_branch=str(payload.get("default_branch") or "main"),
            planning_doc=str(payload.get("planning_doc") or "docs/IMPLEMENTATION_PLAN.md"),
            canonical_docs=tuple(str(item) for item in _list_value(payload.get("canonical_docs"))),
            checks=tuple(str(item) for item in _list_value(payload.get("checks"))),
            docs_checks=tuple(str(item) for item in _list_value(payload.get("docs_checks"))),
            surfaces=frozenset(ApplicationSurface(str(item)) for item in _list_value(payload.get("surfaces"))),
            operation_mode=OperationMode(str(payload.get("operation_mode") or "advisory")),
            completion_policy=CompletionPolicy(str(payload.get("completion_policy") or "owner_approval")),
            allow_autonomous_merge=bool(payload.get("allow_autonomous_merge", False)),
            notification=NotificationConfig(
                kind="stdout",
                route=str(payload.get("notification_route")) if payload.get("notification_route") else None,
            ),
        )
        self._write_config(self.config.model_copy(update={"repos": (*self.config.repos, repo)}))
        return {"repo": self._repo_status(repo), "status": "registered"}

    def _create_greenfield_repo(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Register a local course intent; remote creation waits for course approval."""
        full_name = str(payload.get("full_name") or "").strip()
        local_path_value = str(payload.get("local_path") or "").strip()
        raw_course = payload.get("course")
        if not full_name or not local_path_value or not isinstance(raw_course, dict):
            raise SidecarError("repo.create requires full_name, local_path, and a course object")
        if any(repo.full_name.lower() == full_name.lower() for repo in self.config.repos):
            raise SidecarError(f"repository is already registered: {full_name}")
        local_path = Path(local_path_value)
        if local_path.exists() and (not local_path.is_dir() or any(local_path.iterdir())):
            raise SidecarError("greenfield local_path must be missing or an empty directory")
        course_payload = {**cast(dict[str, Any], raw_course), "repository": full_name}
        try:
            course = Course.model_validate(course_payload)
        except ValueError as exc:
            raise SidecarError(str(exc)) from exc
        if course.kind.value != "greenfield":
            raise SidecarError("repo.create is only valid for a greenfield course")
        visibility = str(payload.get("visibility") or "private")
        if visibility not in {"private", "public"}:
            raise SidecarError("repo.create visibility must be private or public")
        configured_orchestrator = str(payload.get("orchestrator") or "").strip()
        default_orchestrator = (
            "openclaw-workers" if "openclaw-workers" in self.config.orchestrators else None
        )
        repo = RepoConfig(
            full_name=full_name,
            local_path=local_path,
            default_branch=str(payload.get("default_branch") or "main"),
            planning_doc=str(payload.get("planning_doc") or "docs/IMPLEMENTATION_PLAN.md"),
            canonical_docs=tuple(str(item) for item in _list_value(payload.get("canonical_docs"))),
            checks=tuple(str(item) for item in _list_value(payload.get("checks"))),
            docs_checks=tuple(str(item) for item in _list_value(payload.get("docs_checks"))),
            surfaces=frozenset(ApplicationSurface(str(item)) for item in _list_value(payload.get("surfaces"))),
            orchestrator=configured_orchestrator or default_orchestrator,
            orchestration_board=(
                str(payload.get("orchestration_board") or "").strip() or None
            ),
            require_project_manifest=True,
            provisioning=RepositoryProvisioningConfig(
                enabled=True,
                visibility=cast(Literal["private", "public"], visibility),
                description=str(payload.get("description") or course.title).strip(),
            ),
            notification=NotificationConfig(
                kind="stdout",
                route=str(payload.get("notification_route")) if payload.get("notification_route") else None,
            ),
        )
        local_path.mkdir(parents=True, exist_ok=True)
        CourseStore(local_path).save(course)
        self._write_config(self.config.model_copy(update={"repos": (*self.config.repos, repo)}))
        return {"status": "awaiting_course_approval", **self._course_payload(full_name, course)}

    def _update_repo(self, payload: dict[str, Any]) -> dict[str, Any]:
        full_name = str(payload.get("full_name") or "").strip()
        if not full_name:
            raise SidecarError("repo.update requires full_name")
        index = next((i for i, repo in enumerate(self.config.repos) if repo.full_name == full_name), None)
        if index is None:
            raise SidecarError(f"repository is not registered: {full_name}")
        current = self.config.repos[index]
        allowed = {
            key: value
            for key, value in payload.items()
            if key in {
                "local_path",
                "default_branch",
                "planning_doc",
                "operation_mode",
                "completion_policy",
                "allow_autonomous_merge",
                "orchestrator",
                "orchestration_board",
                "model_profiles",
                "surfaces",
                "qa_profiles",
                "schedule_enabled",
            }
        }
        if "notification_route" in payload:
            allowed["notification"] = current.notification.model_copy(
                update={"route": str(payload.get("notification_route") or "") or None}
            )
        updated = RepoConfig.model_validate({**current.model_dump(mode="python"), **allowed})
        repos = list(self.config.repos)
        repos[index] = updated
        self._write_config(self.config.model_copy(update={"repos": tuple(repos)}))
        return {"repo": self._repo_status(updated), "status": "updated"}

    def _validate_model_routes(self, payload: dict[str, Any]) -> dict[str, Any]:
        full_name = str(payload.get("full_name") or "").strip()
        repo: RepoConfig | None = None
        if full_name:
            try:
                repo = self.config.repo(full_name)
            except KeyError as exc:
                raise SidecarError(f"repository is not registered: {full_name}") from exc
        raw_profiles = payload.get("model_profiles")
        if isinstance(raw_profiles, dict):
            profiles_value = cast(dict[str, Any], raw_profiles)
        elif repo is not None:
            profiles_value = cast(dict[str, Any], repo.model_profiles)
        else:
            raise SidecarError("models.validate requires full_name or model_profiles")
        errors: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        valid_roles: list[str] = []
        for role, raw_profile in profiles_value.items():
            if not isinstance(raw_profile, dict):
                errors.append({"role": str(role), "error": "route must be an object keyed by role"})
                continue
            try:
                profile = ModelProfile.model_validate(raw_profile)
            except ValueError as exc:
                errors.append({"role": role, "error": str(exc)[:500]})
                continue
            valid_roles.append(role)
            if profile.primary.capability is None:
                warnings.append(
                    {
                        "role": role,
                        "warning": "runtime capability is unverified; run a harness route test before autonomous use",
                    }
                )
        return {
            "repository": repo.full_name if repo is not None else None,
            "status": "invalid" if errors else "unverified" if warnings else "valid",
            "can_save": not errors,
            "valid_roles": valid_roles,
            "errors": errors,
            "warnings": warnings,
        }

    @staticmethod
    def _parse_model_profiles(raw_profiles: Any) -> dict[str, ModelProfile]:
        if not isinstance(raw_profiles, dict):
            raise SidecarError("model configuration requires a model_profiles object")
        try:
            profiles_value = cast(dict[str, Any], raw_profiles)
            return {
                str(role): ModelProfile.model_validate(raw_profile)
                for role, raw_profile in profiles_value.items()
            }
        except (TypeError, ValueError) as exc:
            raise SidecarError(f"invalid model profile: {exc}") from exc

    @staticmethod
    def _policy_profiles(policy: ModelPolicy) -> dict[str, dict[str, Any]]:
        role_names = {
            "baseline",
            "planner",
            "coder",
            "reviewer",
            "comment_adjudicator",
            "tester",
            "final_reviewer",
            "ux_reviewer",
            *policy.profiles.keys(),
        }
        profiles: dict[str, dict[str, Any]] = {}
        for role in sorted(role_names):
            selected = policy.profiles.get(role)
            if selected is None:
                selected = getattr(policy, role, None)
            if isinstance(selected, ModelProfile):
                profiles[role] = selected.model_dump(mode="json")
        return profiles

    def _model_config_payload(self) -> dict[str, Any]:
        runtimes = set(self.config.harness_model_overrides) | set(self.config.harnesses)
        return {
            "global_profiles": self._policy_profiles(self.config.models),
            "runtime_profiles": {
                runtime: self._policy_profiles(self.config.harness_model_overrides[runtime])
                for runtime in sorted(self.config.harness_model_overrides)
            },
            "runtimes": sorted(runtimes),
            "usage": self.config.usage.model_dump(mode="json"),
        }

    def _usage_config_payload(self) -> dict[str, Any]:
        return {"usage": self.config.usage.model_dump(mode="json")}

    def _update_usage_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            key: payload[key]
            for key in {
                "daily_token_limit",
                "model_daily_token_limits",
                "block_on_unknown",
                "allow_incomplete_telemetry",
                "retention_days",
            }
            if key in payload
        }
        try:
            usage = UsageConfig.model_validate(
                {**self.config.usage.model_dump(mode="python"), **allowed}
            )
        except ValueError as exc:
            raise SidecarError(f"invalid usage configuration: {exc}") from exc
        self._write_config(self.config.model_copy(update={"usage": usage}))
        return {"status": "updated", **self._usage_config_payload()}

    def _update_model_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        scope = str(payload.get("scope") or "global").strip().lower()
        profiles = self._parse_model_profiles(payload.get("model_profiles"))
        if scope == "global":
            updated_global = self.config.models.model_copy(update={"profiles": profiles})
            self._write_config(self.config.model_copy(update={"models": updated_global}))
        elif scope == "runtime":
            runtime = str(payload.get("runtime") or payload.get("harness") or "").strip()
            if not runtime:
                raise SidecarError("runtime model configuration requires runtime")
            if runtime not in self.config.harnesses and runtime not in self.config.harness_model_overrides:
                raise SidecarError(f"unknown model runtime: {runtime}")
            current = self.config.harness_model_overrides.get(runtime, self.config.models)
            updated_runtime = current.model_copy(update={"profiles": profiles})
            overrides = dict(self.config.harness_model_overrides)
            overrides[runtime] = updated_runtime
            self._write_config(self.config.model_copy(update={"harness_model_overrides": overrides}))
        else:
            raise SidecarError("models.update scope must be global or runtime")
        return {"status": "updated", **self._model_config_payload()}

    def _list_courses(self) -> dict[str, Any]:
        courses: list[dict[str, Any]] = []
        for repo in self.config.repos:
            for course in CourseStore(repo.local_path).list():
                courses.append(self._course_payload(repo.full_name, course))
        return {"courses": courses}

    def _course_context(self, payload: dict[str, Any]) -> tuple[RepoConfig, CourseStore, Course]:
        full_name = str(payload.get("full_name") or payload.get("repository") or "").strip()
        course_key = str(payload.get("course_key") or payload.get("key") or "").strip()
        if not full_name or not course_key:
            raise SidecarError("course operation requires full_name and course_key")
        try:
            repo = self.config.repo(full_name)
            store = CourseStore(repo.local_path)
            return repo, store, store.load(course_key)
        except (KeyError, CourseError) as exc:
            raise SidecarError(str(exc)) from exc

    def _course_payload(self, full_name: str, course: Course) -> dict[str, Any]:
        report = readiness_report(course)
        return {
            "repository": full_name,
            "course": course.model_dump(mode="json"),
            "readiness": report.model_dump(mode="json"),
        }

    def _course_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo, _store, course = self._course_context(payload)
        return self._course_payload(repo.full_name, course)

    def _create_course(self, payload: dict[str, Any]) -> dict[str, Any]:
        full_name = str(payload.get("full_name") or payload.get("repository") or "").strip()
        raw_course = payload.get("course")
        if not full_name or not isinstance(raw_course, dict):
            raise SidecarError("course.create requires full_name and a course object")
        try:
            repo = self.config.repo(full_name)
            course_payload: dict[str, Any] = {**cast(dict[str, Any], raw_course), "repository": full_name}
            course = Course.model_validate(course_payload)
            CourseStore(repo.local_path).save(course)
        except (KeyError, CourseError, ValueError) as exc:
            raise SidecarError(str(exc)) from exc
        return {"status": "created", **self._course_payload(full_name, course)}

    def _course_readiness(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo, _store, course = self._course_context(payload)
        return {"repository": repo.full_name, "readiness": readiness_report(course).model_dump(mode="json")}

    def _planning_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo, _store, course = self._course_context(payload)
        return {"repository": repo.full_name, **self.interaction.planning_session(course)}

    def _review_course_readiness(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo, _store, course = self._course_context(payload)
        harness = str(payload.get("harness") or "").strip()
        if not harness:
            raise SidecarError("course.readiness_review requires a harness")
        command = [
            sys.executable,
            "-m",
            "captains_chair.cli",
            "--config",
            str(self.config_path),
            "readiness-review",
            "--repo",
            repo.full_name,
            "--course-key",
            course.key,
            "--harness",
            harness,
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=14400,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[-2000:]
            raise SidecarError(f"readiness review failed: {detail}")
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise SidecarError("readiness review returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise SidecarError("readiness review returned an invalid response")
        return cast(dict[str, Any], value)

    def _update_course_models(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo, store, course = self._course_context(payload)
        layer = str(payload.get("layer") or "course").strip().lower()
        if layer == "package":
            layer = "work_package"
        course_data = course.model_dump(mode="python")
        package_key: str | None = None
        if layer == "stage":
            stage_name = str(payload.get("stage_name") or "").strip()
            stage_scope = str(payload.get("stage_scope") or "course").strip().lower()
            if not stage_name:
                raise SidecarError("course.models stage layer requires stage_name")
            if stage_scope not in {"course", "work_package"}:
                raise SidecarError("course.models stage_scope must be course or work_package")
            try:
                stage_profile = ModelProfile.model_validate(payload.get("stage_profile"))
            except (TypeError, ValueError) as exc:
                raise SidecarError(f"invalid stage model profile: {exc}") from exc
            stage_key = f"stage:{stage_name}"
            if stage_scope == "course":
                profiles = dict(course.model_profiles)
                profiles[stage_key] = stage_profile
                updated = Course.model_validate({**course_data, "model_profiles": profiles})
            else:
                package_key = str(payload.get("work_package_key") or payload.get("package_key") or "").strip()
                if not package_key:
                    raise SidecarError("course.models stage work-package scope requires work_package_key")
                packages = list(course.work_packages)
                for index, package in enumerate(packages):
                    if package.key == package_key:
                        profiles = dict(package.model_profiles)
                        profiles[stage_key] = stage_profile
                        packages[index] = package.model_copy(update={"model_profiles": profiles})
                        break
                else:
                    raise SidecarError(f"work package is not defined by course: {package_key}")
                updated = Course.model_validate({**course_data, "work_packages": tuple(packages)})
            store.save(updated)
            return {
                "status": "updated",
                "layer": "stage",
                "stage_name": stage_name,
                "stage_scope": stage_scope,
                "work_package_key": package_key,
                **self._course_payload(repo.full_name, updated),
            }

        if layer not in {"course", "work_package"}:
            raise SidecarError("course.models layer must be course, work_package, or stage")
        profiles = self._parse_model_profiles(payload.get("model_profiles"))
        if layer == "course":
            updated = Course.model_validate({**course_data, "model_profiles": profiles})
        else:
            package_key = str(payload.get("work_package_key") or payload.get("package_key") or "").strip()
            if not package_key:
                raise SidecarError("course.models work_package layer requires work_package_key")
            packages = list(course.work_packages)
            for index, package in enumerate(packages):
                if package.key == package_key:
                    packages[index] = package.model_copy(update={"model_profiles": profiles})
                    break
            else:
                raise SidecarError(f"work package is not defined by course: {package_key}")
            updated = Course.model_validate({**course_data, "work_packages": tuple(packages)})
        store.save(updated)
        return {
            "status": "updated",
            "layer": layer,
            "work_package_key": package_key,
            **self._course_payload(repo.full_name, updated),
        }

    def _resolve_requirement(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo, store, course = self._course_context(payload)
        requirement_key = str(payload.get("requirement_key") or "").strip()
        status_value = str(payload.get("status") or "").strip()
        if not requirement_key or not status_value:
            raise SidecarError("course.requirement requires requirement_key and status")
        try:
            status = RequirementStatus(status_value)
            evidence_value = payload.get("evidence")
            evidence_items = cast(list[Any], evidence_value) if isinstance(evidence_value, list) else []
            updated = self.interaction.resolve_requirement(
                course,
                requirement_key,
                status,
                str(payload.get("answer")) if payload.get("answer") is not None else None,
                tuple(str(item) for item in evidence_items),
                verified_by=str(payload.get("verified_by") or "") or None,
                verified_at=(
                    datetime.fromisoformat(str(payload["verified_at"]))
                    if payload.get("verified_at")
                    else None
                ),
                verification_model=str(payload.get("verification_model") or "") or None,
            )
            store.save(updated)
        except (CourseError, ValueError) as exc:
            raise SidecarError(str(exc)) from exc
        return {"status": status.value, **self._course_payload(repo.full_name, updated)}

    def _approve_course(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo, store, course = self._course_context(payload)
        approved_by = str(payload.get("approved_by") or "").strip()
        try:
            approved_at_value = payload.get("approved_at")
            approved_at = datetime.fromisoformat(str(approved_at_value)) if approved_at_value else None
            approved = approve_course(course, approved_by, approved_at)
            if course.kind.value == "greenfield":
                if not repo.provisioning.enabled:
                    raise SidecarError(
                        "greenfield course approval requires repository provisioning to be enabled"
                    )
                self._seed_greenfield(repo, approved, store)
                provisioning = self.github.provision_greenfield(repo, approved)
                store.save(approved)
            else:
                store.save(approved)
                provisioning = None
        except (CourseError, ValueError) as exc:
            raise SidecarError(str(exc)) from exc
        except Exception as exc:
            if course.kind.value == "greenfield":
                store.save(course)
            raise SidecarError(f"greenfield repository provisioning failed: {str(exc)[:2000]}") from exc
        response: dict[str, Any] = {"status": "engaged", **self._course_payload(repo.full_name, approved)}
        if provisioning is not None:
            response["provisioning"] = provisioning
        return response

    @staticmethod
    def _seed_greenfield(repo: RepoConfig, course: Course, store: CourseStore) -> None:
        """Create only durable, plan-owned seed files before the GitHub push."""
        if (repo.local_path / ".git").exists():
            raise SidecarError("greenfield local_path already contains a Git repository")
        canonical_docs = repo.canonical_docs or ("README.md", repo.planning_doc)
        manifest = ProjectManifest(
            version=1,
            goal=course.goal,
            canonical_docs=canonical_docs,
            planning_doc=repo.planning_doc,
            checks=repo.checks,
            surfaces=repo.surfaces,
            qa_profiles=course.qa_profiles,
        )
        store.save(course)
        (repo.local_path / "README.md").write_text(
            f"# {course.title}\n\n{course.goal}\n",
            encoding="utf-8",
        )
        plan_path = repo.local_path / repo.planning_doc
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        package_lines = "\n".join(
            f"- `{package.key}`: {package.objective}" for package in course.work_packages
        ) or "- The Captain will decompose the first implementation package after baseline review."
        acceptance = "\n".join(f"- {item}" for item in course.acceptance_criteria) or "- Define acceptance criteria during planning."
        exit_criteria = "\n".join(f"- {item}" for item in course.exit_criteria) or "- Define exit criteria during planning."
        plan_path.write_text(
            "# Implementation Plan\n\n"
            f"## Goal\n{course.goal}\n\n"
            f"## Work packages\n{package_lines}\n\n"
            f"## Acceptance criteria\n{acceptance}\n\n"
            f"## Exit criteria\n{exit_criteria}\n",
            encoding="utf-8",
        )
        manifest_path = repo.local_path / repo.project_manifest
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False), encoding="utf-8")

    def _ready_work(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo, _store, course = self._course_context(payload)
        completed_value = payload.get("completed")
        completed_items = cast(list[Any], completed_value) if isinstance(completed_value, list) else []
        completed: set[str] = {str(item) for item in completed_items}
        return {
            "repository": repo.full_name,
            "course_key": course.key,
            "work_packages": [item.model_dump(mode="json") for item in eligible_work_packages(course, completed)],
        }

    def _resolve_checkpoint(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo, store, course = self._course_context(payload)
        checkpoint_key = str(payload.get("checkpoint_key") or "").strip()
        status_value = str(payload.get("status") or "").strip()
        if not checkpoint_key or not status_value:
            raise SidecarError("course.checkpoint requires checkpoint_key and status")
        try:
            status = CheckpointStatus(status_value)
            evidence_value = payload.get("evidence")
            evidence_items = cast(list[Any], evidence_value) if isinstance(evidence_value, list) else []
            evidence = tuple(str(item) for item in evidence_items)
            updated = self.interaction.resolve_checkpoint(
                course,
                checkpoint_key,
                status,
                str(payload.get("resolved_by") or "") or None,
                datetime.fromisoformat(str(payload["resolved_at"])) if payload.get("resolved_at") else None,
                evidence,
            )
            store.save(updated)
        except (CourseError, ValueError) as exc:
            raise SidecarError(str(exc)) from exc
        return {"status": status.value, **self._course_payload(repo.full_name, updated)}

    def _set_course_status(self, payload: dict[str, Any], transition: Any) -> dict[str, Any]:
        repo, store, course = self._course_context(payload)
        try:
            updated = transition(course)
            store.save(updated)
        except CourseError as exc:
            raise SidecarError(str(exc)) from exc
        return {"status": updated.status.value, **self._course_payload(repo.full_name, updated)}

    def _write_config(self, config: AppConfig) -> None:
        try:
            validated = AppConfig.model_validate(config.model_dump(mode="python"))
        except ValueError as exc:
            raise SidecarError(f"invalid application configuration: {exc}") from exc
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump(validated.model_dump(mode="json"), sort_keys=False)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.config_path.parent, delete=False
        ) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        os.replace(temporary, self.config_path)
        self.config = validated

    def _schedule_description(self) -> dict[str, Any]:
        return {
            "source_of_truth": "openclaw_gateway_cron",
            "jobs": [
                {
                    "name": "captains-chair-reconcile",
                    "every": self.config.schedules.reconcile_every,
                    "kind": "reconcile",
                    "command": ["python", "-m", "captains_chair.sidecar", "--once", "reconcile"],
                },
                {
                    "name": "captains-chair-course-review",
                    "every": self.config.schedules.review_every,
                    "kind": "review",
                    "command": ["python", "-m", "captains_chair.sidecar", "--once", "review"],
                },
            ],
            "install_requires_operator_action": True,
            "repository_enablement": {
                repo.full_name: repo.schedule_enabled for repo in self.config.repos
            },
        }

    def _configure_schedules(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            key: payload[key]
            for key in ("reconcile_every", "review_every")
            if key in payload
        }
        try:
            schedules = ScheduleConfig.model_validate(
                {**self.config.schedules.model_dump(mode="python"), **allowed}
            )
        except ValueError as exc:
            raise SidecarError(f"invalid schedule configuration: {exc}") from exc
        self._write_config(self.config.model_copy(update={"schedules": schedules}))
        return {"status": "updated", **self._schedule_description()}

    def _acknowledge_attention(self, payload: dict[str, Any]) -> dict[str, Any]:
        full_name = str(payload.get("full_name") or "").strip()
        fingerprint = str(payload.get("fingerprint") or "").strip()
        event_type = str(payload.get("event_type") or "").strip() or None
        if not full_name or not fingerprint:
            raise SidecarError("attention.ack requires full_name and fingerprint")
        self.config.repo(full_name)
        count = self.state.acknowledge_attention(full_name, fingerprint, event_type)
        return {
            "status": "acknowledged",
            "repository": full_name,
            "fingerprint": fingerprint,
            "count": count,
        }

    def _run_once(self, kind: str) -> dict[str, Any]:
        if kind not in {"reconcile", "review"}:
            raise SidecarError(f"unsupported one-shot kind: {kind}")
        harness = next(
            (name for name in self.config.harnesses if name == "openclaw"),
            next(iter(self.config.harnesses), None),
        )
        if kind == "review" and harness is None:
            raise SidecarError("course review requires at least one configured harness")
        rows: list[dict[str, Any]] = []
        for repo in self.config.repos:
            if repo.operation_mode.value == "disabled":
                rows.append({"repo": repo.full_name, "status": "disabled", "exit_code": 0})
                continue
            if not repo.schedule_enabled:
                rows.append({"repo": repo.full_name, "status": "skipped", "exit_code": 0})
                continue
            command = [
                sys.executable,
                "-m",
                "captains_chair",
                "--config",
                str(self.config_path),
            ]
            if kind == "reconcile":
                command.extend(["orchestrate", "reconcile", "--repo", repo.full_name])
            else:
                command.extend(
                    [
                        "cycle",
                        "--repo",
                        repo.full_name,
                        "--harness",
                        str(harness),
                        "--live",
                        "--continue-run",
                    ]
                )
            try:
                completed = subprocess.run(
                    command,
                    cwd=repo.local_path if repo.local_path.is_dir() else None,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=1800,
                )
                rows.append(
                    {
                        "repo": repo.full_name,
                        "status": "completed" if completed.returncode == 0 else "failed",
                        "exit_code": completed.returncode,
                        "output": (completed.stdout or completed.stderr).strip()[-3000:],
                    }
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                rows.append({"repo": repo.full_name, "status": "failed", "error": str(exc)[:1000]})
        return {
            "status": "completed"
            if all(row.get("status") in {"completed", "disabled", "skipped"} for row in rows)
            else "degraded",
            "kind": kind,
            "repos": len(self.config.repos),
            "model_invocations": None,
            "execution": rows,
            "timestamp": datetime.now(UTC).isoformat(),
        }


def _list_value(value: Any) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="captains-chair-sidecar")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", choices=("reconcile", "review"))
    return parser


def main() -> int:
    args = _parser().parse_args()
    server = SidecarServer(args.config)
    if args.once:
        result = server.request("run.once", {"kind": args.once})
        print(json.dumps(result, default=str), flush=True)
        return 0 if result.get("status") == "completed" else 2
    for line in sys.stdin:
        if not line.strip():
            continue
        request_id: Any = None
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise SidecarError("request must be a JSON object")
            request = cast(dict[str, Any], raw)
            request_id = request.get("id")
            method = str(request.get("method") or "")
            params = request.get("params")
            params_value = cast(dict[str, Any], params) if isinstance(params, dict) else {}
            result = server.request(method, params_value)
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": "SIDECAR_ERROR", "message": str(exc)[:2000]},
            }
        print(json.dumps(response, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SidecarError", "SidecarServer", "main"]
