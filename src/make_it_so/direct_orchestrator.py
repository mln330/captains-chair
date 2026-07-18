from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from typing import Any, cast
from uuid import uuid4

from make_it_so.direct_workers import (
    WorkerExecutionError,
    WorkerExecutorAdapter,
)
from make_it_so.orchestration import QueueCard, QueueCardSpec, QueueStatus


class DirectOrchestrator:
    """SQLite-backed worker orchestration without an external task board.

    This adapter stores execution state and proof, not a user-facing kanban. Worker
    processes may claim ready cards through a host integration and complete them via
    the lifecycle methods shared with the OpenClaw adapter.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        executor: WorkerExecutorAdapter | None = None,
        lease_seconds: int = 3600,
        max_dispatch_workers: int = 1,
        worker_models: dict[str, str] | None = None,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("direct worker lease_seconds must be positive")
        if max_dispatch_workers < 1:
            raise ValueError("direct max_dispatch_workers must be positive")
        self.database_path = database_path
        self.executor = executor
        self.lease_seconds = lease_seconds
        self.max_dispatch_workers = max_dispatch_workers
        self.worker_models = worker_models or {}
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS direct_boards (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    workspace TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS direct_cards (
                    id TEXT PRIMARY KEY,
                    board_id TEXT NOT NULL,
                    work_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(board_id, work_key),
                    FOREIGN KEY(board_id) REFERENCES direct_boards(id)
                );
                CREATE INDEX IF NOT EXISTS idx_direct_cards_board
                    ON direct_cards(board_id);
                """
            )

    def ensure_board(self, board_id: str, name: str, description: str, workspace: Path) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO direct_boards(id,name,description,workspace) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,description=excluded.description,"
                "workspace=excluded.workspace",
                (board_id, name, description, str(workspace)),
            )

    def list_cards(self, board_id: str) -> list[QueueCard]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM direct_cards WHERE board_id=? ORDER BY rowid",
                (board_id,),
            ).fetchall()
        return [QueueCard.model_validate_json(str(row["payload_json"])) for row in rows]

    def create_card(self, board_id: str, spec: QueueCardSpec) -> QueueCard:
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM direct_cards WHERE board_id=? AND work_key=?",
                (board_id, spec.key),
            ).fetchone()
            if existing is not None:
                return QueueCard.model_validate_json(str(existing["payload_json"]))
            card = QueueCard(
                id=f"direct-{uuid4().hex}",
                title=spec.title,
                notes=spec.notes,
                status=spec.status,
                priority=spec.priority,
                labels=spec.labels,
                agent_id=spec.agent_id,
                source_url=spec.source_url,
                workspace=spec.workspace,
                metadata={
                    **spec.metadata,
                    "parents": list(spec.parents),
                    "automation": {
                        "maxRetries": spec.max_retries,
                        "maxRuntimeSeconds": spec.max_runtime_seconds,
                    },
                },
            )
            connection.execute(
                "INSERT INTO direct_cards(id,board_id,work_key,payload_json) VALUES(?,?,?,?)",
                (card.id, board_id, spec.key, card.model_dump_json()),
            )
            return card

    def complete_card(
        self,
        card_id: str,
        *,
        summary: str,
        proof: tuple[dict[str, Any], ...] = (),
        created_card_ids: tuple[str, ...] = (),
    ) -> QueueCard:
        card = self._card(card_id)
        metadata = {
            **card.metadata,
            "completion": {"summary": summary, "createdCardIds": list(created_card_ids)},
            "proof": list(proof),
        }
        return self._update(card_id, status=QueueStatus.DONE, metadata=metadata)

    def unblock_card(self, card_id: str) -> QueueCard:
        card = self._card(card_id)
        metadata = dict(card.metadata)
        metadata.pop("workerProtocol", None)
        metadata.pop("claim", None)
        return self._update(card_id, status=QueueStatus.TODO, metadata=metadata)

    def reclaim_card(self, card_id: str, *, status: QueueStatus, reason: str) -> QueueCard:
        card = self._card(card_id)
        metadata = {**card.metadata, "reclaimReason": reason}
        metadata.pop("claim", None)
        return self._update(card_id, status=status, metadata=metadata)

    def reassign_card(
        self,
        card_id: str,
        *,
        agent_id: str,
        status: QueueStatus,
        reset_failures: bool,
        reason: str,
    ) -> QueueCard:
        card = self._card(card_id)
        metadata = {**card.metadata, "reassignmentReason": reason}
        metadata.pop("claim", None)
        if reset_failures:
            metadata.pop("failures", None)
        return self._update(card_id, status=status, agent_id=agent_id, metadata=metadata)

    def comment(self, card_id: str, body: str) -> QueueCard:
        card = self._card(card_id)
        comments_value = card.metadata.get("comments", [])
        comments = list(cast(list[Any], comments_value)) if isinstance(comments_value, list) else []
        comments.append(body)
        return self._update(card_id, metadata={**card.metadata, "comments": comments})

    def dispatch(self, board_id: str) -> dict[str, Any]:
        recovered = self.recover_expired_claims(board_id)
        cards = {card.id: card for card in self.list_cards(board_id)}
        promoted: list[str] = []
        for card in cards.values():
            if card.status != QueueStatus.TODO:
                continue
            parents_value = card.metadata.get("parents", [])
            parents = (
                [str(parent) for parent in cast(list[Any], parents_value)]
                if isinstance(parents_value, list)
                else []
            )
            if all(parent in cards and cards[parent].status == QueueStatus.DONE for parent in parents):
                self._update(card.id, status=QueueStatus.READY)
                promoted.append(card.id)
        if self.executor is None:
            return {
                "status": "awaiting_external_worker",
                "promoted": promoted,
                "started": [],
                "completed": [],
                "blocked": [],
                "recovered": list(recovered),
                "count": len(promoted),
            }

        started: list[str] = []
        completed: list[str] = []
        blocked: list[str] = []
        for _ in range(self.max_dispatch_workers):
            token = uuid4().hex
            owner_id = f"direct-dispatch:{uuid4().hex}"
            card = self.claim_next_card(board_id, owner_id=owner_id, token=token)
            if card is None:
                break
            started.append(card.id)
            if self._execute_claimed(board_id, card, owner_id=owner_id, token=token):
                completed.append(card.id)
            else:
                blocked.append(card.id)
        return {
            "status": "dispatched" if started else "idle",
            "promoted": promoted,
            "started": started,
            "completed": completed,
            "blocked": blocked,
            "recovered": list(recovered),
            "count": len(set(promoted) | set(started)),
        }

    def diagnostics(self) -> dict[str, Any]:
        with self._connect() as connection:
            boards = int(connection.execute("SELECT COUNT(*) FROM direct_boards").fetchone()[0])
            cards = int(connection.execute("SELECT COUNT(*) FROM direct_cards").fetchone()[0])
        return {
            "status": "healthy",
            "kind": "direct",
            "boards": boards,
            "cards": cards,
            "worker_execution": "managed" if self.executor is not None else "external_claim_protocol",
        }

    def validate_worker_models(self) -> dict[str, Any]:
        if self.executor is None:
            return {"status": "not_supported", "reason": "external workers own model selection"}
        missing = sorted(
            {
                card.agent_id
                for board_id in self._board_ids()
                for card in self.list_cards(board_id)
                if card.status != QueueStatus.DONE
                and not card.metadata.get("archivedAt")
                and card.agent_id
                and card.agent_id not in self.worker_models
            }
        )
        if missing:
            return {"status": "degraded", "missing_agents": missing}
        return {"status": "ok", "runtime": type(self.executor).__name__}

    def claim_card(
        self,
        card_id: str,
        *,
        owner_id: str,
        token: str,
        attempt_id: str | None = None,
    ) -> QueueCard:
        if not owner_id.strip() or not token:
            raise ValueError("direct worker claims require a non-empty owner and token")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_json FROM direct_cards WHERE id=?", (card_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown direct card: {card_id}")
            card = QueueCard.model_validate_json(str(row["payload_json"]))
            if card.status != QueueStatus.READY:
                raise PermissionError(f"card {card_id} is not ready to be claimed")
            if card.metadata.get("claim") is not None:
                raise PermissionError(f"card {card_id} is already claimed")
            now = datetime.now(UTC)
            metadata = {
                **card.metadata,
                "claim": {
                    "ownerId": owner_id,
                    "token": token,
                    "attemptId": attempt_id or uuid4().hex,
                    "claimedAt": now.isoformat(),
                    "heartbeatAt": now.isoformat(),
                    "expiresAt": (now + timedelta(seconds=self.lease_seconds)).isoformat(),
                    "heartbeat": "claimed",
                },
            }
            claimed = card.model_copy(update={"status": QueueStatus.RUNNING, "metadata": metadata})
            cursor = connection.execute(
                "UPDATE direct_cards SET payload_json=? WHERE id=?",
                (claimed.model_dump_json(), card_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown direct card: {card_id}")
            return claimed

    def claim_next_card(
        self,
        board_id: str,
        *,
        owner_id: str,
        token: str,
        agent_id: str | None = None,
    ) -> QueueCard | None:
        for card in self.list_cards(board_id):
            if card.status != QueueStatus.READY:
                continue
            if agent_id is not None and card.agent_id != agent_id:
                continue
            try:
                return self.claim_card(card.id, owner_id=owner_id, token=token)
            except PermissionError:
                continue
        return None

    def heartbeat_card(self, card_id: str, *, owner_id: str, token: str, note: str) -> QueueCard:
        now = datetime.now(UTC)

        def update(card: QueueCard, claim: dict[str, Any]) -> QueueCard:
            metadata = {
                **card.metadata,
                "claim": {
                    **claim,
                    "heartbeat": note,
                    "heartbeatAt": now.isoformat(),
                    "expiresAt": (now + timedelta(seconds=self.lease_seconds)).isoformat(),
                },
            }
            return card.model_copy(update={"metadata": metadata})

        return self._claimed_update(card_id, owner_id, token, update)

    def complete_claimed_card(
        self,
        card_id: str,
        *,
        owner_id: str,
        token: str,
        summary: str,
        proof: tuple[dict[str, Any], ...],
    ) -> QueueCard:
        def update(card: QueueCard, claim: dict[str, Any]) -> QueueCard:
            metadata: dict[str, Any] = {
                **card.metadata,
                "completion": {"summary": summary, "createdCardIds": []},
                "proof": list(proof),
                "lastClaim": _redacted_claim(claim),
            }
            metadata.pop("claim", None)
            return card.model_copy(update={"status": QueueStatus.DONE, "metadata": metadata})

        return self._claimed_update(card_id, owner_id, token, update)

    def block_claimed_card(
        self,
        card_id: str,
        *,
        owner_id: str,
        token: str,
        reason: str,
    ) -> QueueCard:
        def update(card: QueueCard, claim: dict[str, Any]) -> QueueCard:
            failures = _failure_count(card)
            if reason.strip().upper().startswith("TECHNICAL"):
                failures += 1
            metadata = {
                **card.metadata,
                "workerProtocol": {"state": "blocked", "detail": reason},
                "lastClaim": _redacted_claim(claim),
                "failures": failures,
            }
            metadata.pop("claim", None)
            return card.model_copy(update={"status": QueueStatus.BLOCKED, "metadata": metadata})

        return self._claimed_update(card_id, owner_id, token, update)

    def cancel_claimed_card(self, card_id: str, *, requested_by: str, reason: str) -> QueueCard:
        """Cancel a live direct claim without allowing its late result to overwrite state."""
        if not requested_by.strip() or not reason.strip():
            raise ValueError("direct cancellation requires requester and reason")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            card = self._card_from_connection(connection, card_id)
            claim_value = card.metadata.get("claim")
            if not isinstance(claim_value, dict):
                raise PermissionError(f"card {card_id} has no active claim")
            claim = cast(dict[str, Any], claim_value)
            metadata = {
                **card.metadata,
                "workerProtocol": {
                    "state": "cancelled",
                    "detail": f"CANCELLED: {reason}",
                },
                "cancellation": {
                    "requestedBy": requested_by,
                    "reason": reason,
                    "cancelledAt": datetime.now(UTC).isoformat(),
                },
                "lastClaim": _redacted_claim(claim),
            }
            metadata.pop("claim", None)
            cancelled = card.model_copy(update={"status": QueueStatus.BLOCKED, "metadata": metadata})
            self._write_card(connection, cancelled)
            return cancelled

    def recover_expired_claims(
        self,
        board_id: str,
        *,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        recovered: list[str] = []
        observed_at = now or datetime.now(UTC)
        for current in self.list_cards(board_id):
            if current.status != QueueStatus.RUNNING:
                continue
            claim_value = current.metadata.get("claim")
            if not isinstance(claim_value, dict):
                continue
            expires_at = _parse_datetime(cast(dict[str, Any], claim_value).get("expiresAt"))
            if expires_at is None or expires_at > observed_at:
                continue
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                card = self._card_from_connection(connection, current.id)
                live_claim_value = card.metadata.get("claim")
                if not isinstance(live_claim_value, dict):
                    continue
                live_claim = cast(dict[str, Any], live_claim_value)
                live_expiry = _parse_datetime(live_claim.get("expiresAt"))
                if live_expiry is None or live_expiry > observed_at:
                    continue
                failures = _failure_count(card) + 1
                retry_limit = _retry_limit(card)
                metadata = {
                    **card.metadata,
                    "failures": failures,
                    "lastClaim": _redacted_claim(live_claim),
                    "workerProtocol": {
                        "state": "lease_expired",
                        "detail": "TECHNICAL: direct worker lease expired without completion proof",
                    },
                }
                metadata.pop("claim", None)
                status = QueueStatus.READY if failures <= retry_limit else QueueStatus.BLOCKED
                recovered_card = card.model_copy(update={"status": status, "metadata": metadata})
                self._write_card(connection, recovered_card)
                recovered.append(card.id)
        return tuple(recovered)

    def _execute_claimed(
        self,
        board_id: str,
        card: QueueCard,
        *,
        owner_id: str,
        token: str,
    ) -> bool:
        assert self.executor is not None
        claim = cast(dict[str, Any], card.metadata["claim"])
        attempt_id = str(claim["attemptId"])
        workspace = card.workspace.path if card.workspace and card.workspace.path else self._board_workspace(board_id)
        model = self.worker_models.get(card.agent_id or "")
        if not model:
            self.block_claimed_card(
                card.id,
                owner_id=owner_id,
                token=token,
                reason=f"TECHNICAL: no direct worker model is configured for agent {card.agent_id or '(none)'}",
            )
            return False

        stop = Event()
        heartbeat = Thread(
            target=self._heartbeat_loop,
            args=(stop, card.id, owner_id, token),
            daemon=True,
        )
        heartbeat.start()
        try:
            result = self.executor.execute(
                card,
                attempt_id=attempt_id,
                workspace=workspace,
                model=model,
                timeout_seconds=_runtime_limit(card, self.lease_seconds),
            )
            if result.status == "completed":
                self.complete_claimed_card(
                    card.id,
                    owner_id=owner_id,
                    token=token,
                    summary=result.summary,
                    proof=result.proof,
                )
                return True
            self.block_claimed_card(
                card.id,
                owner_id=owner_id,
                token=token,
                reason=result.reason or "TECHNICAL: worker returned no blocker reason",
            )
            return False
        except (WorkerExecutionError, OSError, TimeoutError) as exc:
            # Cancellation or lease recovery makes a late worker result stale.
            with suppress(PermissionError):
                self.block_claimed_card(
                    card.id,
                    owner_id=owner_id,
                    token=token,
                    reason=f"TECHNICAL: direct worker execution failed: {str(exc)[:1500]}",
                )
            return False
        finally:
            stop.set()
            heartbeat.join(timeout=2)

    def _heartbeat_loop(self, stop: Event, card_id: str, owner_id: str, token: str) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while not stop.wait(interval):
            try:
                self.heartbeat_card(
                    card_id,
                    owner_id=owner_id,
                    token=token,
                    note="direct worker process is still running",
                )
            except (KeyError, PermissionError, ValueError):
                return

    def _claimed_update(
        self,
        card_id: str,
        owner_id: str,
        token: str,
        update: Any,
    ) -> QueueCard:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            card = self._card_from_connection(connection, card_id)
            claim_value = card.metadata.get("claim")
            if claim_value is None:
                raise PermissionError(f"card {card_id} has no active claim")
            if not isinstance(claim_value, dict):
                raise ValueError(f"card {card_id} has invalid claim metadata")
            claim = cast(dict[str, Any], claim_value)
            if claim.get("ownerId") != owner_id or claim.get("token") != token:
                raise PermissionError(f"claim credentials do not match card {card_id}")
            expires_at = _parse_datetime(claim.get("expiresAt"))
            if expires_at is not None and expires_at <= datetime.now(UTC):
                raise PermissionError(f"claim lease expired for card {card_id}")
            updated = cast(QueueCard, update(card, claim))
            self._write_card(connection, updated)
            return updated

    def _card(self, card_id: str) -> QueueCard:
        with self._connect() as connection:
            return self._card_from_connection(connection, card_id)

    @staticmethod
    def _card_from_connection(connection: sqlite3.Connection, card_id: str) -> QueueCard:
        row = connection.execute(
            "SELECT payload_json FROM direct_cards WHERE id=?", (card_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown direct card: {card_id}")
        return QueueCard.model_validate_json(str(row["payload_json"]))

    @staticmethod
    def _write_card(connection: sqlite3.Connection, card: QueueCard) -> None:
        cursor = connection.execute(
            "UPDATE direct_cards SET payload_json=? WHERE id=?",
            (card.model_dump_json(), card.id),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown direct card: {card.id}")

    def _board_workspace(self, board_id: str) -> Path:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT workspace FROM direct_boards WHERE id=?", (board_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown direct board: {board_id}")
        return Path(str(row["workspace"]))

    def _board_ids(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute("SELECT id FROM direct_boards ORDER BY id").fetchall()
        return tuple(str(row["id"]) for row in rows)

    def _update(self, card_id: str, **changes: Any) -> QueueCard:
        card = self._card(card_id).model_copy(update=changes)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE direct_cards SET payload_json=? WHERE id=?",
                (card.model_dump_json(), card_id),
            )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown direct card: {card_id}")
        return card


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _redacted_claim(claim: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in claim.items() if key != "token"}


def _failure_count(card: QueueCard) -> int:
    value = card.metadata.get("failures", 0)
    return value if isinstance(value, int) and value >= 0 else 0


def _retry_limit(card: QueueCard) -> int:
    automation = card.metadata.get("automation")
    if isinstance(automation, dict):
        value = cast(dict[str, Any], automation).get("maxRetries")
        if isinstance(value, int) and value >= 0:
            return value
    return 0


def _runtime_limit(card: QueueCard, default: int) -> int:
    automation = card.metadata.get("automation")
    if isinstance(automation, dict):
        value = cast(dict[str, Any], automation).get("maxRuntimeSeconds")
        if isinstance(value, int) and value > 0:
            return value
    return default


__all__ = ["DirectOrchestrator"]
