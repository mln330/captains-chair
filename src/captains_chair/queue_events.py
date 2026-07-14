from __future__ import annotations

from typing import Any, cast

from captains_chair.models import EventRecord, RepoConfig
from captains_chair.orchestration import QueueCard, QueueStatus, WorkStage, classify_blocker
from captains_chair.state import StateStore


def project_queue_events(
    state: StateStore,
    repo: RepoConfig,
    cards: list[QueueCard],
    *,
    protocol_retries: tuple[str, ...] = (),
    technical_retries: tuple[str, ...] = (),
    repairs_created: tuple[str, ...] = (),
    control_plane_recoveries: tuple[str, ...] = (),
    workspace_cleanup_failures: tuple[str, ...] = (),
    recovery_warnings: tuple[str, ...] = (),
    diagnostics: dict[str, Any] | None = None,
) -> list[EventRecord]:
    """Persist queue observations and emit concise, runtime-neutral transition events."""
    rows = [
        {
            "id": card.id,
            "title": card.title,
            "status": card.status.value,
            "labels": list(card.labels),
            "agent_id": card.agent_id,
            "source_url": card.source_url,
        }
        for card in cards
    ]
    transitions = state.sync_orchestration_cards(repo.full_name, rows)
    by_id = {card.id: card for card in cards}
    events: list[EventRecord] = []
    for card_id in protocol_retries:
        card = by_id.get(card_id)
        if card is None:
            continue
        stage = _card_stage(card)
        dispatch_count = _dispatch_count(card)
        fingerprint = f"queue:{card.id}:protocol-retry:{dispatch_count}"
        if state.event_exists(repo.full_name, fingerprint, "WORK_REQUEUED"):
            continue
        events.append(
            state.record_event(
                repo=repo.full_name,
                run_id=card.id[:16],
                state=state.current_state(repo.full_name),
                event_type="WORK_REQUEUED",
                summary=f"{stage or 'Work'} was requeued after an incomplete worker handoff.",
                reason=(
                    "The worker runtime ended the run without the mandatory CAPTAINS_CHAIR completion proof; "
                    "the stage was automatically retried."
                ),
                fingerprint=fingerprint,
                evidence={
                    "card_id": card.id,
                    "stage": stage,
                    "worker": card.agent_id,
                    "next_action": (
                        "The assigned worker will retry this stage and submit structured proof."
                    ),
                    "links": [card.source_url] if card.source_url else [],
                },
            )
        )

    _append_queue_action_events(
        state,
        repo,
        by_id,
        events,
        technical_retries=technical_retries,
        repairs_created=repairs_created,
        control_plane_recoveries=control_plane_recoveries,
    )

    notable_done = {
        WorkStage.IMPLEMENTATION.value,
        WorkStage.REPAIR.value,
        WorkStage.FINAL_REVIEW.value,
        WorkStage.MERGE.value,
        WorkStage.POST_MERGE.value,
    }
    transitioned_ids = {str(item["id"]) for item in transitions}
    for transition in transitions:
        card = by_id[str(transition["id"])]
        stage = _card_stage(card)
        event_type: str | None = None
        summary = card.title
        reason = f"Worker {card.agent_id or 'unassigned'} changed the queue card."
        next_action: str | None = None
        if card.status == QueueStatus.RUNNING and stage == WorkStage.IMPLEMENTATION.value:
            event_type = "WORK_STARTED"
            next_action = "The coder is implementing this item; dependent review cards remain gated."
        elif card.status == QueueStatus.READY and _has_protocol_recovery_comment(card):
            event_type = "WORK_REQUEUED"
            summary = f"{stage or 'Work'} was requeued after an incomplete worker handoff."
            reason = _latest_comment(card)
            next_action = "The assigned worker will retry this stage and submit structured proof."
        elif card.status == QueueStatus.DONE and stage in notable_done:
            event_type = {
                WorkStage.REPAIR.value: "PR_REPAIRED",
                WorkStage.FINAL_REVIEW.value: "COMPLETION_READY",
                WorkStage.MERGE.value: "PR_MERGED",
                WorkStage.POST_MERGE.value: "POST_MERGE_VERIFIED",
            }.get(stage, "WORK_COMPLETED")
            summary = _completion_summary(card) or summary
            reason = f"Worker {card.agent_id or 'unassigned'} completed the {stage} card with passed proof."
            next_action = {
                WorkStage.IMPLEMENTATION.value: "Independent review, test, and applicable UX gates will run next.",
                WorkStage.REPAIR.value: "The blocked independent gate will rerun on the repaired head.",
                WorkStage.FINAL_REVIEW.value: "The deterministic merge gate will verify the current head next.",
                WorkStage.MERGE.value: "Post-merge verification will inspect the default branch and CI.",
                WorkStage.POST_MERGE.value: "This workflow is complete; the Captain can select the next work item.",
            }[stage]
        elif card.status == QueueStatus.BLOCKED:
            attention = _owner_blocked_event(state, repo, card)
            if attention is not None:
                events.append(attention)
            continue
        if event_type is None:
            continue
        fingerprint = f"queue:{card.id}:{card.status.value}"
        proof = _passed_proof(card)
        links = [card.source_url] if card.source_url else []
        proof_url = str(proof.get("url") or "") if proof else ""
        if proof_url and proof_url not in links:
            links.append(proof_url)
        evidence: dict[str, Any] = {
            "card_id": card.id,
            "stage": stage,
            "worker": card.agent_id,
            "next_action": next_action,
            "links": links,
        }
        if proof:
            evidence["proof_label"] = proof.get("label")
            evidence["proof_note"] = proof.get("note") or proof.get("command")
        events.append(
            state.record_event(
                repo=repo.full_name,
                run_id=card.id[:16],
                state=state.current_state(repo.full_name),
                event_type=event_type,
                summary=summary,
                reason=reason,
                fingerprint=fingerprint,
                evidence=evidence,
            )
        )
    # Status transitions are not enough for owner blockers: a card can remain
    # blocked across many scheduled reconciliations while the owner is away.
    # Re-emit only at ladder levels so the Captain escalates without spamming every
    # two-hour pass. Acknowledgement resets the ladder for the next decision.
    for card in cards:
        if (
            card.id in transitioned_ids
            or card.metadata.get("archivedAt")
            or card.status != QueueStatus.BLOCKED
        ):
            continue
        attention = _owner_blocked_event(state, repo, card)
        if attention is not None:
            events.append(attention)
    degraded = _diagnostic_failure_event(state, repo, diagnostics)
    if degraded is not None:
        events.append(degraded)
    events.extend(_diagnostic_events(state, repo, diagnostics))
    for failure in workspace_cleanup_failures:
        workflow_id = failure.split(":", 1)[0]
        fingerprint = f"workspace-cleanup:{workflow_id}"
        status_level = state.note_attention(repo.full_name, fingerprint, "WORKSPACE_CLEANUP_FAILED")
        if status_level not in {1, 2, 3, 4, 8, 16}:
            continue
        events.append(
            state.record_event(
                repo=repo.full_name,
                run_id=workflow_id[:16],
                state=state.current_state(repo.full_name),
                event_type="WORKSPACE_CLEANUP_FAILED",
                summary="A completed workflow still has a local worktree",
                reason=failure,
                fingerprint=fingerprint,
                evidence={
                    "status_level": status_level,
                    "next_action": "Inspect the worktree and remove it only after confirming it is clean; the GitHub branch and PR remain untouched.",
                    "links": [],
                },
            )
        )
    for warning in recovery_warnings:
        detail = str(warning)[:1000]
        fingerprint = f"queue-recovery:{detail}"
        status_level = state.note_attention(repo.full_name, fingerprint, "QUEUE_DEGRADED")
        if status_level not in {1, 2, 3, 4, 8, 16}:
            continue
        events.append(
            state.record_event(
                repo=repo.full_name,
                run_id="queue-recovery",
                state=state.current_state(repo.full_name),
                event_type="QUEUE_DEGRADED",
                summary="Worker recovery needs another pass",
                reason=detail,
                fingerprint=fingerprint,
                evidence={
                    "status_level": status_level,
                    "next_action": "Retry reconciliation; unrelated ready work continues and the affected card remains recoverable.",
                    "links": [],
                },
            )
        )
    return events


_ATTENTION_LEVELS = frozenset({1, 2, 3, 4, 8, 16})


def _owner_blocked_event(
    state: StateStore,
    repo: RepoConfig,
    card: QueueCard,
) -> EventRecord | None:
    blocker = _card_block_reason(card)
    if classify_blocker(blocker).value == "technical":
        return None
    fingerprint = f"queue:{card.id}:{card.status.value}"
    attention_level = state.note_attention(repo.full_name, fingerprint, "ATTENTION_REQUIRED")
    if attention_level not in _ATTENTION_LEVELS:
        return None
    stage = _card_stage(card)
    return state.record_event(
        repo=repo.full_name,
        run_id=card.id[:16],
        state=state.current_state(repo.full_name),
        event_type="ATTENTION_REQUIRED",
        summary=blocker or card.title,
        reason=f"The {stage or 'worker'} card requires owner input; unrelated ready work continues.",
        fingerprint=fingerprint,
        evidence={
            "card_id": card.id,
            "stage": stage,
            "worker": card.agent_id,
            "next_action": (
                "Provide the requested decision, access, or secret, then resume this card with "
                f"`captains_chair orchestrate unblock --repo {repo.full_name} --card {card.id}` and run "
                "`captains_chair orchestrate reconcile`."
            ),
            "links": [card.source_url] if card.source_url else [],
            "blocker": blocker,
            "owner_required": True,
            "attention_level": attention_level,
        },
    )


def _append_queue_action_events(
    state: StateStore,
    repo: RepoConfig,
    by_id: dict[str, QueueCard],
    events: list[EventRecord],
    *,
    technical_retries: tuple[str, ...],
    repairs_created: tuple[str, ...],
    control_plane_recoveries: tuple[str, ...],
) -> None:
    actions = (
        (
            technical_retries,
            "TECHNICAL_RETRY",
            "Technical failure was requeued automatically.",
            "The Captain reclaimed the failed card and will retry it without owner intervention.",
            "The assigned worker will retry this card; unrelated ready work continues.",
        ),
        (
            repairs_created,
            "REPAIR_QUEUED",
            "A repair card was queued automatically.",
            "An independent review, test, UX, or final-review gate produced repairable findings.",
            "The coder will address the listed findings, then the independent gate will rerun.",
        ),
        (
            control_plane_recoveries,
            "CONTROL_PLANE_RECOVERY_QUEUED",
            "Captain recovery was queued automatically.",
            "The technical retry budget was exhausted, so the Captain will replan the failure.",
            "The Captain recovery worker will inspect the evidence and select the next repairable action.",
        ),
    )
    for card_ids, event_type, summary, reason, next_action in actions:
        for card_id in card_ids:
            card = by_id.get(card_id)
            if card is None or card.metadata.get("archivedAt"):
                continue
            stage = _card_stage(card)
            dispatch_count = _dispatch_count(card)
            failure_count = _failure_count(card)
            fingerprint = f"queue-action:{event_type}:{card.id}:{dispatch_count}:{failure_count}"
            if state.event_exists(repo.full_name, fingerprint, event_type):
                continue
            links = [card.source_url] if card.source_url else []
            events.append(
                state.record_event(
                    repo=repo.full_name,
                    run_id=card.id[:16],
                    state=state.current_state(repo.full_name),
                    event_type=event_type,
                    summary=summary,
                    reason=reason,
                    fingerprint=fingerprint,
                    evidence={
                        "card_id": card.id,
                        "stage": stage,
                        "worker": card.agent_id,
                        "next_action": next_action,
                        "links": links,
                    },
                )
            )


def _diagnostic_failure_event(
    state: StateStore,
    repo: RepoConfig,
    diagnostics: dict[str, Any] | None,
) -> EventRecord | None:
    if not isinstance(diagnostics, dict) or str(diagnostics.get("status") or "").lower() != "degraded":
        return None
    error = str(diagnostics.get("error") or "The runtime returned a degraded diagnostics status.")[:1000]
    fingerprint = "queue-diagnostics:unavailable"
    status_level = state.note_attention(repo.full_name, fingerprint, "QUEUE_DEGRADED")
    if status_level not in {1, 2, 3, 4, 8, 16}:
        return None
    return state.record_event(
        repo=repo.full_name,
        run_id="queue-diagnostics",
        state=state.current_state(repo.full_name),
        event_type="QUEUE_DEGRADED",
        summary="Workboard diagnostics are unavailable",
        reason=error,
        fingerprint=fingerprint,
        evidence={
            "status_level": status_level,
            "next_action": "Check the configured queue runtime and rerun reconciliation; worker recovery remains safe to retry.",
            "links": [],
        },
    )


def _diagnostic_events(
    state: StateStore,
    repo: RepoConfig,
    diagnostics: dict[str, Any] | None,
) -> list[EventRecord]:
    if not isinstance(diagnostics, dict):
        return []

    events: list[EventRecord] = []
    seen: set[tuple[str, str, str]] = set()
    for row in _diagnostic_rows(diagnostics):
        card = row.get("card")
        card_data = cast(dict[str, Any], card) if isinstance(card, dict) else {}
        metadata = card_data.get("metadata")
        if card_data.get("archivedAt") or (
            isinstance(metadata, dict) and cast(dict[str, Any], metadata).get("archivedAt")
        ):
            continue
        card_id = str(row.get("card_id") or card_data.get("id") or "queue")
        kind = str(row.get("kind") or "diagnostic")
        title = str(row.get("title") or kind.replace("_", " ").title())
        key = (card_id, kind, title)
        if key in seen:
            continue
        seen.add(key)

        detail = str(row.get("detail") or row.get("reason") or title)
        action = _diagnostic_action(row, kind)
        source_url = str(row.get("source_url") or card_data.get("sourceUrl") or "")
        fingerprint = f"queue-diagnostic:{card_id}:{kind}:{title}"
        status_level = state.note_attention(repo.full_name, fingerprint, "QUEUE_STALLED")
        if status_level not in {1, 2, 3, 4, 8, 16}:
            continue
        evidence: dict[str, Any] = {
            "card_id": card_id,
            "diagnostic_kind": kind,
            "severity": row.get("severity"),
            "status_level": status_level,
            "next_action": action,
            "links": [source_url] if source_url else [],
        }
        events.append(
            state.record_event(
                repo=repo.full_name,
                run_id=card_id[:16],
                state=state.current_state(repo.full_name),
                event_type="QUEUE_STALLED",
                summary=title,
                reason=detail,
                fingerprint=fingerprint,
                evidence=evidence,
            )
        )
    return events


def _diagnostic_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize runtime-specific and portable queue diagnostic payloads."""
    raw = payload.get("diagnostics")
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in cast(list[Any], raw):
        if not isinstance(item, dict):
            continue
        entry = cast(dict[str, Any], item)
        nested = entry.get("diagnostics")
        if isinstance(nested, list):
            card = entry.get("card")
            for child in cast(list[Any], nested):
                if not isinstance(child, dict):
                    continue
                row = dict(cast(dict[str, Any], child))
                if isinstance(card, dict):
                    row["card"] = card
                rows.append(row)
        else:
            rows.append(entry)
    return rows


def _diagnostic_action(row: dict[str, Any], kind: str) -> str:
    actions = row.get("actions")
    if isinstance(actions, list) and actions:
        return str(cast(list[Any], actions)[0])
    if kind == "stranded_ready":
        return "Reconcile the queue; the assigned worker should be dispatched or reclaimed automatically."
    if kind == "missing_proof":
        return "Reconcile the queue; CAPTAINS_CHAIR will retry the incomplete worker handoff or route recovery."
    return "Reconcile the queue and inspect the linked card before the next cycle."


def _card_stage(card: QueueCard) -> str | None:
    return next((label.split(":", 1)[1] for label in card.labels if label.startswith("stage:")), None)


def _card_block_reason(card: QueueCard) -> str:
    protocol = card.metadata.get("workerProtocol")
    if not isinstance(protocol, dict):
        return ""
    return str(cast(dict[str, Any], protocol).get("detail") or "")


def _latest_comment(card: QueueCard) -> str:
    comments = card.metadata.get("comments")
    if isinstance(comments, list):
        for item in reversed(cast(list[Any], comments)):
            if isinstance(item, dict) and cast(dict[str, Any], item).get("body"):
                return str(cast(dict[str, Any], item)["body"])
    return "The previous worker ended without structured completion proof."


def _has_protocol_recovery_comment(card: QueueCard) -> bool:
    return "mandatory CAPTAINS_CHAIR completion proof" in _latest_comment(card)


def _dispatch_count(card: QueueCard) -> int:
    automation = card.metadata.get("automation")
    if isinstance(automation, dict):
        value = cast(dict[str, Any], automation).get("dispatchCount")
        if isinstance(value, int):
            return value
    return 0


def _failure_count(card: QueueCard) -> int:
    value = card.metadata.get("failureCount")
    if isinstance(value, int) and value >= 0:
        return value
    attempts = card.metadata.get("attempts")
    if not isinstance(attempts, list):
        return 0
    return sum(
        1
        for item in cast(list[Any], attempts)
        if isinstance(item, dict)
        and cast(dict[str, Any], item).get("status") in {"failed", "blocked", "stopped"}
    )


def _completion_summary(card: QueueCard) -> str | None:
    automation = card.metadata.get("automation")
    if not isinstance(automation, dict):
        return None
    summary = cast(dict[str, Any], automation).get("summary")
    return str(summary) if summary else None


def _passed_proof(card: QueueCard) -> dict[str, Any] | None:
    proof = card.metadata.get("proof")
    if not isinstance(proof, list):
        return None
    for item in reversed(cast(list[Any], proof)):
        if not isinstance(item, dict):
            continue
        row = cast(dict[str, Any], item)
        if str(row.get("status") or "").lower() == "passed":
            return row
    return None
