from __future__ import annotations

import enum
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from pydantic import Field

from captains_chair.models import (
    ActionKind,
    CompletionPolicy,
    PlanDecision,
    RepoConfig,
    StrictModel,
    WorkerAssignments,
)


class WorkStage(enum.StrEnum):
    CONTROL_PLANE_ACTION = "control_plane_action"
    IMPLEMENTATION = "implementation"
    REPAIR = "repair"
    REVIEW = "review"
    TEST = "test"
    UX_REVIEW = "ux_review"
    FINAL_REVIEW = "final_review"
    MERGE = "merge"
    POST_MERGE = "post_merge"


class QueueStatus(enum.StrEnum):
    TRIAGE = "triage"
    BACKLOG = "backlog"
    TODO = "todo"
    SCHEDULED = "scheduled"
    READY = "ready"
    RUNNING = "running"
    REVIEW = "review"
    BLOCKED = "blocked"
    DONE = "done"


class BlockerKind(enum.StrEnum):
    USER_SECRET = "user_secret"
    GOAL_DIVERGENCE = "goal_divergence"
    EXTERNAL_ACCESS = "external_access"
    HIGH_RISK_DECISION = "high_risk_decision"
    TECHNICAL = "technical"


USER_BLOCKER_PREFIXES: dict[str, BlockerKind] = {
    "USER_SECRET:": BlockerKind.USER_SECRET,
    "GOAL_DIVERGENCE:": BlockerKind.GOAL_DIVERGENCE,
    "EXTERNAL_ACCESS:": BlockerKind.EXTERNAL_ACCESS,
    "HIGH_RISK_DECISION:": BlockerKind.HIGH_RISK_DECISION,
}


def classify_blocker(reason: str) -> BlockerKind:
    normalized = reason.strip().upper()
    for prefix, kind in USER_BLOCKER_PREFIXES.items():
        if normalized.startswith(prefix):
            return kind
    return BlockerKind.TECHNICAL


class WorkspaceRef(StrictModel):
    kind: str
    path: Path | None = None
    branch: str | None = None
    push_branch: str | None = None


class QueueCard(StrictModel):
    id: str
    title: str
    notes: str | None = None
    status: QueueStatus
    priority: str = "normal"
    labels: tuple[str, ...] = ()
    agent_id: str | None = None
    source_url: str | None = None
    workspace: WorkspaceRef | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class QueueCardSpec(StrictModel):
    key: str
    title: str
    notes: str
    status: QueueStatus = QueueStatus.TODO
    priority: str = "normal"
    labels: tuple[str, ...] = ()
    agent_id: str | None = None
    source_url: str | None = None
    parents: tuple[str, ...] = ()
    workspace: WorkspaceRef | None = None
    max_runtime_seconds: int = 3600
    max_retries: int = 2


class WorkflowSpec(StrictModel):
    workflow_id: str
    repo: str
    board_id: str
    title: str
    summary: str
    source_url: str
    root: QueueCardSpec
    stages: tuple[QueueCardSpec, ...]


@dataclass(frozen=True)
class EnqueuedWorkflow:
    workflow_id: str
    board_id: str
    root_card_id: str
    stage_cards: dict[str, str]


@dataclass(frozen=True)
class ReconcileResult:
    board_id: str
    proof_retries: tuple[str, ...]
    protocol_retries: tuple[str, ...]
    repairs_created: tuple[str, ...]
    retried: tuple[str, ...]
    control_plane_recoveries: tuple[str, ...]
    unblocked: tuple[str, ...]
    user_blockers: tuple[str, ...]
    dispatch: dict[str, Any]
    cleaned_workspaces: tuple[str, ...] = ()
    workspace_cleanup_failures: tuple[str, ...] = ()
    recovery_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompletionValidation:
    allowed: bool
    reason: str


WorkspaceCleanup = Callable[[RepoConfig, WorkspaceRef], bool]


class CompletionValidator(Protocol):
    """Optional live evidence check for a completed final-review card."""

    def validate(
        self,
        repo: RepoConfig,
        card: QueueCard,
        workflow_cards: list[QueueCard],
    ) -> CompletionValidation: ...


@runtime_checkable
class WorkQueueAdapter(Protocol):
    def ensure_board(self, board_id: str, name: str, description: str, workspace: Path) -> None: ...

    def list_cards(self, board_id: str) -> list[QueueCard]: ...

    def create_card(self, board_id: str, spec: QueueCardSpec) -> QueueCard: ...

    def complete_card(
        self,
        card_id: str,
        *,
        summary: str,
        proof: tuple[dict[str, Any], ...] = (),
        created_card_ids: tuple[str, ...] = (),
    ) -> QueueCard: ...

    def unblock_card(self, card_id: str) -> QueueCard: ...

    def reclaim_card(self, card_id: str, *, status: QueueStatus, reason: str) -> QueueCard: ...

    def reassign_card(
        self,
        card_id: str,
        *,
        agent_id: str,
        status: QueueStatus,
        reset_failures: bool,
        reason: str,
    ) -> QueueCard: ...

    def comment(self, card_id: str, body: str) -> QueueCard: ...

    def dispatch(self, board_id: str) -> dict[str, Any]: ...

    def diagnostics(self) -> dict[str, Any]: ...


@runtime_checkable
class WorkerLifecycleAdapter(Protocol):
    """Runtime-neutral lifecycle operations for a claimed worker card."""

    def heartbeat_card(self, card_id: str, *, owner_id: str, token: str, note: str) -> QueueCard: ...

    def complete_claimed_card(
        self,
        card_id: str,
        *,
        owner_id: str,
        token: str,
        summary: str,
        proof: tuple[dict[str, Any], ...],
    ) -> QueueCard: ...

    def block_claimed_card(
        self,
        card_id: str,
        *,
        owner_id: str,
        token: str,
        reason: str,
    ) -> QueueCard: ...


class OrchestrationPolicy(Protocol):
    """Runtime-neutral worker topology and retry policy consumed by the core."""

    board_prefix: str
    workers: WorkerAssignments
    max_runtime_seconds: int
    max_retries: int
    require_live_completion_validation: bool


class WorkflowOrchestrator:
    def __init__(
        self,
        adapter: WorkQueueAdapter,
        config: OrchestrationPolicy,
        *,
        workspace_cleanup: WorkspaceCleanup | None = None,
        completion_validator: CompletionValidator | None = None,
    ) -> None:
        if config.require_live_completion_validation and completion_validator is None:
            raise ValueError(
                "live completion validation is required; provide a GitHub-backed CompletionValidator "
                "or explicitly disable it for a portable test boundary"
            )
        self.adapter = adapter
        self.config = config
        self.workspace_cleanup = workspace_cleanup
        self.completion_validator = completion_validator
        self._completion_validation_cache: dict[str, CompletionValidation] = {}

    def enqueue(
        self,
        repo: RepoConfig,
        decision: PlanDecision,
        action_id: str,
        *,
        workspace: WorkspaceRef | None = None,
    ) -> EnqueuedWorkflow:
        workflow = build_workflow(repo, decision, action_id, self.config, workspace=workspace)
        workspace_materialized = False
        try:
            self.adapter.ensure_board(
                workflow.board_id,
                repo.full_name.split("/", 1)[-1],
                f"Captain's Chair work for {repo.full_name}",
                repo.local_path,
            )
            root = self.adapter.create_card(workflow.board_id, workflow.root)
            cards_by_key = {workflow.root.key: root.id}
            direct_children: list[str] = []
            for stage in workflow.stages:
                resolved = stage.model_copy(
                    update={"parents": tuple(cards_by_key[parent] for parent in stage.parents)}
                )
                card = self.adapter.create_card(workflow.board_id, resolved)
                if workspace is not None:
                    workspace_materialized = True
                cards_by_key[stage.key] = card.id
                if workflow.root.key in stage.parents:
                    direct_children.append(card.id)
            self.adapter.complete_card(
                root.id,
                summary="CAPTAINS_CHAIR materialized the policy-gated worker workflow.",
                proof=(
                    {
                        "status": "passed",
                        "label": "GitHub work contract",
                        "url": workflow.source_url,
                        "note": workflow.summary,
                    },
                ),
                created_card_ids=tuple(direct_children),
            )
            return EnqueuedWorkflow(
                workflow_id=workflow.workflow_id,
                board_id=workflow.board_id,
                root_card_id=root.id,
                stage_cards={key: value for key, value in cards_by_key.items() if key != workflow.root.key},
            )
        except Exception:
            # A worktree allocated before queue materialization must not survive a
            # partial gateway failure. Preserve the original enqueue error.
            if not workspace_materialized and workspace is not None and self.workspace_cleanup is not None:
                with suppress(Exception):
                    self.workspace_cleanup(repo, workspace)
            raise

    def reconcile(
        self,
        repo: RepoConfig,
        *,
        dispatch: bool = True,
        dispatch_reason: str | None = None,
    ) -> ReconcileResult:
        self._completion_validation_cache = {}
        board_id = repo.orchestration_board or (
            f"{self.config.board_prefix}-{repo.full_name.replace('/', '-').lower()}"
        )
        cards = self.adapter.list_cards(board_id)
        recovery_warning_values: list[str] = []
        recover_ended_workers = getattr(self.adapter, "recover_ended_workers", None)
        if callable(recover_ended_workers):
            try:
                recover_ended_workers(board_id, cards)
            except Exception as exc:
                recovery_warning_values.append(
                    f"Worker recovery adapter failed: {str(exc)[:1000]}"
                )
            warnings = getattr(self.adapter, "recovery_warnings", None)
            if callable(warnings):
                try:
                    raw_warnings = warnings()
                    if isinstance(raw_warnings, (tuple, list)):
                        warning_values = cast(tuple[object, ...] | list[object], raw_warnings)
                        recovery_warning_values.extend(str(item)[:1000] for item in warning_values)
                except Exception as exc:
                    recovery_warning_values.append(
                        f"Worker recovery warning reporting failed: {str(exc)[:1000]}"
                    )
            cards = self.adapter.list_cards(board_id)
        recovery_warnings = tuple(recovery_warning_values)
        proof_retries: list[str] = []
        protocol_retries: list[str] = []
        repairs_created: list[str] = []
        retried: list[str] = []
        control_plane_recoveries: list[str] = []
        unblocked: list[str] = []
        user_blockers: list[str] = []

        for card in cards:
            if card.metadata.get("archivedAt"):
                continue
            stage = _card_stage(card)
            retry_with_proof = self._retry_with_passed_completion(repo, cards, card.id)
            if (
                card.status == QueueStatus.REVIEW
                and stage is not None
            ):
                if self._has_valid_completion(repo, card, cards):
                    self.adapter.complete_card(
                        card.id,
                        summary=_completion_summary(card),
                        proof=_passed_proof(card),
                    )
                elif retry_with_proof:
                    # The fresh retry owns the evidence; the original card remains the
                    # durable stage identity that downstream dependencies reference.
                    retry = next(
                        item
                        for item in cards
                        if _is_retry_for(item, card.id) and self._has_valid_completion(repo, item, cards)
                    )
                    self.adapter.complete_card(
                        card.id,
                        summary=_completion_summary(retry),
                        proof=_passed_proof(retry),
                    )
                else:
                    retry = _active_retry(cards, card.id)
                    if retry is not None:
                        if _protocol_retry_exhausted(retry, cards, self.config.max_retries):
                            recovery = self._create_control_plane_recovery(repo, retry, cards)
                            _append_unique(control_plane_recoveries, recovery.id)
                        else:
                            protocol_retries.append(retry.id)
                    elif _protocol_retry_exhausted(card, cards, self.config.max_retries):
                        recovery = self._create_control_plane_recovery(repo, card, cards)
                        _append_unique(control_plane_recoveries, recovery.id)
                    else:
                        retry = self._create_fresh_retry(repo, card, cards)
                        protocol_retries.append(retry.id)
                continue
            if (
                card.status == QueueStatus.DONE
                and stage is not None
                and not self._has_valid_completion(repo, card, cards)
            ):
                proof_retries.append(card.id)
                if not retry_with_proof:
                    retry = _active_retry(cards, card.id)
                    if retry is not None:
                        if _protocol_retry_exhausted(retry, cards, self.config.max_retries):
                            recovery = self._create_control_plane_recovery(repo, retry, cards)
                            _append_unique(control_plane_recoveries, recovery.id)
                        else:
                            protocol_retries.append(retry.id)
                    elif _protocol_retry_exhausted(card, cards, self.config.max_retries):
                        recovery = self._create_control_plane_recovery(repo, card, cards)
                        _append_unique(control_plane_recoveries, recovery.id)
                    else:
                        retry = self._create_fresh_retry(repo, card, cards)
                        protocol_retries.append(retry.id)
                continue
            if card.status != QueueStatus.BLOCKED:
                continue
            reason = _block_reason(card)
            if classify_blocker(reason) != BlockerKind.TECHNICAL:
                user_blockers.append(card.id)
                continue
            stage = _card_stage(card)
            if stage in {
                WorkStage.REVIEW,
                WorkStage.TEST,
                WorkStage.UX_REVIEW,
                WorkStage.FINAL_REVIEW,
            }:
                repair_label = _repair_label(card.id)
                repairs = [item for item in cards if _is_repair_for(item, card.id)]
                completed = next(
                    (
                        item
                        for item in repairs
                        if item.status == QueueStatus.DONE and self._has_valid_completion(repo, item, cards)
                    ),
                    None,
                )
                if completed:
                    retry_with_proof = self._retry_with_passed_completion(repo, cards, card.id)
                    if retry_with_proof:
                        retry = next(
                            item
                            for item in cards
                            if _is_retry_for(item, card.id) and self._has_valid_completion(repo, item, cards)
                        )
                        self.adapter.complete_card(
                            card.id,
                            summary=_completion_summary(retry),
                            proof=_passed_proof(retry),
                        )
                    else:
                        retry = _active_retry(cards, card.id)
                        if retry is not None:
                            if _protocol_retry_exhausted(retry, cards, self.config.max_retries):
                                recovery = self._create_control_plane_recovery(repo, retry, cards)
                                _append_unique(control_plane_recoveries, recovery.id)
                            else:
                                protocol_retries.append(retry.id)
                        elif _protocol_retry_exhausted(card, cards, self.config.max_retries):
                            recovery = self._create_control_plane_recovery(repo, card, cards)
                            _append_unique(control_plane_recoveries, recovery.id)
                        else:
                            retry = self._create_fresh_retry(repo, card, cards)
                            protocol_retries.append(retry.id)
                    continue
                if repairs:
                    repair = repairs[-1]
                    if _failure_count(repair) > _retry_limit(repair, self.config.max_retries):
                        recovery = self._create_control_plane_recovery(repo, repair, cards)
                        _append_unique(control_plane_recoveries, recovery.id)
                    else:
                        self._recover_card(repair, retried, control_plane_recoveries)
                    continue
                workflow_label_value = _card_workflow_label(card)
                repair_labels = [
                    "captains_chair",
                    f"repo:{repo.full_name.lower()}",
                    "stage:repair",
                    repair_label,
                ]
                if workflow_label_value is not None:
                    repair_labels.insert(2, workflow_label_value)
                repair = self.adapter.create_card(
                    board_id,
                    QueueCardSpec(
                        key=f"captains_chair:repair:{card.id}:{_failure_count(card)}",
                        title=f"Repair findings from {card.title}",
                        notes=(
                            f"Repository: {repo.full_name}\n"
                            f"Repair blocked card: {card.id}\n"
                            f"Failure evidence: {reason}\n\n"
                            "Address only the actionable findings on the current PR branch, run targeted checks, push the repair, "
                            "and complete this card with the new PR head and test proof. Never approve or merge your own repair."
                        ),
                        status=QueueStatus.READY,
                        priority="high",
                        labels=tuple(repair_labels),
                        agent_id=self.config.workers.coder,
                        source_url=card.source_url,
                        workspace=card.workspace,
                        max_runtime_seconds=self.config.max_runtime_seconds,
                        max_retries=self.config.max_retries,
                    ),
                )
                repairs_created.append(repair.id)
                continue
            if _failure_count(card) > _retry_limit(card, self.config.max_retries):
                recovery = self._create_control_plane_recovery(repo, card, cards)
                _append_unique(control_plane_recoveries, recovery.id)
            else:
                self._recover_card(card, retried, control_plane_recoveries)

        cleaned_workspaces, workspace_cleanup_failures = self._cleanup_completed_workflows(
            repo, board_id
        )

        dispatch_result: dict[str, Any]
        if dispatch:
            model_health = worker_model_health(self.adapter)
            if model_health.get("status") not in {"ok", "not_supported"}:
                dispatch_result = {
                    "status": "dispatch_suppressed",
                    "reason": "worker model health is not valid; no new sessions were started",
                    "promoted": [],
                    "count": 0,
                    "model_health": model_health,
                }
            else:
                dispatch_result = {
                    **self.adapter.dispatch(board_id),
                    "model_health": model_health,
                }
        else:
            dispatch_result = {
                "status": "dispatch_suppressed",
                "reason": dispatch_reason or "new worker sessions were suppressed by policy",
                "promoted": [],
                "count": 0,
            }
        return ReconcileResult(
            board_id=board_id,
            proof_retries=tuple(proof_retries),
            protocol_retries=tuple(protocol_retries),
            repairs_created=tuple(repairs_created),
            retried=tuple(retried),
            control_plane_recoveries=tuple(control_plane_recoveries),
            unblocked=tuple(unblocked),
            user_blockers=tuple(user_blockers),
            dispatch=dispatch_result,
            cleaned_workspaces=tuple(cleaned_workspaces),
            workspace_cleanup_failures=tuple(workspace_cleanup_failures),
            recovery_warnings=recovery_warnings,
        )

    def _cleanup_completed_workflows(
        self, repo: RepoConfig, board_id: str
    ) -> tuple[list[str], list[str]]:
        """Release finished disposable workspaces without touching GitHub branches."""
        if self.workspace_cleanup is None:
            return [], []

        cards = self.adapter.list_cards(board_id)
        groups: dict[str, list[QueueCard]] = {}
        for card in cards:
            if card.metadata.get("archivedAt"):
                continue
            workflow_id = next(
                (label.split(":", 1)[1] for label in card.labels if label.startswith("workflow:")),
                None,
            )
            if workflow_id is not None:
                groups.setdefault(workflow_id, []).append(card)

        cleaned: list[str] = []
        failures: list[str] = []
        for workflow_id, workflow_cards in groups.items():
            if not self._workflow_has_passed_completion(repo, workflow_cards):
                continue
            workspace_values = {
                (
                    workspace.kind,
                    str(workspace.path.resolve()) if workspace.path is not None else "",
                    workspace.branch or "",
                    workspace.push_branch or "",
                )
                for card in workflow_cards
                if (workspace := card.workspace) is not None
            }
            if not workspace_values:
                continue
            if len(workspace_values) != 1:
                failures.append(f"{workflow_id}: inconsistent workspace references")
                continue
            kind, path_value, branch, push_branch = next(iter(workspace_values))
            if kind != "worktree" or not path_value:
                failures.append(f"{workflow_id}: unsupported workspace kind or missing path")
                continue
            workspace = WorkspaceRef(
                kind=kind,
                path=Path(path_value),
                branch=branch or None,
                push_branch=push_branch or None,
            )
            try:
                if self.workspace_cleanup(repo, workspace):
                    cleaned.append(path_value)
            except Exception as exc:
                failures.append(f"{workflow_id}: {str(exc)[:500]}")
        return cleaned, failures

    def _has_valid_completion(
        self,
        repo: RepoConfig,
        card: QueueCard,
        workflow_cards: list[QueueCard],
    ) -> bool:
        if not _has_valid_proof(repo, card):
            return False
        if _card_stage(card) != WorkStage.FINAL_REVIEW:
            return True
        if self.completion_validator is None:
            return not self.config.require_live_completion_validation
        cached = self._completion_validation_cache.get(card.id)
        if cached is not None:
            return cached.allowed
        try:
            result = self.completion_validator.validate(repo, card, workflow_cards)
        except Exception as exc:
            result = CompletionValidation(False, f"completion validator failed: {str(exc)[:500]}")
        self._completion_validation_cache[card.id] = result
        return result.allowed

    def _retry_with_passed_completion(
        self,
        repo: RepoConfig,
        cards: list[QueueCard],
        card_id: str,
    ) -> bool:
        return any(
            _is_retry_for(item, card_id) and self._has_valid_completion(repo, item, cards)
            for item in cards
        )

    def _workflow_has_passed_completion(
        self,
        repo: RepoConfig,
        cards: list[QueueCard],
    ) -> bool:
        return bool(cards) and all(
            card.status == QueueStatus.DONE
            and self._has_valid_completion(repo, card, cards)
            for card in cards
        )

    def _create_fresh_retry(
        self,
        repo: RepoConfig,
        card: QueueCard,
        cards: list[QueueCard],
    ) -> QueueCard:
        stage = _card_stage(card)
        if stage is None:
            raise ValueError(f"Cannot retry a card without a stage label: {card.id}")
        all_retries = [item for item in cards if _is_retry_for(item, card.id)]
        retries = [item for item in all_retries if not item.metadata.get("archivedAt")]
        live_retry = next(
            (
                item
                for item in retries
                if item.status not in {QueueStatus.DONE, QueueStatus.BLOCKED}
            ),
            None,
        )
        if live_retry is not None:
            return live_retry
        attempt = len(all_retries) + 1
        retry_label = f"retry-for:{card.id}"
        if len(retry_label) > 40:
            retry_label = f"retry:{card.id[:32]}"
        validation_reason = self._completion_validation_cache.get(card.id)
        validation_note = (
            f"\nLive completion-gate result: {validation_reason.reason}\n"
            if validation_reason is not None and not validation_reason.allowed
            else ""
        )
        retry = self.adapter.create_card(
            repo.orchestration_board
            or f"{self.config.board_prefix}-{repo.full_name.replace('/', '-').lower()}",
            QueueCardSpec(
                key=f"captains_chair:retry:{card.id}:{attempt}",
                title=f"Retry {stage.value}: {card.title}",
                notes=(
                    f"Repository: {repo.full_name}\n"
                    f"Original Workboard card: {card.id}\n"
                    f"Original notes:\n{card.notes or '(none)'}\n\n"
                    f"{validation_note}"
                    "This is a fresh-context retry. Use this card's own runtime session and do not rely on a prior chat. "
                    "Complete only through the CAPTAINS_CHAIR worker lifecycle helper with current-head evidence."
                ),
                status=QueueStatus.READY,
                priority="high",
                labels=tuple(_bounded_label(label) for label in (*card.labels, retry_label)),
                agent_id=card.agent_id or _worker_for(stage, self.config),
                source_url=card.source_url,
                parents=_parent_ids(card),
                workspace=card.workspace,
                max_runtime_seconds=self.config.max_runtime_seconds,
                max_retries=self.config.max_retries,
            ),
        )
        if retry.status != QueueStatus.READY:
            retry = self.adapter.reclaim_card(
                retry.id,
                status=QueueStatus.READY,
                reason="TECHNICAL_fresh_retry_card_created_for_dispatch",
            )
        return retry

    def _create_control_plane_recovery(
        self,
        repo: RepoConfig,
        card: QueueCard,
        cards: list[QueueCard],
    ) -> QueueCard:
        recoveries = [item for item in cards if _is_control_plane_recovery_for(item, card.id)]
        live_recovery = next(
            (
                item
                for item in recoveries
                if not item.metadata.get("archivedAt")
                and item.status not in {QueueStatus.DONE, QueueStatus.BLOCKED}
            ),
            None,
        )
        if live_recovery is not None:
            return live_recovery
        attempt = len(recoveries) + 1
        board_id = repo.orchestration_board or (
            f"{self.config.board_prefix}-{repo.full_name.replace('/', '-').lower()}"
        )
        recovery = self.adapter.create_card(
            board_id,
            QueueCardSpec(
                key=f"captains_chair:control-plane-recovery:{card.id}:{attempt}",
                title=f"Captain recovery: {card.title}",
                notes=(
                    f"Repository: {repo.full_name}\n"
                    f"Failed card: {card.id}\n"
                    f"Failure evidence: {_block_reason(card)}\n\n"
                    "This is a fresh Captain recovery context. Re-read the current repository, PR, issue, and Workboard state. "
                    "Determine the smallest autonomous replanning action that advances the original goal. Preserve the failed "
                    "card as evidence, create or retarget fresh work only when justified, and complete this card with a concise "
                    "decision, created-card links, and current-head proof. Use USER_SECRET:, GOAL_DIVERGENCE:, EXTERNAL_ACCESS:, "
                    "or HIGH_RISK_DECISION: only when owner intervention is truly required."
                ),
                status=QueueStatus.READY,
                priority="high",
                labels=(
                    "captains_chair",
                    f"repo:{repo.full_name.lower()}",
                    "stage:control_plane_action",
                    _control_plane_recovery_label(card.id),
                ),
                agent_id=self.config.workers.captain,
                source_url=card.source_url,
                max_runtime_seconds=self.config.max_runtime_seconds,
                max_retries=self.config.max_retries,
            ),
        )
        if recovery.status != QueueStatus.READY:
            recovery = self.adapter.reclaim_card(
                recovery.id,
                status=QueueStatus.READY,
                reason="TECHNICAL_control_plane_recovery_card_created_for_dispatch",
            )
        return recovery

    def has_active_workflow(self, repo: RepoConfig, decision: PlanDecision) -> bool:
        """Return whether Workboard already owns the issue or PR in this decision."""
        board_id = repo.orchestration_board or (
            f"{self.config.board_prefix}-{repo.full_name.replace('/', '-').lower()}"
        )
        source_urls = {
            url
            for url in (
                _issue_url(repo, decision.target_issue),
                _pr_url(repo, decision.target_pr),
            )
            if url is not None
        }
        if not source_urls:
            return False
        groups: dict[str, list[QueueCard]] = {}
        for card in self.adapter.list_cards(board_id):
            workflow_id = next(
                (label.split(":", 1)[1] for label in card.labels if label.startswith("workflow:")),
                None,
            )
            if workflow_id is None or card.source_url not in source_urls:
                continue
            if card.metadata.get("archivedAt"):
                continue
            groups.setdefault(workflow_id, []).append(card)
        return any(any(card.status != QueueStatus.DONE for card in cards) for cards in groups.values())

    def active_workflow_count(self, repo: RepoConfig) -> int:
        """Count workflows that consume PR capacity while preserving owner-blocker isolation."""
        board_id = repo.orchestration_board or (
            f"{self.config.board_prefix}-{repo.full_name.replace('/', '-').lower()}"
        )
        groups: dict[str, list[QueueCard]] = {}
        for card in self.adapter.list_cards(board_id):
            workflow_id = next(
                (label.split(":", 1)[1] for label in card.labels if label.startswith("workflow:")),
                None,
            )
            if workflow_id is None or card.metadata.get("archivedAt"):
                continue
            groups.setdefault(workflow_id, []).append(card)

        count = 0
        for cards in groups.values():
            active_cards = [
                card
                for card in cards
                if card.status != QueueStatus.DONE
                and not (
                    card.status == QueueStatus.BLOCKED
                    and classify_blocker(_block_reason(card)) != BlockerKind.TECHNICAL
                )
            ]
            if not active_cards:
                continue
            count += 1
        return count

    def _recover_card(
        self,
        card: QueueCard,
        retried: list[str],
        control_plane_recoveries: list[str],
    ) -> None:
        failures = _failure_count(card)
        retry_limit = _retry_limit(card, self.config.max_retries)
        if failures <= retry_limit:
            self.adapter.reclaim_card(
                card.id,
                status=QueueStatus.READY,
                reason="Automatic retry of a repairable technical blocker.",
            )
            retried.append(card.id)
            return
        self.adapter.reassign_card(
            card.id,
            agent_id=self.config.workers.captain,
            status=QueueStatus.READY,
            reset_failures=True,
            reason="Retry budget exhausted; route to Captain recovery for autonomous replanning.",
        )
        _append_unique(control_plane_recoveries, card.id)


def build_workflow(
    repo: RepoConfig,
    decision: PlanDecision,
    action_id: str,
    config: OrchestrationPolicy,
    *,
    workspace: WorkspaceRef | None = None,
) -> WorkflowSpec:
    board_id = repo.orchestration_board or (
        f"{config.board_prefix}-{repo.full_name.replace('/', '-').lower()}"
    )
    source_url = _source_url(repo, decision)
    common_labels = (
        "captains_chair",
        f"repo:{repo.full_name.lower()}",
        f"action:{decision.action.value}",
        workflow_label(action_id),
    )
    root_key = f"{action_id}:root"
    root = QueueCardSpec(
        key=root_key,
        title=f"{repo.full_name.split('/', 1)[-1]}: {decision.summary}",
        notes=_root_notes(repo, decision, action_id),
        labels=(*common_labels, "stage:orchestration"),
        agent_id=config.workers.captain,
        source_url=source_url,
        max_runtime_seconds=config.max_runtime_seconds,
        max_retries=config.max_retries,
    )
    stages = _stage_sequence(repo, decision)
    specs: list[QueueCardSpec] = []
    for stage, dependencies in stages:
        key = f"{action_id}:{stage.value}"
        stage_workspace = None if stage in {WorkStage.MERGE, WorkStage.POST_MERGE} else workspace
        parents = tuple(root_key if parent is None else f"{action_id}:{parent.value}" for parent in dependencies)
        specs.append(
            QueueCardSpec(
                key=key,
                title=f"{stage.value.replace('_', ' ').title()}: {decision.summary}",
                notes=_stage_notes(repo, decision, action_id, stage, workspace=stage_workspace),
                labels=(*common_labels, f"stage:{stage.value}"),
                agent_id=_worker_for(stage, config),
                source_url=source_url,
                parents=parents,
                workspace=stage_workspace,
                max_runtime_seconds=config.max_runtime_seconds,
                max_retries=config.max_retries,
            )
        )
    return WorkflowSpec(
        workflow_id=action_id,
        repo=repo.full_name,
        board_id=board_id,
        title=decision.summary,
        summary=decision.reason,
        source_url=source_url,
        root=root,
        stages=tuple(specs),
    )


def _stage_sequence(
    repo: RepoConfig, decision: PlanDecision
) -> tuple[tuple[WorkStage, tuple[WorkStage | None, ...]], ...]:
    if decision.action in {
        ActionKind.CREATE_ISSUE,
        ActionKind.UPDATE_ISSUE,
        ActionKind.LABEL_ISSUE,
        ActionKind.RETARGET_ISSUE,
        ActionKind.CLOSE_ISSUE,
    }:
        return (
            (WorkStage.CONTROL_PLANE_ACTION, (None,)),
            (WorkStage.POST_MERGE, (WorkStage.CONTROL_PLANE_ACTION,)),
        )

    if decision.action in {ActionKind.REVIEW_PR, ActionKind.MERGE_PR}:
        first = WorkStage.REVIEW
        rows: list[tuple[WorkStage, tuple[WorkStage | None, ...]]] = [
            (WorkStage.REVIEW, (None,)),
            (WorkStage.TEST, (None,)),
        ]
    else:
        first = WorkStage.REPAIR if decision.action == ActionKind.REPAIR_PR else WorkStage.IMPLEMENTATION
        rows = [
            (first, (None,)),
            (WorkStage.REVIEW, (first,)),
            (WorkStage.TEST, (first,)),
        ]

    review_dependencies: list[WorkStage] = [WorkStage.REVIEW, WorkStage.TEST]
    if repo.ux_enabled and decision.action != ActionKind.UPDATE_PLAN:
        rows.append((WorkStage.UX_REVIEW, (first,)))
        review_dependencies.append(WorkStage.UX_REVIEW)
    rows.append((WorkStage.FINAL_REVIEW, tuple(review_dependencies)))
    if repo.completion_policy == CompletionPolicy.AUTO_MERGE:
        rows.append((WorkStage.MERGE, (WorkStage.FINAL_REVIEW,)))
        rows.append((WorkStage.POST_MERGE, (WorkStage.MERGE,)))
    return tuple(rows)


def _worker_for(stage: WorkStage, config: OrchestrationPolicy) -> str:
    return {
        WorkStage.CONTROL_PLANE_ACTION: config.workers.captain,
        WorkStage.IMPLEMENTATION: config.workers.coder,
        WorkStage.REPAIR: config.workers.coder,
        WorkStage.REVIEW: config.workers.reviewer,
        WorkStage.TEST: config.workers.tester,
        WorkStage.UX_REVIEW: config.workers.ux_reviewer,
        WorkStage.FINAL_REVIEW: config.workers.final_reviewer,
        WorkStage.MERGE: config.workers.merger,
        WorkStage.POST_MERGE: config.workers.verifier,
    }[stage]


def _source_url(repo: RepoConfig, decision: PlanDecision) -> str:
    if decision.target_pr:
        return f"https://github.com/{repo.full_name}/pull/{decision.target_pr}"
    if decision.target_issue:
        return f"https://github.com/{repo.full_name}/issues/{decision.target_issue}"
    return f"https://github.com/{repo.full_name}"


def _issue_url(repo: RepoConfig, issue: int | None) -> str | None:
    return f"https://github.com/{repo.full_name}/issues/{issue}" if issue else None


def _pr_url(repo: RepoConfig, pr: int | None) -> str | None:
    return f"https://github.com/{repo.full_name}/pull/{pr}" if pr else None


def _root_notes(repo: RepoConfig, decision: PlanDecision, action_id: str) -> str:
    criteria = "\n".join(f"- {item}" for item in decision.acceptance_criteria) or "- Use the repository's documented acceptance criteria."
    return (
        f"Repository: {repo.full_name}\n"
        f"CAPTAINS_CHAIR workflow: {action_id}\n"
        f"Action: {decision.action.value}\n"
        f"Goal: {decision.summary}\n"
        f"Reason: {decision.reason}\n\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        "This parent card records the durable work contract. Child cards are independently "
        "claimed by role-separated workers supplied by the configured runtime."
    )


def _stage_notes(
    repo: RepoConfig,
    decision: PlanDecision,
    action_id: str,
    stage: WorkStage,
    *,
    workspace: WorkspaceRef | None = None,
) -> str:
    contracts = {
        WorkStage.CONTROL_PLANE_ACTION: "Perform only the exact GitHub issue action in this card and verify it by reading GitHub back.",
        WorkStage.IMPLEMENTATION: (
            "Implement the linked work contract in the supplied isolated worktree. Keep scope tight, run targeted checks, "
            "push the CAPTAINS_CHAIR branch, open or update a PR, and include its exact GitHub PR URL and head SHA in completion proof. "
            "Never merge your own work."
        ),
        WorkStage.REPAIR: (
            "Repair only the blocking findings on the current PR head, run targeted checks, push the same PR branch, and provide new-head proof."
        ),
        WorkStage.REVIEW: (
            "Review the current PR head with fresh context. Do not edit files. Check scope, correctness, security, tests, "
            "documentation alignment, and unrelated churn. Block with actionable findings or complete with current-head proof."
        ),
        WorkStage.TEST: (
            "Independently run the configured targeted tests and inspect required GitHub checks for the current PR head. "
            "Do not waive failures or pending checks."
        ),
        WorkStage.UX_REVIEW: (
            "Use browser-based testing at mobile, tablet, and desktop sizes. Verify usability, contrast, keyboard/focus behavior, "
            "responsive layout, error/loading/empty states, functionality, and visual cohesion. Attach screenshot proof when possible."
        ),
        WorkStage.FINAL_REVIEW: (
            "Perform the Captain final review against the original issue, repository plan, acceptance criteria, independent review, "
            "UX evidence, tests, CI, unresolved threads, and current PR head. Complete only when the configured completion policy is satisfied, "
            "and include the matching READY_FOR_OWNER:<head-sha>, CONTROL_PLANE_COMPLETE:<head-sha>, or AUTO_MERGE_ALLOWED:<head-sha> proof marker."
        ),
        WorkStage.MERGE: (
            "Re-read the live PR and run the deterministic CAPTAINS_CHAIR merge gate. Merge only when AUTO_MERGE_ALLOWED is anchored to the "
            "current head and all required checks, mergeability, and review-thread gates pass."
        ),
        WorkStage.POST_MERGE: (
            "Verify the actual default-branch merge commit, main CI, and configured deployment policy. Record direct GitHub proof."
        ),
    }
    blocker_rules = (
        "Only request user intervention by starting the block reason with USER_SECRET:, GOAL_DIVERGENCE:, "
        "EXTERNAL_ACCESS:, or HIGH_RISK_DECISION:. Use TECHNICAL: for repairable failures so the supervisor can retry or route repair work. "
        "A blocked card must not prevent workers from completing unrelated ready cards."
    )
    return (
        f"Repository: {repo.full_name}\n"
        f"CAPTAINS_CHAIR workflow: {action_id}\n"
        f"Stage: {stage.value}\n"
        f"Goal: {decision.summary}\n"
        f"Reason: {decision.reason}\n\n"
        + _workspace_notes(workspace)
        + f"Stage contract: {contracts[stage]}\n\n"
        f"Worker protocol: claim the card, heartbeat during long work, and call Workboard complete with a concise summary and proof, "
        f"or Workboard block with a specific reason. {blocker_rules}"
    )


def _card_stage(card: QueueCard) -> WorkStage | None:
    for label in card.labels:
        if not label.startswith("stage:"):
            continue
        try:
            return WorkStage(label.split(":", 1)[1])
        except ValueError:
            return None
    return None


def _block_reason(card: QueueCard) -> str:
    protocol = card.metadata.get("workerProtocol")
    if isinstance(protocol, dict):
        protocol_row = cast(dict[str, object], protocol)
        if protocol_row.get("detail"):
            return str(protocol_row["detail"])
    logs = card.metadata.get("workerLogs")
    if isinstance(logs, list):
        for item in reversed(cast(list[object], logs)):
            if isinstance(item, dict):
                log_row = cast(dict[str, object], item)
                if log_row.get("message"):
                    return str(log_row["message"])
    attempts = card.metadata.get("attempts")
    if isinstance(attempts, list):
        for item in reversed(cast(list[object], attempts)):
            if isinstance(item, dict):
                attempt_row = cast(dict[str, object], item)
                if attempt_row.get("error"):
                    return str(attempt_row["error"])
    return "TECHNICAL: worker blocked without structured failure evidence"


def _failure_count(card: QueueCard) -> int:
    value = card.metadata.get("failureCount")
    if isinstance(value, int) and value >= 0:
        return value
    attempts = card.metadata.get("attempts")
    if not isinstance(attempts, list):
        return 1
    return max(
        1,
        sum(
            1
            for item in cast(list[object], attempts)
            if isinstance(item, dict)
            and cast(dict[str, object], item).get("status") in {"failed", "blocked", "stopped"}
        ),
    )


def _retry_limit(card: QueueCard, default: int) -> int:
    automation = card.metadata.get("automation")
    if isinstance(automation, dict):
        value = cast(dict[str, object], automation).get("maxRetries")
        if isinstance(value, int) and value >= 0:
            return value
    return default


def _has_passed_proof(card: QueueCard) -> bool:
    value = card.metadata.get("proof")
    if not isinstance(value, list):
        return False
    return any(
        isinstance(item, dict)
        and str(cast(dict[str, object], item).get("status") or "").lower() == "passed"
        for item in cast(list[Any], value)
    )


def _has_valid_proof(repo: RepoConfig, card: QueueCard) -> bool:
    if not _has_passed_proof(card):
        return False
    if _card_stage(card) != WorkStage.FINAL_REVIEW:
        return True
    marker = {
        CompletionPolicy.OWNER_APPROVAL: "READY_FOR_OWNER:",
        CompletionPolicy.CONTROL_PLANE_COMPLETE: "CONTROL_PLANE_COMPLETE:",
        CompletionPolicy.AUTO_MERGE: "AUTO_MERGE_ALLOWED:",
    }[repo.completion_policy]
    proof_value = card.metadata.get("proof")
    if not isinstance(proof_value, list):
        return False
    latest_passed = next(
        (
            cast(dict[str, object], value)
            for value in reversed(cast(list[object], proof_value))
            if isinstance(value, dict)
            and str(cast(dict[str, object], value).get("status") or "").lower() == "passed"
        ),
        None,
    )
    if latest_passed is None:
        return False
    for field in ("note", "label"):
        text = str(latest_passed.get(field) or "")
        offset = text.upper().find(marker)
        if offset < 0:
            continue
        head = text[offset + len(marker) :].strip().split(maxsplit=1)[0].strip("`.,;)")
        if 7 <= len(head) <= 64 and all(character in "0123456789abcdefABCDEF" for character in head):
            return True
    return False


def _workspace_notes(workspace: WorkspaceRef | None) -> str:
    if workspace is None:
        return ""
    local = workspace.branch or "the supplied workspace branch"
    push = workspace.push_branch or local
    return (
        f"Workspace contract: work only in `{workspace.path or '(runtime-supplied path)'}`. "
        f"The local branch is `{local}`; push implementation changes to `{push}`.\n\n"
    )


def worker_model_health(adapter: WorkQueueAdapter) -> dict[str, Any]:
    validator = getattr(adapter, "validate_worker_models", None)
    if not callable(validator):
        return {"status": "not_supported"}
    try:
        value = validator()
    except Exception as exc:
        return {"status": "degraded", "error": str(exc)[:2000]}
    if not isinstance(value, dict):
        return {"status": "degraded", "error": "model health adapter returned a non-object"}
    return cast(dict[str, Any], value)


def _passed_proof(card: QueueCard) -> tuple[dict[str, Any], ...]:
    value = card.metadata.get("proof")
    if not isinstance(value, list):
        return ()
    return tuple(
        cast(dict[str, Any], item)
        for item in cast(list[Any], value)
        if isinstance(item, dict)
        and str(cast(dict[str, object], item).get("status") or "").lower() == "passed"
    )


def _completion_summary(card: QueueCard) -> str:
    automation = card.metadata.get("automation")
    if isinstance(automation, dict):
        summary = cast(dict[str, object], automation).get("summary")
        if summary:
            return str(summary)
    stage = _card_stage(card)
    return f"Recovered completed {stage.value if stage else 'work'} card from runtime review status."


def _retry_for(card: QueueCard) -> str | None:
    for label in reversed(card.labels):
        if label.startswith("retry-for:"):
            return label.split(":", 1)[1]
        if label.startswith("retry:"):
            return label.split(":", 1)[1]
    return None


def _active_retry(cards: list[QueueCard], parent_id: str) -> QueueCard | None:
    return next(
        (
            item
            for item in cards
            if not item.metadata.get("archivedAt")
            and _is_retry_for(item, parent_id)
            and item.status not in {QueueStatus.DONE, QueueStatus.BLOCKED}
        ),
        None,
    )


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _retry_depth(card: QueueCard, cards: list[QueueCard]) -> int:
    """Count fresh retry ancestors, tolerating compact UUID labels."""
    depth = 0
    current = card
    seen: set[str] = set()
    while True:
        token = _retry_for(current)
        if token is None or current.id in seen:
            return depth
        seen.add(current.id)
        parent = next(
            (
                item
                for item in cards
                if item.id == token or item.id.startswith(token)
            ),
            None,
        )
        depth += 1
        if parent is None:
            return depth
        current = parent


def _protocol_retry_exhausted(card: QueueCard, cards: list[QueueCard], default: int) -> bool:
    """Stop proofless retry chains before they become an unattended loop."""
    direct_retries = sum(1 for item in cards if _is_retry_for(item, card.id))
    return _retry_depth(card, cards) + direct_retries >= _retry_limit(card, default)


def _bounded_label(label: str) -> str:
    if len(label) <= 40:
        return label
    return label[:37].rstrip() + "..."


def _repair_label(card_id: str) -> str:
    label = f"repair-for:{card_id}"
    return label if len(label) <= 40 else f"repair:{card_id[:32]}"


def workflow_label(action_id: str) -> str:
    """Return a bounded workflow label that remains unique for long IDs."""
    prefix = "workflow:"
    if len(action_id) <= 31:
        return f"{prefix}{action_id}"
    digest = sha256(action_id.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}{action_id[:22]}-{digest}"


def _card_workflow_label(card: QueueCard) -> str | None:
    return next((label for label in card.labels if label.startswith("workflow:")), None)


def _control_plane_recovery_label(card_id: str) -> str:
    label = f"control-plane-recovery-for:{card_id}"
    return label if len(label) <= 40 else f"control-plane-recovery:{card_id[:17]}"


def _is_control_plane_recovery_for(card: QueueCard, parent_id: str) -> bool:
    for label in card.labels:
        if label.startswith("control-plane-recovery-for:"):
            token = label.split(":", 1)[1]
            return token == parent_id or parent_id.startswith(token)
        if label.startswith("control-plane-recovery:"):
            return parent_id.startswith(label.split(":", 1)[1])
    return False


def _is_repair_for(card: QueueCard, parent_id: str) -> bool:
    for label in card.labels:
        if label.startswith("repair-for:"):
            token = label.split(":", 1)[1]
            return token == parent_id or parent_id.startswith(token)
        if label.startswith("repair:"):
            return parent_id.startswith(label.split(":", 1)[1])
    return False


def _is_retry_for(card: QueueCard, parent_id: str) -> bool:
    token = _retry_for(card)
    return token == parent_id or (token is not None and parent_id.startswith(token))


def _parent_ids(card: QueueCard) -> tuple[str, ...]:
    links = card.metadata.get("links")
    if not isinstance(links, list):
        return ()
    return tuple(
        str(target)
        for item in cast(list[object], links)
        if isinstance(item, dict)
        for link in [cast(dict[str, object], item)]
        if link.get("type") == "parent"
        for target in [link.get("targetCardId")]
        if target
    )
