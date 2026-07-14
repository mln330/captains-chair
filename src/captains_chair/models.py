from __future__ import annotations

import enum
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OperationMode(enum.StrEnum):
    DISABLED = "disabled"
    ADVISORY = "advisory"
    SUPERVISED = "supervised"
    AUTONOMOUS = "autonomous"


class CompletionPolicy(enum.StrEnum):
    OWNER_APPROVAL = "owner_approval"
    CONTROL_PLANE_COMPLETE = "control_plane_complete"
    AUTO_MERGE = "auto_merge"


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


class FinalVerdict(enum.StrEnum):
    REQUEST_CHANGES = "REQUEST_CHANGES"
    READY_FOR_OWNER = "READY_FOR_OWNER"
    CONTROL_PLANE_COMPLETE = "CONTROL_PLANE_COMPLETE"
    AUTO_MERGE_ALLOWED = "AUTO_MERGE_ALLOWED"


class ModelTarget(StrictModel):
    model: str = Field(min_length=1)
    agent: str | None = None
    thinking: Literal["off", "low", "medium", "high", "xhigh"] = "high"


class RoleModels(StrictModel):
    primary: ModelTarget
    fallbacks: tuple[ModelTarget, ...] = ()
    allow_fallback: bool = True


class ModelPolicy(StrictModel):
    baseline: RoleModels
    planner: RoleModels
    coder: RoleModels
    reviewer: RoleModels
    tester: RoleModels | None = None
    final_reviewer: RoleModels
    ux_reviewer: RoleModels | None = None

    def for_role(self, role: str) -> RoleModels:
        if role in {"tester", "ux_reviewer"}:
            selected = getattr(self, role)
            return selected or self.coder
        selected = getattr(self, role, None)
        if not isinstance(selected, RoleModels):
            raise ValueError(f"model policy has no configured role: {role}")
        return selected

    @model_validator(mode="after")
    def final_review_must_not_downgrade(self) -> ModelPolicy:
        if self.final_reviewer.allow_fallback or self.final_reviewer.fallbacks:
            raise ValueError("final_reviewer must use exactly one model with fallback disabled")
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
    coder: str = "codex/gpt-5.3-codex"
    reviewer: str = "codex/gpt-5.5"
    tester: str = "codex/gpt-5.3-codex"
    ux_reviewer: str = "codex/gpt-5.3-codex"
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
        if not self.captains_chair_command or any(
            not item.strip() for item in self.captains_chair_command
        ):
            raise ValueError("captains_chair_command must contain non-empty argv items")
        return self


class HermesWorkboardConfig(WorkerOrchestrationConfig):
    """Reserved configuration shape for a future Hermes task/session adapter."""

    kind: Literal["hermes_workboard"] = "hermes_workboard"
    executable: str = "hermes"


class CodexWorkboardConfig(WorkerOrchestrationConfig):
    """Reserved configuration shape for a future standalone Codex queue adapter."""

    kind: Literal["codex_workboard"] = "codex_workboard"
    executable: str = "codex"


class ExternalWorkboardConfig(WorkerOrchestrationConfig):
    """Configuration envelope for a queue adapter supplied by an extension package."""

    kind: str = Field(min_length=1)
    executable: str = Field(min_length=1)
    settings: dict[str, Any] = Field(default_factory=dict)


OrchestratorConfig = (
    OpenClawWorkboardConfig
    | HermesWorkboardConfig
    | CodexWorkboardConfig
    | ExternalWorkboardConfig
)


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


class UsageRate(StrictModel):
    """Optional ChatGPT/Codex credit rates, expressed per million tokens."""

    input_credits_per_million: float = Field(default=0, ge=0)
    cached_input_credits_per_million: float = Field(default=0, ge=0)
    output_credits_per_million: float = Field(default=0, ge=0)


class UsageConfig(StrictModel):
    """Accounting is best-effort; unknown provider telemetry stays unknown."""

    rates: dict[str, UsageRate] = Field(default_factory=dict)
    daily_budget_credits: float | None = Field(default=None, ge=0)
    block_on_unknown: bool = True
    allow_incomplete_telemetry: bool = False
    retention_days: int = Field(default=90, ge=1, le=3650)


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
    notification: NotificationConfig = NotificationConfig()
    approval_whitelist: frozenset[ActionKind] = frozenset()
    max_parallel_prs: int = Field(default=1, ge=1, le=10)
    deploy_is_merge_gate: bool = False
    preserved_prs: tuple[int, ...] = ()
    orchestrator: str | None = None
    orchestration_board: str | None = None

    @model_validator(mode="after")
    def validate_policy(self) -> RepoConfig:
        if self.operation_mode != OperationMode.DISABLED and self.completion_policy == CompletionPolicy.AUTO_MERGE and (
            self.operation_mode != OperationMode.AUTONOMOUS or not self.allow_autonomous_merge
        ):
            raise ValueError("auto_merge requires autonomous mode and allow_autonomous_merge=true")
        return self


class AppConfig(StrictModel):
    version: Literal[1]
    state_dir: Path
    artifact_dir: Path
    harnesses: dict[str, HarnessConfig]
    orchestrators: dict[str, OrchestratorConfig] = Field(default_factory=dict)
    models: ModelPolicy
    harness_model_overrides: dict[str, ModelPolicy] = Field(default_factory=dict)
    usage: UsageConfig = UsageConfig()
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

    def model_policy(self, harness_name: str) -> ModelPolicy:
        return self.harness_model_overrides.get(harness_name, self.models)


class ProjectManifest(StrictModel):
    version: Literal[1]
    goal: str = Field(min_length=10)
    canonical_docs: tuple[str, ...]
    planning_doc: str
    checks: tuple[str, ...]
    required_check_names: tuple[str, ...] = ()
    later_phase: tuple[str, ...] = ()


class PlanDecision(StrictModel):
    action: ActionKind
    scope: ActionScope = ActionScope.MANAGED_REPO
    summary: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    work_item_id: str | None = None
    target_pr: int | None = Field(default=None, ge=1)
    target_issue: int | None = Field(default=None, ge=1)
    issue_title: str | None = None
    issue_body: str | None = None
    issue_labels: tuple[str, ...] = ()
    issue_assignees: tuple[str, ...] = ()
    issue_milestone: str | None = None
    acceptance_criteria: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
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
