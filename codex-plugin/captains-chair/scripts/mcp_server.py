"""Minimal stdio MCP bridge for the Captain's Chair Codex plugin.

The bridge exposes the installed Captain's Chair CLI rather than duplicating
control-plane policy in a Codex-specific process. Every mutating operation is
still evaluated by the configured mode, approval, and completion gates.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from captains_chair.models import Course
from captains_chair.sidecar import SidecarServer


def _object_schema(required: tuple[str, ...], properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required),
        "properties": {"config_path": {"type": "string"}, **properties},
    }


REPO = {"repo": {"type": "string"}}
COURSE = {**REPO, "course_key": {"type": "string"}}

TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "captains_chair_doctor",
        "description": "Validate Captain's Chair configuration and runtime prerequisites.",
        "inputSchema": _object_schema((), {}),
    },
    {
        "name": "captains_chair_baseline",
        "description": "Run a deep repository baseline in the configured harness.",
        "inputSchema": _object_schema(
            ("repo", "harness"),
            {
                "repo": {"type": "string"},
                "harness": {"type": "string"},
                "run_checks": {"type": "boolean"},
            },
        ),
    },
    {
        "name": "captains_chair_status",
        "description": "Read durable repository state, recent events, and model token telemetry.",
        "inputSchema": _object_schema(("repo",), REPO),
    },
    {
        "name": "captains_chair_planning_session",
        "description": "Return durable course context and next questions for the native Codex planning conversation.",
        "inputSchema": _object_schema(("repo", "course_key"), COURSE),
    },
    {
        "name": "captains_chair_cycle",
        "description": "Run one bounded Captain's Chair cycle; policy gates determine whether it can mutate.",
        "inputSchema": _object_schema(
            ("repo", "harness"),
            {
                "repo": {"type": "string"},
                "harness": {"type": "string"},
                "live": {"type": "boolean"},
                "continue_run": {"type": "boolean"},
            },
        ),
    },
    {
        "name": "captains_chair_usage",
        "description": "Report provider-reported token usage by model and workflow role.",
        "inputSchema": _object_schema((), REPO),
    },
    {
        "name": "captains_chair_course_create",
        "description": "Create a durable course charter for greenfield, takeover, or feature work.",
        "inputSchema": _object_schema(("repo", "course"), {**REPO, "course": Course.model_json_schema()}),
    },
    {
        "name": "captains_chair_course_readiness",
        "description": "Read the current course charter and readiness evidence without mutation.",
        "inputSchema": _object_schema(("repo", "course_key"), COURSE),
    },
    {
        "name": "captains_chair_course_answer",
        "description": "Record or waive one owner readiness answer for independent review.",
        "inputSchema": _object_schema(
            ("repo", "course_key", "requirement_key", "status"),
            {
                **COURSE,
                "requirement_key": {"type": "string"},
                "status": {"type": "string", "enum": ["answered", "waived"]},
                "answer": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
            },
        ),
    },
    {
        "name": "captains_chair_course_approve",
        "description": "Engage a readiness-complete course after explicit builder approval.",
        "inputSchema": _object_schema(
            ("repo", "course_key", "approved_by"), {**COURSE, "approved_by": {"type": "string"}}
        ),
    },
    {
        "name": "captains_chair_course_pause",
        "description": "Pause an engaged course without discarding its durable state.",
        "inputSchema": _object_schema(("repo", "course_key"), COURSE),
    },
    {
        "name": "captains_chair_course_resume",
        "description": "Resume a paused course.",
        "inputSchema": _object_schema(("repo", "course_key"), COURSE),
    },
    {
        "name": "captains_chair_course_checkpoint",
        "description": "Resolve a dependency-scoped course checkpoint.",
        "inputSchema": _object_schema(
            ("repo", "course_key", "checkpoint_key", "status"),
            {
                **COURSE,
                "checkpoint_key": {"type": "string"},
                "status": {"type": "string", "enum": ["approved", "blocked", "resolved", "waived"]},
                "resolved_by": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
            },
        ),
    },
    {
        "name": "captains_chair_attention_ack",
        "description": "Acknowledge one repeated attention item after the builder has seen it.",
        "inputSchema": _object_schema(
            ("repo", "fingerprint"),
            {**REPO, "fingerprint": {"type": "string"}, "event_type": {"type": "string"}},
        ),
    },
    {
        "name": "captains_chair_ready_work",
        "description": "Discover dependency-ready work packages for an engaged course.",
        "inputSchema": _object_schema(("repo", "course_key"), COURSE),
    },
    {
        "name": "captains_chair_worker_discover",
        "description": "List runtime-neutral direct worker cards and their current lifecycle state.",
        "inputSchema": _object_schema(("repo",), REPO),
    },
    {
        "name": "captains_chair_worker_claim",
        "description": "Claim a ready direct-worker card or the next dependency-ready card.",
        "inputSchema": _object_schema(
            ("repo", "owner_id", "claim_token"),
            {
                **REPO,
                "card": {"type": "string"},
                "agent_id": {"type": "string"},
                "owner_id": {"type": "string"},
                "claim_token": {"type": "string"},
            },
        ),
    },
    {
        "name": "captains_chair_worker_heartbeat",
        "description": "Heartbeat a claimed worker card with bounded progress evidence.",
        "inputSchema": _object_schema(
            ("repo", "card", "owner_id", "claim_token"),
            {
                **REPO,
                "card": {"type": "string"},
                "owner_id": {"type": "string"},
                "claim_token": {"type": "string"},
                "note": {"type": "string"},
            },
        ),
    },
    {
        "name": "captains_chair_worker_complete",
        "description": "Complete a claimed worker card with summary and verifiable proof.",
        "inputSchema": _object_schema(
            ("repo", "card", "owner_id", "claim_token", "summary", "proof_note"),
            {
                **REPO,
                "card": {"type": "string"},
                "owner_id": {"type": "string"},
                "claim_token": {"type": "string"},
                "summary": {"type": "string"},
                "proof_note": {"type": "string"},
                "proof_url": {"type": "string"},
            },
        ),
    },
    {
        "name": "captains_chair_worker_block",
        "description": "Block a claimed worker card with a precise technical or owner reason.",
        "inputSchema": _object_schema(
            ("repo", "card", "owner_id", "claim_token", "reason"),
            {
                **REPO,
                "card": {"type": "string"},
                "owner_id": {"type": "string"},
                "claim_token": {"type": "string"},
                "reason": {"type": "string"},
            },
        ),
    },
)


def _config_path(arguments: dict[str, Any]) -> str:
    value = str(arguments.get("config_path") or os.environ.get("CAPTAINS_CHAIR_CONFIG") or "").strip()
    if not value:
        raise ValueError("config_path is required or CAPTAINS_CHAIR_CONFIG must be set")
    return value


def _run_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    config = _config_path(arguments)
    repo = str(arguments.get("repo") or "").strip()
    harness = str(arguments.get("harness") or "").strip()
    command: list[str]
    sidecar_methods = {
        "captains_chair_course_create": "course.create",
        "captains_chair_course_readiness": "course.get",
        "captains_chair_course_answer": "course.requirement",
        "captains_chair_course_approve": "course.approve",
        "captains_chair_course_pause": "course.pause",
        "captains_chair_course_resume": "course.resume",
        "captains_chair_course_checkpoint": "course.checkpoint",
        "captains_chair_ready_work": "course.ready_work",
    }
    if name in sidecar_methods:
        params = {**arguments, "full_name": repo}
        params.pop("config_path", None)
        params.pop("repo", None)
        return {"exit_code": 0, "result": SidecarServer(Path(config)).request(sidecar_methods[name], params)}
    if name == "captains_chair_doctor":
        command = ["doctor"]
    elif name == "captains_chair_baseline":
        if not repo or not harness:
            raise ValueError("baseline requires repo and harness")
        command = ["baseline", "--repo", repo, "--harness", harness, "--analyze"]
        if bool(arguments.get("run_checks")):
            command.append("--run-checks")
    elif name == "captains_chair_status":
        if not repo:
            raise ValueError("status requires repo")
        command = ["status", "--repo", repo]
    elif name == "captains_chair_planning_session":
        course_key = str(arguments.get("course_key") or "").strip()
        if not repo or not course_key:
            raise ValueError("planning session requires repo and course_key")
        command = ["planning-session", "--repo", repo, "--course-key", course_key]
    elif name == "captains_chair_cycle":
        if not repo or not harness:
            raise ValueError("cycle requires repo and harness")
        command = ["cycle", "--repo", repo, "--harness", harness]
        command.append("--live" if bool(arguments.get("live")) else "--shadow")
        if bool(arguments.get("continue_run")):
            command.append("--continue-run")
    elif name == "captains_chair_usage":
        command = ["usage", "report"]
        if repo:
            command.extend(["--repo", repo])
    elif name == "captains_chair_attention_ack":
        if not repo or not str(arguments.get("fingerprint") or "").strip():
            raise ValueError("attention acknowledgement requires repo and fingerprint")
        command = ["ack", "--repo", repo, "--fingerprint", str(arguments["fingerprint"])]
        if str(arguments.get("event_type") or "").strip():
            command.extend(["--event-type", str(arguments["event_type"])])
    elif name == "captains_chair_worker_discover":
        if not repo:
            raise ValueError("worker discovery requires repo")
        command = ["orchestrate", "status", "--repo", repo]
    elif name.startswith("captains_chair_worker_"):
        action = name.removeprefix("captains_chair_worker_")
        if (
            not repo
            or not str(arguments.get("owner_id") or "").strip()
            or not str(arguments.get("claim_token") or "")
        ):
            raise ValueError(f"worker {action} requires repo, owner_id, and claim_token")
        command = [
            "worker-protocol",
            action,
            "--repo",
            repo,
            "--owner-id",
            str(arguments["owner_id"]),
            "--token",
            str(arguments["claim_token"]),
        ]
        for key, option in (
            ("card", "--card"),
            ("agent_id", "--agent-id"),
            ("note", "--note"),
            ("summary", "--summary"),
            ("proof_note", "--proof-note"),
            ("proof_url", "--proof-url"),
            ("reason", "--reason"),
        ):
            if arguments.get(key) is not None:
                command.extend([option, str(arguments[key])])
    else:
        raise ValueError(f"unknown tool: {name}")

    completed = subprocess.run(
        [sys.executable, "-m", "captains_chair", "--config", config, *command],
        check=False,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    output = (completed.stdout or completed.stderr or "").strip()
    return {"exit_code": completed.returncode, "output": output[-12000:]}


def _send(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _handle(request: dict[str, Any]) -> None:
    request_id = request.get("id")
    method = str(request.get("method") or "")
    if method == "initialize":
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "captains-chair", "version": "0.2.0"},
                },
            }
        )
        return
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return
    if method == "ping":
        _send({"jsonrpc": "2.0", "id": request_id, "result": {}})
        return
    if method == "tools/list":
        _send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": list(TOOLS)}})
        return
    if method == "tools/call":
        params = request.get("params")
        params_value = params if isinstance(params, dict) else {}
        name = str(params_value.get("name") or "")
        arguments = params_value.get("arguments")
        arguments_value = arguments if isinstance(arguments, dict) else {}
        try:
            result = _run_tool(name, arguments_value)
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(result)}],
                        "isError": result["exit_code"] != 0,
                    },
                }
            )
        except Exception as exc:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": str(exc)}],
                        "isError": True,
                    },
                }
            )
        return
    if request_id is not None:
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }
        )


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("MCP request must be an object")
            _handle(request)
        except Exception as exc:
            _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
