from __future__ import annotations

import json
import os
import shlex
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from make_it_so.command import CommandRunner, run_command
from make_it_so.model_policy import models_match
from make_it_so.models import OpenClawWorkboardConfig
from make_it_so.openclaw_workboard import (
    REQUIRED_WORKER_TOOLS,
    OpenClawWorkboardError,
    decode_openclaw_json,
)

COMMON_PROTOCOL = """# MAKE_IT_SO OpenClaw Worker

You are a role-separated worker for Make It So. The Workboard card is your complete work contract.

Mandatory protocol:

1. Read the card, parent results, source links, repository AGENTS.md, `.make-it-so/project.yaml`, and canonical planning documents before acting.
2. Work only in the workspace or worktree supplied by the card. Never switch the shared checkout to a feature branch.
3. Keep the card claim alive with the MAKE_IT_SO `worker-protocol heartbeat` command during long operations. Never call `heartbeat_respond`; it is not a Workboard lease heartbeat and may be unavailable.
4. Record direct proof: commands, current GitHub links, current head SHA, tests, CI, screenshots, or artifacts as appropriate.
5. Use the lifecycle helper below for heartbeat, completion, or blocking. Never simply stop after writing a narrative response.
6. Use `TECHNICAL:` for failures that another worker or retry can repair.
7. Request the owner only with `USER_SECRET:`, `GOAL_DIVERGENCE:`, `EXTERNAL_ACCESS:`, or `HIGH_RISK_DECISION:`.
8. Continue any unrelated ready work that remains within your assigned card; one blocker must not freeze the board.

Never bypass MAKE_IT_SO policy, fabricate proof, approve your own implementation, merge from a coding role, expose secrets, force-push, delete branches, release, or deploy production unless the card and deterministic policy explicitly authorize it.

Lifecycle helper command prefix:

`{make_it_so_command}`

Examples (replace values with the card id, ownerId, and claim token from the Workboard prompt):

- Heartbeat: `{make_it_so_command} worker-protocol heartbeat --repo <owner/repo> --orchestrator openclaw-workers --card <id> --owner-id <ownerId> --token <token> --note "still working"`
- Complete: `{make_it_so_command} worker-protocol complete --repo <owner/repo> --orchestrator openclaw-workers --card <id> --owner-id <ownerId> --token <token> --summary "what changed" --proof-note "tests and current-head evidence"`
- Block: `{make_it_so_command} worker-protocol block --repo <owner/repo> --orchestrator openclaw-workers --card <id> --owner-id <ownerId> --token <token> --reason "TECHNICAL: exact repairable failure"`
"""


ROLE_PROTOCOLS: dict[str, str] = {
    "captain": """\n## Role: Captain supervisor\n\nReconcile the card against live GitHub state and repository-owned plans. Make routine decisions autonomously when they preserve the documented goal. Decompose work into bounded cards, route technical blockers to repair or recovery workers, and reserve owner questions for true goal divergence, access, secrets, or high-risk decisions.\n""",
    "coder": """\n## Role: implementation and repair\n\nImplement only the linked acceptance criteria in the supplied isolated worktree. Run targeted checks, scan the diff for secrets and unrelated churn, push the MAKE_IT_SO branch, and open or update the linked PR with its exact GitHub PR URL, head SHA, and test proof in the structured completion proof. Never review, approve, or merge your own work.\n""",
    "reviewer": """\n## Role: independent code reviewer\n\nUse fresh context and inspect the current PR head. Do not edit files. Review correctness, security, scope, tests, docs alignment, and unrelated churn. Complete only with current-head evidence; otherwise block with actionable findings prefixed `TECHNICAL:`.\n""",
    "tester": """\n## Role: independent test and CI checker\n\nRun configured targeted checks and inspect required GitHub checks on the current PR head. Do not waive pending or failed checks. Record exact commands and links. Block repairably failing work with `TECHNICAL:`.\n""",
    "ux_reviewer": """\n## Role: frontend usability reviewer\n\nFor UI-impacting work, exercise real flows in a browser at mobile, tablet, and desktop sizes. Check functionality, contrast, keyboard and focus behavior, responsive layout, error/loading/empty states, touch targets, overflow, and visual cohesion. Attach screenshot proof. Do not edit implementation files.\n""",
    "final_reviewer": """\n## Role: Captain final reviewer\n\nCompare the current PR head with the original issue, repository plan, acceptance criteria, independent review, UX evidence, tests, CI, and unresolved threads. Complete only when all evidence is current. The card states the exact configured completion policy and its one required marker; use that marker and never infer a different policy. Return a passed proof marker anchored to the current head: `READY_FOR_OWNER:<head-sha>` for owner approval, `CONTROL_PLANE_COMPLETE:<head-sha>` for Captain completion, or `AUTO_MERGE_ALLOWED:<head-sha>` for autonomous merge. READY_FOR_OWNER is not permission to auto-merge.\n\nEfficiency boundary: use only focused evidence calls against this card, its parent cards, the linked PR, and the repository. Never call session-inventory tools or inspect other workers' conversations. Do not repeat an unchanged command; after a small bounded set of checks, complete with proof or block with one precise `TECHNICAL:` reason.\n""",
    "merger": """\n## Role: deterministic merger\n\nDo not reinterpret requirements. Read the PR number and final-review card id from the card's parent results, then run `{make_it_so_command} merge-gate --repo <owner/repo> --pr <number> --final-card <id> --merge`. Complete the Workboard card only when that command reports both `allowed: true` and `merged: true`, and attach the PR URL and merge result as proof. Never invoke `gh pr merge` directly. Pending checks, unresolved threads, stale review evidence, scope drift, or an owner-only completion policy must fail closed.\n""",
    "verifier": """\n## Role: post-action verifier\n\nRead the result back from GitHub. For merges, verify the actual default-branch commit and main CI; treat deployment according to repository policy. Complete with direct links and SHAs, or block with a precise tagged reason.\n""",
}


@dataclass(frozen=True)
class RuntimeInstallAction:
    role: str
    agent_id: str
    model: str
    workspace: str
    action: str


class OpenClawRuntimeInstaller:
    def __init__(
        self,
        config: OpenClawWorkboardConfig,
        runner: CommandRunner = run_command,
    ) -> None:
        self.config = config
        self.runner = runner

    def plan(self, workspace_root: Path) -> tuple[RuntimeInstallAction, ...]:
        existing = self._agents()
        actions: list[RuntimeInstallAction] = []
        for role, agent_id, model in self._roles():
            workspace = (workspace_root / agent_id).resolve()
            current = existing.get(agent_id)
            if current is None:
                action = "create"
            elif not models_match(model, str(current.get("model") or "")):
                action = "model_mismatch"
            else:
                action = "update_instructions"
            actions.append(RuntimeInstallAction(role, agent_id, model, str(workspace), action))
        return tuple(actions)

    def install(self, workspace_root: Path) -> tuple[RuntimeInstallAction, ...]:
        existing = self._agents()
        actions = self.plan(workspace_root)
        mismatches = [item for item in actions if item.action == "model_mismatch"]
        if mismatches:
            first = mismatches[0]
            raise OpenClawWorkboardError(
                f"agent {first.agent_id} uses a different model; update it explicitly before install"
            )
        for item in actions:
            workspace = Path(item.workspace)
            if item.action == "create":
                result = self.runner(
                    [
                        self.config.executable,
                        "agents",
                        "add",
                        item.agent_id,
                        "--workspace",
                        str(workspace),
                        "--model",
                        item.model,
                        "--non-interactive",
                        "--json",
                    ],
                    timeout=120,
                )
                if result.returncode:
                    raise OpenClawWorkboardError(
                        f"failed to create OpenClaw agent {item.agent_id}: "
                        f"{(result.stderr or result.stdout).strip()[:2000]}"
                    )
            workspace.mkdir(parents=True, exist_ok=True)
            command = shlex.join(self.config.make_it_so_command)
            instructions = (
                COMMON_PROTOCOL.replace("{make_it_so_command}", command)
                + ROLE_PROTOCOLS[item.role].replace("{make_it_so_command}", command)
            )
            (workspace / "AGENTS.md").write_text(instructions, encoding="utf-8")
        self._install_safety_policy()
        if self.config.auth_source_agent:
            current = self._agents()
            source = existing.get(self.config.auth_source_agent) or current.get(
                self.config.auth_source_agent
            )
            if source is None or not source.get("agentDir"):
                raise OpenClawWorkboardError(
                    f"auth source agent was not found: {self.config.auth_source_agent}"
                )
            source_db = Path(str(source["agentDir"])) / "openclaw-agent.sqlite"
            for item in actions:
                target = current.get(item.agent_id)
                if target is None or not target.get("agentDir"):
                    raise OpenClawWorkboardError(
                        f"installed agent could not be read back: {item.agent_id}"
                    )
                sync_openclaw_auth_profiles(
                    source_db,
                    Path(str(target["agentDir"])) / "openclaw-agent.sqlite",
                )
        return actions

    def _install_safety_policy(self) -> None:
        tools_result = self.runner(
            [self.config.executable, "config", "get", "tools", "--json"],
            timeout=60,
        )
        if tools_result.returncode:
            raise OpenClawWorkboardError(
                "failed to read OpenClaw tool policy: "
                f"{(tools_result.stderr or tools_result.stdout).strip()[:2000]}"
            )
        raw_tools = decode_openclaw_json(tools_result.stdout)
        if not isinstance(raw_tools, dict):
            raise OpenClawWorkboardError("OpenClaw tool policy did not return an object")
        allow_value = cast(dict[str, Any], raw_tools).get("allow")
        if isinstance(allow_value, list):
            allow = [str(value) for value in cast(list[object], allow_value)]
            for tool in REQUIRED_WORKER_TOOLS:
                if tool not in allow:
                    allow.append(tool)
            self._set_config("tools.allow", allow)
        self._set_config(
            "agents.defaults.subagents.maxConcurrent",
            self.config.max_concurrent_subagents,
        )

    def _set_config(self, path: str, value: object) -> None:
        result = self.runner(
            [
                self.config.executable,
                "config",
                "set",
                path,
                json.dumps(value, separators=(",", ":")),
                "--strict-json",
            ],
            timeout=60,
        )
        if result.returncode:
            raise OpenClawWorkboardError(
                f"failed to configure OpenClaw runtime safety at {path}: "
                f"{(result.stderr or result.stdout).strip()[:2000]}"
            )

    def _agents(self) -> dict[str, dict[str, Any]]:
        result = self.runner(
            [self.config.executable, "agents", "list", "--json"],
            timeout=60,
        )
        if result.returncode:
            raise OpenClawWorkboardError(
                f"failed to list OpenClaw agents: {(result.stderr or result.stdout).strip()[:2000]}"
            )
        raw = decode_openclaw_json(result.stdout)
        if not isinstance(raw, list):
            raise OpenClawWorkboardError("OpenClaw agents list did not return an array")
        agents: dict[str, dict[str, Any]] = {}
        for value in cast(list[object], raw):
            if not isinstance(value, dict):
                continue
            row = cast(dict[str, Any], value)
            if row.get("id"):
                agents[str(row["id"])] = row
        return agents

    def _roles(self) -> tuple[tuple[str, str, str], ...]:
        workers = self.config.workers
        models = self.config.worker_models
        return (
            ("captain", workers.captain, models.captain),
            ("coder", workers.coder, models.coder),
            ("reviewer", workers.reviewer, models.reviewer),
            ("tester", workers.tester, models.tester),
            ("ux_reviewer", workers.ux_reviewer, models.ux_reviewer),
            ("final_reviewer", workers.final_reviewer, models.final_reviewer),
            ("merger", workers.merger, models.merger),
            ("verifier", workers.verifier, models.verifier),
        )


def sync_openclaw_auth_profiles(source_db: Path, target_db: Path) -> None:
    if not source_db.is_file():
        raise OpenClawWorkboardError(f"auth source database is missing: {source_db}")
    target_db.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    target = sqlite3.connect(target_db)
    try:
        with target:
            for table in ("auth_profile_store", "auth_profile_state"):
                schema_row = source.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if schema_row is None or not schema_row[0]:
                    raise OpenClawWorkboardError(f"auth source is missing table: {table}")
                schema = str(schema_row[0]).replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)
                target.execute(schema)
                columns = [
                    str(row[1]) for row in source.execute(f'PRAGMA table_info("{table}")').fetchall()
                ]
                if not columns:
                    raise OpenClawWorkboardError(f"auth source table has no columns: {table}")
                quoted = ",".join(f'"{column}"' for column in columns)
                placeholders = ",".join("?" for _ in columns)
                rows = source.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
                target.executemany(
                    f'INSERT OR REPLACE INTO "{table}" ({quoted}) VALUES ({placeholders})', rows
                )
    finally:
        source.close()
        target.close()
    os.chmod(target_db, 0o600)
