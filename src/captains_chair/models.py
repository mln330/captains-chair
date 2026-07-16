from __future__ import annotations

import enum
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _repository_relative_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(value)
    if (
        not normalized
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ".." in posix.parts
        or normalized == "."
    ):
        raise ValueError("repository document paths must be non-empty relative paths without parent traversal")
    return str(posix)


class OperationMode(enum.StrEnum):
    DISABLED = "disabled"
    ADVISORY = "advisory"
    SUPERVISED = "supervised"
    AUTONOMOUS = "autonomous"


class CompletionPolicy(enum.StrEnum):
    OWNER_APPROVAL = "owner_approval"
    CONTROL_PLANE_COMPLETE = "control_plane_complete"
    AUTO_MERGE = "auto_merge"


class CourseKind(enum.StrEnum):
    GREENFIELD = "greenfield"
    TAKEOVER = "takeover"
    FEATURE = "feature"


class CourseStatus(enum.StrEnum):
    DRAFT = "draft"
    READINESS_REVIEW = "readiness_review"
    AWAITING_APPROVAL = "awaiting_approval"
    ENGAGED = "engaged"
    PAUSED = "paused"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class RequirementStatus(enum.StrEnum):
    UNKNOWN = "unknown"
    ANSWERED = "answered"
    VERIFIED = "verified"
    WAIVED = "waived"
    BLOCKED = "blocked"


class ReadinessReviewVerdict(enum.StrEnum):
    READY = "ready"
    NEEDS_INPUT = "needs_input"


class ReadinessCheckStatus(enum.StrEnum):
    VERIFIED = "verified"
    NOT_APPLICABLE = "not_applicable"
    BLOCKED = "blocked"


class CheckpointKind(enum.StrEnum):
    COURSE_APPROVAL = "course_approval"
    ARCHITECTURE = "architecture"
    MILESTONE_DEMO = "milestone_demo"
    HUMAN_DECISION = "human_decision"
    RELEASE = "release"


class CheckpointStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    BLOCKED = "blocked"
    RESOLVED = "resolved"
    WAIVED = "waived"


class WorkPackageStatus(enum.StrEnum):
    PLANNED = "planned"
    READY = "ready"
    EXECUTING = "executing"
    REVIEWING = "reviewing"
    BLOCKED = "blocked"
    COMPLETE = "complete"


class ApplicationSurface(enum.StrEnum):
    WEB_UI = "web_ui"
    CLI = "cli"
    API = "api"
    LIBRARY = "library"
    DATA_PIPELINE = "data_pipeline"
    INFRASTRUCTURE_RELEASE = "infrastructure_release"
    CUSTOM = "custom"


class RepositoryProvisioningConfig(StrictModel):
    """Approval-gated settings for creating a greenfield GitHub repository."""

    enabled: bool = False
    visibility: Literal["private", "public"] = "private"
    description: str = ""


class RunState(enum.StrEnum):
    UNBASELINED = "unbaselined"
    BASELINE_REVIEW = "baseline_review"
    READY = "ready"
    PLANNING = "planning"
    EXECUTING = "executing"
    PR_OPEN = "pr_open"
    REVIEWING = "reviewing"
    REPAIRING = "repairing"
    COMPLETION_READY = "completion_ready"
    MERGED = "merged"
    POST_MERGE_VERIFICATION = "post_merge_verification"
    BLOCKED = "blocked"
    DEGRADED = "degraded"


class ActionKind(enum.StrEnum):
    REPORT_ONLY = "report_only"
    NO_ACTION = "no_action"
    UPDATE_PLAN = "update_plan"
    IMPLEMENT = "implement"
    REVIEW_PR = "review_pr"
    REPAIR_PR = "repair_pr"
    MERGE_PR = "merge_pr"
    CREATE_ISSUE = "create_issue"
    UPDATE_ISSUE = "update_issue"
    LABEL_ISSUE = "label_issue"
    RETARGET_ISSUE = "retarget_issue"
    CLOSE_ISSUE = "close_issue"
    MAINTENANCE = "maintenance"
    RELEASE = "release"
    PRODUCTION_DEPLOY = "production_deploy"
    SECRETS = "secrets"
    BILLING = "billing"
    DESTRUCTIVE = "destructive"
    FORCE_PUSH = "force_push"
    DELETE_BRANCH = "delete_branch"


class ActionScope(enum.StrEnum):
    MANAGED_REPO = "managed_repo"
    CONTROL_PLANE = "control_plane"


class ReviewVerdict(enum.StrEnum):
    REQUEST_CHANGES = "REQUEST_CHANGES"
    PASS = "PASS"


class CommentDisposition(enum.StrEnum):
    ADDRESS = "address"
    ALREADY_ADDRESSED = "already_addressed"
    REJECT_WITH_REASON = "reject_with_reason"
    FOLLOW_UP = "follow_up"
    NEEDS_HUMAN = "needs_human"


class FinalVerdict(enum.StrEnum):
    REQUEST_CHANGES = "REQUEST_CHANGES"
    READY_FOR_OWNER = "READY_FOR_OWNER"
    CONTROL_PLANE_COMPLETE = "CONTROL_PLANE_COMPLETE"
    AUTO_MERGE_ALLOWED = "AUTO_MERGE_ALLOWED"


class ReasoningEffort(enum.StrEnum):
    OFF = "off"
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class ModelExecutionMode(enum.StrEnum):
    STANDARD = "standard"
    PRO = "pro"


class ModelQualification(enum.StrEnum):
    UNTESTED = "untested"
    SHADOW = "shadow"
    CANARY = "canary"
    CERTIFIED = "certified"
    AUTONOMOUS = "autonomous"


class ModelCapability(StrictModel):
    structured_output: bool = False
    tool_access: bool = False
    token_telemetry: bool = False
    supported_efforts: frozenset[ReasoningEffort] = frozenset()
    supported_execution_modes: frozenset[ModelExecutionMode] = frozenset({ModelExecutionMode.STANDARD})


class ModelTarget(StrictModel):
    model: str = Field(min_length=1)
    agent: str | None = None
    runtime: str | None = None
    provider: str | None = None
    thinking: ReasoningEffort = ReasoningEffort.HIGH
    execution_mode: ModelExecutionMode = ModelExecutionMode.STANDARD
    qualification: ModelQualification = ModelQualification.UNTESTED
    capability: ModelCapability | None = None
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_total_tokens: int | None = Field(default=None, ge=1)
    local: bool = False
    autonomous_eligible: bool = False

    @model_validator(mode="after")
    def validate_route(self) -> ModelTarget:
        if (
            self.max_input_tokens is not None
            and self.max_output_tokens is not None
            and self.max_total_tokens is not None
            and self.max_input_tokens + self.max_output_tokens > self.max_total_tokens
        ):
            raise ValueError("max_total_tokens must cover configured input and output limits")
        if self.autonomous_eligible and self.qualification != ModelQualification.AUTONOMOUS:
            raise ValueError("autonomous_eligible requires qualification=autonomous")
        if self.capability is not None:
            if self.capability.supported_efforts and self.thinking not in self.capability.supported_efforts:
                raise ValueError("configured reasoning effort is not supported by this route")
            if self.execution_mode not in self.capability.supported_execution_modes:
                raise ValueError("configured execution mode is not supported by this route")
        return self


class ModelProfile(StrictModel):
    primary: ModelTarget
    fallbacks: tuple[ModelTarget, ...] = ()
    allow_fallback: bool = True
    max_attempts: int = Field(default=2, ge=1, le=10)
    escalation_conditions: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def validate_fallbacks(self) -> ModelProfile:
        if not self.allow_fallback and self.fallbacks:
            raise ValueError("fallback routes require allow_fallback=true")
        if self.max_attempts < 1 + len(self.fallbacks):
            raise ValueError("max_attempts must allow the primary and every configured fallback")
        return self


# Backwards-compatible public name while callers migrate from fixed roles to profiles.
RoleModels = ModelProfile


class ModelPolicy(StrictModel):
    baseline: ModelProfile
    planner: ModelProfile
    coder: ModelProfile
    reviewer: ModelProfile
    comment_adjudicator: ModelProfile | None = None
    tester: ModelProfile | None = None
    final_reviewer: ModelProfile
    ux_reviewer: ModelProfile | None = None
    profiles: dict[str, ModelProfile] = Field(default_factory=dict)

    def for_role(self, role: str) -> ModelProfile:
        if role in self.profiles:
            return self.profiles[role]
        if role in {"tester", "ux_reviewer"}:
            selected = getattr(self, role)
            return selected or self.coder
        selected = getattr(self, role, None)
        if not isinstance(selected, ModelProfile):
            raise ValueError(f"model policy has no configured role: {role}")
        return selected

    def effective_for_role(
        self,
        role: str,
        *overrides: Mapping[str, ModelProfile] | None,
    ) -> ModelProfile:
        """Resolve a role from global policy through increasingly specific layers."""
        selected = self.for_role(role)
        for override in overrides:
            if override is not None and role in override:
                selected = override[role]
        return selected

    @model_validator(mode="after")
    def final_review_must_not_downgrade(self) -> ModelPolicy:
        if self.final_reviewer.allow_fallback or self.final_reviewer.fallbacks:
            raise ValueError("final_reviewer must use exactly one model with fallback disabled")
        return self


class QAProfile(StrictModel):
    version: Literal[1] = 1
    key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    surfaces: frozenset[ApplicationSurface] = frozenset()
    checks: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    reviewer_role: str = "qa_assistant"
    enabled: bool = True

    @model_validator(mode="after")
    def require_surface_or_custom_check(self) -> QAProfile:
        if not self.surfaces and not self.checks:
            raise ValueError("QA profile requires at least one application surface or deterministic check")
        return self


class HarnessConfig(StrictModel):
    """Runtime-neutral harness connection settings.

    Built-in kinds are validated by the harness registry. Keeping the kind
    open here lets an installed adapter package add a new runtime without
    changing the core configuration model.
    """

    kind: str = Field(min_length=1)
    executable: str = Field(min_length=1)
    default_agent: str | None = None
    timeout_seconds: int = Field(default=3600, ge=30, le=14400)
    settings: dict[str, Any] = Field(default_factory=dict)


class WorkerAssignments(StrictModel):
    captain: str = Field(min_length=1)
    coder: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    tester: str = Field(min_length=1)
    ux_reviewer: str = Field(min_length=1)
    final_reviewer: str = Field(min_length=1)
    merger: str = Field(min_length=1)
    verifier: str = Field(min_length=1)

    @model_validator(mode="after")
    def roles_must_use_distinct_agents(self) -> WorkerAssignments:
        roles = {
            "captain": self.captain,
            "coder": self.coder,
            "reviewer": self.reviewer,
            "tester": self.tester,
            "ux_reviewer": self.ux_reviewer,
            "final_reviewer": self.final_reviewer,
            "merger": self.merger,
            "verifier": self.verifier,
        }
        by_agent: dict[str, list[str]] = {}
        for role, agent_id in roles.items():
            by_agent.setdefault(agent_id, []).append(role)
        duplicates = [
            f"{agent_id} ({', '.join(agent_roles)})"
            for agent_id, agent_roles in by_agent.items()
            if len(agent_roles) > 1
        ]
        if duplicates:
            raise ValueError("worker agent IDs must be unique across roles: " + "; ".join(duplicates))
        return self


class WorkerModelAssignments(StrictModel):
    captain: str = "codex/gpt-5.5"
    coder: str = "codex/gpt-5.3-codex-spark"
    reviewer: str = "codex/gpt-5.5"
    tester: str = "codex/gpt-5.3-codex-spark"
    ux_reviewer: str = "codex/gpt-5.3-codex-spark"
    final_reviewer: str = "codex/gpt-5.5"
    merger: str = "codex/gpt-5.5"
    verifier: str = "codex/gpt-5.5"


class WorkerOrchestrationConfig(StrictModel):
    """Runtime-neutral worker topology and bounded execution policy."""

    board_prefix: str = Field(default="captains-chair", min_length=1)
    workers: WorkerAssignments
    worker_models: WorkerModelAssignments = WorkerModelAssignments()
    max_runtime_seconds: int = Field(default=3600, ge=60, le=14400)
    max_retries: int = Field(default=2, ge=0, le=10)
    require_live_completion_validation: bool = True


class OpenClawWorkboardConfig(WorkerOrchestrationConfig):
    kind: Literal["openclaw_workboard"] = "openclaw_workboard"
    executable: str = "openclaw"
    captains_chair_command: tuple[str, ...] = ("captains_chair",)
    auth_source_agent: str | None = None
    dispatch_timeout_seconds: int = Field(default=120, ge=10, le=900)
    session_limit: int = Field(default=1000, ge=1, le=10000)

    @model_validator(mode="after")
    def command_must_not_be_empty(self) -> OpenClawWorkboardConfig:
        if not self.captains_chair_command or any(not item.strip() for item in self.captains_chair_command):
            raise ValueError("captains_chair_command must contain non-empty argv items")
        return self


class DirectOrchestratorConfig(WorkerOrchestrationConfig):
    """Durable board-free orchestration for Codex and portable runtimes."""

    kind: Literal["direct"] = "direct"
    database_path: Path
    worker_runtime: Literal["external", "openclaw", "codex"] = "external"
    executable: str | None = None
    lease_seconds: int = Field(default=3600, ge=30, le=14400)
    max_dispatch_workers: int = Field(default=1, ge=1, le=10)

    @model_validator(mode="after")
    def managed_runtime_requires_executable(self) -> DirectOrchestratorConfig:
        if self.worker_runtime != "external" and not (self.executable or "").strip():
            raise ValueError("managed direct worker runtimes require executable")
        return self


class ExternalWorkboardConfig(WorkerOrchestrationConfig):
    """Configuration envelope for an orchestrator supplied by an extension package."""

    kind: str = Field(min_length=1)
    executable: str = Field(min_length=1)
    settings: dict[str, Any] = Field(default_factory=dict)


OrchestratorConfig = OpenClawWorkboardConfig | DirectOrchestratorConfig | ExternalWorkboardConfig


class NotificationConfig(StrictModel):
    """Built-in delivery settings plus an extension-owned settings envelope."""

    kind: str = Field(default="stdout", min_length=1)
    route: str | None = None
    webhook_env: str | None = None
    executable: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_delivery(self) -> NotificationConfig:
        if self.kind == "openclaw_discord" and not (self.route and self.executable):
            raise ValueError("openclaw_discord requires route and executable")
        if self.kind == "discord_webhook" and not self.webhook_env:
            raise ValueError("discord_webhook requires webhook_env")
        return self


class UsageConfig(StrictModel):
    """Provider-reported token safeguards; missing telemetry remains unknown."""

    daily_token_limit: int | None = Field(default=None, ge=0)
    model_daily_token_limits: dict[str, int] = Field(default_factory=dict)
    block_on_unknown: bool = True
    allow_incomplete_telemetry: bool = False
    retention_days: int = Field(default=90, ge=1, le=3650)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_accounting(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        migrated = dict(cast(dict[str, Any], value))
        # Historical rate cards and synthetic budgets cannot be converted into
        # authoritative token limits. Accept and discard them during migration.
        migrated.pop("rates", None)
        migrated.pop("daily_budget_credits", None)
        return migrated

    @model_validator(mode="after")
    def validate_model_limits(self) -> UsageConfig:
        invalid = [model for model, limit in self.model_daily_token_limits.items() if not model or limit < 0]
        if invalid:
            raise ValueError(
                "model_daily_token_limits requires non-empty model names and non-negative limits"
            )
        return self


ALWAYS_APPROVAL_ACTIONS = frozenset(
    {
        ActionKind.MERGE_PR,
        ActionKind.RELEASE,
        ActionKind.PRODUCTION_DEPLOY,
        ActionKind.SECRETS,
        ActionKind.BILLING,
        ActionKind.DESTRUCTIVE,
        ActionKind.FORCE_PUSH,
        ActionKind.DELETE_BRANCH,
    }
)


class RepoConfig(StrictModel):
    full_name: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    local_path: Path
    default_branch: str = "main"
    provisioning: RepositoryProvisioningConfig = RepositoryProvisioningConfig()
    operation_mode: OperationMode = OperationMode.ADVISORY
    completion_policy: CompletionPolicy = CompletionPolicy.OWNER_APPROVAL
    allow_autonomous_merge: bool = False
    canonical_docs: tuple[str, ...] = ()
    planning_doc: str
    project_manifest: str = ".captains-chair/project.yaml"
    require_project_manifest: bool = False
    checks: tuple[str, ...] = ()
    docs_checks: tuple[str, ...] = ()
    ux_paths: tuple[str, ...] = ("frontend/", "*.tsx", "*.jsx", "*.css", "*.html")
    ux_enabled: bool = True
    surfaces: frozenset[ApplicationSurface] = frozenset()
    qa_profiles: tuple[QAProfile, ...] = ()
    notification: NotificationConfig = NotificationConfig()
    model_profiles: dict[str, ModelProfile] = Field(default_factory=dict)
    approval_whitelist: frozenset[ActionKind] = frozenset()
    max_parallel_prs: int = Field(default=1, ge=1, le=10)
    deploy_is_merge_gate: bool = False
    preserved_prs: tuple[int, ...] = ()
    require_engaged_course: bool = True
    orchestrator: str | None = None
    orchestration_board: str | None = None
    schedule_enabled: bool = True

    @field_validator("planning_doc", "project_manifest")
    @classmethod
    def validate_document_path(cls, value: str) -> str:
        return _repository_relative_path(value)

    @field_validator("canonical_docs")
    @classmethod
    def validate_canonical_document_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_repository_relative_path(value) for value in values)

    @model_validator(mode="after")
    def validate_policy(self) -> RepoConfig:
        if (
            self.operation_mode != OperationMode.DISABLED
            and self.completion_policy == CompletionPolicy.AUTO_MERGE
            and (self.operation_mode != OperationMode.AUTONOMOUS or not self.allow_autonomous_merge)
        ):
            raise ValueError("auto_merge requires autonomous mode and allow_autonomous_merge=true")
        return self


class ScheduleConfig(StrictModel):
    reconcile_every: str = Field(default="5m", pattern=r"^[1-9][0-9]*(s|m|h|d)$")
    review_every: str = Field(default="2h", pattern=r"^[1-9][0-9]*(s|m|h|d)$")


class AppConfig(StrictModel):
    version: Literal[1]
    state_dir: Path
    artifact_dir: Path
    harnesses: dict[str, HarnessConfig]
    orchestrators: dict[str, OrchestratorConfig] = Field(default_factory=dict)
    models: ModelPolicy
    harness_model_overrides: dict[str, ModelPolicy] = Field(default_factory=dict)
    usage: UsageConfig = UsageConfig()
    schedules: ScheduleConfig = ScheduleConfig()
    repos: tuple[RepoConfig, ...]

    @model_validator(mode="after")
    def unique_repositories(self) -> AppConfig:
        names = [repo.full_name.lower() for repo in self.repos]
        if len(names) != len(set(names)):
            raise ValueError("repository names must be unique")
        if self.usage.allow_incomplete_telemetry and any(
            repo.operation_mode == OperationMode.AUTONOMOUS for repo in self.repos
        ):
            raise ValueError("allow_incomplete_telemetry is only permitted before autonomous mode")
        missing = sorted(
            {
                repo.orchestrator
                for repo in self.repos
                if repo.orchestrator is not None and repo.orchestrator not in self.orchestrators
            }
        )
        if missing:
            raise ValueError(f"repositories reference unknown orchestrators: {missing}")
        return self

    def repo(self, full_name: str) -> RepoConfig:
        for repo in self.repos:
            if repo.full_name.lower() == full_name.lower():
                return repo
        raise KeyError(full_name)

    def model_policy(
        self,
        harness_name: str,
        *,
        repo_profiles: Mapping[str, ModelProfile] | None = None,
        course_profiles: Mapping[str, ModelProfile] | None = None,
        work_package_profiles: Mapping[str, ModelProfile] | None = None,
        stage_profiles: Mapping[str, ModelProfile] | None = None,
    ) -> ModelPolicy:
        """Resolve global, runtime, repository, course, package, and stage layers."""
        base = self.harness_model_overrides.get(harness_name, self.models)
        merged = dict(base.profiles)
        for layer in (repo_profiles, course_profiles, work_package_profiles, stage_profiles):
            if layer:
                merged.update(layer)
        return base.model_copy(update={"profiles": merged})


class ProjectManifest(StrictModel):
    version: Literal[1]
    goal: str = Field(min_length=10)
    canonical_docs: tuple[str, ...]
    planning_doc: str
    checks: tuple[str, ...]
    required_check_names: tuple[str, ...] = ()
    later_phase: tuple[str, ...] = ()
    surfaces: frozenset[ApplicationSurface] = frozenset()
    qa_profiles: tuple[QAProfile, ...] = ()

    @field_validator("planning_doc")
    @classmethod
    def validate_planning_document_path(cls, value: str) -> str:
        return _repository_relative_path(value)

    @field_validator("canonical_docs")
    @classmethod
    def validate_manifest_document_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_repository_relative_path(value) for value in values)


class ReadinessRequirement(StrictModel):
    version: Literal[1] = 1
    key: str = Field(min_length=1)
    category: str = Field(min_length=1)
    question: str = Field(min_length=1)
    required: bool = True
    status: RequirementStatus = RequirementStatus.UNKNOWN
    answer: str | None = None
    evidence: tuple[str, ...] = ()
    owner_decision_required: bool = False
    verified_by: str | None = None
    verified_at: datetime | None = None
    verification_model: str | None = None

    @model_validator(mode="after")
    def validate_resolution(self) -> ReadinessRequirement:
        if self.status in {RequirementStatus.ANSWERED, RequirementStatus.VERIFIED} and not (
            self.answer and self.answer.strip()
        ):
            raise ValueError(f"readiness requirement {self.key!r} needs an answer before it is resolved")
        if self.status == RequirementStatus.WAIVED and self.required and not self.owner_decision_required:
            raise ValueError(
                f"required readiness requirement {self.key!r} can only be waived with owner decision"
            )
        if self.status == RequirementStatus.VERIFIED and (
            not self.evidence
            or not self.verified_by
            or self.verified_at is None
            or not self.verification_model
        ):
            raise ValueError(
                f"readiness requirement {self.key!r} needs independent verification provenance"
            )
        return self


class ReadinessReviewCheck(StrictModel):
    version: Literal[1] = 1
    category: str = Field(min_length=1)
    status: ReadinessCheckStatus
    finding: str = Field(min_length=1)
    evidence: tuple[str, ...] = ()


class ReadinessReviewRecord(StrictModel):
    version: Literal[1] = 1
    verdict: ReadinessReviewVerdict
    summary: str = Field(min_length=1)
    input_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning: ReasoningEffort
    prompt_version: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    reviewed_at: datetime
    checks: tuple[ReadinessReviewCheck, ...]
    next_questions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_checks(self) -> ReadinessReviewRecord:
        categories = [check.category for check in self.checks]
        if len(categories) != len(set(categories)):
            raise ValueError("readiness review check categories must be unique")
        if self.verdict == ReadinessReviewVerdict.READY and any(
            check.status == ReadinessCheckStatus.BLOCKED for check in self.checks
        ):
            raise ValueError("ready readiness review cannot contain blocked checks")
        return self


class Checkpoint(StrictModel):
    version: Literal[1] = 1
    key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    kind: CheckpointKind = CheckpointKind.HUMAN_DECISION
    reason: str = Field(min_length=1)
    blocks_work_packages: tuple[str, ...] = ()
    status: CheckpointStatus = CheckpointStatus.PENDING
    required: bool = True
    owner_decision_required: bool = True
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    evidence: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_owner_policy(self) -> Checkpoint:
        if self.kind in {CheckpointKind.COURSE_APPROVAL, CheckpointKind.HUMAN_DECISION} and not self.owner_decision_required:
            raise ValueError(f"checkpoint {self.key!r} requires owner_decision_required=true")
        if (
            self.status in {CheckpointStatus.APPROVED, CheckpointStatus.RESOLVED, CheckpointStatus.WAIVED}
            and self.owner_decision_required
            and (not self.resolved_by or self.resolved_at is None)
        ):
            raise ValueError(f"checkpoint {self.key!r} needs resolution provenance")
        return self


class WorkPackage(StrictModel):
    version: Literal[1] = 1
    key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    dependencies: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    qa_profiles: tuple[str, ...] = ()
    checkpoint_keys: tuple[str, ...] = ()
    model_profiles: dict[str, ModelProfile] = Field(default_factory=dict)
    risk: Literal["low", "medium", "high", "critical"] = "medium"
    status: WorkPackageStatus = WorkPackageStatus.PLANNED
    source_issue: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_dependencies(self) -> WorkPackage:
        if self.key in self.dependencies:
            raise ValueError(f"work package {self.key!r} cannot depend on itself")
        if len(self.dependencies) != len(set(self.dependencies)):
            raise ValueError(f"work package {self.key!r} has duplicate dependencies")
        return self


class Course(StrictModel):
    version: Literal[1] = 1
    key: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    repository: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    kind: CourseKind
    title: str = Field(min_length=1)
    goal: str = Field(min_length=10)
    non_goals: tuple[str, ...] = ()
    scope: tuple[str, ...] = ()
    users: tuple[str, ...] = ()
    architecture_constraints: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    exit_criteria: tuple[str, ...] = ()
    readiness: tuple[ReadinessRequirement, ...] = ()
    readiness_review: ReadinessReviewRecord | None = None
    work_packages: tuple[WorkPackage, ...] = ()
    checkpoints: tuple[Checkpoint, ...] = ()
    qa_profiles: tuple[QAProfile, ...] = ()
    model_profiles: dict[str, ModelProfile] = Field(default_factory=dict)
    status: CourseStatus = CourseStatus.DRAFT
    approved_by: str | None = None
    approved_at: datetime | None = None

    @model_validator(mode="after")
    def validate_graph(self) -> Course:
        package_keys = {package.key for package in self.work_packages}
        if len(package_keys) != len(self.work_packages):
            raise ValueError("course work package keys must be unique")
        checkpoint_keys = {checkpoint.key for checkpoint in self.checkpoints}
        if len(checkpoint_keys) != len(self.checkpoints):
            raise ValueError("course checkpoint keys must be unique")
        qa_keys = {profile.key for profile in self.qa_profiles}
        if len(qa_keys) != len(self.qa_profiles):
            raise ValueError("course QA profile keys must be unique")
        requirement_keys = {item.key for item in self.readiness}
        if len(requirement_keys) != len(self.readiness):
            raise ValueError("course readiness keys must be unique")
        for package in self.work_packages:
            missing = sorted(set(package.dependencies) - package_keys)
            if missing:
                raise ValueError(f"work package {package.key!r} has unknown dependencies: {missing}")
            missing_checkpoints = sorted(set(package.checkpoint_keys) - checkpoint_keys)
            if missing_checkpoints:
                raise ValueError(
                    f"work package {package.key!r} has unknown checkpoints: {missing_checkpoints}"
                )
            missing_profiles = sorted(set(package.qa_profiles) - qa_keys)
            if missing_profiles:
                raise ValueError(f"work package {package.key!r} has unknown QA profiles: {missing_profiles}")
        for checkpoint in self.checkpoints:
            missing = sorted(set(checkpoint.blocks_work_packages) - package_keys)
            if missing:
                raise ValueError(f"checkpoint {checkpoint.key!r} has unknown work packages: {missing}")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(package_key: str) -> None:
            if package_key in visiting:
                raise ValueError(f"course work package dependencies contain a cycle at {package_key!r}")
            if package_key in visited:
                return
            visiting.add(package_key)
            package = next(item for item in self.work_packages if item.key == package_key)
            for dependency in package.dependencies:
                visit(dependency)
            visiting.remove(package_key)
            visited.add(package_key)

        for package in self.work_packages:
            visit(package.key)
        if self.status == CourseStatus.ENGAGED and not (self.approved_by and self.approved_at):
            raise ValueError("engaged course requires approval provenance")
        return self


class WorkMapping(StrictModel):
    version: Literal[1] = 1
    repository: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    course_key: str = Field(min_length=1)
    work_package_key: str = Field(min_length=1)
    tracker_kind: str = Field(min_length=1)
    external_id: str = Field(min_length=1)


class TokenUsageRecord(StrictModel):
    version: Literal[1] = 1
    record_id: str = Field(min_length=1)
    repository: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    course_key: str | None = None
    work_package_key: str | None = None
    stage: str = Field(min_length=1)
    role: str = Field(min_length=1)
    runtime: str = Field(min_length=1)
    requested_model: str = Field(min_length=1)
    resolved_model: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    telemetry_status: Literal["complete", "partial", "unknown"] = "unknown"
    success: bool = False
    fallback: bool = False
    duration_ms: int = Field(default=0, ge=0)


class PlanDecision(StrictModel):
    action: ActionKind
    scope: ActionScope = ActionScope.MANAGED_REPO
    summary: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    work_item_id: str | None = None
    course_key: str | None = None
    work_package_key: str | None = None
    target_pr: int | None = Field(default=None, ge=1)
    target_issue: int | None = Field(default=None, ge=1)
    issue_title: str | None = None
    issue_body: str | None = None
    issue_labels: tuple[str, ...] = ()
    issue_assignees: tuple[str, ...] = ()
    issue_milestone: str | None = None
    acceptance_criteria: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    requires_owner_approval: bool = False
    owner_blocker: str | None = None

    @model_validator(mode="after")
    def validate_owner_blocker(self) -> PlanDecision:
        if self.owner_blocker is None:
            return self
        normalized = self.owner_blocker.strip().upper()
        prefixes = (
            "USER_SECRET:",
            "GOAL_DIVERGENCE:",
            "EXTERNAL_ACCESS:",
            "HIGH_RISK_DECISION:",
        )
        if not any(normalized.startswith(prefix) for prefix in prefixes):
            raise ValueError(
                "owner_blocker must begin with USER_SECRET:, GOAL_DIVERGENCE:, "
                "EXTERNAL_ACCESS:, or HIGH_RISK_DECISION:"
            )
        if not self.requires_owner_approval:
            raise ValueError("owner_blocker requires requires_owner_approval=true")
        return self

    @model_validator(mode="after")
    def validate_issue_mutation(self) -> PlanDecision:
        if self.action == ActionKind.LABEL_ISSUE:
            if self.target_issue is None:
                raise ValueError("label_issue requires target_issue")
            if not self.issue_labels:
                raise ValueError("label_issue requires at least one issue_labels entry")
        if self.action == ActionKind.RETARGET_ISSUE:
            if self.target_issue is None:
                raise ValueError("retarget_issue requires target_issue")
            if self.issue_milestone is None and not self.issue_assignees:
                raise ValueError(
                    "retarget_issue requires issue_milestone or at least one issue_assignees entry"
                )
        return self


class Finding(StrictModel):
    priority: Literal["P0", "P1", "P2", "P3"]
    title: str
    detail: str
    path: str | None = None
    line: int | None = Field(default=None, ge=1)


class IndependentReview(StrictModel):
    verdict: ReviewVerdict
    summary: str
    findings: tuple[Finding, ...] = ()
    tests_assessed: tuple[str, ...] = ()
    residual_risks: tuple[str, ...] = ()


class ReviewCommentDecision(StrictModel):
    thread_id: str = Field(min_length=1)
    disposition: CommentDisposition
    rationale: str = Field(min_length=1)
    finding: Finding | None = None


class CommentTriage(StrictModel):
    head_sha: str = Field(min_length=1)
    verdict: ReviewVerdict
    summary: str = Field(min_length=1)
    decisions: tuple[ReviewCommentDecision, ...] = ()
    accepted_findings: tuple[Finding, ...] = ()
    owner_decisions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_dispositions(self) -> CommentTriage:
        addressable = {
            CommentDisposition.ADDRESS,
            CommentDisposition.FOLLOW_UP,
        }
        decision_dispositions = {item.disposition for item in self.decisions}
        if self.verdict == ReviewVerdict.REQUEST_CHANGES and not (
            self.accepted_findings or decision_dispositions.intersection(addressable)
        ):
            raise ValueError("comment triage requesting changes needs an actionable finding")
        return self


class UXReview(StrictModel):
    verdict: ReviewVerdict
    summary: str
    findings: tuple[Finding, ...] = ()
    flows_tested: tuple[str, ...] = ()
    screenshots: tuple[str, ...] = ()
    contrast_passed: bool
    functionality_passed: bool
    cohesion_passed: bool
    residual_risks: tuple[str, ...] = ()


class FinalReview(StrictModel):
    verdict: FinalVerdict
    summary: str
    scope_match: bool
    checks_green: bool
    unresolved_threads: int = Field(ge=0)
    residual_risks: tuple[str, ...] = ()
    owner_blocker: str | None = None


class BaselineAnalysis(StrictModel):
    summary: str
    implementation_status: tuple[str, ...]
    intended_divergences: tuple[str, ...] = ()
    unintended_divergences: tuple[str, ...] = ()
    gaps: tuple[str, ...]
    next_work_items: tuple[str, ...]
    owner_decisions: tuple[str, ...] = ()


class WorkerResult(StrictModel):
    summary: str
    changed_files: tuple[str, ...] = ()
    checks_run: tuple[str, ...] = ()
    blocked: bool = False
    blocker: str | None = None


class HarnessHealth(StrictModel):
    status: Literal["ok"]
    message: str


class ModelAttempt(StrictModel):
    model: str
    reported_model: str | None = None
    agent: str | None = None
    session_id: str | None = None
    success: bool
    duration_ms: int = Field(ge=0)
    error: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    prompt_bytes: int = Field(default=0, ge=0)
    response_bytes: int = Field(default=0, ge=0)
    usage_source: str | None = None


class ModelUsage(StrictModel):
    reported_model: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    prompt_bytes: int = Field(default=0, ge=0)
    response_bytes: int = Field(default=0, ge=0)
    source: str | None = None


class HarnessInvocation(StrictModel):
    payload: dict[str, Any]
    usage: ModelUsage = Field(default_factory=ModelUsage)


class HarnessResult(StrictModel):
    role: str
    output: dict[str, Any]
    attempts: tuple[ModelAttempt, ...]
    resolved_model: str
    session_id: str


class CheckResult(StrictModel):
    name: str
    status: str
    conclusion: str | None = None
    url: HttpUrl | None = None


class PullRequestGate(StrictModel):
    number: int
    head_sha: str
    mergeable: bool
    merge_state: str
    draft: bool
    checks_green: bool
    required_checks: tuple[CheckResult, ...]
    unresolved_threads: int
    review_head_sha: str | None = None


class EventRecord(StrictModel):
    event_id: str
    repo: str
    run_id: str
    state: RunState
    event_type: str
    summary: str
    reason: str
    fingerprint: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
