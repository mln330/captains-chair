from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from captains_chair.models import EventRecord, RunState

ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.UNBASELINED: frozenset({RunState.BASELINE_REVIEW, RunState.BLOCKED, RunState.DEGRADED}),
    RunState.BASELINE_REVIEW: frozenset({RunState.READY, RunState.BLOCKED, RunState.DEGRADED}),
    RunState.READY: frozenset(
        {RunState.PLANNING, RunState.PR_OPEN, RunState.BASELINE_REVIEW, RunState.BLOCKED, RunState.DEGRADED}
    ),
    RunState.PLANNING: frozenset({RunState.READY, RunState.EXECUTING, RunState.BLOCKED, RunState.DEGRADED}),
    RunState.EXECUTING: frozenset({RunState.PR_OPEN, RunState.READY, RunState.BLOCKED, RunState.DEGRADED}),
    RunState.PR_OPEN: frozenset(
        {RunState.PLANNING, RunState.REVIEWING, RunState.REPAIRING, RunState.BLOCKED, RunState.DEGRADED}
    ),
    RunState.REVIEWING: frozenset(
        {
            RunState.PLANNING,
            RunState.PR_OPEN,
            RunState.REPAIRING,
            RunState.COMPLETION_READY,
            RunState.BLOCKED,
            RunState.DEGRADED,
        }
    ),
    RunState.REPAIRING: frozenset(
        {RunState.PLANNING, RunState.PR_OPEN, RunState.REVIEWING, RunState.BLOCKED, RunState.DEGRADED}
    ),
    RunState.COMPLETION_READY: frozenset(
        {
            RunState.PLANNING,
            RunState.PR_OPEN,
            RunState.REVIEWING,
            RunState.MERGED,
            RunState.READY,
            RunState.BLOCKED,
            RunState.DEGRADED,
        }
    ),
    RunState.MERGED: frozenset({RunState.POST_MERGE_VERIFICATION, RunState.DEGRADED}),
    RunState.POST_MERGE_VERIFICATION: frozenset(
        {RunState.PLANNING, RunState.READY, RunState.BLOCKED, RunState.DEGRADED}
    ),
    RunState.BLOCKED: frozenset(
        {
            RunState.PLANNING,
            RunState.PR_OPEN,
            RunState.REVIEWING,
            RunState.REPAIRING,
            RunState.BASELINE_REVIEW,
            RunState.READY,
            RunState.DEGRADED,
        }
    ),
    RunState.DEGRADED: frozenset(
        {
            RunState.PLANNING,
            RunState.EXECUTING,
            RunState.PR_OPEN,
            RunState.BASELINE_REVIEW,
            RunState.READY,
            RunState.BLOCKED,
        }
    ),
}

_ATTEMPT_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
    "output_tokens",
    "total_tokens",
)


def _direct_attempt_groups(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Attribute direct telemetry to the model that actually made each attempt."""
    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    known_fields: dict[tuple[str, ...], set[str]] = {}
    for row in rows:
        try:
            attempts = json.loads(str(row["attempts_json"]))
        except json.JSONDecodeError:
            continue
        if not isinstance(attempts, list):
            continue
        for attempt_index, raw_attempt in enumerate(cast(list[Any], attempts)):
            if not isinstance(raw_attempt, dict):
                continue
            attempt = cast(dict[str, Any], raw_attempt)
            model = str(attempt.get("model") or row["resolved_model"] or "unknown")
            stage = str(row["stage"] or row["role"])
            key = (
                str(row["repo"]),
                str(row["runtime"]),
                str(row["role"]),
                str(row["course_key"] or ""),
                str(row["work_package_key"] or ""),
                stage,
                model,
            )
            item = grouped.setdefault(
                key,
                {
                    "repo": key[0],
                    "runtime": key[1],
                    "role": key[2],
                    "course_key": row["course_key"],
                    "work_package_key": row["work_package_key"],
                    "stage": key[5],
                    "model": key[6],
                    "date": str(row["created_at"])[:10],
                    "calls": 0,
                    "fallback_attempts": 0,
                    "model_mismatch_attempts": 0,
                    "measured_calls": 0,
                    "unknown_calls": 0,
                    "breakdown_calls": 0,
                    "aggregate_only_calls": 0,
                    "aggregate_only_tokens": 0,
                    "prompt_bytes": 0,
                    "response_bytes": 0,
                    "duration_ms": 0,
                    **{field: None for field in _ATTEMPT_TOKEN_FIELDS},
                },
            )
            known = known_fields.setdefault(key, set())
            item["calls"] += 1
            if attempt_index > 0:
                item["fallback_attempts"] += 1
            if "model route mismatch" in str(attempt.get("error") or ""):
                item["model_mismatch_attempts"] += 1
            token_values = {field: attempt.get(field) for field in _ATTEMPT_TOKEN_FIELDS}
            has_telemetry = any(value is not None for value in token_values.values())
            if has_telemetry:
                item["measured_calls"] += 1
            else:
                item["unknown_calls"] += 1
            if token_values["input_tokens"] is not None and token_values["output_tokens"] is not None:
                item["breakdown_calls"] += 1
            elif token_values["total_tokens"] is not None:
                item["aggregate_only_calls"] += 1
                item["aggregate_only_tokens"] += int(token_values["total_tokens"])
            for field, value in token_values.items():
                if value is not None:
                    item[field] = int(item[field] or 0) + int(value)
                    known.add(field)
            item["prompt_bytes"] += int(attempt.get("prompt_bytes", 0) or 0)
            item["response_bytes"] += int(attempt.get("response_bytes", 0) or 0)
            item["duration_ms"] += int(attempt.get("duration_ms", 0) or 0)
    for key, item in grouped.items():
        known = known_fields[key]
        for field in _ATTEMPT_TOKEN_FIELDS:
            if field not in known:
                item[field] = None
    return list(grouped.values())


class LeaseBusyError(RuntimeError):
    pass


def _cli_owner_process_alive(owner: str) -> bool | None:
    """Return process liveness for local CLI leases; unknown owners stay fail-closed."""
    parts = owner.split(":")
    if len(parts) < 3 or parts[0] != "cli":
        return None
    try:
        pid = int(parts[2])
    except ValueError:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _usage_attempt_index(
    attempts: list[Any], external_id: str | None, root_session_id: str
) -> int | None:
    """Resolve a synced OpenClaw key to the direct call attempt it measured."""
    attempt_session_id: str | None = None
    if external_id:
        parts = external_id.rsplit(":", 2)
        if len(parts) == 3 and parts[-1] == root_session_id and parts[-2].startswith("attempt-"):
            attempt_session_id = f"{parts[-2]}:{parts[-1]}"
    if attempt_session_id:
        for index, item in enumerate(attempts):
            item_value = cast(dict[str, Any], item) if isinstance(item, dict) else None
            if item_value is not None and item_value.get("session_id") == attempt_session_id:
                return index
        return None
    for index, item in enumerate(attempts):
        item_value = cast(dict[str, Any], item) if isinstance(item, dict) else None
        if item_value is not None and not item_value.get("session_id"):
            return index
    return 0 if len(attempts) == 1 else None


def _event_from_row(row: sqlite3.Row) -> EventRecord:
    return EventRecord(
        event_id=row["event_id"],
        repo=row["repo"],
        run_id=row["run_id"],
        state=RunState(row["state"]),
        event_type=row["event_type"],
        summary=row["summary"],
        reason=row["reason"],
        fingerprint=row["fingerprint"],
        evidence=json.loads(row["evidence_json"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS repo_state (
                    repo TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    repo TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_repo_created ON events(repo, created_at DESC);
                CREATE INDEX IF NOT EXISTS events_repo_fingerprint ON events(repo, fingerprint, created_at DESC);
                CREATE TABLE IF NOT EXISTS leases (
                    repo TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS baselines (
                    repo TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    analyzed INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    session_id TEXT,
                    runtime TEXT NOT NULL DEFAULT 'legacy',
                    role TEXT NOT NULL,
                    course_key TEXT,
                    work_package_key TEXT,
                    stage TEXT,
                    resolved_model TEXT,
                    attempts_json TEXT NOT NULL,
                    input_tokens INTEGER,
                    cached_input_tokens INTEGER,
                    reasoning_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    context_tokens INTEGER,
                    prompt_bytes INTEGER NOT NULL DEFAULT 0,
                    response_bytes INTEGER NOT NULL DEFAULT 0,
                    usage_known INTEGER NOT NULL DEFAULT 0,
                    fallback_count INTEGER NOT NULL DEFAULT 0,
                    model_mismatch_count INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    prompt_fingerprint TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS external_usage (
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    role TEXT NOT NULL,
                    course_key TEXT,
                    work_package_key TEXT,
                    stage TEXT,
                    status TEXT,
                    provider TEXT,
                    model TEXT,
                    input_tokens INTEGER,
                    cached_input_tokens INTEGER,
                    reasoning_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    total_tokens_fresh INTEGER,
                    model_mismatch_count INTEGER NOT NULL DEFAULT 0,
                    prompt_bytes INTEGER NOT NULL DEFAULT 0,
                    response_bytes INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(source, external_id)
                );
                CREATE INDEX IF NOT EXISTS external_usage_repo_updated
                    ON external_usage(repo, updated_at DESC);
                CREATE TABLE IF NOT EXISTS approvals (
                    action_id TEXT PRIMARY KEY,
                    repo TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    consumed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS proposals (
                    action_id TEXT PRIMARY KEY,
                    repo TEXT NOT NULL,
                    snapshot_fingerprint TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS proposals_repo_status
                    ON proposals(repo, status, created_at DESC);
                CREATE TABLE IF NOT EXISTS active_work (
                    repo TEXT PRIMARY KEY,
                    action_id TEXT,
                    pr_number INTEGER NOT NULL,
                    branch TEXT NOT NULL,
                    head_sha TEXT,
                    status TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notification_attention (
                    repo TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    acknowledged_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(repo, fingerprint, event_type)
                );
                CREATE TABLE IF NOT EXISTS orchestration_cards (
                    repo TEXT NOT NULL,
                    card_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(repo, card_id)
                );
                """
            )
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(model_calls)")}
            migrations = {
                "session_id": "TEXT",
                "runtime": "TEXT NOT NULL DEFAULT 'legacy'",
                "input_tokens": "INTEGER",
                "cached_input_tokens": "INTEGER",
                "reasoning_tokens": "INTEGER",
                "output_tokens": "INTEGER",
                "total_tokens": "INTEGER",
                "prompt_bytes": "INTEGER NOT NULL DEFAULT 0",
                "response_bytes": "INTEGER NOT NULL DEFAULT 0",
                "usage_known": "INTEGER NOT NULL DEFAULT 0",
                "fallback_count": "INTEGER NOT NULL DEFAULT 0",
                "model_mismatch_count": "INTEGER NOT NULL DEFAULT 0",
                "duration_ms": "INTEGER NOT NULL DEFAULT 0",
                "prompt_fingerprint": "TEXT",
                "course_key": "TEXT",
                "work_package_key": "TEXT",
                "stage": "TEXT",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE model_calls ADD COLUMN {name} {definition}")
            external_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(external_usage)")}
            external_migrations = {
                "course_key": "TEXT",
                "work_package_key": "TEXT",
                "stage": "TEXT",
                "status": "TEXT",
                "reasoning_tokens": "INTEGER",
                "context_tokens": "INTEGER",
                "total_tokens_fresh": "INTEGER",
                "model_mismatch_count": "INTEGER NOT NULL DEFAULT 0",
                "prompt_bytes": "INTEGER NOT NULL DEFAULT 0",
                "response_bytes": "INTEGER NOT NULL DEFAULT 0",
                "duration_ms": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in external_migrations.items():
                if name not in external_columns:
                    conn.execute(f"ALTER TABLE external_usage ADD COLUMN {name} {definition}")

    def sync_orchestration_cards(self, repo: str, cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Persist queue observations and return only status-changing cards."""
        now = datetime.now(UTC).isoformat()
        transitions: list[dict[str, Any]] = []
        with self._connect() as conn:
            for card in cards:
                card_id = str(card["id"])
                status = str(card["status"])
                row = conn.execute(
                    "SELECT status FROM orchestration_cards WHERE repo=? AND card_id=?",
                    (repo, card_id),
                ).fetchone()
                old_status = str(row["status"]) if row else None
                if old_status != status:
                    transitions.append({"old_status": old_status, "new_status": status, **card})
                conn.execute(
                    "INSERT INTO orchestration_cards(repo,card_id,status,payload_json,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(repo,card_id) DO UPDATE SET "
                    "status=excluded.status,payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                    (repo, card_id, status, json.dumps(card, sort_keys=True, default=str), now),
                )
        return transitions

    def openclaw_session_context(self, repo: str) -> dict[str, dict[str, str]]:
        """Return stage context keyed by managed Workboard card ID.

        Managed OpenClaw session keys contain the card ID, not the repository
        name. Persisted card labels are the durable source for the worker stage.
        """
        context: dict[str, dict[str, str]] = {}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT card_id, payload_json FROM orchestration_cards WHERE repo=?",
                (repo,),
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            payload = cast(dict[str, Any], payload)
            raw_labels = payload.get("labels")
            labels: list[Any] = cast(list[Any], raw_labels) if isinstance(raw_labels, list) else []
            values = [str(label) for label in labels if isinstance(label, str)]
            values_by_prefix = {
                value.split(":", 1)[0]: value.split(":", 1)[1]
                for value in values
                if ":" in value
            }
            raw_metadata = payload.get("metadata")
            metadata: dict[str, Any] = (
                cast(dict[str, Any], raw_metadata) if isinstance(raw_metadata, dict) else {}
            )
            context_value: dict[str, str] = {}
            for key in ("course_key", "work_package_key"):
                value = payload.get(key) or metadata.get(key)
                if isinstance(value, str) and value.strip():
                    context_value[key] = value.strip()
            if values_by_prefix.get("stage"):
                context_value["stage"] = values_by_prefix["stage"]
            if values_by_prefix.get("workflow") and "work_package_key" not in context_value:
                context_value["work_package_key"] = values_by_prefix["workflow"]
            context[str(row["card_id"])] = context_value
        return context

    def approve(self, repo: str, action_id: str, approved_by: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO approvals(action_id,repo,approved_by,approved_at,consumed_at) VALUES(?,?,?,?,NULL) "
                "ON CONFLICT(action_id) DO UPDATE SET repo=excluded.repo, "
                "approved_by=excluded.approved_by, approved_at=excluded.approved_at, consumed_at=NULL",
                (action_id, repo, approved_by, datetime.now(UTC).isoformat()),
            )

    def is_approved(self, repo: str, action_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM approvals WHERE repo = ? AND action_id = ? AND consumed_at IS NULL",
                (repo, action_id),
            ).fetchone()
        return row is not None

    def consume_approval(self, repo: str, action_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE approvals SET consumed_at = ? WHERE repo = ? AND action_id = ? AND consumed_at IS NULL",
                (datetime.now(UTC).isoformat(), repo, action_id),
            )

    def save_proposal(
        self, repo: str, action_id: str, snapshot_fingerprint: str, decision: dict[str, Any]
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE proposals SET status='superseded', updated_at=? WHERE repo=? AND status='proposed'",
                (now, repo),
            )
            conn.execute(
                "INSERT INTO proposals(action_id,repo,snapshot_fingerprint,decision_json,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(action_id) DO UPDATE SET "
                "snapshot_fingerprint=excluded.snapshot_fingerprint, decision_json=excluded.decision_json, "
                "status='proposed', updated_at=excluded.updated_at",
                (
                    action_id,
                    repo,
                    snapshot_fingerprint,
                    json.dumps(decision, sort_keys=True),
                    "proposed",
                    now,
                    now,
                ),
            )

    def proposal(self, repo: str, action_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE repo=? AND action_id=?", (repo, action_id)
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["decision"] = json.loads(value.pop("decision_json"))
        return value

    def approved_proposal(self, repo: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT p.* FROM proposals p JOIN approvals a ON a.action_id=p.action_id "
                "WHERE p.repo=? AND p.status='proposed' AND a.consumed_at IS NULL "
                "ORDER BY a.approved_at DESC LIMIT 1",
                (repo,),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["decision"] = json.loads(value.pop("decision_json"))
        return value

    def set_proposal_status(self, repo: str, action_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE proposals SET status=?, updated_at=? WHERE repo=? AND action_id=?",
                (status, datetime.now(UTC).isoformat(), repo, action_id),
            )

    def save_active_work(
        self,
        repo: str,
        *,
        pr_number: int,
        branch: str,
        decision: dict[str, Any],
        action_id: str | None = None,
        head_sha: str | None = None,
        status: str = "pr_open",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO active_work(repo,action_id,pr_number,branch,head_sha,status,decision_json,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(repo) DO UPDATE SET "
                "action_id=excluded.action_id, pr_number=excluded.pr_number, branch=excluded.branch, "
                "head_sha=excluded.head_sha, status=excluded.status, decision_json=excluded.decision_json, "
                "updated_at=excluded.updated_at",
                (
                    repo,
                    action_id,
                    pr_number,
                    branch,
                    head_sha,
                    status,
                    json.dumps(decision, sort_keys=True),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def active_work(self, repo: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM active_work WHERE repo=?", (repo,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["decision"] = json.loads(value.pop("decision_json"))
        return value

    def update_active_work(
        self,
        repo: str,
        *,
        status: str | None = None,
        head_sha: str | None = None,
    ) -> None:
        current = self.active_work(repo)
        if current is None:
            return
        with self._connect() as conn:
            conn.execute(
                "UPDATE active_work SET status=?, head_sha=?, updated_at=? WHERE repo=?",
                (
                    status or str(current["status"]),
                    head_sha if head_sha is not None else current.get("head_sha"),
                    datetime.now(UTC).isoformat(),
                    repo,
                ),
            )

    def clear_active_work(self, repo: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM active_work WHERE repo=?", (repo,))

    def note_attention(self, repo: str, fingerprint: str, event_type: str) -> int:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT count FROM notification_attention "
                "WHERE repo=? AND fingerprint=? AND event_type=? AND acknowledged_at IS NULL",
                (repo, fingerprint, event_type),
            ).fetchone()
            count = int(row["count"]) + 1 if row else 1
            conn.execute(
                "INSERT INTO notification_attention(repo,fingerprint,event_type,count,acknowledged_at,updated_at) "
                "VALUES(?,?,?,?,NULL,?) ON CONFLICT(repo,fingerprint,event_type) DO UPDATE SET "
                "count=excluded.count, updated_at=excluded.updated_at",
                (repo, fingerprint, event_type, count, now),
            )
        return count

    def acknowledge_attention(self, repo: str, fingerprint: str, event_type: str | None = None) -> int:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            if event_type:
                cursor = conn.execute(
                    "UPDATE notification_attention SET acknowledged_at=?, updated_at=? "
                    "WHERE repo=? AND fingerprint=? AND event_type=? AND acknowledged_at IS NULL",
                    (now, now, repo, fingerprint, event_type),
                )
            else:
                cursor = conn.execute(
                    "UPDATE notification_attention SET acknowledged_at=?, updated_at=? "
                    "WHERE repo=? AND fingerprint=? AND acknowledged_at IS NULL",
                    (now, now, repo, fingerprint),
                )
        return cursor.rowcount

    def current_state(self, repo: str) -> RunState:
        with self._connect() as conn:
            row = conn.execute("SELECT state FROM repo_state WHERE repo = ?", (repo,)).fetchone()
        return RunState(row["state"]) if row else RunState.UNBASELINED

    def transition(self, repo: str, target: RunState) -> None:
        current = self.current_state(repo)
        if target != current and target not in ALLOWED_TRANSITIONS[current]:
            raise ValueError(f"invalid state transition for {repo}: {current.value} -> {target.value}")
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO repo_state(repo,state,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(repo) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
                (repo, target.value, now),
            )

    def record_event(
        self,
        *,
        repo: str,
        run_id: str,
        state: RunState,
        event_type: str,
        summary: str,
        reason: str,
        fingerprint: str,
        evidence: dict[str, Any] | None = None,
    ) -> EventRecord:
        event = EventRecord(
            event_id=str(uuid.uuid4()),
            repo=repo,
            run_id=run_id,
            state=state,
            event_type=event_type,
            summary=summary,
            reason=reason,
            fingerprint=fingerprint,
            evidence=evidence or {},
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    event.event_id,
                    event.repo,
                    event.run_id,
                    event.state.value,
                    event.event_type,
                    event.summary,
                    event.reason,
                    event.fingerprint,
                    json.dumps(event.evidence, sort_keys=True, default=str),
                    event.created_at.isoformat(),
                ),
            )
        return event

    def record_notification_failure(self, event: EventRecord, reason: str) -> EventRecord:
        """Persist delivery failure without losing the event that could not be sent."""
        self.transition(event.repo, RunState.DEGRADED)
        return self.record_event(
            repo=event.repo,
            run_id=event.run_id,
            state=RunState.DEGRADED,
            event_type="NOTIFICATION_FAILED",
            summary=event.summary,
            reason=reason[:2000],
            fingerprint=f"notification:{event.event_id}",
            evidence={
                "original_event": event.event_id,
                "original_event_type": event.event_type,
                "next_action": "Check the configured notification route and resend the stored event.",
                "links": event.evidence.get("links", []),
            },
        )

    def recent_events(self, repo: str, limit: int = 20) -> list[EventRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE repo = ? ORDER BY created_at DESC LIMIT ?", (repo, limit)
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def latest_operational_event(self, repo: str) -> EventRecord | None:
        """Return the latest event that represents repository/runtime progress.

        Notification failures are retained as audit events, but they must not
        hide the state evidence that controls no-progress suppression.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM events WHERE repo = ? AND event_type != ? "
                "ORDER BY created_at DESC LIMIT 1",
                (repo, "NOTIFICATION_FAILED"),
            ).fetchone()
        return _event_from_row(row) if row is not None else None

    def event_exists(self, repo: str, fingerprint: str, event_type: str) -> bool:
        """Return whether one idempotent event has already been persisted."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM events WHERE repo=? AND fingerprint=? AND event_type=? LIMIT 1",
                (repo, fingerprint, event_type),
            ).fetchone()
        return row is not None

    def repeated_fingerprint(self, repo: str, fingerprint: str, limit: int = 2) -> int:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT fingerprint FROM events WHERE repo = ? ORDER BY created_at DESC LIMIT ?",
                (repo, limit),
            ).fetchall()
        return sum(row["fingerprint"] == fingerprint for row in rows)

    @contextmanager
    def lease(self, repo: str, owner: str, ttl: timedelta = timedelta(hours=4)) -> Generator[None]:
        now = datetime.now(UTC)
        expires = now + ttl
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT owner,expires_at FROM leases WHERE repo = ?", (repo,)).fetchone()
            if row and datetime.fromisoformat(row["expires_at"]) > now and row["owner"] != owner:
                existing_owner = str(row["owner"])
                if _cli_owner_process_alive(existing_owner) is not False:
                    raise LeaseBusyError(f"repository lease is held by {existing_owner}")
                conn.execute("DELETE FROM leases WHERE repo = ? AND owner = ?", (repo, existing_owner))
            conn.execute(
                "INSERT INTO leases(repo,owner,expires_at) VALUES(?,?,?) "
                "ON CONFLICT(repo) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at",
                (repo, owner, expires.isoformat()),
            )
        try:
            yield
        finally:
            with self._connect() as conn:
                conn.execute("DELETE FROM leases WHERE repo = ? AND owner = ?", (repo, owner))

    def save_baseline(self, repo: str, fingerprint: str, artifact_path: Path, analyzed: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO baselines(repo,fingerprint,artifact_path,analyzed,created_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(repo) DO UPDATE SET fingerprint=excluded.fingerprint, "
                "artifact_path=excluded.artifact_path, analyzed=excluded.analyzed, created_at=excluded.created_at",
                (repo, fingerprint, str(artifact_path), int(analyzed), datetime.now(UTC).isoformat()),
            )

    def baseline(self, repo: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM baselines WHERE repo = ?", (repo,)).fetchone()
        return dict(row) if row else None

    def record_model_call(
        self,
        repo: str,
        run_id: str,
        role: str,
        resolved_model: str,
        attempts: Any,
        *,
        prompt: str | None = None,
        prompt_fingerprint: str | None = None,
        session_id: str | None = None,
        runtime: str = "captains_chair",
        course_key: str | None = None,
        work_package_key: str | None = None,
        stage: str | None = None,
    ) -> None:
        serialized = attempts if isinstance(attempts, str) else json.dumps(attempts, default=str)
        if prompt is not None:
            prompt_fingerprint = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        values: Any = json.loads(serialized)
        rows: list[dict[str, Any]] = []
        if isinstance(values, list):
            for raw_row in cast(list[Any], values):
                if isinstance(raw_row, dict):
                    rows.append(cast(dict[str, Any], raw_row))

        def total(name: str) -> int | None:
            items = [row[name] for row in rows if row.get(name) is not None]
            return sum(int(item) for item in items) if items else None

        input_tokens = total("input_tokens")
        cached_input_tokens = total("cached_input_tokens")
        reasoning_tokens = total("reasoning_tokens")
        output_tokens = total("output_tokens")
        total_tokens = total("total_tokens")
        prompt_bytes = sum(int(row.get("prompt_bytes", 0)) for row in rows)
        response_bytes = sum(int(row.get("response_bytes", 0)) for row in rows)
        model_mismatch_count = sum(
            1 for row in rows if "model route mismatch" in str(row.get("error") or "")
        )
        duration_ms = sum(int(row.get("duration_ms", 0)) for row in rows)
        usage_known = int(any(value is not None for value in (input_tokens, cached_input_tokens, output_tokens, total_tokens)))
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO model_calls(repo,run_id,session_id,runtime,role,course_key,work_package_key,stage,resolved_model,attempts_json,input_tokens,"
                "cached_input_tokens,reasoning_tokens,output_tokens,total_tokens,prompt_bytes,response_bytes,usage_known,"
                "fallback_count,model_mismatch_count,duration_ms,prompt_fingerprint,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    repo,
                    run_id,
                    session_id,
                    runtime.strip() or "captains_chair",
                    role,
                    course_key,
                    work_package_key,
                    stage or role,
                    resolved_model,
                    serialized,
                    input_tokens,
                    cached_input_tokens,
                    reasoning_tokens,
                    output_tokens,
                    total_tokens,
                    prompt_bytes,
                    response_bytes,
                    usage_known,
                    max(0, len(rows) - 1),
                    model_mismatch_count,
                    duration_ms,
                    prompt_fingerprint,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def usage_dimensions(self, repo: str) -> list[dict[str, Any]]:
        """Group direct and OpenClaw worker telemetry by stage and model."""
        summary = self.usage_summary(repo=repo)
        dimensions: list[dict[str, Any]] = []
        for group in cast(list[dict[str, Any]], summary.get("direct_attempt_groups", [])):
            model = str(group.get("model") or "unknown")
            if "/" not in model and str(group.get("runtime") or "").lower() == "codex":
                model = f"codex/{model}"
            dimensions.append(
                {
                    "date": group.get("date"),
                    "course_key": group.get("course_key"),
                    "work_package_key": group.get("work_package_key"),
                    "stage": group.get("stage"),
                    "model": model,
                    "calls": group.get("calls", 0),
                    "tokens": group.get("total_tokens")
                    or sum(
                        int(group.get(field) or 0)
                        for field in ("input_tokens", "cached_input_tokens", "output_tokens")
                    ),
                }
            )
        for group in cast(list[dict[str, Any]], summary.get("external_groups", [])):
            provider = str(group.get("provider") or "unknown")
            model_name = str(group.get("model") or "unknown")
            model = model_name if "/" in model_name else f"{provider}/{model_name}"
            dimensions.append(
                {
                    "date": group.get("date"),
                    "course_key": group.get("course_key"),
                    "work_package_key": group.get("work_package_key"),
                    "stage": group.get("stage") or group.get("role"),
                    "model": model,
                    "calls": group.get("calls", 0),
                    "tokens": group.get("total_tokens")
                    or sum(
                        int(group.get(field) or 0)
                        for field in ("input_tokens", "cached_input_tokens", "output_tokens")
                    ),
                }
            )
        return sorted(dimensions, key=lambda item: (str(item["date"]), int(item["tokens"])), reverse=True)

    def direct_session_ids(self, repo: str) -> set[str]:
        """Return opaque direct-harness IDs that may be correlated with OpenClaw sessions."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_id FROM model_calls WHERE repo=? AND session_id IS NOT NULL",
                (repo,),
            ).fetchall()
        return {str(row["session_id"]) for row in rows if row["session_id"]}

    def enrich_model_call_usage(
        self,
        session_id: str,
        record: dict[str, Any],
        *,
        external_id: str | None = None,
    ) -> bool:
        """Merge one provider session's counters into its direct-call attempt."""
        total_tokens = record.get("total_tokens")
        if record.get("total_tokens_fresh") is False:
            total_tokens = None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, attempts_json FROM model_calls WHERE session_id=? "
                "ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if row is None:
                return False
            try:
                attempts_value = json.loads(str(row["attempts_json"]))
            except json.JSONDecodeError:
                return False
            if not isinstance(attempts_value, list):
                return False
            attempts = cast(list[Any], attempts_value)
            attempt_index = _usage_attempt_index(attempts, external_id, session_id)
            if attempt_index is None:
                return False
            attempt = attempts[attempt_index]
            if not isinstance(attempt, dict):
                return False
            attempt_value = cast(dict[str, Any], attempt)
            attempt_value.update(
                {
                    "input_tokens": record.get("input_tokens"),
                    "cached_input_tokens": record.get("cached_input_tokens"),
                    "reasoning_tokens": record.get("reasoning_tokens"),
                    "output_tokens": record.get("output_tokens"),
                    "total_tokens": total_tokens,
                    "prompt_bytes": record.get("prompt_bytes", 0),
                    "response_bytes": record.get("response_bytes", 0),
                    "usage_source": record.get("source"),
                }
            )

            def total(name: str) -> int | None:
                values: list[Any] = []
                for item in attempts:
                    if isinstance(item, dict):
                        values.append(cast(dict[str, Any], item).get(name))
                known = [int(value) for value in values if value is not None]
                return sum(known) if known else None

            input_tokens = total("input_tokens")
            cached_input_tokens = total("cached_input_tokens")
            reasoning_tokens = total("reasoning_tokens")
            output_tokens = total("output_tokens")
            total_tokens_value = total("total_tokens")
            prompt_bytes = sum(
                int(cast(dict[str, Any], item).get("prompt_bytes", 0))
                for item in attempts
                if isinstance(item, dict)
            )
            response_bytes = sum(
                int(cast(dict[str, Any], item).get("response_bytes", 0))
                for item in attempts
                if isinstance(item, dict)
            )
            usage_known = int(
                any(
                    value is not None
                    for value in (
                        input_tokens,
                        cached_input_tokens,
                        output_tokens,
                        total_tokens_value,
                    )
                )
            )
            cursor = conn.execute(
                "UPDATE model_calls SET attempts_json=?,input_tokens=?,cached_input_tokens=?,"
                "reasoning_tokens=?,output_tokens=?,total_tokens=?,prompt_bytes=?,response_bytes=?,"
                "usage_known=? WHERE id=?",
                (
                    json.dumps(attempts, sort_keys=True, default=str),
                    input_tokens,
                    cached_input_tokens,
                    reasoning_tokens,
                    output_tokens,
                    total_tokens_value,
                    prompt_bytes,
                    response_bytes,
                    usage_known,
                    row["id"],
                ),
            )
        return cursor.rowcount > 0

    def record_external_usage(self, record: dict[str, Any]) -> None:
        required = ("source", "external_id", "repo", "role")
        if any(not str(record.get(key, "")).strip() for key in required):
            raise ValueError(f"external usage record requires {required}")
        activity_at = _external_activity_timestamp(record)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO external_usage(source,external_id,repo,role,course_key,work_package_key,stage,status,provider,model,input_tokens,"
                "cached_input_tokens,reasoning_tokens,output_tokens,total_tokens,total_tokens_fresh,context_tokens,"
                "model_mismatch_count,prompt_bytes,response_bytes,duration_ms,updated_at,payload_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source,external_id) DO UPDATE SET repo=excluded.repo,role=excluded.role,"
                "course_key=excluded.course_key,work_package_key=excluded.work_package_key,stage=excluded.stage,status=excluded.status,"
                "provider=excluded.provider,model=excluded.model,input_tokens=excluded.input_tokens,"
                "cached_input_tokens=excluded.cached_input_tokens,reasoning_tokens=excluded.reasoning_tokens,"
                "output_tokens=excluded.output_tokens,"
                "total_tokens=excluded.total_tokens,total_tokens_fresh=excluded.total_tokens_fresh,"
                "context_tokens=excluded.context_tokens,model_mismatch_count=excluded.model_mismatch_count,"
                "prompt_bytes=excluded.prompt_bytes,"
                "response_bytes=excluded.response_bytes,duration_ms=excluded.duration_ms,"
                "updated_at=excluded.updated_at,payload_json=excluded.payload_json",
                (
                    str(record["source"]),
                    str(record["external_id"]),
                    str(record["repo"]),
                    str(record["role"]),
                    record.get("course_key"),
                    record.get("work_package_key"),
                    record.get("stage") or record.get("role"),
                    record.get("status"),
                    record.get("provider"),
                    record.get("model"),
                    record.get("input_tokens"),
                    record.get("cached_input_tokens"),
                    record.get("reasoning_tokens"),
                    record.get("output_tokens"),
                    record.get("total_tokens"),
                    record.get("total_tokens_fresh"),
                    record.get("context_tokens"),
                    record.get("model_mismatch_count", 0),
                    record.get("prompt_bytes", 0),
                    record.get("response_bytes", 0),
                    record.get("duration_ms", 0),
                    activity_at,
                    json.dumps(record, sort_keys=True, default=str),
                ),
            )

    def prune_usage(self, retention_days: int, *, now: datetime | None = None) -> dict[str, int]:
        """Delete usage rows older than the configured retention window."""
        if retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
        cutoff_value = cutoff.astimezone(UTC).isoformat()
        with self._connect() as conn:
            direct = conn.execute(
                "DELETE FROM model_calls WHERE created_at < ?", (cutoff_value,)
            ).rowcount
            external = conn.execute(
                "DELETE FROM external_usage WHERE updated_at < ?", (cutoff_value,)
            ).rowcount
        return {"model_calls": max(0, direct), "external_usage": max(0, external)}

    def usage_summary(self, repo: str | None = None, since: str | None = None) -> dict[str, Any]:
        filters: list[str] = []
        params: list[Any] = []
        if repo:
            filters.append("repo = ?")
            params.append(repo)
        if since:
            filters.append("created_at >= ?")
            params.append(since)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        external_filters: list[str] = []
        external_params: list[Any] = []
        if repo:
            external_filters.append("repo = ?")
            external_params.append(repo)
        if since:
            external_filters.append("updated_at >= ?")
            external_params.append(since)
        external_where = " WHERE " + " AND ".join(external_filters) if external_filters else ""
        with self._connect() as conn:
            calls = conn.execute(
                "SELECT COUNT(*) AS calls, SUM(CASE WHEN runtime='legacy' THEN 1 ELSE 0 END) AS legacy_calls, "
                "SUM(fallback_count) AS fallback_attempts, "
                "SUM(model_mismatch_count) AS model_mismatch_attempts, "
                "SUM(usage_known) AS measured_calls, SUM(CASE WHEN usage_known=0 THEN 1 ELSE 0 END) AS unknown_calls, "
                "SUM(CASE WHEN input_tokens IS NOT NULL AND output_tokens IS NOT NULL THEN 1 ELSE 0 END) AS breakdown_calls, "
                "SUM(CASE WHEN total_tokens IS NOT NULL AND (input_tokens IS NULL OR output_tokens IS NULL) THEN 1 ELSE 0 END) AS aggregate_only_calls, "
                "SUM(CASE WHEN total_tokens IS NOT NULL AND (input_tokens IS NULL OR output_tokens IS NULL) THEN total_tokens ELSE 0 END) AS aggregate_only_tokens, "
                "SUM(input_tokens) AS input_tokens, SUM(cached_input_tokens) AS cached_input_tokens, "
                "SUM(reasoning_tokens) AS reasoning_tokens, "
                "SUM(output_tokens) AS output_tokens, SUM(total_tokens) AS total_tokens, "
                "SUM(prompt_bytes) AS prompt_bytes, SUM(response_bytes) AS response_bytes, "
                "SUM(duration_ms) AS duration_ms FROM model_calls" + where,
                params,
            ).fetchone()
            groups = conn.execute(
                "SELECT repo, runtime, role, resolved_model AS model, COUNT(*) AS calls, SUM(fallback_count) AS fallback_attempts, "
                "SUM(model_mismatch_count) AS model_mismatch_attempts, "
                "SUM(usage_known) AS measured_calls, SUM(input_tokens) AS input_tokens, "
                "SUM(CASE WHEN input_tokens IS NOT NULL AND output_tokens IS NOT NULL THEN 1 ELSE 0 END) AS breakdown_calls, "
                "SUM(CASE WHEN total_tokens IS NOT NULL AND (input_tokens IS NULL OR output_tokens IS NULL) THEN 1 ELSE 0 END) AS aggregate_only_calls, "
                "SUM(CASE WHEN total_tokens IS NOT NULL AND (input_tokens IS NULL OR output_tokens IS NULL) THEN total_tokens ELSE 0 END) AS aggregate_only_tokens, "
                "SUM(cached_input_tokens) AS cached_input_tokens, SUM(reasoning_tokens) AS reasoning_tokens, "
                "SUM(output_tokens) AS output_tokens, "
                "SUM(total_tokens) AS total_tokens, SUM(prompt_bytes) AS prompt_bytes, "
                "SUM(response_bytes) AS response_bytes, SUM(duration_ms) AS duration_ms "
                "FROM model_calls" + where + " GROUP BY repo, runtime, role, resolved_model ORDER BY total_tokens DESC NULLS LAST, calls DESC",
                params,
            ).fetchall()
            external = conn.execute(
                "SELECT COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, SUM(cached_input_tokens) AS cached_input_tokens, "
                "SUM(reasoning_tokens) AS reasoning_tokens, "
                "SUM(output_tokens) AS output_tokens, SUM(total_tokens) AS total_tokens, "
                "SUM(model_mismatch_count) AS model_mismatch_attempts, "
                "SUM(CASE WHEN (input_tokens IS NOT NULL AND output_tokens IS NOT NULL) OR status='failed' THEN 1 ELSE 0 END) AS breakdown_sessions, "
                "SUM(CASE WHEN total_tokens IS NOT NULL AND (input_tokens IS NULL OR output_tokens IS NULL) THEN 1 ELSE 0 END) AS aggregate_only_sessions, "
                "SUM(CASE WHEN total_tokens IS NOT NULL AND (input_tokens IS NULL OR output_tokens IS NULL) THEN total_tokens ELSE 0 END) AS aggregate_only_tokens, "
                "SUM(CASE WHEN total_tokens IS NOT NULL AND total_tokens_fresh = 0 THEN 1 ELSE 0 END) AS stale_total_sessions, "
                "SUM(CASE WHEN total_tokens IS NOT NULL AND total_tokens_fresh = 0 THEN total_tokens ELSE 0 END) AS stale_total_tokens, "
                "MAX(context_tokens) AS max_context_tokens, AVG(context_tokens) AS average_context_tokens, "
                "SUM(prompt_bytes) AS prompt_bytes, SUM(response_bytes) AS response_bytes, SUM(duration_ms) AS duration_ms, "
                "SUM(CASE WHEN input_tokens IS NULL AND cached_input_tokens IS NULL AND output_tokens IS NULL "
                "AND total_tokens IS NULL AND COALESCE(status,'') != 'failed' THEN 1 ELSE 0 END) AS unknown_sessions, "
                "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_sessions FROM external_usage" + external_where,
                external_params,
            ).fetchone()
            external_groups = conn.execute(
                "SELECT repo, substr(updated_at,1,10) AS date, role, course_key, work_package_key, stage, provider, model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, "
                "SUM(cached_input_tokens) AS cached_input_tokens, SUM(reasoning_tokens) AS reasoning_tokens, "
                "SUM(output_tokens) AS output_tokens, "
                "SUM(total_tokens) AS total_tokens, SUM(model_mismatch_count) AS model_mismatch_attempts, "
                "SUM(prompt_bytes) AS prompt_bytes, "
                "SUM(CASE WHEN (input_tokens IS NOT NULL AND output_tokens IS NOT NULL) OR status='failed' THEN 1 ELSE 0 END) AS breakdown_sessions, "
                "SUM(CASE WHEN total_tokens IS NOT NULL AND (input_tokens IS NULL OR output_tokens IS NULL) THEN 1 ELSE 0 END) AS aggregate_only_sessions, "
                "SUM(CASE WHEN total_tokens IS NOT NULL AND (input_tokens IS NULL OR output_tokens IS NULL) THEN total_tokens ELSE 0 END) AS aggregate_only_tokens, "
                "SUM(CASE WHEN total_tokens IS NOT NULL AND total_tokens_fresh = 0 THEN 1 ELSE 0 END) AS stale_total_sessions, "
                "SUM(CASE WHEN total_tokens IS NOT NULL AND total_tokens_fresh = 0 THEN total_tokens ELSE 0 END) AS stale_total_tokens, "
                "MAX(context_tokens) AS max_context_tokens, AVG(context_tokens) AS average_context_tokens, "
                "SUM(response_bytes) AS response_bytes, SUM(duration_ms) AS duration_ms, "
                "SUM(CASE WHEN input_tokens IS NULL AND cached_input_tokens IS NULL "
                "AND output_tokens IS NULL AND total_tokens IS NULL THEN 1 ELSE 0 END) AS unknown_sessions "
                "FROM external_usage" + external_where
                + " GROUP BY repo, date, role, course_key, work_package_key, stage, provider, model ORDER BY total_tokens DESC NULLS LAST, calls DESC",
                external_params,
            ).fetchall()
            repeated_prompts = conn.execute(
                "SELECT repo, role, resolved_model AS model, prompt_fingerprint, COUNT(*) AS calls, "
                "SUM(input_tokens) AS input_tokens, SUM(cached_input_tokens) AS cached_input_tokens, "
                "SUM(output_tokens) AS output_tokens, SUM(total_tokens) AS total_tokens, "
                "SUM(prompt_bytes) AS prompt_bytes, SUM(duration_ms) AS duration_ms "
                "FROM model_calls WHERE prompt_fingerprint IS NOT NULL"
                + (" AND " + " AND ".join(filters) if filters else "")
                + " GROUP BY repo, role, resolved_model, prompt_fingerprint HAVING COUNT(*) > 1 "
                "ORDER BY calls DESC, prompt_bytes DESC LIMIT 50",
                params,
            ).fetchall()
            attempt_records = conn.execute(
                "SELECT repo, role, resolved_model AS model, attempts_json FROM model_calls" + where,
                params,
            ).fetchall()
            model_call_rows = conn.execute(
                "SELECT repo, runtime, role, course_key, work_package_key, stage, resolved_model, "
                "attempts_json, created_at FROM model_calls" + where,
                params,
            ).fetchall()
        return {
            "direct_calls": dict(calls) if calls else {},
            "direct_groups": [dict(row) for row in groups],
            "direct_attempt_groups": _direct_attempt_groups(model_call_rows),
            "external_sessions": dict(external) if external else {},
            "external_groups": [dict(row) for row in external_groups],
            "repeated_prompts": [dict(row) for row in repeated_prompts],
            "attempt_records": [dict(row) for row in attempt_records],
        }


def _external_activity_timestamp(record: dict[str, Any]) -> str:
    value = record.get("updated_at_ms")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return datetime.fromtimestamp(value / 1000, UTC).isoformat()
    value = record.get("updated_at") or record.get("observed_at")
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC).isoformat()
        except ValueError:
            pass
    return datetime.now(UTC).isoformat()
