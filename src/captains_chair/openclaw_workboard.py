from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, cast

from captains_chair.command import CommandRunner, run_command
from captains_chair.json_tools import decode_first_json
from captains_chair.model_policy import models_match
from captains_chair.models import OpenClawWorkboardConfig
from captains_chair.orchestration import (
    QueueCard,
    QueueCardSpec,
    QueueStatus,
    WorkerLifecycleAdapter,
    WorkQueueAdapter,
    WorkspaceRef,
)


class OpenClawWorkboardError(RuntimeError):
    pass


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
            "notes": spec.notes,
            "status": spec.status.value,
            "priority": spec.priority,
            "labels": list(spec.labels),
            "agentId": spec.agent_id or "",
            "boardId": board_id,
            "tenant": "captains-chair",
            "idempotencyKey": spec.key,
            "parents": list(spec.parents),
            "maxRuntimeSeconds": spec.max_runtime_seconds,
            "maxRetries": spec.max_retries,
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
        return self._rpc(
            "workboard.cards.dispatch",
            {"boardId": board_id},
            timeout=self.config.dispatch_timeout_seconds,
        )

    def diagnostics(self) -> dict[str, Any]:
        return self._rpc("workboard.cards.diagnostics.refresh", {})

    def recover_ended_workers(self, board_id: str, cards: list[QueueCard]) -> tuple[str, ...]:
        """Reconcile ended sessions and expired claims without completion proof."""
        del board_id
        self._recovery_warnings = []
        recovered: list[str] = []
        for card in cards:
            if card.status != QueueStatus.RUNNING or card.metadata.get("archivedAt"):
                continue
            try:
                if _claim_expired(card):
                    self.reclaim_card(
                        card.id,
                        status=QueueStatus.REVIEW,
                        reason="TECHNICAL_worker_claim_expired_without_heartbeat",
                    )
                    recovered.append(card.id)
                    continue
                session_key = _session_key(card)
                if not session_key or not self._session_ended(session_key):
                    continue
                self.reclaim_card(
                    card.id,
                    status=QueueStatus.REVIEW,
                    reason="TECHNICAL_worker_session_ended_without_CAPTAINS_CHAIR_completion_proof",
                )
                recovered.append(card.id)
            except Exception as exc:
                self._recovery_warnings.append(
                    f"Worker recovery failed for card {card.id}: {str(exc)[:800]}"
                )
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
                marker in normalized
                for marker in ("session not found", "unknown session", "no such session")
            ):
                return True
            self._recovery_warnings.append(
                f"Could not inspect OpenClaw session {session_key}: {output[:800] or 'unknown gateway error'}"
            )
            return False
        return _session_output_ended(output)

    def validate_worker_models(self) -> dict[str, Any]:
        """Verify configured OpenClaw worker agents before starting new sessions."""
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
        mismatches = [
            {
                "agent_id": agent_id,
                "expected_model": model,
                "observed_model": observed.get(agent_id),
                "reason": "missing agent" if agent_id not in observed else "model mismatch",
            }
            for agent_id, model in expected.items()
            if agent_id not in observed
            or not isinstance(observed.get(agent_id), str)
            or not models_match(model, str(observed[agent_id]))
        ]
        return {
            "status": "degraded" if mismatches else "ok",
            "checked_agents": len(expected),
            "mismatches": mismatches,
        }
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
            detail = "\n".join(
                value.strip() for value in (result.stdout, result.stderr) if value.strip()
            )[:3000]
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
        workspace_value = raw.get("workspace")
        if workspace_value is not None and not isinstance(workspace_value, dict):
            raise OpenClawWorkboardError("Workboard card workspace must be an object")
        if not isinstance(workspace_value, dict):
            workspace_value = metadata.get("workspace")
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
            WorkspaceRef.model_validate(workspace_value)
            if isinstance(workspace_value, dict)
            else None
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
            notes=str(raw["notes"]) if raw.get("notes") is not None else None,
            status=QueueStatus(str(raw.get("status") or "todo")),
            priority=str(raw.get("priority") or "normal"),
            labels=tuple(str(item) for item in labels if isinstance(item, str)),
            agent_id=str(raw["agentId"]) if raw.get("agentId") else None,
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


def _session_key(card: QueueCard) -> str | None:
    attempts = card.metadata.get("attempts")
    if not isinstance(attempts, list):
        return None
    for item in reversed(cast(list[object], attempts)):
        if isinstance(item, dict):
            value = cast(dict[str, object], item).get("sessionKey")
            if value:
                return str(value)
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
