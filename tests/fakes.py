from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from captains_chair.models import OpenClawWorkboardConfig, WorkerAssignments
from captains_chair.orchestration import QueueCard, QueueCardSpec, QueueStatus


class InMemoryWorkQueue:
    """Workboard-compatible queue double for cross-layer tests."""

    def __init__(self) -> None:
        self.cards: dict[str, QueueCard] = {}
        self.keys: dict[str, str] = {}
        self.boards: set[str] = set()
        self.dispatches = 0

    def ensure_board(self, board_id: str, name: str, description: str, workspace: Path) -> None:
        del name, description, workspace
        self.boards.add(board_id)

    def list_cards(self, board_id: str) -> list[QueueCard]:
        del board_id
        return list(self.cards.values())

    def create_card(self, board_id: str, spec: QueueCardSpec) -> QueueCard:
        del board_id
        if spec.key in self.keys:
            return self.cards[self.keys[spec.key]]
        card_id = f"card-{len(self.cards) + 1}"
        card = QueueCard(
            id=card_id,
            title=spec.title,
            notes=spec.notes,
            status=spec.status,
            priority=spec.priority,
            labels=spec.labels,
            agent_id=spec.agent_id,
            source_url=spec.source_url,
            workspace=spec.workspace,
            metadata={
                "parents": list(spec.parents),
                "automation": {"maxRetries": spec.max_retries},
            },
        )
        self.cards[card_id] = card
        self.keys[spec.key] = card_id
        return card

    def complete_card(
        self,
        card_id: str,
        *,
        summary: str,
        proof: tuple[dict[str, Any], ...] = (),
        created_card_ids: tuple[str, ...] = (),
    ) -> QueueCard:
        del summary, created_card_ids
        return self._update(
            card_id,
            status=QueueStatus.DONE,
            metadata={**self.cards[card_id].metadata, "proof": list(proof)},
        )

    def unblock_card(self, card_id: str) -> QueueCard:
        return self._update(card_id, status=QueueStatus.TODO)

    def reclaim_card(self, card_id: str, *, status: QueueStatus, reason: str) -> QueueCard:
        del reason
        return self._update(card_id, status=status)

    def reassign_card(
        self,
        card_id: str,
        *,
        agent_id: str,
        status: QueueStatus,
        reset_failures: bool,
        reason: str,
    ) -> QueueCard:
        del reset_failures, reason
        return self._update(card_id, status=status, agent_id=agent_id)

    def comment(self, card_id: str, body: str) -> QueueCard:
        del body
        return self.cards[card_id]

    def dispatch(self, board_id: str) -> dict[str, Any]:
        del board_id
        self.dispatches += 1
        promoted: list[str] = []
        started: list[str] = []
        for card_id, card in list(self.cards.items()):
            if card.status == QueueStatus.READY and "runtime-canary" in card.labels:
                self._update(card_id, status=QueueStatus.RUNNING)
                started.append(card_id)
                continue
            if card.status != QueueStatus.TODO:
                continue
            parents = card.metadata.get("parents")
            parent_ids = (
                [str(value) for value in cast(list[Any], parents)]
                if isinstance(parents, list)
                else []
            )
            if all(self.cards[parent].status == QueueStatus.DONE for parent in parent_ids):
                self._update(card_id, status=QueueStatus.READY)
                promoted.append(card_id)
        return {
            "promoted": promoted,
            "started": started,
            "count": len(promoted) + len(started),
        }

    def diagnostics(self) -> dict[str, Any]:
        return {"cards": len(self.cards)}

    def heartbeat_card(self, card_id: str, *, owner_id: str, token: str, note: str) -> QueueCard:
        del owner_id, token, note
        return self.cards[card_id]

    def complete_claimed_card(
        self,
        card_id: str,
        *,
        owner_id: str,
        token: str,
        summary: str,
        proof: tuple[dict[str, Any], ...],
    ) -> QueueCard:
        del owner_id, token
        return self.complete_card(card_id, summary=summary, proof=proof)

    def block_claimed_card(
        self,
        card_id: str,
        *,
        owner_id: str,
        token: str,
        reason: str,
    ) -> QueueCard:
        del owner_id, token
        return self.block(card_id, reason)

    def block(self, card_id: str, reason: str) -> QueueCard:
        card = self.cards[card_id]
        return self._update(
            card_id,
            status=QueueStatus.BLOCKED,
            metadata={
                **card.metadata,
                "workerProtocol": {"state": "blocked", "detail": reason},
            },
        )

    def _update(self, card_id: str, **changes: Any) -> QueueCard:
        card = self.cards[card_id].model_copy(update=changes)
        self.cards[card_id] = card
        return card


class PersistentWorkQueue(InMemoryWorkQueue):
    """Small durable queue double for restart and replay integration tests."""

    def __init__(self, path: Path) -> None:
        self.path = path
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.cards = {
                str(item["id"]): QueueCard.model_validate(item)
                for item in cast(list[dict[str, Any]], payload.get("cards", []))
            }
            self.keys = {
                str(key): str(value)
                for key, value in cast(dict[str, Any], payload.get("keys", {})).items()
            }
            self.boards = {
                str(value) for value in cast(list[Any], payload.get("boards", []))
            }
            self.dispatches = int(payload.get("dispatches", 0))
        else:
            super().__init__()
            self._persist()

    def ensure_board(self, board_id: str, name: str, description: str, workspace: Path) -> None:
        super().ensure_board(board_id, name, description, workspace)
        self._persist()

    def create_card(self, board_id: str, spec: QueueCardSpec) -> QueueCard:
        card = super().create_card(board_id, spec)
        self._persist()
        return card

    def dispatch(self, board_id: str) -> dict[str, Any]:
        result = super().dispatch(board_id)
        self._persist()
        return result

    def _update(self, card_id: str, **changes: Any) -> QueueCard:
        card = super()._update(card_id, **changes)
        self._persist()
        return card

    def _persist(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "cards": [card.model_dump(mode="json") for card in self.cards.values()],
                    "keys": self.keys,
                    "boards": sorted(self.boards),
                    "dispatches": self.dispatches,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )


def worker_policy() -> OpenClawWorkboardConfig:
    return OpenClawWorkboardConfig(
        require_live_completion_validation=False,
        workers=WorkerAssignments(
            captain="captain",
            coder="coder",
            reviewer="reviewer",
            tester="tester",
            ux_reviewer="ux",
            final_reviewer="final",
            merger="merger",
            verifier="verifier",
        )
    )
