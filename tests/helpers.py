from __future__ import annotations

from pathlib import Path

from captains_chair.models import (
    AppConfig,
    CompletionPolicy,
    HarnessConfig,
    ModelPolicy,
    ModelTarget,
    NotificationConfig,
    OperationMode,
    RepoConfig,
    RoleModels,
    UsageConfig,
)


def model_policy() -> ModelPolicy:
    role = RoleModels(primary=ModelTarget(model="test-model"))
    final = RoleModels(primary=ModelTarget(model="test-final"), allow_fallback=False)
    return ModelPolicy(
        baseline=role,
        planner=role,
        coder=role,
        reviewer=role,
        final_reviewer=final,
    )


def repo_config(
    root: Path,
    *,
    mode: OperationMode = OperationMode.ADVISORY,
    completion: CompletionPolicy = CompletionPolicy.OWNER_APPROVAL,
    require_engaged_course: bool = False,
) -> RepoConfig:
    return RepoConfig(
        full_name="example/project",
        local_path=root,
        operation_mode=mode,
        completion_policy=completion,
        allow_autonomous_merge=completion == CompletionPolicy.AUTO_MERGE,
        planning_doc="ISSUES_EXECUTION_PLAN.md",
        notification=NotificationConfig(kind="stdout"),
        require_engaged_course=require_engaged_course,
    )


def app_config(root: Path, repo: RepoConfig) -> AppConfig:
    policy = model_policy()
    return AppConfig(
        version=1,
        state_dir=root / "state",
        artifact_dir=root / "artifacts",
        harnesses={"test": HarnessConfig(kind="codex", executable="codex", timeout_seconds=30)},
        models=policy,
        repos=(repo,),
        # Synthetic harnesses do not expose provider token telemetry. Production
        # configuration keeps the fail-closed default of block_on_unknown=True.
        usage=UsageConfig(block_on_unknown=False),
    )
