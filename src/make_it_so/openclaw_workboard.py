from __future__ import annotations

import json
import re
import time
from contextlib import suppress
from pathlib import Path
from threading import Event, Thread
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from uuid import uuid4

from make_it_so.command import CommandRunner, run_command
from make_it_so.direct_workers import (
    CommandWorkerExecutor,
    WorkerExecutionError,
    WorkerExecutionResult,
)
from make_it_so.json_tools import decode_first_json
from make_it_so.model_policy import models_match
from make_it_so.models import OpenClawWorkboardConfig
from make_it_so.orchestration import (
    QueueCard,
    QueueCardSpec,
    QueueStatus,
    WorkerLifecycleAdapter,
    WorkQueueAdapter,
    WorkspaceRef,
)


class OpenClawWorkboardError(RuntimeError):
    pass


WORKER_EXECUTION_COMMENT_PREFIX = "MAKE_IT_SO_WORKER_EXECUTION:"
MANAGED_METADATA_MARKER = "MAKE_IT_SO_METADATA_JSON:"
TEST_EVIDENCE_MARKER = "MAKE_IT_SO_TEST_EVIDENCE_JSON:"


def _notes_with_managed_metadata(notes: str | None, metadata: dict[str, Any]) -> str | None:
    """Persist Make It So fields through Workboard's stable notes surface."""
    values = {
        key: value
        for key, value in metadata.items()
        if key
        in {
            "courseKey",
            "workPackageKey",
            "qaEvidenceVersion",
            "qaProfile",
            "qaSurfaces",
            "qaChecks",
            "qaRequired",
            "plannedChangedPaths",
            "actualChangedPaths",
            "discoveredHeadSha",
            "testEvidenceRequired",
            "testEvidencePolicy",
            "testEvidenceScreenshotRequired",
        }
    }
    if not values:
        return notes
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
    base = (notes or "").rstrip()
    if MANAGED_METADATA_MARKER in base:
        return base
    return f"{base}\n\n{MANAGED_METADATA_MARKER}{encoded}" if base else f"{MANAGED_METADATA_MARKER}{encoded}"


def _metadata_from_notes(notes: str | None) -> dict[str, Any]:
    if not notes:
        return {}
    match = re.search(rf"(?m)^{re.escape(MANAGED_METADATA_MARKER)}(\{{.*\}})\s*$", notes)
    if not match:
        return {}
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


REQUIRED_WORKER_TOOLS = (
    "workboard_block",
    "workboard_comment",
    "workboard_complete",
    "workboard_heartbeat",
    "workboard_proof",
    "workboard_read",
    "workboard_worker_log",
)

WORKER_HEARTBEAT_GRACE_SECONDS = 120


class OpenClawWorkboardAdapter(WorkQueueAdapter, WorkerLifecycleAdapter):
    def __init__(
        self,
        config: OpenClawWorkboardConfig,
        runner: CommandRunner = run_command,
    ) -> None:
        self.config = config
        self.runner = runner
        self._recovery_warnings: list[str] = []

    def ensure_board(self, board_id: str, name: str, description: str, workspace: Path) -> None:
        self._rpc(
            "workboard.boards.upsert",
            {
                "id": board_id,
                "name": name,
                "description": description,
                "defaultWorkspace": {"kind": "dir", "path": str(workspace.resolve())},
            },
        )

    def list_cards(self, board_id: str) -> list[QueueCard]:
        payload = self._rpc("workboard.cards.list", {"boardId": board_id})
        raw_cards = payload.get("cards")
        if not isinstance(raw_cards, list):
            raise OpenClawWorkboardError("workboard.cards.list did not return a cards array")
        card_values = cast(list[object], raw_cards)
        if any(not isinstance(item, dict) for item in card_values):
            raise OpenClawWorkboardError("workboard.cards.list contained a non-object card")
        cards = [self._card(cast(dict[str, Any], item)) for item in card_values]
        ids = [card.id for card in cards]
        if len(ids) != len(set(ids)):
            raise OpenClawWorkboardError("workboard.cards.list contained duplicate card ids")
        return cards

    def create_card(self, board_id: str, spec: QueueCardSpec) -> QueueCard:
        params: dict[str, Any] = {
            "title": _bounded_title(spec.title),
            "notes": _notes_with_managed_metadata(spec.notes, spec.metadata),
            "status": spec.status.value,
            "priority": spec.priority,
            "labels": [_bounded_label(label) for label in spec.labels],
            "agentId": spec.agent_id or "",
            "boardId": board_id,
            "tenant": "make-it-so",
            "idempotencyKey": spec.key,
            "parents": list(spec.parents),
            "maxRuntimeSeconds": spec.max_runtime_seconds,
            "maxRetries": spec.max_retries,
            "metadata": spec.metadata,
        }
        if spec.source_url:
            params["sourceUrl"] = spec.source_url
        if spec.workspace:
            workspace_payload: dict[str, Any] = {
                "kind": spec.workspace.kind,
                **({"path": str(spec.workspace.path)} if spec.workspace.path else {}),
                **({"branch": spec.workspace.branch} if spec.workspace.branch else {}),
            }
            if spec.workspace.push_branch:
                workspace_payload["pushBranch"] = spec.workspace.push_branch
            params["workspace"] = workspace_payload
        return self._card_response("workboard.cards.create", params)

    def complete_card(
        self,
        card_id: str,
        *,
        summary: str,
        proof: tuple[dict[str, Any], ...] = (),
        created_card_ids: tuple[str, ...] = (),
    ) -> QueueCard:
        proof_value = self._completion_proof(proof)
        return self._card_response(
            "workboard.cards.complete",
            {
                "id": card_id,
                "summary": summary,
                **({"proof": proof_value} if proof_value else {}),
                "createdCardIds": list(created_card_ids),
            },
        )

    def heartbeat_card(self, card_id: str, *, owner_id: str, token: str, note: str) -> QueueCard:
        return self._card_response(
            "workboard.cards.heartbeat",
            {"id": card_id, "ownerId": owner_id, "token": token, "note": note},
        )

    def complete_claimed_card(
        self,
        card_id: str,
        *,
        owner_id: str,
        token: str,
        summary: str,
        proof: tuple[dict[str, Any], ...],
    ) -> QueueCard:
        proof_value = self._completion_proof(proof)
        return self._card_response(
            "workboard.cards.complete",
            {
                "id": card_id,
                "ownerId": owner_id,
                "token": token,
                "summary": summary,
                **({"proof": proof_value} if proof_value else {}),
            },
        )

    def block_claimed_card(
        self,
        card_id: str,
        *,
        owner_id: str,
        token: str,
        reason: str,
    ) -> QueueCard:
        return self._card_response(
            "workboard.cards.block",
            {"id": card_id, "ownerId": owner_id, "token": token, "reason": reason},
        )

    def unblock_card(self, card_id: str) -> QueueCard:
        return self._card_response("workboard.cards.unblock", {"id": card_id})

    def reclaim_card(self, card_id: str, *, status: QueueStatus, reason: str) -> QueueCard:
        return self._card_response(
            "workboard.cards.reclaim",
            {"id": card_id, "status": status.value, "reason": reason},
        )

    def reassign_card(
        self,
        card_id: str,
        *,
        agent_id: str,
        status: QueueStatus,
        reset_failures: bool,
        reason: str,
    ) -> QueueCard:
        return self._card_response(
            "workboard.cards.reassign",
            {
                "id": card_id,
                "agentId": agent_id,
                "status": status.value,
                "resetFailures": reset_failures,
                "reason": reason,
            },
        )

    def comment(self, card_id: str, body: str) -> QueueCard:
        return self._card_response("workboard.cards.comment", {"id": card_id, "body": body})

    def dispatch(self, board_id: str) -> dict[str, Any]:
        merge_result = self._dispatch_deterministic_merge(board_id)
        if self.config.dispatch_strategy == "managed_single":
            result = self._managed_single_dispatch(board_id)
        else:
            result = self._rpc(
                "workboard.cards.dispatch",
                {"boardId": board_id},
                timeout=self.config.dispatch_timeout_seconds,
            )
        if merge_result is None:
            return result
        return {**result, "deterministic_merge": merge_result}

    def _dispatch_deterministic_merge(self, board_id: str) -> dict[str, Any] | None:
        """Execute one eligible merge card without granting merge authority to a model."""
        cards = self.list_cards(board_id)
        assigned_merge_cards = [
            card
            for card in cards
            if "stage:merge" in card.labels
            and card.status in {QueueStatus.TODO, QueueStatus.READY}
            and not card.metadata.get("archivedAt")
            and card.agent_id
        ]
        for card in assigned_merge_cards:
            self.reassign_card(
                card.id,
                agent_id="",
                status=card.status,
                reset_failures=False,
                reason="TECHNICAL_migrate_merge_card_to_deterministic_control_plane",
            )
        if assigned_merge_cards:
            cards = self.list_cards(board_id)
        by_id = {card.id: card for card in cards}
        merge_card = next(
            (
                card
                for card in cards
                if "stage:merge" in card.labels
                and card.status in {QueueStatus.TODO, QueueStatus.READY}
                and not card.metadata.get("archivedAt")
                and _parent_ids(card)
                and all(
                    parent_id in by_id and by_id[parent_id].status == QueueStatus.DONE
                    for parent_id in _parent_ids(card)
                )
            ),
            None,
        )
        if merge_card is None:
            return None
        workflow_cards = _workflow_scope(cards, merge_card)
        active_repairs = [
            card
            for card in workflow_cards
            if "stage:repair" in card.labels
            and card.status != QueueStatus.DONE
            and not _is_cancelled_card(card)
            and not card.metadata.get("archivedAt")
        ]
        if active_repairs:
            return {
                "status": "waiting",
                "card": merge_card.id,
                "reason": "merge is waiting for active repair cards to complete",
                "repairs": [card.id for card in active_repairs],
            }
        repo = _repository_name(workflow_cards)
        pr_numbers = _pull_request_numbers(workflow_cards, repo)
        final_cards = [
            by_id[parent_id]
            for parent_id in _parent_ids(merge_card)
            if parent_id in by_id and "stage:final_review" in by_id[parent_id].labels
        ]
        if repo is None or len(pr_numbers) != 1 or len(final_cards) != 1:
            return {
                "status": "waiting",
                "card": merge_card.id,
                "reason": "deterministic merge context is incomplete or ambiguous",
            }
        if merge_card.status == QueueStatus.TODO:
            merge_card = self.reclaim_card(
                merge_card.id,
                status=QueueStatus.READY,
                reason="TECHNICAL_deterministic_merge_dependencies_satisfied",
            )
        token = uuid4().hex
        attempt_id = f"deterministic-merge:{merge_card.id}:{uuid4().hex}"
        owner_id = f"make-it-so-managed:{attempt_id}"
        claimed = self.claim_card(
            merge_card.id,
            owner_id=owner_id,
            token=token,
            attempt_id=attempt_id,
        )
        pr_number = next(iter(pr_numbers))
        final_card = final_cards[0]
        command = [
            *self.config.make_it_so_command,
            "merge-gate",
            "--repo",
            repo,
            "--pr",
            str(pr_number),
            "--final-card",
            final_card.id,
            "--merge",
        ]
        try:
            result = self.runner(command, timeout=self.config.dispatch_timeout_seconds)
        except (OSError, TimeoutError) as exc:
            reason = f"deterministic merge command failed: {str(exc).strip()[:1400]}"
            self.block_claimed_card(
                claimed.id,
                owner_id=owner_id,
                token=token,
                reason=f"TECHNICAL: {reason}",
            )
            return {"status": "blocked", "card": claimed.id, "reason": reason}
        payload: dict[str, Any] = {}
        with suppress(ValueError):
            decoded = decode_first_json(result.stdout)
            if isinstance(decoded, dict):
                payload = cast(dict[str, Any], decoded)
        if result.returncode or payload.get("allowed") is not True or payload.get("merged") is not True:
            reason = str(
                payload.get("reason")
                or result.stderr
                or result.stdout
                or "deterministic merge gate did not authorize the mutation"
            ).strip()[:1500]
            self.block_claimed_card(
                claimed.id,
                owner_id=owner_id,
                token=token,
                reason=f"TECHNICAL: deterministic merge gate denied: {reason}",
            )
            return {"status": "blocked", "card": claimed.id, "reason": reason}
        pr_url = f"https://github.com/{repo}/pull/{pr_number}"
        current_head = str(payload.get("current_head") or "").strip()
        proof_note = f"Deterministic merge gate authorized and merged PR #{pr_number}"
        if current_head:
            proof_note += f" at reviewed head {current_head}"
        proof_note += "; Model: deterministic/no-model; Provider: make-it-so"
        self.complete_claimed_card(
            claimed.id,
            owner_id=owner_id,
            token=token,
            summary=f"Merged PR #{pr_number} through the deterministic Make It So gate.",
            proof=(
                {
                    "status": "passed",
                    "label": "Deterministic autonomous merge",
                    "note": proof_note,
                    "url": pr_url,
                    "model": "deterministic/no-model",
                    "provider": "make-it-so",
                },
            ),
        )
        return {"status": "completed", "card": claimed.id, "pr": pr_number, "url": pr_url}

    def _managed_single_dispatch(self, board_id: str) -> dict[str, Any]:
        """Dispatch one Workboard card without invoking OpenClaw's board dispatcher."""
        cards = self.list_cards(board_id)
        promoted = self._promote_dependency_ready_cards(cards)
        cards = self.list_cards(board_id) if promoted else cards
        ready = next(
            (
                card
                for card in cards
                if card.status == QueueStatus.READY
                and not card.metadata.get("archivedAt")
                and card.agent_id
                and "stage:merge" not in card.labels
            ),
            None,
        )
        if ready is None:
            return {
                "status": "idle",
                "strategy": "managed_single",
                "promoted": promoted,
                "started": [],
                "completed": [],
                "blocked": [],
                "count": len(promoted),
            }
        token = uuid4().hex
        attempt_id = f"managed:{ready.id}:{uuid4().hex}"
        # Keep the attempt in the durable claim owner so a later reconcile can
        # reconstruct and inspect the exact OpenClaw session after a crash.
        owner_id = f"make-it-so-managed:{attempt_id}"
        claimed = self.claim_card(ready.id, owner_id=owner_id, token=token, attempt_id=attempt_id)
        model = _worker_models(self.config).get(claimed.agent_id or "")
        runtime = _worker_runtimes(self.config).get(claimed.agent_id or "", "openclaw")
        completed = False
        blocked = False
        stop = Event()
        heartbeat = Thread(
            target=self._heartbeat_loop,
            args=(stop, claimed.id, owner_id, token),
            daemon=True,
        )
        heartbeat.start()
        try:
            executable = self.config.codex_executable if runtime == "codex" else self.config.executable
            if not model:
                self.block_claimed_card(
                    claimed.id,
                    owner_id=owner_id,
                    token=token,
                    reason=f"TECHNICAL: no OpenClaw worker model is configured for agent {claimed.agent_id or '(none)'}",
                )
                blocked = True
            elif not executable:
                self.block_claimed_card(
                    claimed.id,
                    owner_id=owner_id,
                    token=token,
                    reason=f"TECHNICAL: no {runtime} worker executable is configured",
                )
                blocked = True
            else:
                executor = CommandWorkerExecutor(runtime, executable, self.runner)
                workspace = (
                    claimed.workspace.path
                    if claimed.workspace is not None and claimed.workspace.path is not None
                    else Path.cwd()
                )
                result = executor.execute(
                    claimed,
                    attempt_id=attempt_id,
                    workspace=workspace,
                    model=model,
                    timeout_seconds=_runtime_limit(claimed, self.config.max_runtime_seconds),
                )
                if result.status == "completed":
                    result = self._publish_worker_changes(claimed, result)
                    execution = (
                        result.telemetry.model_dump(mode="json")
                        if result.telemetry is not None
                        else None
                    )
                    if execution is not None:
                        self.comment(
                            claimed.id,
                            WORKER_EXECUTION_COMMENT_PREFIX
                            + json.dumps(execution, separators=(",", ":")),
                        )
                    self.complete_claimed_card(
                        claimed.id,
                        owner_id=owner_id,
                        token=token,
                        summary=result.summary,
                        proof=_managed_completion_proof(
                            result.proof,
                            result.summary,
                            execution=execution,
                        ),
                    )
                    completed = True
                else:
                    self.block_claimed_card(
                        claimed.id,
                        owner_id=owner_id,
                        token=token,
                        reason=result.reason or "TECHNICAL: worker returned no blocker reason",
                    )
                    blocked = True
        except (WorkerExecutionError, OSError, TimeoutError) as exc:
            with suppress(Exception):
                self.block_claimed_card(
                    claimed.id,
                    owner_id=owner_id,
                    token=token,
                    reason=f"TECHNICAL: managed {runtime} worker execution failed: {str(exc)[:1500]}",
                )
            blocked = True
        finally:
            stop.set()
            heartbeat.join(timeout=2)
        return {
            "status": "dispatched",
            "strategy": "managed_single",
            "promoted": promoted,
            "started": [claimed.id],
            "completed": [claimed.id] if completed else [],
            "blocked": [claimed.id] if blocked else [],
            "count": len(set(promoted) | {claimed.id}),
            "runtime": runtime,
            "model": model,
        }

    def _publish_worker_changes(
        self, card: QueueCard, result: WorkerExecutionResult
    ) -> WorkerExecutionResult:
        """Publish implementation changes from the trusted host boundary.

        Codex is intentionally kept in a workspace-write sandbox without network
        access. Git commit, push, and PR creation therefore happen here, after
        the worker has returned structured evidence, so the model never needs
        host credentials or unrestricted network access.
        """
        if not any(label in {"stage:implementation", "stage:repair"} for label in card.labels):
            return result
        workspace = card.workspace
        if workspace is None or workspace.path is None:
            raise WorkerExecutionError("TECHNICAL: implementation worker has no publishable worktree")
        branch = (workspace.push_branch or workspace.branch or "").strip()
        if not branch:
            raise WorkerExecutionError("TECHNICAL: implementation worker has no push branch")
        full_name = _github_repo_name(card.source_url)
        if not full_name:
            raise WorkerExecutionError("TECHNICAL: implementation worker has no GitHub repository URL")

        status = self.runner(
            ["git", "-C", str(workspace.path), "status", "--porcelain"],
            cwd=workspace.path,
            timeout=60,
        )
        _require_publish_success(status, "inspect worker changes")
        if (status.stdout or "").strip():
            _require_publish_success(
                self.runner(
                    ["git", "-C", str(workspace.path), "add", "--all"],
                    cwd=workspace.path,
                    timeout=120,
                ),
                "stage worker changes",
            )
            _require_publish_success(
                self.runner(
                    [
                        "git",
                        "-C",
                        str(workspace.path),
                        "commit",
                        "-m",
                        _publish_commit_message(card),
                    ],
                    cwd=workspace.path,
                    timeout=180,
                ),
                "commit worker changes",
            )

        _require_publish_success(
            self.runner(
                [
                    "git",
                    "-C",
                    str(workspace.path),
                    "push",
                    "--set-upstream",
                    "origin",
                    f"HEAD:refs/heads/{branch}",
                ],
                cwd=workspace.path,
                timeout=300,
            ),
            "push worker branch",
        )
        pr = _find_worker_pr(self.runner, workspace.path, full_name, branch)
        if pr is None:
            base = _worker_base_branch(self.runner, workspace.path)
            body = _worker_pr_body(card, result)
            created = self.runner(
                [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    full_name,
                    "--base",
                    base,
                    "--head",
                    branch,
                    "--title",
                    card.title[:200],
                    "--body",
                    body,
                ],
                cwd=workspace.path,
                timeout=180,
            )
            _require_publish_success(created, "create worker pull request")
            pr = _find_worker_pr(self.runner, workspace.path, full_name, branch)
        if pr is None:
            raise WorkerExecutionError("TECHNICAL: worker branch was pushed but no pull request was returned")
        if bool(pr.get("isDraft")):
            number = str(pr.get("number") or "")
            _require_publish_success(
                self.runner(
                    ["gh", "pr", "ready", number, "--repo", full_name],
                    cwd=workspace.path,
                    timeout=120,
                ),
                "mark worker pull request ready",
            )
            pr = _find_worker_pr(self.runner, workspace.path, full_name, branch) or pr
        url = str(pr.get("url") or "").strip()
        head_sha = _git_head_sha(self.runner, workspace.path)
        if not url or not head_sha:
            raise WorkerExecutionError("TECHNICAL: worker pull request proof is missing URL or head SHA")
        proof = [dict(item) for item in result.proof]
        primary = proof[0] if proof else {"status": "passed", "note": result.summary}
        primary["url"] = url
        primary["note"] = (
            f"{str(primary.get('note') or result.summary).strip()} "
            f"Published by the host controller at {url} (head {head_sha})."
        ).strip()
        proof[0] = primary
        published = result.model_copy(
            update={
                "summary": f"{result.summary.rstrip('.')} PR opened: {url} at {head_sha}.",
                "proof": tuple(proof),
            }
        )
        if result.telemetry is not None:
            published.attach_telemetry(result.telemetry)
        return published

    def _promote_dependency_ready_cards(self, cards: list[QueueCard]) -> list[str]:
        by_id = {card.id: card for card in cards}
        promoted: list[str] = []
        for card in cards:
            if card.status != QueueStatus.TODO or card.metadata.get("archivedAt"):
                continue
            parents = _parent_ids(card)
            if parents and not all(
                parent in by_id and by_id[parent].status == QueueStatus.DONE for parent in parents
            ):
                continue
            promoted_card = self.reclaim_card(
                card.id,
                status=QueueStatus.READY,
                reason="TECHNICAL_managed_dispatch_promoted_dependency_ready_card",
            )
            promoted.append(promoted_card.id)
        return promoted

    def claim_card(
        self,
        card_id: str,
        *,
        owner_id: str,
        token: str,
        attempt_id: str | None = None,
    ) -> QueueCard:
        return self._card_response(
            "workboard.cards.claim",
            {
                "id": card_id,
                "ownerId": owner_id,
                "token": token,
                **({"attemptId": attempt_id} if attempt_id else {}),
            },
        )

    def _heartbeat_loop(self, stop: Event, card_id: str, owner_id: str, token: str) -> None:
        while not stop.wait(60):
            with suppress(Exception):
                self.heartbeat_card(
                    card_id,
                    owner_id=owner_id,
                    token=token,
                    note="managed OpenClaw worker process is still running",
                )

    def diagnostics(self) -> dict[str, Any]:
        return self._rpc("workboard.cards.diagnostics.refresh", {})

    def diagnostics_for_board(self, board_id: str) -> dict[str, Any]:
        """Limit global Workboard diagnostics to cards owned by this board."""
        payload = self.diagnostics()
        raw = payload.get("diagnostics")
        if not isinstance(raw, list):
            return payload
        filtered: list[dict[str, Any]] = []
        for value in cast(list[object], raw):
            if not isinstance(value, dict):
                continue
            entry = cast(dict[str, Any], value)
            card = entry.get("card")
            if not isinstance(card, dict):
                continue
            card_row = cast(dict[str, Any], card)
            metadata_value = card_row.get("metadata")
            metadata = cast(dict[str, Any], metadata_value) if isinstance(metadata_value, dict) else {}
            automation_value = metadata.get("automation")
            automation = cast(dict[str, Any], automation_value) if isinstance(automation_value, dict) else {}
            candidate = automation.get("boardId")
            if str(candidate or "").lower() == board_id.lower():
                filtered.append(entry)
        return {**payload, "diagnostics": filtered}

    def recover_ended_workers(self, board_id: str, cards: list[QueueCard]) -> tuple[str, ...]:
        """Reconcile ended sessions and expired claims without completion proof."""
        del board_id
        self._recovery_warnings = []
        recovered: list[str] = []
        for card in cards:
            if card.status != QueueStatus.RUNNING or card.metadata.get("archivedAt"):
                continue
            try:
                if _latest_attempt_ended(card):
                    self.reclaim_card(
                        card.id,
                        status=QueueStatus.REVIEW,
                        reason="TECHNICAL_worker_attempt_ended_without_MAKE_IT_SO_completion_proof",
                    )
                    recovered.append(card.id)
                    continue
                if _claim_expired(card):
                    self.reclaim_card(
                        card.id,
                        status=QueueStatus.REVIEW,
                        reason="TECHNICAL_worker_claim_expired_without_heartbeat",
                    )
                    recovered.append(card.id)
                    continue
                if _claim_heartbeat_stale(card):
                    self.reclaim_card(
                        card.id,
                        status=QueueStatus.REVIEW,
                        reason="TECHNICAL_worker_heartbeat_stale_after_runtime_restart",
                    )
                    recovered.append(card.id)
                    continue
                session_key = _session_key(card)
                if not session_key or not self._session_ended(session_key):
                    continue
                self.reclaim_card(
                    card.id,
                    status=QueueStatus.REVIEW,
                    reason="TECHNICAL_worker_session_ended_without_MAKE_IT_SO_completion_proof",
                )
                recovered.append(card.id)
            except Exception as exc:
                self._recovery_warnings.append(f"Worker recovery failed for card {card.id}: {str(exc)[:800]}")
        return tuple(recovered)

    def recovery_warnings(self) -> tuple[str, ...]:
        """Return warnings from the latest recovery pass without hiding them."""
        return tuple(self._recovery_warnings)

    def _session_ended(self, session_key: str) -> bool:
        result = self.runner(
            [
                self.config.executable,
                "sessions",
                "tail",
                "--session-key",
                session_key,
                "--tail",
                "80",
            ],
            timeout=20,
        )
        output = "\n".join(value for value in (result.stdout, result.stderr) if value)
        if result.returncode:
            normalized = output.lower()
            if any(
                marker in normalized for marker in ("session not found", "unknown session", "no such session")
            ):
                return True
            self._recovery_warnings.append(
                f"Could not inspect OpenClaw session {session_key}: {output[:800] or 'unknown gateway error'}"
            )
            return False
        return _session_output_ended(output)

    def validate_worker_models(self) -> dict[str, Any]:
        """Verify worker models, lifecycle tools, and host concurrency before dispatch."""
        result = self.runner(
            [self.config.executable, "agents", "list", "--json"],
            timeout=60,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()[:2000]
            raise OpenClawWorkboardError(f"agent model health check failed: {detail}")
        raw = decode_openclaw_json(result.stdout)
        if not isinstance(raw, list):
            raise OpenClawWorkboardError("agent model health check did not return an array")
        observed: dict[str, Any] = {}
        for item in cast(list[object], raw):
            if not isinstance(item, dict):
                continue
            row = cast(dict[str, Any], item)
            if row.get("id"):
                observed[str(row["id"])] = row.get("model")
        expected = _worker_models(self.config)
        runtimes = _worker_runtimes(self.config)
        mismatches = [
            {
                "agent_id": agent_id,
                "expected_model": model,
                "observed_model": observed.get(agent_id),
                "reason": "missing agent" if agent_id not in observed else "model mismatch",
            }
            for agent_id, model in expected.items()
            if agent_id not in observed
            or (
                runtimes.get(agent_id, "openclaw") == "openclaw"
                and (
                    not isinstance(observed.get(agent_id), str)
                    or not models_match(model, str(observed[agent_id]))
                )
            )
        ]
        codex_check: dict[str, Any] = {"required": False, "status": "not_required"}
        if "codex" in runtimes.values():
            codex_check = {"required": True, "status": "unavailable"}
            if self.config.codex_executable:
                codex_result = self.runner([self.config.codex_executable, "--version"], timeout=60)
                codex_check = {
                    "required": True,
                    "status": "ok" if codex_result.returncode == 0 else "unavailable",
                    "executable": self.config.codex_executable,
                    "version": codex_result.stdout.strip()[:200] or None,
                    "error": (codex_result.stderr or codex_result.stdout).strip()[:500]
                    if codex_result.returncode
                    else None,
                }
        tools = self._config_object("tools")
        allow_value = tools.get("allow")
        allowed = (
            {str(value) for value in cast(list[object], allow_value)}
            if isinstance(allow_value, list)
            else None
        )
        missing_tools = (
            [tool for tool in REQUIRED_WORKER_TOOLS if tool not in allowed] if allowed is not None else []
        )
        subagents = self._config_object("agents.defaults.subagents")
        observed_concurrency = subagents.get("maxConcurrent", 8)
        concurrency_valid = (
            isinstance(observed_concurrency, int)
            and not isinstance(observed_concurrency, bool)
            and observed_concurrency <= self.config.max_concurrent_subagents
        )
        return {
            "status": "degraded"
            if mismatches or missing_tools or not concurrency_valid or codex_check["status"] == "unavailable"
            else "ok",
            "checked_agents": len(expected),
            "mismatches": mismatches,
            "worker_runtimes": runtimes,
            "codex": codex_check,
            "missing_worker_tools": missing_tools,
            "max_concurrent_subagents": {
                "expected_max": self.config.max_concurrent_subagents,
                "observed": observed_concurrency,
                "valid": concurrency_valid,
            },
        }

    def _config_object(self, path: str) -> dict[str, Any]:
        result = self.runner(
            [self.config.executable, "config", "get", path, "--json"],
            timeout=60,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()[:2000]
            raise OpenClawWorkboardError(f"OpenClaw runtime safety check failed for {path}: {detail}")
        raw = decode_openclaw_json(result.stdout)
        if not isinstance(raw, dict):
            raise OpenClawWorkboardError(f"OpenClaw runtime safety check for {path} did not return an object")
        return cast(dict[str, Any], raw)

    @staticmethod
    def _completion_proof(proof: tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
        if len(proof) > 1:
            raise OpenClawWorkboardError(
                "OpenClaw Workboard completion accepts exactly one structured proof record"
            )
        return proof[0] if proof else None

    def _card_response(self, method: str, params: dict[str, Any]) -> QueueCard:
        payload = self._rpc(method, params)
        raw = payload.get("card")
        if not isinstance(raw, dict):
            raise OpenClawWorkboardError(f"{method} did not return a card object")
        return self._card(cast(dict[str, Any], raw))

    def _rpc(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: int = 30,
    ) -> dict[str, Any]:
        result = self.runner(
            [
                self.config.executable,
                "gateway",
                "call",
                method,
                "--json",
                "--timeout",
                str(timeout * 1000),
                "--params",
                json.dumps(params, separators=(",", ":"), default=str),
            ],
            timeout=timeout + 10,
        )
        if result.returncode:
            detail = "\n".join(value.strip() for value in (result.stdout, result.stderr) if value.strip())[
                :3000
            ]
            raise OpenClawWorkboardError(f"{method} failed: {detail}")
        payload = decode_openclaw_json(result.stdout)
        if not isinstance(payload, dict):
            raise OpenClawWorkboardError(f"{method} returned a non-object JSON value")
        return cast(dict[str, Any], payload)

    @staticmethod
    def _card(raw: dict[str, Any]) -> QueueCard:
        raw_id = raw.get("id")
        if raw_id is None or not str(raw_id).strip():
            raise OpenClawWorkboardError("Workboard card payload is missing a non-empty id")
        raw_metadata = raw.get("metadata")
        if raw_metadata is not None and not isinstance(raw_metadata, dict):
            raise OpenClawWorkboardError("Workboard card metadata must be an object")
        metadata = cast(dict[str, Any], raw_metadata or {})
        notes = str(raw["notes"]) if raw.get("notes") is not None else None
        metadata = {**_metadata_from_notes(notes), **metadata}
        if "discoveredHeadSha" not in metadata and notes:
            head_match = re.search(r"(?m)^Current head:\s*([0-9a-fA-F]{7,64})\s*$", notes)
            if head_match:
                metadata["discoveredHeadSha"] = head_match.group(1)
        workspace_value = raw.get("workspace")
        if workspace_value is not None and not isinstance(workspace_value, dict):
            raise OpenClawWorkboardError("Workboard card workspace must be an object")
        if not isinstance(workspace_value, dict):
            workspace_value = metadata.get("workspace")
        if not isinstance(workspace_value, dict):
            automation = metadata.get("automation")
            if isinstance(automation, dict):
                workspace_value = cast(dict[str, Any], automation).get("workspace")
        if workspace_value is not None and not isinstance(workspace_value, dict):
            raise OpenClawWorkboardError("Workboard card metadata.workspace must be an object")
        if isinstance(workspace_value, dict) and (
            "pushBranch" in workspace_value and "push_branch" not in workspace_value
        ):
            workspace_mapping = cast(dict[str, Any], workspace_value)
            workspace_value = {
                **workspace_mapping,
                "push_branch": workspace_mapping["pushBranch"],
            }
            workspace_value.pop("pushBranch", None)
        workspace = (
            WorkspaceRef.model_validate(workspace_value) if isinstance(workspace_value, dict) else None
        )
        raw_labels = raw.get("labels", [])
        if raw_labels is None:
            raw_labels = []
        if not isinstance(raw_labels, list):
            raise OpenClawWorkboardError("Workboard card labels must be an array")
        labels = cast(list[object], raw_labels)
        return QueueCard(
            id=str(raw.get("id") or ""),
            title=str(raw.get("title") or ""),
            notes=notes,
            status=QueueStatus(str(raw.get("status") or "todo")),
            priority=str(raw.get("priority") or "normal"),
            labels=tuple(str(item) for item in labels if isinstance(item, str)),
            agent_id=str(raw["agentId"]) if raw.get("agentId") else None,
            linked_session_id=_linked_session_id(raw),
            source_url=str(raw["sourceUrl"]) if raw.get("sourceUrl") else None,
            workspace=workspace,
            metadata=metadata,
        )


def decode_openclaw_json(value: str) -> object:
    try:
        return decode_first_json(value)
    except ValueError as exc:
        raise OpenClawWorkboardError("OpenClaw output did not contain valid JSON") from exc


_TERMINAL_SESSION_OUTPUT = re.compile(
    r"(?:session[. _-](?:ended|terminated|failed|crashed|aborted|killed)|"
    r"gateway\s+closed\s*\(1006\s+abnormal\s+closure\)|"
    r"[\"']?(?:status|state|event|type|name)[\"']?\s*[=:]\s*[\"']?(?:ended|completed|terminated|closed|failed|error|crashed|aborted|killed)[\"']?|"
    r"[\"'](?:ended|terminated|failed|crashed|aborted|killed)[\"']\s*[=:]\s*true)",
    re.IGNORECASE,
)


def _session_output_ended(value: str) -> bool:
    """Accept structured and human-readable terminal session events."""
    return bool(_TERMINAL_SESSION_OUTPUT.search(value))


def _bounded_title(value: str) -> str:
    if len(value) <= 180:
        return value
    return value[:177].rstrip() + "..."


def _bounded_label(value: str) -> str:
    cleaned = value.strip()
    if len(cleaned) <= 40:
        return cleaned
    return cleaned[:37].rstrip() + "..."


def _session_key(card: QueueCard) -> str | None:
    if card.linked_session_id:
        return card.linked_session_id
    attempts = card.metadata.get("attempts")
    if isinstance(attempts, list):
        for item in reversed(cast(list[object], attempts)):
            if isinstance(item, dict):
                value = cast(dict[str, object], item).get("sessionKey")
                if value:
                    return str(value)
    claim_value = card.metadata.get("claim")
    if isinstance(claim_value, dict) and card.agent_id:
        owner_id = cast(dict[str, object], claim_value).get("ownerId")
        prefix = "make-it-so-managed:"
        if isinstance(owner_id, str) and owner_id.startswith(prefix):
            attempt_id = owner_id.removeprefix(prefix).strip()
            if attempt_id:
                return f"agent:{card.agent_id}:make-it-so:worker:{card.id}:{attempt_id}"
    return None


def _linked_session_id(raw: dict[str, Any]) -> str | None:
    """Read the loose Workboard session link used by direct UI launches.

    Workboard stores this outside the Make It So attempt metadata. Preserving it
    lets recovery distinguish an ended manual launch from a live managed claim.
    """
    for key in ("sessionId", "sessionKey", "linkedSessionId", "linkedSessionKey"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _claim_expired(card: QueueCard) -> bool:
    claim_value = card.metadata.get("claim")
    if not isinstance(claim_value, dict):
        return False
    claim = cast(dict[str, Any], claim_value)
    expires_at = claim.get("expiresAt")
    return (
        isinstance(expires_at, (int, float))
        and not isinstance(expires_at, bool)
        and expires_at <= int(time.time() * 1000)
    )


def _claim_heartbeat_stale(card: QueueCard) -> bool:
    claim_value = card.metadata.get("claim")
    if not isinstance(claim_value, dict):
        return False
    claim = cast(dict[str, Any], claim_value)
    heartbeat_at = claim.get("lastHeartbeatAt") or claim.get("claimedAt")
    return (
        isinstance(heartbeat_at, (int, float))
        and not isinstance(heartbeat_at, bool)
        and heartbeat_at <= int(time.time() * 1000) - WORKER_HEARTBEAT_GRACE_SECONDS * 1000
    )


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


def _is_cancelled_card(card: QueueCard) -> bool:
    comments = card.metadata.get("comments")
    return isinstance(comments, list) and any(
        isinstance(item, dict)
        and str(cast(dict[str, object], item).get("body") or "").upper().startswith("CANCELLED:")
        for item in cast(list[object], comments)
    )


def _runtime_limit(card: QueueCard, default: int) -> int:
    automation = card.metadata.get("automation")
    if isinstance(automation, dict):
        value = cast(dict[str, object], automation).get("maxRuntimeSeconds")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return default


def _github_repo_name(source_url: str | None) -> str | None:
    if not source_url:
        return None
    parsed = urlsplit(source_url.strip())
    path = parsed.path.strip("/")
    if parsed.netloc.lower() not in {"github.com", "www.github.com"} or not path:
        return None
    parts = [item for item in path.removesuffix(".git").split("/") if item]
    return "/".join(parts[:2]) if len(parts) >= 2 else None


def _require_publish_success(result: Any, operation: str) -> None:
    if getattr(result, "returncode", 1):
        detail = str(getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
        raise WorkerExecutionError(f"EXTERNAL_ACCESS: {operation} failed: {detail[:1800]}")


def _worker_base_branch(runner: CommandRunner, workspace: Path) -> str:
    result = runner(
        ["git", "-C", str(workspace), "symbolic-ref", "refs/remotes/origin/HEAD"],
        cwd=workspace,
        timeout=30,
    )
    if result.returncode == 0:
        value = (result.stdout or "").strip().rsplit("/", 1)[-1]
        if value:
            return value
    return "main"


def _git_head_sha(runner: CommandRunner, workspace: Path) -> str:
    result = runner(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        cwd=workspace,
        timeout=30,
    )
    _require_publish_success(result, "read worker branch head")
    return (result.stdout or "").strip()


def _find_worker_pr(
    runner: CommandRunner,
    workspace: Path,
    full_name: str,
    branch: str,
) -> dict[str, Any] | None:
    result = runner(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            full_name,
            "--state",
            "open",
            "--head",
            branch,
            "--json",
            "number,url,headRefOid,isDraft",
        ],
        cwd=workspace,
        timeout=120,
    )
    _require_publish_success(result, "inspect worker pull requests")
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise WorkerExecutionError("TECHNICAL: GitHub pull-request response was not valid JSON") from exc
    if not isinstance(payload, list) or not payload:
        return None
    first = payload[0]
    return cast(dict[str, Any], first) if isinstance(first, dict) else None


def _publish_commit_message(card: QueueCard) -> str:
    title = re.sub(r"\s+", " ", card.title).strip().rstrip(".")
    return f"feat: {title[:160]}"


def _worker_pr_body(card: QueueCard, result: WorkerExecutionResult) -> str:
    proof = json.dumps(list(result.proof), indent=2, default=str)
    return (
        "## Make It So implementation\n\n"
        f"{result.summary.strip()}\n\n"
        "## Worker evidence\n\n"
        f"```json\n{proof[:7000]}\n```\n\n"
        f"Managed Workboard card: `{card.id}`\n"
        "PR publishing was performed by the host controller after the isolated worker returned."
    )


def _managed_completion_proof(
    proof: tuple[dict[str, Any], ...], summary: str = "", execution: dict[str, Any] | None = None
) -> tuple[dict[str, Any], ...]:
    """Collapse model-supplied evidence to the single proof record OpenClaw accepts."""
    if not proof:
        return ()
    primary = dict(next(
        (
            item
            for item in proof
            if str(item.get("status") or "").lower() == "passed"
            or any(str(item.get(field) or "").strip() for field in ("note", "label", "proof_note"))
        ),
        proof[0],
    ))
    note = str(primary.get("note") or primary.get("label") or primary.get("proof_note") or "")
    if not note:
        note = " | ".join(
            str(item.get(field) or "").strip()
            for item in proof
            for field in ("note", "label", "proof_note", "command")
            if str(item.get(field) or "").strip()
        )
    marker = re.search(
        r"\b(?:READY_FOR_OWNER|CONTROL_PLANE_COMPLETE|AUTO_MERGE_ALLOWED):[0-9a-fA-F]{7,64}\b",
        summary,
    )
    if marker and marker.group(0) not in note:
        note = f"{note} {marker.group(0)}".strip()
    test_evidence = primary.get("test_evidence") or primary.get("testEvidence")
    if isinstance(test_evidence, dict):
        encoded_evidence = json.dumps(test_evidence, sort_keys=True, separators=(",", ":"), default=str)
        note = f"{note}\n{TEST_EVIDENCE_MARKER}{encoded_evidence}".strip()
    return (
        {
            **primary,
            "status": str(primary.get("status") or "passed"),
            "note": note,
            **({"evidence": list(proof)} if len(proof) > 1 else {}),
            **({"execution": execution} if execution is not None else {}),
        },
    )


def _latest_attempt_ended(card: QueueCard) -> bool:
    attempts = card.metadata.get("attempts")
    if not isinstance(attempts, list):
        return False
    latest: dict[str, object] | None = None
    for item in reversed(cast(list[object], attempts)):
        if isinstance(item, dict):
            latest = cast(dict[str, object], item)
            break
    if latest is None:
        return False
    status = str(latest.get("status") or "").strip().lower()
    return status in {"stopped", "failed", "cancelled", "canceled", "timed_out", "expired"}


def _repository_name(cards: list[QueueCard]) -> str | None:
    pattern = re.compile(r"(?m)^Repository:\s*([^/\s]+/[^\s]+)\s*$")
    values = {match.group(1).strip() for card in cards for match in pattern.finditer(card.notes or "")}
    return next(iter(values)) if len(values) == 1 else None


def _workflow_scope(cards: list[QueueCard], target: QueueCard) -> list[QueueCard]:
    workflow_labels = [label for label in target.labels if label.startswith("workflow:")]
    if len(workflow_labels) != 1:
        return cards
    workflow_label = workflow_labels[0]
    return [card for card in cards if workflow_label in card.labels]


def _pull_request_numbers(cards: list[QueueCard], repo: str | None) -> set[int]:
    if repo is None:
        return set()
    pattern = re.compile(
        rf"https?://github\.com/{re.escape(repo)}/pull/(\d+)\b",
        re.IGNORECASE,
    )
    numbers: set[int] = set()
    for card in cards:
        evidence = "\n".join(
            (
                card.source_url or "",
                card.notes or "",
                json.dumps(card.metadata, sort_keys=True, default=str),
            )
        )
        numbers.update(int(match.group(1)) for match in pattern.finditer(evidence))
    return numbers


def _worker_models(config: OpenClawWorkboardConfig) -> dict[str, str]:
    workers = config.workers
    models = config.worker_models
    return {
        workers.captain: models.captain,
        workers.coder: models.coder,
        workers.reviewer: models.reviewer,
        workers.tester: models.tester,
        workers.ux_reviewer: models.ux_reviewer,
        workers.final_reviewer: models.final_reviewer,
        workers.merger: models.merger,
        workers.verifier: models.verifier,
    }


def _worker_runtimes(
    config: OpenClawWorkboardConfig,
) -> dict[str, Literal["openclaw", "codex"]]:
    workers = config.workers
    runtimes = config.worker_runtimes
    return {
        workers.captain: runtimes.captain,
        workers.coder: runtimes.coder,
        workers.reviewer: runtimes.reviewer,
        workers.tester: runtimes.tester,
        workers.ux_reviewer: runtimes.ux_reviewer,
        workers.final_reviewer: runtimes.final_reviewer,
        workers.merger: runtimes.merger,
        workers.verifier: runtimes.verifier,
    }
