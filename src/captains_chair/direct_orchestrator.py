from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from captains_chair.orchestration import QueueCard, QueueCardSpec, QueueStatus


class DirectOrchestrator:
    """SQLite-backed worker orchestration without an external task board.

    This adapter stores execution state and proof, not a user-facing kanban. Worker
    processes may claim ready cards through a host integration and complete them via
    the lifecycle methods shared with the OpenClaw adapter.
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
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
        return self._update(card_id, status=QueueStatus.TODO, metadata=metadata)

    def reclaim_card(self, card_id: str, *, status: QueueStatus, reason: str) -> QueueCard:
        card = self._card(card_id)
        metadata = {**card.metadata, "reclaimReason": reason}
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
        cards = {card.id: card for card in self.list_cards(board_id)}
        promoted: list[str] = []
        started: list[str] = []
        for card in cards.values():
            if card.status == QueueStatus.READY and "runtime-canary" in card.labels:
                self._update(card.id, status=QueueStatus.RUNNING)
                started.append(card.id)
                continue
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
        return {"promoted": promoted, "started": started, "count": len(promoted) + len(started)}

    def diagnostics(self) -> dict[str, Any]:
        with self._connect() as connection:
            boards = int(connection.execute("SELECT COUNT(*) FROM direct_boards").fetchone()[0])
            cards = int(connection.execute("SELECT COUNT(*) FROM direct_cards").fetchone()[0])
        return {"status": "healthy", "kind": "direct", "boards": boards, "cards": cards}

    def heartbeat_card(self, card_id: str, *, owner_id: str, token: str, note: str) -> QueueCard:
        card = self._card(card_id)
        metadata = {
            **card.metadata,
            "claim": {"ownerId": owner_id, "token": token, "heartbeat": note},
        }
        status = QueueStatus.RUNNING if card.status == QueueStatus.READY else card.status
        return self._update(card_id, status=status, metadata=metadata)

    def complete_claimed_card(
        self,
        card_id: str,
        *,
        owner_id: str,
        token: str,
        summary: str,
        proof: tuple[dict[str, Any], ...],
    ) -> QueueCard:
        self._validate_claim(card_id, owner_id, token)
        return self.complete_card(card_id, summary=summary, proof=proof)

    def block_claimed_card(
        self,
        card_id: str,
        *,
        owner_id: str,
        token: str,
        reason: str,
    ) -> QueueCard:
        self._validate_claim(card_id, owner_id, token)
        card = self._card(card_id)
        return self._update(
            card_id,
            status=QueueStatus.BLOCKED,
            metadata={
                **card.metadata,
                "workerProtocol": {"state": "blocked", "detail": reason},
            },
        )

    def _validate_claim(self, card_id: str, owner_id: str, token: str) -> None:
        claim = self._card(card_id).metadata.get("claim")
        if claim is None:
            return
        if not isinstance(claim, dict):
            raise ValueError(f"card {card_id} has invalid claim metadata")
        claim_value = cast(dict[str, Any], claim)
        if claim_value.get("ownerId") != owner_id or claim_value.get("token") != token:
            raise PermissionError(f"claim credentials do not match card {card_id}")

    def _card(self, card_id: str) -> QueueCard:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM direct_cards WHERE id=?", (card_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown direct card: {card_id}")
        return QueueCard.model_validate_json(str(row["payload_json"]))

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


__all__ = ["DirectOrchestrator"]
