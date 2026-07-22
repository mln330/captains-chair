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
import shutil
import subprocess
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import yaml

from make_it_so import SIDECAR_PROTOCOL_VERSION, __version__
from make_it_so.adapters import InteractionAdapter, NativeInteractionAdapter
from make_it_so.config import load_config, load_project_manifest
from make_it_so.courses import (
    CourseError,
    CourseStore,
    approve_course,
    eligible_work_packages,
    pause_course,
    readiness_report,
    resume_course,
)
from make_it_so.evidence import extract_test_evidence, validate_test_evidence
from make_it_so.github import GhGitHubProvider, GitHubProvider
from make_it_so.milestones import MilestoneError, apply_milestone_changes, validate_milestone_changes
from make_it_so.models import (
    AppConfig,
    ApplicationSurface,
    CheckpointStatus,
    CompletionPolicy,
    Course,
    CourseKind,
    CourseStatus,
    MilestoneApprovalPolicy,
    MilestoneChangeProposal,
    MilestoneChangeRequest,
    MilestoneChangeStatus,
    ModelPolicy,
    ModelProfile,
    NotificationConfig,
    OnboardingPreferences,
    OpenClawWorkboardConfig,
    OperationMode,
    ProjectManifest,
    ReadinessRequirement,
    RepoConfig,
    RepositoryProvisioningConfig,
    RequirementStatus,
    RunState,
    ScheduleConfig,
    UsageConfig,
    WorkPackageStatus,
)
from make_it_so.openclaw_runtime import OpenClawRuntimeInstaller
from make_it_so.openclaw_usage import sync_openclaw_sessions
from make_it_so.openclaw_workboard import WORKER_EXECUTION_COMMENT_PREFIX
from make_it_so.orchestration import QueueCard, QueueStatus
from make_it_so.runtime import build_work_queue_adapter
from make_it_so.state import StateStore
from make_it_so.usage import build_usage_report


class SidecarError(RuntimeError):
    """A request failed with an operator-actionable error."""


SIDECAR_TIMEOUT_SAFETY_SECONDS = 300

_BOOTSTRAP_ROLES = (
    "captain",
    "coder",
    "reviewer",
    "tester",
    "ux_reviewer",
    "final_reviewer",
    "merger",
    "verifier",
)
_BOOTSTRAP_AGENT_IDS = {
    "captain": "github-captain",
    "coder": "github-coder",
    "reviewer": "github-reviewer",
    "tester": "github-tester",
    "ux_reviewer": "github-ux",
    "final_reviewer": "github-final",
    "merger": "github-merge",
    "verifier": "github-verify",
}
_BOOTSTRAP_MODELS = {
    "captain": "codex/gpt-5.6-terra",
    "coder": "codex/gpt-5.3-codex-spark",
    "reviewer": "codex/gpt-5.6-terra",
    "tester": "codex/gpt-5.6-luna",
    "ux_reviewer": "codex/gpt-5.6-terra",
    "final_reviewer": "codex/gpt-5.6-sol",
    "merger": "codex/gpt-5.6-terra",
    "verifier": "codex/gpt-5.6-terra",
}
_BOOTSTRAP_THINKING = {
    "captain": "high",
    "coder": "medium",
    "reviewer": "high",
    "tester": "medium",
    "ux_reviewer": "medium",
    "final_reviewer": "high",
    "merger": "medium",
    "verifier": "medium",
}


def _bootstrap_model_profile(
    model: str,
    agent_id: str,
    thinking: str,
    *,
    allow_fallback: bool = True,
) -> dict[str, Any]:
    return {
        "primary": {"model": model, "agent": agent_id, "thinking": thinking},
        "fallbacks": [],
        "allow_fallback": allow_fallback,
        "max_attempts": 1,
    }


def _executable_available(value: str) -> bool:
    """Check a configured command without executing it during onboarding."""
    candidate = value.strip()
    if not candidate:
        return False
    path = Path(candidate).expanduser()
    if path.is_absolute() or path.parent != Path("."):
        return path.is_file()
    return shutil.which(candidate) is not None


def _bootstrap_config(config_path: Path, payload: dict[str, Any] | None = None) -> AppConfig:
    """Build a conservative, valid configuration for plugin-first onboarding."""
    values = payload or {}
    workers_value = values.get("workers")
    raw_workers = cast(dict[str, Any], workers_value) if isinstance(workers_value, dict) else {}
    workers: dict[str, str] = {}
    worker_models: dict[str, str] = {}
    worker_runtimes: dict[str, str] = {}
    for role in _BOOTSTRAP_ROLES:
        worker_value = raw_workers.get(role)
        item = cast(dict[str, Any], worker_value) if isinstance(worker_value, dict) else {}
        agent_id = str(item.get("agent_id") or _BOOTSTRAP_AGENT_IDS[role]).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", agent_id):
            raise SidecarError(f"invalid OpenClaw agent ID for {role}: {agent_id!r}")
        workers[role] = agent_id
        worker_models[role] = str(item.get("model") or _BOOTSTRAP_MODELS[role]).strip()
        worker_runtimes[role] = str(item.get("runtime") or "openclaw").strip()

    executable = str(values.get("openclaw_executable") or "openclaw").strip()
    state_dir = config_path.parent / "state"
    artifact_dir = state_dir / "artifacts"
    lifecycle_value = values.get("lifecycle_command")
    lifecycle_items = cast(list[Any], lifecycle_value) if isinstance(lifecycle_value, list) else []
    if not (
        lifecycle_items
        and all(isinstance(item, str) and item.strip() for item in lifecycle_items)
    ):
        lifecycle_command: list[str] = ["make-it-so", "--config", str(config_path)]
    else:
        lifecycle_command = [str(item) for item in lifecycle_items]

    profiles = {
        role: _bootstrap_model_profile(
            worker_models[role],
            workers[role],
            _BOOTSTRAP_THINKING[role],
            allow_fallback=role != "final_reviewer",
        )
        for role in _BOOTSTRAP_ROLES
    }
    model_policy = {
        "baseline": profiles["captain"],
        "planner": profiles["captain"],
        "coder": profiles["coder"],
        "reviewer": profiles["reviewer"],
        "comment_adjudicator": profiles["reviewer"],
        "tester": profiles["tester"],
        "final_reviewer": profiles["final_reviewer"],
        "ux_reviewer": profiles["ux_reviewer"],
        "profiles": {
            "strategist": profiles["final_reviewer"],
            "course_verifier": profiles["final_reviewer"],
            "recovery_planner": profiles["captain"],
            "code_reviewer": profiles["reviewer"],
            "qa_assistant": profiles["tester"],
            "ui_qa_reviewer": profiles["ux_reviewer"],
        },
    }
    raw: dict[str, Any] = {
        "version": 1,
        "state_dir": str(state_dir),
        "artifact_dir": str(artifact_dir),
        "harnesses": {
            "openclaw": {
                "kind": "openclaw",
                "executable": executable,
                "default_agent": workers["captain"],
                "timeout_seconds": 3600,
            }
        },
        "orchestrators": {
            "openclaw-workers": {
                "kind": "openclaw_workboard",
                "executable": executable,
                "make_it_so_command": lifecycle_command,
                "board_prefix": "make-it-so",
                "workers": workers,
                "worker_models": worker_models,
                "worker_runtimes": worker_runtimes,
                "codex_executable": str(values.get("codex_executable") or "codex"),
                "max_concurrent_subagents": 1,
                "dispatch_strategy": "managed_single",
            }
        },
        "models": model_policy,
        "harness_model_overrides": {"openclaw": model_policy},
        "usage": {"block_on_unknown": True, "retention_days": 90},
        "schedules": {
            "reconcile_every": str(values.get("reconcile_every") or "5m"),
            "review_every": str(values.get("review_every") or "2h"),
        },
        "repos": [],
    }
    try:
        return AppConfig.model_validate(raw)
    except ValueError as exc:
        raise SidecarError(f"invalid first-run configuration: {exc}") from exc


_WORKBOARD_ACTIVE_STATUSES = frozenset(
    {
        QueueStatus.TODO,
        QueueStatus.READY,
        QueueStatus.RUNNING,
        QueueStatus.REVIEW,
    }
)

_REGISTRATION_IGNORED_PARTS = frozenset(
    {".git", ".venv", "node_modules", "bin", "obj", "dist", "build", "__pycache__"}
)
_DEFAULT_PLANNING_DOC = "docs/IMPLEMENTATION_PLAN.md"
_PLANNING_DOC_CANDIDATES = (
    "ISSUES_EXECUTION_PLAN.md",
    "docs/IMPLEMENTATION_PLAN.md",
    "docs/IMPLEMENTATION_ROADMAP.md",
    "docs/EXECUTION_PLAN.md",
    "EXECUTION_PLAN.md",
    "ROADMAP.md",
    "docs/ROADMAP.md",
    "PLAN.md",
    "docs/PLAN.md",
)

_TAKEOVER_READINESS_QUESTIONS = {
    "goals": "What outcome should this takeover achieve, and what must be visibly true when it is complete?",
    "non-goals": "What is explicitly out of scope for this course?",
    "users": "Who is the primary user, and what workflow must work for them?",
    "architecture-constraints": "What architecture, dependency, compatibility, or technology constraints must be preserved?",
    "permissions": "What permissions may the crew use, and which actions must remain owner-approved?",
    "secret-references": "Which secret references or credential names are required, without sharing secret values?",
    "external-access": "What external systems or accounts must the crew access, and are those connections available?",
    "environments": "Where should this be developed, tested, and run for the course?",
    "test-data": "What safe test data or fixtures should be used to prove the workflow?",
    "CI": "Which CI checks are required, and what counts as passing?",
    "deployment": "Is deployment part of this course, or is local/test verification the exit surface?",
    "rollback": "What is the rollback or recovery path if a change fails?",
    "observability": "What logs, status, metrics, or other evidence must be available to diagnose the result?",
    "security": "What security and privacy properties must be verified before completion?",
    "UX-inputs": "What user-interface or command-line usability expectations should be tested?",
    "token-policy": "What model and token-use limits should Number One enforce for this course?",
    "exit-criteria": "What exact acceptance and exit criteria must Number One verify before declaring completion?",
}


def _provisional_takeover_course(
    full_name: str,
    onboarding: OnboardingPreferences,
    planning: dict[str, Any],
    existing: tuple[Course, ...],
) -> Course:
    """Create the durable course container before the first Discord question.

    Registration must not be able to start implementation, but the planning
    conversation still needs a course to own answers, readiness review, and
    approval. Keeping every required category explicit also makes the
    dashboard truthful while Number One is interviewing the owner.
    """
    existing_keys = {course.key for course in existing}
    course_key = "takeover"
    if course_key in existing_keys:
        course_key = f"takeover-{uuid.uuid4().hex[:8]}"
    repository_name = full_name.split("/", 1)[-1]
    goal = onboarding.goal or "Complete and stabilize the repository's documented delivery plan."
    planning_path = str(planning.get("path") or _DEFAULT_PLANNING_DOC)
    readiness = tuple(
        ReadinessRequirement(
            key=key,
            category=key.replace("-", " ") if key != "CI" else "CI",
            question=question,
            owner_decision_required=True,
        )
        for key, question in _TAKEOVER_READINESS_QUESTIONS.items()
    )
    return Course(
        key=course_key,
        repository=full_name,
        kind=CourseKind.TAKEOVER,
        title=f"{repository_name} takeover",
        goal=goal,
        scope=(f"Reconcile the current implementation with {planning_path} before implementation begins.",),
        users=("Repository owner and maintainers",),
        acceptance_criteria=("Number One and the owner agree on a course charter and executable work packages.",),
        exit_criteria=("All agreed acceptance criteria pass with current test and user-acceptance evidence.",),
        readiness=readiness,
        pending_readiness_key="goals",
        pending_readiness_question=_TAKEOVER_READINESS_QUESTIONS["goals"],
        status=CourseStatus.READINESS_REVIEW,
    )


def _default_registration_orchestrator(config: AppConfig, requested: Any = None) -> str | None:
    """Choose the native OpenClaw Workboard when registering through OpenClaw.

    A repo may still explicitly select another adapter. When no adapter is
    supplied, selecting the configured OpenClaw Workboard prevents a new
    OpenClaw registration from silently falling back to the board-free direct
    runtime, which has no claimable OpenClaw worker cards.
    """
    explicit = str(requested or "").strip()
    if explicit:
        return explicit
    for name, value in config.orchestrators.items():
        if isinstance(value, OpenClawWorkboardConfig):
            return name
    return None


def _normalize_github_repository(value: str) -> str:
    """Accept the forms people naturally copy from GitHub and persist one form."""
    candidate = value.strip()
    if not candidate:
        raise SidecarError("repo.register requires full_name")
    if candidate.lower().startswith("git@github.com:"):
        candidate = candidate.split(":", 1)[1]
    elif candidate.lower().startswith(("https://github.com/", "http://github.com/", "https://www.github.com/", "http://www.github.com/")):
        parsed = urlsplit(candidate)
        if parsed.netloc.lower() not in {"github.com", "www.github.com"} or parsed.query or parsed.fragment:
            raise SidecarError("repo.register requires a GitHub repository URL or owner/repository")
        candidate = parsed.path.strip("/")
    candidate = candidate.removesuffix("/").removesuffix(".git")
    parts = candidate.split("/")
    if len(parts) != 2 or any(not part or any(char.isspace() for char in part) for part in parts):
        raise SidecarError("repo.register requires a GitHub repository URL or owner/repository")
    return "/".join(parts)


def _planning_document_discovery(root: Path) -> dict[str, Any]:
    """Find the repository's durable plan without making the user provide a path."""
    if not root.is_dir():
        return {
            "found": False,
            "path": _DEFAULT_PLANNING_DOC,
            "candidates": [],
            "reason": "The repository is not cloned locally yet.",
        }

    manifest_path = root / ".make-it-so" / "project.yaml"
    manifest_error: str | None = None
    if manifest_path.is_file():
        try:
            manifest = load_project_manifest(root, ".make-it-so/project.yaml")
        except (OSError, ValueError) as exc:
            manifest = None
            manifest_error = f"The project manifest could not be read: {type(exc).__name__}."
        if manifest is not None:
            plan_path = root / manifest.planning_doc
            if plan_path.is_file():
                return {
                    "found": True,
                    "path": manifest.planning_doc,
                    "candidates": [manifest.planning_doc],
                    "source": "project_manifest",
                    "canonical_docs": list(manifest.canonical_docs),
                    "checks": list(manifest.checks),
                }
            manifest_error = "The project manifest names a planning document that is not present."

    candidates: list[str] = []
    for relative in _PLANNING_DOC_CANDIDATES:
        if (root / relative).is_file():
            candidates.append(relative)
    if not candidates:
        try:
            discovered = sorted(
                path.relative_to(root).as_posix()
                for path in root.rglob("*.md")
                if path.is_file()
                and not any(part in _REGISTRATION_IGNORED_PARTS for part in path.relative_to(root).parts)
                and any(token in path.name.lower() for token in ("plan", "roadmap", "execution"))
            )
        except OSError:
            discovered = []
        candidates.extend(discovered[:8])
    if candidates:
        return {
            "found": True,
            "path": candidates[0],
            "candidates": candidates,
            "source": "repository_scan",
            "reason": manifest_error,
            "canonical_docs": [item for item in ("README.md", candidates[0]) if (root / item).is_file()],
            "checks": [],
        }
    return {
        "found": False,
        "path": _DEFAULT_PLANNING_DOC,
        "candidates": [],
        "reason": manifest_error or "No likely planning document was found.",
        "canonical_docs": ["README.md"] if (root / "README.md").is_file() else [],
        "checks": [],
    }


def _number_one_registration_prompt(
    full_name: str,
    local_discovery: dict[str, Any],
    planning: dict[str, Any],
    onboarding: OnboardingPreferences | None = None,
) -> str:
    """Build the first user-facing Number One turn for a newly registered repo."""
    clone_path = str(local_discovery.get("path") or "the configured workspace")
    clone_state = "a local clone was found" if local_discovery.get("cloned") else "no local clone was found"
    plan_path = str(planning.get("path") or _DEFAULT_PLANNING_DOC)
    plan_state = "a durable planning document was found" if planning.get("found") else "no durable planning document was found"
    choices: list[str] = []
    if onboarding is not None:
        if onboarding.phase is not None:
            choices.append(f"course type selected in the dashboard: {onboarding.phase.value}")
        if onboarding.goal:
            choices.append(f"the builder's initial goal: {onboarding.goal}")
        if onboarding.clone_allowed is not None:
            choices.append(f"clone permission: {'allowed' if onboarding.clone_allowed else 'not granted'}")
        if onboarding.detected_surface is not None:
            choices.append(f"application surface: {onboarding.detected_surface.value}")
        choices.extend(
            [
                f"UAT evidence required: {'yes' if onboarding.uat_required else 'no'}",
                f"screenshots required: {'yes' if onboarding.screenshots_required else 'no'}",
                f"checkpoint preference: {onboarding.checkpoint_policy.replace('_', ' ')}",
                f"intelligence level: {onboarding.intelligence_level}",
            ]
        )
    known_choices = "\n".join(f"- {item}" for item in choices) or "- No dashboard preferences were supplied yet."
    missing = (
        "The dashboard already captured the items listed below. Do not ask for them again."
        if choices
        else "The dashboard did not capture any preferences yet."
    )
    return (
        "You are Number One, the first in command for Make It So. This is the initial planning conversation "
        f"for the newly registered repository {full_name}. {clone_state} at {clone_path}; {plan_state} "
        f"(checked path: {plan_path}).\n\n"
        "A provisional, paused takeover course has been created to hold this conversation. It is not ready "
        "for implementation and must remain paused until the readiness review and explicit charter approval "
        "are recorded.\n\n"
        "Reply directly in this Discord channel with the heading `NUMBER ONE | INITIAL PLANNING`. "
        "Your only job in this turn is to begin a short, conversational requirements interview that will "
        "produce a course charter. Do not clone the repository, run a baseline, edit files, create branches, create "
        "issues or pull requests, dispatch workers, or claim that implementation has started.\n\n"
        f"{missing}\n{known_choices}\n\n"
        "Ask exactly this one readiness question in this turn and do not substitute another topic: "
        f"{_TAKEOVER_READINESS_QUESTIONS['goals']} "
        "Acknowledge the builder's answer briefly on later turns, "
        "never repeat a question already answered or verified by inspection, and offer a sensible default "
        "when the builder says they are unsure. Use short paragraphs and Discord-friendly quick choices "
        "such as `1`, `2`, or `3` for fixed options. After the unresolved questions are answered, present "
        "a concise course charter for explicit approval before any work begins."
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


def _card_model(card: QueueCard, worker_models: dict[str, str]) -> str | None:
    execution = _card_execution(card)
    if execution is not None and execution.get("requested_model"):
        return str(execution["requested_model"])
    if card.agent_id and card.agent_id in worker_models:
        return worker_models[card.agent_id]
    if str(card.agent_id or "").startswith("make-it-so-managed:deterministic-merge:"):
        return "deterministic gate"
    return None


def _card_execution(card: QueueCard) -> dict[str, Any] | None:
    proof = card.metadata.get("proof")
    if isinstance(proof, list):
        for raw in reversed(cast(list[object], proof)):
            if not isinstance(raw, dict):
                continue
            execution = cast(dict[str, Any], raw).get("execution")
            if isinstance(execution, dict):
                return cast(dict[str, Any], execution)
    comments = card.metadata.get("comments")
    if isinstance(comments, list):
        for raw in reversed(cast(list[object], comments)):
            if not isinstance(raw, dict):
                continue
            body = cast(dict[str, Any], raw).get("body")
            if not isinstance(body, str) or not body.startswith(WORKER_EXECUTION_COMMENT_PREFIX):
                continue
            try:
                execution = json.loads(body.removeprefix(WORKER_EXECUTION_COMMENT_PREFIX))
            except json.JSONDecodeError:
                continue
            if isinstance(execution, dict):
                return cast(dict[str, Any], execution)
    return None


def _sync_workboard_worker_usage(state: StateStore, *, repo: str, cards: list[QueueCard]) -> dict[str, int]:
    imported = 0
    for card in cards:
        execution = _card_execution(card)
        if execution is None or execution.get("runtime") != "codex":
            continue
        usage_value = execution.get("usage")
        usage = cast(dict[str, Any], usage_value) if isinstance(usage_value, dict) else {}
        attempt_id = str(execution.get("attempt_id") or "").strip()
        if not attempt_id:
            continue
        state.record_external_usage(
            {
                "source": "make-it-so-worker",
                "external_id": f"{card.id}:{attempt_id}",
                "repo": repo,
                "role": card.agent_id or _workflow_stage(card) or "worker",
                "stage": _workflow_stage(card) or "worker",
                "status": "completed" if card.status == QueueStatus.DONE else card.status.value,
                "provider": "codex",
                "model": execution.get("requested_model"),
                "input_tokens": usage.get("input_tokens"),
                "cached_input_tokens": usage.get("cached_input_tokens"),
                "cache_write_tokens": usage.get("cache_write_tokens"),
                "reasoning_tokens": usage.get("reasoning_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "prompt_bytes": usage.get("prompt_bytes", 0),
                "response_bytes": usage.get("response_bytes", 0),
                "duration_ms": execution.get("duration_ms", 0),
                "updated_at": _card_activity_time(card) or datetime.now(UTC).isoformat(),
                "payload": execution,
            }
        )
        imported += 1
    return {"imported": imported}


def _is_loop_card(card: QueueCard) -> bool:
    title = card.title.lower()
    return (
        _workflow_stage(card) == "repair"
        or title.startswith("retry ")
        or any(label.startswith(("retry:", "retry-for:")) for label in card.labels)
        or _attempt_count(card) > 1
    )


def _is_superseded_retry(card: QueueCard, cards: list[QueueCard]) -> bool:
    """Return whether a blocked loop card was superseded by a successful stage."""
    if card.status != QueueStatus.BLOCKED or not _is_loop_card(card):
        return False
    stage = _workflow_stage(card)
    return stage is not None and any(
        other.id != card.id
        and other.status == QueueStatus.DONE
        and _workflow_stage(other) == stage
        for other in cards
    )


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
        str(getattr(configured.workers, role)): str(getattr(configured.worker_models, role)) for role in roles
    }


_COURSE_GATE_STATUSES = frozenset(
    {
        CourseStatus.DRAFT,
        CourseStatus.READINESS_REVIEW,
        CourseStatus.AWAITING_APPROVAL,
        CourseStatus.PAUSED,
    }
)


def _current_course(repo: RepoConfig) -> Course | None:
    """Return the active course that must gate execution and dashboard history.

    A repository can retain completed courses as durable history.  A newer
    readiness-review course must still win over that history, otherwise a
    stale terminal Workboard can make a fresh registration appear complete.
    """
    try:
        courses = CourseStore(repo.local_path).list()
    except (CourseError, OSError, ValueError):
        return None
    active = [course for course in courses if course.status != CourseStatus.COMPLETED]
    return active[-1] if active else (courses[-1] if courses else None)


def _milestone_rows(repo: RepoConfig, cards: list[QueueCard]) -> list[dict[str, Any]]:
    """Build compact milestone/evidence rows without fetching model transcripts."""
    try:
        courses = CourseStore(repo.local_path).list()
    except (CourseError, OSError, ValueError):
        return []
    active_cards = [card for card in cards if not card.metadata.get("archivedAt")]
    workflows: dict[str, list[QueueCard]] = {}
    for card in active_cards:
        label = _workflow_label(card)
        if label:
            workflows.setdefault(label, []).append(card)
    latest_cards = active_cards
    if workflows:
        _latest_label, latest_cards = max(
            workflows.items(),
            key=lambda item: max((_card_activity_timestamp(card) for card in item[1]), default=0),
        )
    rows: list[dict[str, Any]] = []
    for course in courses:
        course_cards = [
            card
            for card in latest_cards
            if str(card.metadata.get("courseKey") or "") == course.key
        ]
        if not course_cards:
            course_cards = [
                card
                for card in active_cards
                if str(card.metadata.get("courseKey") or "") == course.key
            ]
        for package in course.work_packages:
            candidates = [
                card
                for card in course_cards
                if str(card.metadata.get("workPackageKey") or "") == package.key
                and _workflow_stage(card) in {"test", "ux_review"}
            ]
            candidates.sort(key=_card_activity_timestamp, reverse=True)
            current_head = next(
                (
                    str(card.metadata.get("discoveredHeadSha") or "").strip()
                    for card in candidates
                    if str(card.metadata.get("discoveredHeadSha") or "").strip()
                ),
                None,
            )
            policy = package.test_evidence_policy
            validations: list[tuple[QueueCard, Any, dict[str, Any] | None]] = []
            for card in candidates:
                validation = validate_test_evidence(
                    card.metadata.get("proof"),
                    policy,
                    current_head,
                )
                validations.append((card, validation, extract_test_evidence(card.metadata.get("proof"))))
            passed = next((row for row in validations if row[1].allowed), None)
            inspected = passed or next((row for row in validations if row[2] is not None), None)
            validation = inspected[1] if inspected else None
            parsed = inspected[2] if inspected else None
            evidence_status = (
                "passed"
                if passed is not None
                else str(validation.summary.get("status") or "missing")
                if validation is not None
                else "not_run"
            )
            evidence: dict[str, Any] = {
                "status": evidence_status,
                "reason": validation.reason if validation is not None else "no test or UX evidence recorded",
                "source_card_id": inspected[0].id if inspected else None,
                "head_sha": parsed.get("head_sha") if parsed else None,
                "current_head_sha": current_head,
                "pass_rate": parsed.get("pass_rate") if parsed else None,
                "tests_total": parsed.get("tests_total") if parsed else None,
                "tests_passed": parsed.get("tests_passed") if parsed else None,
                "tests_failed": parsed.get("tests_failed") if parsed else None,
                "tests_skipped": parsed.get("tests_skipped") if parsed else None,
                "commands": parsed.get("commands", []) if parsed else [],
                "screenshots": parsed.get("screenshots", []) if parsed else [],
                "artifacts": parsed.get("artifacts", []) if parsed else [],
                "model": parsed.get("model") if parsed else None,
                "provider": parsed.get("provider") if parsed else None,
                "captured_at": parsed.get("captured_at") if parsed else None,
                "summary": parsed.get("summary") if parsed else None,
            }
            pr_url = next(
                (
                    card.source_url
                    for card in candidates
                    if card.source_url and "/pull/" in card.source_url
                ),
                next(
                    (
                        card.source_url
                        for card in course_cards
                        if card.source_url and "/pull/" in card.source_url
                    ),
                    None,
                ),
            )
            rows.append(
                {
                    "course_key": course.key,
                    "work_package_key": package.key,
                    "title": package.title,
                    "objective": package.objective,
                    "status": package.status.value,
                    "policy": policy.model_dump(mode="json"),
                    "evidence": evidence,
                    "pr_url": pr_url,
                }
            )
    return rows


def _summarize_workboard(
    cards: list[QueueCard],
    board_id: str,
    *,
    usage_sync: dict[str, Any] | None = None,
    repo_full_name: str | None = None,
    worker_models: dict[str, str] | None = None,
    repo: RepoConfig | None = None,
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
            "milestones": _milestone_rows(repo, cards) if repo is not None else [],
        }

    configured_models = worker_models or {}
    ordered_workflows = sorted(
        workflows.items(),
        key=lambda item: max((_card_activity_timestamp(card) for card in item[1]), default=0),
    )
    all_workflow_cards = [card for _label, workflow_cards in ordered_workflows for card in workflow_cards]
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
                "historical_blockers": sum(
                    card.status == QueueStatus.BLOCKED and not _is_superseded_retry(card, latest_cards)
                    for card in stage_cards
                ),
                "retry_attempts": sum(_is_loop_card(card) for card in stage_cards),
                "superseded_retries": sum(
                    _is_superseded_retry(card, latest_cards) for card in stage_cards
                ),
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
                "model": _card_model(card, configured_models),
                "attempts": _attempt_count(card),
                "workflow": latest_label.removeprefix("workflow:"),
                "pr_url": card.source_url if "/pull/" in str(card.source_url or "") else None,
                "updated_at": _card_activity_time(card),
                "loop": _is_loop_card(card),
                "superseded_retry": _is_superseded_retry(card, all_workflow_cards),
            }
            for card in latest_cards
        ),
        key=lambda item: str(item["updated_at"] or ""),
    )[-16:]
    loop_count = sum(int(bool(item["loop"])) for item in timeline)
    review_cards = [
        card for card in all_workflow_cards if _workflow_stage(card) in {"review", "final_review"}
    ]
    current_review_cards = [
        card for card in latest_cards if _workflow_stage(card) in {"review", "final_review"}
    ]
    review_active = any(card.status in _WORKBOARD_ACTIVE_STATUSES for card in current_review_cards)
    review_blocked = any(card.status == QueueStatus.BLOCKED for card in current_review_cards)
    historical_review_blockers = sum(
        card.status == QueueStatus.BLOCKED and not _is_superseded_retry(card, all_workflow_cards)
        for card in review_cards
    )
    review_status = (
        "passed"
        if terminal and any(card.status == QueueStatus.DONE for card in review_cards)
        else "blocked"
        if review_blocked
        else "in_review"
        if review_active
        else "passed"
        if review_cards and any(card.status == QueueStatus.DONE for card in review_cards)
        else "not_run"
    )
    test_cards = [card for card in all_workflow_cards if _workflow_stage(card) in {"test", "qa", "ux_review"}]
    current_test_cards = [
        card for card in latest_cards if _workflow_stage(card) in {"test", "qa", "ux_review"}
    ]
    test_active = any(card.status in _WORKBOARD_ACTIVE_STATUSES for card in current_test_cards)
    test_blocked = any(card.status == QueueStatus.BLOCKED for card in current_test_cards)
    test_status = (
        "blocked"
        if test_blocked
        else "running"
        if test_active
        else "passed"
        if test_cards and any(card.status == QueueStatus.DONE for card in test_cards)
        else "not_run"
    )
    active_stage = next(
        (
            _workflow_stage(card) or "unknown"
            for card in sorted(latest_cards, key=_card_activity_timestamp, reverse=True)
            if card.status in _WORKBOARD_ACTIVE_STATUSES
        ),
        None,
    )
    current_stage = active_stage or (
        "post_merge"
        if terminal
        else "merge"
        if "merge" in done_stages
        else timeline[-1]["stage"]
        if timeline
        else None
    )
    superseded_retries = sum(
        _is_superseded_retry(card, all_workflow_cards) for card in all_workflow_cards
    )
    historical_blockers = sum(
        card.status == QueueStatus.BLOCKED and not _is_superseded_retry(card, all_workflow_cards)
        for card in all_workflow_cards
    )
    current_blockers = 0 if terminal else sum(
        card.status == QueueStatus.BLOCKED and not _is_superseded_retry(card, latest_cards)
        for card in latest_cards
    )
    latest_card_ids = {card.id for card in latest_cards}
    stage_history: list[dict[str, Any]] = []
    for stage_name in stage_names:
        stage_cards = [card for card in all_workflow_cards if _workflow_stage(card) == stage_name]
        if not stage_cards:
            continue
        models = sorted(
            {model for card in stage_cards for model in (_card_model(card, configured_models),) if model}
        )
        stage_history.append(
            {
                "stage": stage_name,
                "total": len(stage_cards),
                "done": sum(card.status == QueueStatus.DONE for card in stage_cards),
                "active": sum(
                    card.id in latest_card_ids and card.status in _WORKBOARD_ACTIVE_STATUSES
                    for card in stage_cards
                ),
                "blocked": sum(card.status == QueueStatus.BLOCKED for card in stage_cards),
                "historical_blockers": sum(
                    card.status == QueueStatus.BLOCKED
                    and not _is_superseded_retry(card, all_workflow_cards)
                    for card in stage_cards
                ),
                "retry_attempts": sum(_is_loop_card(card) for card in stage_cards),
                "superseded_retries": sum(
                    _is_superseded_retry(card, all_workflow_cards) for card in stage_cards
                ),
                "loops": sum(_is_loop_card(card) for card in stage_cards),
                "models": models,
            }
        )

    workflow_runs: list[dict[str, Any]] = []
    for run_index, (workflow_label, workflow_cards) in enumerate(ordered_workflows, start=1):
        run_active = [card for card in workflow_cards if card.status in _WORKBOARD_ACTIVE_STATUSES]
        run_done_stages = {
            stage
            for card in workflow_cards
            if card.status == QueueStatus.DONE
            for stage in (_workflow_stage(card),)
            if stage is not None
        }
        run_terminal = not run_active and {"merge", "post_merge"}.issubset(run_done_stages)
        run_is_latest = workflow_label == latest_label
        run_status = (
            "completed"
            if run_terminal
            else "in_progress"
            if run_is_latest and run_active
            else "blocked"
            if run_is_latest
            else "superseded"
        )
        run_stage_names = {
            stage for card in workflow_cards for stage in (_workflow_stage(card),) if stage is not None
        }
        run_kind = (
            "build"
            if "implementation" in run_stage_names
            else "completion"
            if {"merge", "post_merge"} & run_stage_names
            else "review"
        )
        root_card = next(
            (card for card in workflow_cards if _workflow_stage(card) == "orchestration"),
            workflow_cards[0],
        )
        run_timeline = sorted(
            (
                {
                    "id": card.id,
                    "stage": _workflow_stage(card) or "unknown",
                    "status": card.status.value,
                    "title": card.title,
                    "summary": _card_summary(card),
                    "agent": card.agent_id,
                    "model": _card_model(card, configured_models),
                    "attempts": _attempt_count(card),
                    "pr_url": card.source_url if "/pull/" in str(card.source_url or "") else None,
                    "updated_at": _card_activity_time(card),
                    "loop": _is_loop_card(card),
                    "superseded_retry": _is_superseded_retry(card, all_workflow_cards),
                }
                for card in workflow_cards
                if card.status in {QueueStatus.DONE, QueueStatus.BLOCKED}
                or card.status in _WORKBOARD_ACTIVE_STATUSES
            ),
            key=lambda item: (
                str(item["updated_at"] or ""),
                stage_names.index(str(item["stage"])) if item["stage"] in stage_names else len(stage_names),
            ),
        )
        workflow_runs.append(
            {
                "workflow": workflow_label.removeprefix("workflow:"),
                "index": run_index,
                "title": root_card.title,
                "kind": run_kind,
                "status": run_status,
                "current": run_is_latest,
                "cards": len(workflow_cards),
                "loops": sum(_is_loop_card(card) for card in workflow_cards),
                "blocked": sum(card.status == QueueStatus.BLOCKED for card in workflow_cards),
                "done": sum(card.status == QueueStatus.DONE for card in workflow_cards),
                "historical_blockers": sum(
                    card.status == QueueStatus.BLOCKED
                    and not _is_superseded_retry(card, all_workflow_cards)
                    for card in workflow_cards
                ),
                "superseded_retries": sum(
                    _is_superseded_retry(card, all_workflow_cards) for card in workflow_cards
                ),
                "updated_at": max((_card_activity_time(card) or "" for card in workflow_cards), default="")
                or None,
                "timeline": run_timeline[-18:],
            }
        )
    pr_numbers: set[int] = set()
    explicit_pr_numbers: set[int] = set()
    pr_urls_set: set[str] = set()
    for card in all_workflow_cards:
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
        "stage_history": stage_history,
        "timeline": timeline,
        "workflow_runs": workflow_runs[-6:],
        "loop_count": loop_count,
        "total_loop_count": sum(_is_loop_card(card) for card in all_workflow_cards),
        "review_cycles": len(review_cards),
        "reviews_passed": sum(card.status == QueueStatus.DONE for card in review_cards),
        "review_status": review_status,
        "test_status": test_status,
        # A terminal workflow has no current blocker. Preserve the blocked
        # cards separately so the execution history remains auditable without
        # making a completed course look stuck.
        "blockers": current_blockers,
        "current_blockers": current_blockers,
        "historical_blockers": historical_blockers,
        "historical_review_blockers": historical_review_blockers,
        "superseded_retries": superseded_retries,
        "terminal": terminal,
        "completion_status": "verified" if terminal else status,
        "pr_count": max(len(pr_numbers), len(pr_urls)),
        "pr_numbers": sorted(pr_numbers),
        "pr_urls": pr_urls,
        "milestones": _milestone_rows(repo, all_workflow_cards) if repo is not None else [],
    }
    if usage_sync is not None:
        summary["usage_sync"] = usage_sync
    if terminal:
        summary["message"] = (
            "Implementation merged and post-merge verification completed. "
            "Historical blockers and superseded retry cards are retained for audit; "
            "superseded retries do not block this course."
        )
    return summary


class SidecarServer:
    def __init__(
        self,
        config_path: Path,
        interaction: InteractionAdapter | None = None,
        github: GitHubProvider | None = None,
    ) -> None:
        self.config_path = config_path
        self.configured = config_path.is_file()
        self.config = load_config(config_path) if self.configured else _bootstrap_config(config_path)
        self.state = StateStore(self.config.state_dir / "state.db")
        self.interaction = interaction or NativeInteractionAdapter()
        self.github = github or GhGitHubProvider()

    def _orchestration_board_id(self, repo: RepoConfig) -> tuple[OpenClawWorkboardConfig, str] | None:
        """Resolve a repo's optional Workboard adapter and derived board id."""
        if not repo.orchestrator:
            return None
        configured = self.config.orchestrators.get(repo.orchestrator)
        if not isinstance(configured, OpenClawWorkboardConfig):
            return None
        board_id = repo.orchestration_board or (
            f"{configured.board_prefix}-{repo.full_name.replace('/', '-').lower()}"
        )
        return configured, board_id

    def _expected_openclaw_models(self, configured: OpenClawWorkboardConfig) -> dict[str, str]:
        """Include the named Number One strategist route in usage attribution."""
        expected = _expected_worker_models(configured)
        try:
            number_one = self.config.model_policy("openclaw").for_role("number_one").primary.model
        except (KeyError, ValueError):
            number_one = None
        if number_one:
            expected["number_one"] = number_one
            expected["strategist"] = number_one
        return expected

    def _workboard_status(self, repo: RepoConfig, *, sync_usage: bool = True) -> dict[str, Any] | None:
        """Read Workboard progress and reconcile worker telemetry for the dashboard."""
        resolved = self._orchestration_board_id(repo)
        if resolved is None:
            return None
        configured, board_id = resolved
        try:
            worker_models = self._expected_openclaw_models(configured)
            adapter = build_work_queue_adapter(configured)
            cards = adapter.list_cards(board_id)
            self.state.sync_orchestration_cards(repo.full_name, _card_context_rows(cards))
            if sync_usage:
                try:
                    direct_usage = _sync_workboard_worker_usage(self.state, repo=repo.full_name, cards=cards)
                    usage_sync = {
                        "status": "ok",
                        "direct_workers": direct_usage,
                        **sync_openclaw_sessions(
                            self.state,
                            repo=repo.full_name,
                            executable=configured.executable,
                            expected_models=worker_models,
                            session_context=self.state.openclaw_session_context(repo.full_name),
                            number_one_context=self.state.number_one_session_context(repo.full_name),
                            session_limit=configured.session_limit,
                        ),
                    }
                except Exception as exc:
                    usage_sync = {"status": "unavailable", "error": str(exc)[:500]}
            else:
                usage_sync = {"status": "cached"}
            return _summarize_workboard(
                cards,
                board_id,
                usage_sync=usage_sync,
                repo_full_name=repo.full_name,
                worker_models=worker_models,
                repo=repo,
            )
        except Exception as exc:
            return {
                "status": "unavailable",
                "board": repo.orchestration_board,
                "error": str(exc)[:500],
            }

    def _cached_workboard_status(self, repo: RepoConfig) -> dict[str, Any] | None:
        """Project the durable Workboard mirror without a slow OpenClaw RPC."""
        resolved = self._orchestration_board_id(repo)
        if resolved is None:
            return None
        configured, board_id = resolved
        worker_models = self._expected_openclaw_models(configured)
        payloads = self.state.orchestration_card_payloads(repo.full_name)
        cards: list[QueueCard] = []
        for payload in payloads:
            try:
                cards.append(QueueCard.model_validate(payload))
            except ValueError:
                continue
        if not cards:
            return {
                "status": "unknown",
                "board": board_id,
                "cards": 0,
                "counts": {},
                "usage_sync": {"status": "cached"},
                "milestones": _milestone_rows(repo, cards),
            }
        return _summarize_workboard(
            cards,
            board_id,
            usage_sync={"status": "cached"},
            repo_full_name=repo.full_name,
            worker_models=worker_models,
            repo=repo,
        )

    def _github_status(self, repo: RepoConfig) -> dict[str, Any]:
        """Collect the small GitHub slice needed for a portfolio card."""
        try:
            snapshot = self.github.snapshot(repo)
            runs = snapshot.workflow_runs
            failed_conclusions = {"failure", "cancelled", "timed_out", "action_required", "stale"}
            failed_runs = sum(str(run.get("conclusion") or "").lower() in failed_conclusions for run in runs)
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

    def _fast_repo_status(self, repo: RepoConfig) -> dict[str, Any]:
        """Build a dashboard status without blocking on provider session import."""
        return self._repo_status(repo, sync_usage=False, cached_workboard=True)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = params or {}
        if method == "health":
            return {
                "status": "healthy",
                "service": "make-it-so-sidecar",
                "version": __version__,
                "protocol_version": SIDECAR_PROTOCOL_VERSION,
                "config": str(self.config_path),
                "configured": self.configured,
                "setup_required": not self.configured,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        if method == "bootstrap.status":
            return self._bootstrap_status(payload)
        if method == "bootstrap.apply":
            return self._apply_bootstrap(payload)
        if method in {"portfolio.status", "repos.list"}:
            repos = self.config.repos
            if not repos:
                return {"repos": []}
            fast = bool(payload.get("fast", False))
            with ThreadPoolExecutor(max_workers=min(8, len(repos))) as executor:
                if fast:
                    return {
                        "repos": list(executor.map(self._fast_repo_status, repos)),
                        "freshness": "github_workboard_live_usage_cached",
                    }
                return {"repos": list(executor.map(self._repo_status, repos))}
        if method == "registration.options":
            return self._registration_options()
        if method == "repo.register":
            return self._register_repo(payload)
        if method == "repo.inspect":
            return self._inspect_repository(payload)
        if method == "repo.create":
            return self._create_greenfield_repo(payload)
        if method == "repo.update":
            return self._update_repo(payload)
        if method == "discord.planning_bindings":
            return self._discord_planning_bindings()
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
        if method == "course.milestone_evidence":
            return self._course_milestone_evidence(payload)
        if method == "course.milestone_changes":
            return self._course_milestone_changes(payload)
        if method == "course.milestone_change_propose":
            return self._propose_milestone_change(payload)
        if method == "course.milestone_change_approve":
            return self._approve_milestone_change(payload)
        if method == "course.milestone_change_reject":
            return self._reject_milestone_change(payload)
        if method == "course.create":
            return self._create_course(payload)
        if method == "course.readiness":
            return self._course_readiness(payload)
        if method == "course.planning_session":
            return self._planning_session(payload)
        if method == "course.readiness_review":
            return self._review_course_readiness(payload)
        if method == "course.pending_question":
            return self._set_pending_readiness_question(payload)
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
        if method == "run.start":
            return self._start_once(
                str(payload.get("kind") or "review"),
                force_replan=bool(payload.get("force_replan", False)),
            )
        if method == "run.once":
            return self._run_once(
                str(payload.get("kind") or "reconcile"),
                force_replan=bool(payload.get("force_replan", False)),
            )
        raise SidecarError(f"unknown sidecar method: {method}")

    def _bootstrap_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        executable = str(payload.get("openclaw_executable") or "").strip()
        config = self.config
        configured = config.orchestrators.get("openclaw-workers")
        if not isinstance(configured, OpenClawWorkboardConfig):
            generated = _bootstrap_config(
                self.config_path,
                {"openclaw_executable": executable or "openclaw"},
            ).orchestrators.get("openclaw-workers")
            if not isinstance(generated, OpenClawWorkboardConfig):
                raise SidecarError("first-run OpenClaw workboard configuration is unavailable")
            configured = generated
        if executable and executable != configured.executable:
            configured = configured.model_copy(update={"executable": executable})
        installer = OpenClawRuntimeInstaller(configured)
        workspace_root = Path(
            str(payload.get("workspace_root") or self.config_path.parent / "workers")
        ).expanduser()
        try:
            inventory = list(installer.agent_inventory())
            actions = [item.__dict__ for item in installer.plan(workspace_root)]
            runtime_available = True
            warning = None
        except Exception as exc:
            inventory = []
            actions = []
            runtime_available = False
            warning = str(exc)[:1000]
        worker_payload = {
            role: {
                "agent_id": getattr(configured.workers, role),
                "model": getattr(configured.worker_models, role),
                "runtime": getattr(configured.worker_runtimes, role),
            }
            for role in _BOOTSTRAP_ROLES
        }
        return {
            "status": "configured" if self.configured else "setup_required",
            "configured": self.configured,
            "setup_required": not self.configured,
            "config_path": str(self.config_path),
            "openclaw_executable": configured.executable,
            "codex_executable": configured.codex_executable or "codex",
            "workspace_root": str(workspace_root),
            "runtime_available": runtime_available,
            "codex_available": _executable_available(configured.codex_executable or "codex"),
            "warning": warning,
            "agents": inventory,
            "workers": worker_payload,
            "actions": actions,
            "schedules": self.config.schedules.model_dump(mode="json"),
            "automation_enabled": False,
        }

    def _apply_bootstrap(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.configured and self.config.repos and not bool(payload.get("reconfigure", False)):
            raise SidecarError(
                "first-run setup cannot replace a configuration with registered repositories"
            )
        candidate = _bootstrap_config(self.config_path, payload)
        configured = candidate.orchestrators.get("openclaw-workers")
        if not isinstance(configured, OpenClawWorkboardConfig):
            raise SidecarError("first-run configuration did not create the OpenClaw orchestrator")
        workspace_root = Path(
            str(payload.get("workspace_root") or self.config_path.parent / "workers")
        ).expanduser().resolve()
        if workspace_root == Path(workspace_root.anchor):
            raise SidecarError("worker workspace root cannot be a filesystem root")

        installer = OpenClawRuntimeInstaller(configured)
        # Planning before persistence catches agent/model conflicts without
        # replacing an otherwise healthy configuration.
        planned = installer.plan(workspace_root)
        mismatches = [item for item in planned if item.action == "model_mismatch"]
        if mismatches:
            first = mismatches[0]
            raise SidecarError(
                f"agent {first.agent_id} already uses a different model; "
                "choose another agent ID or update it explicitly"
            )
        if "codex" in configured.worker_runtimes.model_dump().values() and not _executable_available(
            configured.codex_executable or ""
        ):
            raise SidecarError(
                f"Direct Codex workers require an executable that is available: "
                f"{configured.codex_executable or 'codex'}"
            )
        actions = installer.install(workspace_root)
        self._write_config(candidate)
        self.configured = True
        self.state = StateStore(candidate.state_dir / "state.db")
        return {
            "status": "configured",
            "configured": True,
            "setup_required": False,
            "config_path": str(self.config_path),
            "workspace_root": str(workspace_root),
            "agents": [item.__dict__ for item in actions],
            "schedules": candidate.schedules.model_dump(mode="json"),
            "automation_enabled": False,
            "next_action": "Register a repository, run the canary, then explicitly enable schedules.",
        }

    def _discord_planning_bindings(self) -> dict[str, Any]:
        """Return durable Discord-to-Number-One bindings for plugin recovery."""
        bindings = [
            {
                "repository": repo.full_name,
                "route": repo.notification.route,
                "session_key": f"make-it-so:number-one:{repo.full_name.replace('/', '-')}",
            }
            for repo in self.config.repos
            if repo.notification.route
        ]
        return {"bindings": bindings}

    def _repo_status(
        self,
        repo: RepoConfig,
        *,
        sync_usage: bool = True,
        cached_workboard: bool = False,
    ) -> dict[str, Any]:
        with ThreadPoolExecutor(max_workers=1) as executor:
            github_future = executor.submit(self._github_status, repo)
            workboard = (
                self._cached_workboard_status(repo)
                if cached_workboard
                else self._workboard_status(repo, sync_usage=False)
                if not sync_usage
                else self._workboard_status(repo)
            )
            summary = self.state.usage_summary(repo=repo.full_name)
            usage = build_usage_report(summary, self.config.usage)
            github = github_future.result()
        state = self.state.current_state(repo.full_name).value
        course = _current_course(repo)
        course_gates_execution = course is not None and course.status in _COURSE_GATE_STATUSES
        if course_gates_execution:
            # A new course charter is the source of truth until it is engaged.
            # Historical Workboard cards and merged PRs must not leak into the
            # current course's progress or terminal status.
            workboard = None
            state = RunState.BASELINE_REVIEW.value
        elif workboard is not None and workboard.get("status") == "completed":
            state = "merged"
        configured_orchestrator = self.config.orchestrators.get(repo.orchestrator or "")
        worker_models = (
            configured_orchestrator.worker_models.model_dump(mode="json")
            if isinstance(configured_orchestrator, OpenClawWorkboardConfig)
            else {}
        )
        worker_runtimes = (
            configured_orchestrator.worker_runtimes.model_dump(mode="json")
            if isinstance(configured_orchestrator, OpenClawWorkboardConfig)
            else {}
        )
        events = [
            event.model_dump(mode="json") for event in self.state.recent_events(repo.full_name, limit=12)
        ]
        local_path = repo.local_path
        dirty = False
        control_plane_dirty = False
        if local_path.is_dir() and (local_path / ".git").exists():
            try:
                result = subprocess.run(
                    ["git", "-C", str(local_path), "status", "--porcelain", "--untracked-files=all"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                changed_paths: list[str] = []
                for line in result.stdout.splitlines():
                    path = line[3:].strip() if len(line) >= 4 else ""
                    if not path:
                        continue
                    changed_paths.append(path)
                control_plane_dirty = any(
                    path == ".make-it-so" or path.startswith(".make-it-so/")
                    for path in changed_paths
                )
                dirty = any(
                    not (path == ".make-it-so" or path.startswith(".make-it-so/"))
                    for path in changed_paths
                )
            except (OSError, subprocess.TimeoutExpired):
                dirty = True
        return {
            "full_name": repo.full_name,
            "local_path": str(local_path),
            "exists": local_path.exists(),
            "dirty": dirty,
            "control_plane_dirty": control_plane_dirty,
            "default_branch": repo.default_branch,
            "operation_mode": repo.operation_mode.value,
            "completion_policy": repo.completion_policy.value,
            "allow_autonomous_merge": repo.allow_autonomous_merge,
            "milestone_approval": repo.milestone_approval.value,
            "state": state,
            "state_source": "workboard" if workboard is not None else "state_store",
            "course_key": course.key if course is not None else None,
            "course_status": course.status.value if course is not None else None,
            "workboard_status": workboard,
            "github_status": github,
            "orchestrator": repo.orchestrator or "direct",
            "orchestration_board": repo.orchestration_board,
            "worker_models": worker_models,
            "worker_runtimes": worker_runtimes,
            "schedule_enabled": repo.schedule_enabled,
            "notification_route": repo.notification.route,
            "onboarding": repo.onboarding.model_dump(mode="json"),
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

    def _registration_roots(self) -> list[Path]:
        roots: list[Path] = []
        for configured in self.config.repos:
            roots.append(configured.local_path.expanduser().parent)
        # Existing repository parents are the authoritative discovery hint. These
        # fallbacks keep first-repository registration useful on OpenClaw while
        # leaving explicit local paths available to other runtimes.
        roots.extend(
            [
                Path.home() / ".openclaw" / "workspace" / "make-it-so-managed",
                Path.home() / ".openclaw" / "workspace",
                Path.home() / "workspace",
                Path.home() / "repos",
            ]
        )
        unique: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            resolved = root.expanduser()
            key = str(resolved).lower()
            if key not in seen:
                seen.add(key)
                unique.append(resolved)
        return unique

    @staticmethod
    def _github_full_name_from_remote(remote: str) -> str | None:
        value = remote.strip()
        if not value:
            return None
        scp_match = re.fullmatch(r"git@github\.com:([^/\s]+/[^/\s]+?)(?:\.git)?", value, re.IGNORECASE)
        if scp_match:
            return scp_match.group(1)
        try:
            parsed = urlsplit(value)
        except ValueError:
            return None
        if parsed.hostname is None or parsed.hostname.lower() != "github.com":
            return None
        parts = parsed.path.removesuffix(".git").strip("/").split("/")
        if len(parts) != 2 or not all(parts):
            return None
        return f"{parts[0]}/{parts[1]}"

    @staticmethod
    def _git_text(path: Path, *args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(path), *args],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    def _local_clone_details(self, path: Path, expected_full_name: str | None = None) -> dict[str, Any]:
        expanded = path.expanduser()
        cloned = (expanded / ".git").exists()
        origin = self._git_text(expanded, "remote", "get-url", "origin") if cloned else None
        full_name = self._github_full_name_from_remote(origin or "")
        remote_matches = (
            full_name.lower() == expected_full_name.lower()
            if full_name is not None and expected_full_name is not None
            else None
        )
        branch = self._git_text(expanded, "branch", "--show-current") if cloned else None
        status = self._git_text(expanded, "status", "--porcelain") if cloned else None
        return {
            "path": str(expanded),
            "exists": expanded.exists(),
            "cloned": cloned,
            "remote_matches": remote_matches,
            "full_name": full_name,
            "branch": branch or None,
            "dirty": bool(status) if status is not None else None,
        }

    def _discover_local_repositories(self) -> tuple[list[dict[str, Any]], list[str]]:
        candidates: list[Path] = [repo.local_path.expanduser() for repo in self.config.repos]
        warnings: list[str] = []
        for root in self._registration_roots():
            if not root.is_dir():
                continue
            try:
                candidates.extend(path for path in root.iterdir() if path.is_dir() and (path / ".git").exists())
            except OSError as exc:
                warnings.append(f"Could not inspect {root}: {exc}")

        unique: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            try:
                key = str(candidate.resolve()).lower()
            except OSError:
                key = str(candidate).lower()
            if key not in seen and (candidate / ".git").exists():
                seen.add(key)
                unique.append(candidate)

        with ThreadPoolExecutor(max_workers=min(8, max(1, len(unique)))) as executor:
            inspected = list(executor.map(self._local_clone_details, unique))
        registered = {repo.full_name.lower() for repo in self.config.repos}
        clones = [
            {
                "full_name": detail["full_name"],
                "local_path": detail["path"],
                "branch": detail["branch"],
                "dirty": detail["dirty"],
                "registered": str(detail["full_name"]).lower() in registered,
            }
            for detail in inspected
            if detail["full_name"]
        ]
        clones.sort(key=lambda item: (str(item["full_name"]).lower(), str(item["local_path"]).lower()))
        return clones, warnings

    def _registration_options(self) -> dict[str, Any]:
        clones, warnings = self._discover_local_repositories()
        return {"local_clones": clones, "warnings": warnings}

    def _discover_local_repository(self, full_name: str) -> tuple[Path, dict[str, Any]]:
        repository_name = full_name.split("/", 1)[1]
        candidates = [root / repository_name for root in self._registration_roots()]
        inspected = [(path, self._local_clone_details(path, full_name)) for path in candidates if (path / ".git").exists()]
        selected_pair = next((item for item in inspected if item[1]["remote_matches"] is True), None)
        # Repositories created before remote verification may not have an origin.
        selected_pair = selected_pair or next((item for item in inspected if item[1]["remote_matches"] is None), None)
        if selected_pair:
            return selected_pair
        selected = candidates[0]
        return selected, self._local_clone_details(selected, full_name)

    def _register_repo(self, payload: dict[str, Any]) -> dict[str, Any]:
        full_name = _normalize_github_repository(str(payload.get("full_name") or ""))
        if not full_name:
            raise SidecarError("repo.register requires full_name")
        existing_repo = next((item for item in self.config.repos if item.full_name == full_name), None)
        local_path_value = str(payload.get("local_path") or "").strip()
        if local_path_value:
            local_path = Path(local_path_value).expanduser()
            local_discovery = self._local_clone_details(local_path, full_name)
            local_discovery["source"] = "explicit"
        else:
            local_path, local_discovery = self._discover_local_repository(full_name)
            local_discovery["source"] = "repository_roots"
        planning = _planning_document_discovery(local_path)
        requested_planning_doc = str(payload.get("planning_doc") or "").strip()
        if requested_planning_doc:
            planning = {
                **planning,
                "path": requested_planning_doc,
                "source": "explicit",
                "found": (local_path / requested_planning_doc).is_file(),
            }
        discovered_canonical = planning.get("canonical_docs")
        discovered_checks = planning.get("checks")
        canonical_values = cast(list[Any], discovered_canonical) if isinstance(discovered_canonical, list) else []
        check_values = cast(list[Any], discovered_checks) if isinstance(discovered_checks, list) else []
        canonical_docs = tuple(str(item) for item in _list_value(payload.get("canonical_docs"))) or tuple(
            str(item) for item in canonical_values
        )
        checks = tuple(str(item) for item in _list_value(payload.get("checks"))) or tuple(
            str(item) for item in check_values
        )
        raw_onboarding = {
            "phase": str(payload.get("phase") or "") or None,
            "goal": str(payload.get("goal") or "").strip() or None,
            "clone_allowed": payload.get("clone_allowed"),
            "planning_doc_choice": str(payload.get("planning_doc_choice") or "").strip() or None,
            "detected_surface": str(payload.get("detected_surface") or "") or None,
            "uat_required": bool(payload.get("uat_required", True)),
            "screenshots_required": bool(payload.get("screenshots_required", False)),
            "deployment_required": bool(payload.get("deployment_required", False)),
            "deployment_authority": str(payload.get("deployment_authority") or "always_ask"),
            "checkpoint_policy": str(payload.get("checkpoint_policy") or "major_changes"),
            "intelligence_level": str(payload.get("intelligence_level") or "balanced"),
        }
        try:
            onboarding = OnboardingPreferences.model_validate(raw_onboarding)
        except ValueError as exc:
            raise SidecarError(f"invalid onboarding choice: {exc}") from exc
        follow_up_reasons: list[str] = []
        if not bool(local_discovery["cloned"]):
            follow_up_reasons.append(
                f"No local clone was found at {local_discovery['path']}; Number One will confirm whether to clone it."
            )
        if not bool(planning.get("found")):
            follow_up_reasons.append(
                "No durable planning document was identified; Number One will ask you to confirm the plan source in chat."
            )
        follow_up_message = (
            f"Repository {full_name} is registered. "
            + (" ".join(follow_up_reasons) if follow_up_reasons else "The local clone and planning document were found.")
            + " Number One will follow up in chat before any work begins."
        )
        number_one_session_key = f"make-it-so:number-one:{full_name.replace('/', '-')}"
        notification_kind = str(payload.get("notification_kind") or "stdout")
        notification_executable = str(payload.get("notification_executable") or "").strip() or None
        repo = RepoConfig(
            full_name=full_name,
            local_path=local_path,
            default_branch=str(payload.get("default_branch") or "main"),
            planning_doc=str(planning.get("path") or _DEFAULT_PLANNING_DOC),
            canonical_docs=canonical_docs,
            checks=checks,
            docs_checks=tuple(str(item) for item in _list_value(payload.get("docs_checks"))),
            surfaces=frozenset(
                ApplicationSurface(str(item)) for item in _list_value(payload.get("surfaces"))
            ),
            operation_mode=OperationMode(str(payload.get("operation_mode") or "advisory")),
            completion_policy=CompletionPolicy(str(payload.get("completion_policy") or "owner_approval")),
            milestone_approval=MilestoneApprovalPolicy(
                str(payload.get("milestone_approval") or "mode_default")
            ),
            allow_autonomous_merge=bool(payload.get("allow_autonomous_merge", False)),
            notification=NotificationConfig(
                kind=notification_kind,
                route=str(payload.get("notification_route") or "notifications"),
                executable=notification_executable,
            ),
            onboarding=onboarding,
            orchestrator=_default_registration_orchestrator(
                self.config,
                payload.get("orchestrator"),
            ),
            orchestration_board=(str(payload.get("orchestration_board") or "").strip() or None),
        )
        configured_repos = tuple(
            repo if item.full_name == full_name else item
            for item in self.config.repos
        )
        if existing_repo is None:
            configured_repos = (*self.config.repos, repo)
        self._write_config(self.config.model_copy(update={"repos": configured_repos}))
        course_store = CourseStore(local_path)
        try:
            courses = course_store.list()
        except (CourseError, OSError, ValueError):
            courses = ()
        active_courses = tuple(
            course
            for course in courses
            if course.status not in {CourseStatus.COMPLETED}
        )
        course = active_courses[0] if active_courses else _provisional_takeover_course(
            full_name,
            onboarding,
            planning,
            courses,
        )
        if not active_courses:
            course_store.save(course)
        try:
            number_one_model = self.config.model_policy("openclaw").for_role("number_one").primary.model
        except (KeyError, ValueError):
            number_one_model = "codex/gpt-5.6-sol"
        self.state.save_number_one_session(
            full_name,
            course.key,
            number_one_session_key,
            "openclaw",
            number_one_model,
            course.plan_revision,
        )
        return {
            "repo": self._repo_status(repo),
            "status": "registered",
            "course_key": course.key,
            "course_created": not bool(active_courses),
            "discovery": {
                "local_clone": local_discovery,
                "planning_document": planning,
            },
            "follow_up_required": True,
            "follow_up_message": follow_up_message,
            "number_one_prompt": _number_one_registration_prompt(full_name, local_discovery, planning, onboarding),
            "number_one_session_key": number_one_session_key,
            "notification_route": repo.notification.route,
        }

    def _inspect_repository(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Inspect registration facts without writing config or starting an agent."""
        full_name = _normalize_github_repository(str(payload.get("full_name") or ""))
        local_path_value = str(payload.get("local_path") or "").strip()
        if local_path_value:
            local_path = Path(local_path_value).expanduser()
            local_discovery = self._local_clone_details(local_path, full_name)
            if not local_discovery["cloned"]:
                raise SidecarError("selected local_path is not a Git repository")
            if local_discovery["remote_matches"] is False:
                raise SidecarError("selected local_path does not match the requested GitHub repository")
            local_discovery["source"] = "explicit"
        else:
            local_path, local_discovery = self._discover_local_repository(full_name)
            local_discovery["source"] = "repository_roots"
        planning = _planning_document_discovery(local_path)
        git_state = {"branch": local_discovery.get("branch"), "dirty": local_discovery.get("dirty")}
        return {
            "status": "inspected",
            "full_name": full_name,
            "discovery": {
                "local_clone": local_discovery,
                "planning_document": planning,
                "git": git_state,
            },
            "mutation_started": False,
        }

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
        default_orchestrator = "openclaw-workers" if "openclaw-workers" in self.config.orchestrators else None
        repo = RepoConfig(
            full_name=full_name,
            local_path=local_path,
            default_branch=str(payload.get("default_branch") or "main"),
            planning_doc=str(payload.get("planning_doc") or "docs/IMPLEMENTATION_PLAN.md"),
            canonical_docs=tuple(str(item) for item in _list_value(payload.get("canonical_docs"))),
            checks=tuple(str(item) for item in _list_value(payload.get("checks"))),
            docs_checks=tuple(str(item) for item in _list_value(payload.get("docs_checks"))),
            surfaces=frozenset(
                ApplicationSurface(str(item)) for item in _list_value(payload.get("surfaces"))
            ),
            orchestrator=configured_orchestrator or default_orchestrator,
            orchestration_board=(str(payload.get("orchestration_board") or "").strip() or None),
            milestone_approval=MilestoneApprovalPolicy(
                str(payload.get("milestone_approval") or "mode_default")
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
            if key
            in {
                "local_path",
                "default_branch",
                "planning_doc",
                "operation_mode",
                "completion_policy",
                "milestone_approval",
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
            "number_one",
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
            usage = UsageConfig.model_validate({**self.config.usage.model_dump(mode="python"), **allowed})
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
        session = self.state.number_one_session(full_name, course.key)
        return {
            "repository": full_name,
            "course": course.model_dump(mode="json"),
            "readiness": report.model_dump(mode="json"),
            "number_one": session,
            "milestone_changes": self.state.milestone_changes(full_name, course.key),
            "milestone_reviews": self.state.milestone_reviews(full_name, course.key, limit=12),
        }

    def _course_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo, _store, course = self._course_context(payload)
        return self._course_payload(repo.full_name, course)

    def _course_milestone_evidence(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo, _store, course = self._course_context(payload)
        status = self._cached_workboard_status(repo)
        rows = [
            row
            for row in (status or {}).get("milestones", [])
            if str(row.get("course_key") or "") == course.key
        ]
        package_key = str(payload.get("work_package_key") or payload.get("package_key") or "").strip()
        if package_key:
            rows = [row for row in rows if str(row.get("work_package_key") or "") == package_key]
        return {
            "repository": repo.full_name,
            "course_key": course.key,
            "milestones": rows,
            "source": "workboard_mirror",
        }

    def _course_milestone_changes(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo, _store, course = self._course_context(payload)
        return {
            "repository": repo.full_name,
            "course_key": course.key,
            "plan_revision": course.plan_revision,
            "changes": self.state.milestone_changes(repo.full_name, course.key),
            "reviews": self.state.milestone_reviews(repo.full_name, course.key, limit=12),
        }

    def _propose_milestone_change(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo, _store, course = self._course_context(payload)
        raw_changes = payload.get("changes")
        if not isinstance(raw_changes, list) or not raw_changes:
            raise SidecarError("course.milestone_change_propose requires a non-empty changes list")
        try:
            raw_change_items = cast(list[Any], raw_changes)
            changes = tuple(MilestoneChangeRequest.model_validate(item) for item in raw_change_items)
            validate_milestone_changes(course, changes)
            now = datetime.now(UTC)
            proposal = MilestoneChangeProposal(
                proposal_id=str(payload.get("proposal_id") or uuid.uuid4()),
                repository=repo.full_name,
                course_key=course.key,
                base_revision=course.plan_revision,
                summary=str(payload.get("summary") or "Number One proposed a milestone change").strip(),
                reason=str(payload.get("reason") or "Course evidence requires a milestone correction").strip(),
                requested_by=str(payload.get("requested_by") or "owner").strip(),
                changes=changes,
                requires_owner_approval=bool(payload.get("requires_owner_approval", True)),
                impact=str(payload.get("impact") or "routine"),  # type: ignore[arg-type]
                created_at=now,
                updated_at=now,
            )
            self.state.save_milestone_change(proposal.model_dump(mode="json"))
        except (MilestoneError, ValueError) as exc:
            raise SidecarError(str(exc)) from exc
        return {"status": "proposed", "proposal": proposal.model_dump(mode="json"), **self._course_payload(repo.full_name, course)}

    def _approve_milestone_change(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo, store, course = self._course_context(payload)
        proposal_id = str(payload.get("proposal_id") or "").strip()
        approved_by = str(payload.get("approved_by") or "").strip()
        if not proposal_id or not approved_by:
            raise SidecarError("course.milestone_change_approve requires proposal_id and approved_by")
        raw = self.state.milestone_change(repo.full_name, proposal_id)
        if raw is None:
            raise SidecarError(f"milestone change proposal does not exist: {proposal_id}")
        try:
            proposal = MilestoneChangeProposal.model_validate(raw)
            if proposal.status != MilestoneChangeStatus.PROPOSED:
                raise MilestoneError(f"proposal {proposal_id} is {proposal.status.value}, not proposed")
            if proposal.base_revision != course.plan_revision:
                raise MilestoneError("milestone proposal is stale; the course plan revision changed")
            updated = apply_milestone_changes(course, proposal.changes)
            store.save(updated)
            now = datetime.now(UTC)
            applied = proposal.model_copy(
                update={
                    "status": MilestoneChangeStatus.APPLIED,
                    "approved_by": approved_by,
                    "approved_at": now,
                    "applied_at": now,
                    "updated_at": now,
                }
            )
            self.state.save_milestone_change(applied.model_dump(mode="json"))
        except (MilestoneError, ValueError) as exc:
            raise SidecarError(str(exc)) from exc
        return {"status": "applied", "proposal": applied.model_dump(mode="json"), **self._course_payload(repo.full_name, updated)}

    def _reject_milestone_change(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo, _store, course = self._course_context(payload)
        proposal_id = str(payload.get("proposal_id") or "").strip()
        if not proposal_id:
            raise SidecarError("course.milestone_change_reject requires proposal_id")
        raw = self.state.milestone_change(repo.full_name, proposal_id)
        if raw is None:
            raise SidecarError(f"milestone change proposal does not exist: {proposal_id}")
        try:
            proposal = MilestoneChangeProposal.model_validate(raw)
            if proposal.status != MilestoneChangeStatus.PROPOSED:
                raise MilestoneError(f"proposal {proposal_id} is {proposal.status.value}, not proposed")
            rejected = proposal.model_copy(
                update={"status": MilestoneChangeStatus.REJECTED, "updated_at": datetime.now(UTC)}
            )
            self.state.save_milestone_change(rejected.model_dump(mode="json"))
        except (MilestoneError, ValueError) as exc:
            raise SidecarError(str(exc)) from exc
        return {"status": "rejected", "proposal": rejected.model_dump(mode="json"), **self._course_payload(repo.full_name, course)}

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
            "make_it_so.cli",
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

    def _set_pending_readiness_question(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo, store, course = self._course_context(payload)
        requirement_key = str(payload.get("requirement_key") or "").strip()
        question = str(payload.get("question") or "").strip()
        if bool(requirement_key) != bool(question):
            raise SidecarError("course.pending_question requires both requirement_key and question, or neither")
        if requirement_key and requirement_key not in {item.key for item in course.readiness}:
            raise SidecarError(f"course has no readiness requirement: {requirement_key}")
        try:
            updated = Course.model_validate(
                {
                    **course.model_dump(mode="python"),
                    "pending_readiness_key": requirement_key or None,
                    "pending_readiness_question": question or None,
                }
            )
            store.save(updated)
        except (CourseError, ValueError) as exc:
            raise SidecarError(str(exc)) from exc
        return {
            "status": "pending" if requirement_key else "cleared",
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
            answer = str(payload.get("answer")) if payload.get("answer") is not None else None
            existing = next((item for item in course.readiness if item.key == requirement_key), None)
            if (
                bool(payload.get("append_answer"))
                and answer
                and existing is not None
                and existing.answer
                and answer.strip() != existing.answer.strip()
            ):
                answer = f"{existing.answer.rstrip()}\n\nFollow-up answer:\n{answer.strip()}"
            updated = self.interaction.resolve_requirement(
                course,
                requirement_key,
                status,
                answer,
                tuple(str(item) for item in evidence_items),
                verified_by=str(payload.get("verified_by") or "") or None,
                verified_at=(
                    datetime.fromisoformat(str(payload["verified_at"]))
                    if payload.get("verified_at")
                    else None
                ),
                verification_model=str(payload.get("verification_model") or "") or None,
            )
            if course.pending_readiness_key == requirement_key:
                updated = Course.model_validate(
                    {
                        **updated.model_dump(mode="python"),
                        "pending_readiness_key": None,
                        "pending_readiness_question": None,
                    }
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
        package_lines = (
            "\n".join(f"- `{package.key}`: {package.objective}" for package in course.work_packages)
            or "- The Number One will decompose the first implementation package after baseline review."
        )
        acceptance = (
            "\n".join(f"- {item}" for item in course.acceptance_criteria)
            or "- Define acceptance criteria during planning."
        )
        exit_criteria = (
            "\n".join(f"- {item}" for item in course.exit_criteria)
            or "- Define exit criteria during planning."
        )
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
        manifest_path.write_text(
            yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
        )

    def _ready_work(self, payload: dict[str, Any]) -> dict[str, Any]:
        repo, _store, course = self._course_context(payload)
        completed_value = payload.get("completed")
        completed_items = cast(list[Any], completed_value) if isinstance(completed_value, list) else []
        completed: set[str] = {str(item) for item in completed_items}
        # The course store is the durable source of truth. Callers may omit
        # completed package keys because the UI and Number One usually send only
        # the course identity. Treat terminal package statuses as completed so
        # dependent work becomes eligible after a prior package finishes.
        completed.update(
            item.key
            for item in course.work_packages
            if item.status == WorkPackageStatus.COMPLETE
        )
        return {
            "repository": repo.full_name,
            "course_key": course.key,
            "work_packages": [
                item.model_dump(mode="json") for item in eligible_work_packages(course, completed)
            ],
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
        # The scheduler must outlive the longest worker it can dispatch. The
        # margin covers proof persistence and the final notification.
        timeout_seconds = self._one_shot_timeout_seconds()
        return {
            "source_of_truth": "openclaw_gateway_cron",
            "jobs": [
                {
                    "name": "make-it-so-reconcile",
                    "every": self.config.schedules.reconcile_every,
                    "kind": "reconcile",
                    "command": ["python", "-m", "make_it_so.sidecar", "--once", "reconcile", "--background"],
                    "timeout_seconds": timeout_seconds,
                },
                {
                    "name": "make-it-so-course-review",
                    "every": self.config.schedules.review_every,
                    "kind": "review",
                    "command": ["python", "-m", "make_it_so.sidecar", "--once", "review", "--background"],
                    "timeout_seconds": timeout_seconds,
                },
            ],
            "install_requires_operator_action": True,
            "repository_enablement": {repo.full_name: repo.schedule_enabled for repo in self.config.repos},
        }

    def _configure_schedules(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {key: payload[key] for key in ("reconcile_every", "review_every") if key in payload}
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

    def _start_once(self, kind: str, *, force_replan: bool = False) -> dict[str, Any]:
        if kind not in {"reconcile", "review"}:
            raise SidecarError(f"unsupported one-shot kind: {kind}")
        return self._spawn_background_once(kind, force_replan=force_replan)

    def _run_once(self, kind: str, *, force_replan: bool = False) -> dict[str, Any]:
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
                "make_it_so",
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
                if force_replan:
                    command.append("--force-replan")
            try:
                completed = subprocess.run(
                    command,
                    cwd=repo.local_path if repo.local_path.is_dir() else None,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self._one_shot_timeout_seconds(),
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

    def _one_shot_timeout_seconds(self) -> int:
        """Allow the sidecar to supervise a full worker lease plus cleanup."""
        worker_runtime = max(
            (
                int(getattr(orchestrator, "max_runtime_seconds", 3600))
                for orchestrator in self.config.orchestrators.values()
            ),
            default=3600,
        )
        return worker_runtime + SIDECAR_TIMEOUT_SAFETY_SECONDS

    def _spawn_background_once(self, kind: str, *, force_replan: bool = False) -> dict[str, Any]:
        """Detach scheduled work from OpenClaw's short command supervisor."""
        log_dir = self.config_path.parent / "run-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"sidecar-{kind}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.log"
        handle = log_path.open("a", encoding="utf-8")
        try:
            command = [
                    sys.executable,
                    "-m",
                    "make_it_so.sidecar",
                    "--once",
                    kind,
                    "--config",
                    str(self.config_path),
                ]
            if force_replan:
                command.append("--force-replan")
            child = subprocess.Popen(
                command,
                cwd=self.config_path.parent,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            handle.close()
        return {"status": "started", "kind": kind, "pid": child.pid, "log": str(log_path)}


def _list_value(value: Any) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="make-it-so-sidecar")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", choices=("reconcile", "review"))
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--force-replan", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    server = SidecarServer(args.config)
    if args.once:
        if args.background:
            print(json.dumps(server._spawn_background_once(args.once, force_replan=args.force_replan), default=str), flush=True)  # pyright: ignore[reportPrivateUsage]
            return 0
        params: dict[str, Any] = {"kind": args.once}
        if args.force_replan:
            params["force_replan"] = True
        result = server.request("run.once", params)
        print(json.dumps(result, default=str), flush=True)
        return 0 if result.get("status") == "completed" else 2
    output_lock = Lock()

    def handle_line(line: str) -> str:
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
        return json.dumps(response, default=str)

    def emit(future: Any) -> None:
        try:
            response = future.result()
        except Exception as exc:  # pragma: no cover - defensive executor boundary
            response = json.dumps({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": "SIDECAR_ERROR", "message": str(exc)[:2000]},
            })
        with output_lock:
            print(response, flush=True)

    # Long model-backed requests must not prevent health, answer, or dashboard
    # requests from being serviced by the same persistent sidecar. JSON-RPC
    # ids allow responses to arrive out of order and the TypeScript supervisor
    # already correlates them correctly.
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="sidecar-rpc") as executor:
        for line in sys.stdin:
            if not line.strip():
                continue
            executor.submit(handle_line, line).add_done_callback(emit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SidecarError", "SidecarServer", "main"]
