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
from typing import Any

TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "captains_chair_doctor",
        "description": "Validate Captain's Chair configuration and runtime prerequisites.",
        "inputSchema": {"type": "object", "properties": {"config_path": {"type": "string"}}},
    },
    {
        "name": "captains_chair_baseline",
        "description": "Run a deep repository baseline in the configured harness.",
        "inputSchema": {
            "type": "object",
            "required": ["repo", "harness"],
            "properties": {
                "config_path": {"type": "string"},
                "repo": {"type": "string"},
                "harness": {"type": "string"},
                "run_checks": {"type": "boolean"},
            },
        },
    },
    {
        "name": "captains_chair_status",
        "description": "Read durable repository state, recent events, and model token telemetry.",
        "inputSchema": {
            "type": "object",
            "required": ["repo"],
            "properties": {"config_path": {"type": "string"}, "repo": {"type": "string"}},
        },
    },
    {
        "name": "captains_chair_planning_session",
        "description": "Return durable course context and next questions for the native Codex planning conversation.",
        "inputSchema": {
            "type": "object",
            "required": ["repo", "course_key"],
            "properties": {
                "config_path": {"type": "string"},
                "repo": {"type": "string"},
                "course_key": {"type": "string"},
            },
        },
    },
    {
        "name": "captains_chair_cycle",
        "description": "Run one bounded Captain's Chair cycle; policy gates determine whether it can mutate.",
        "inputSchema": {
            "type": "object",
            "required": ["repo", "harness"],
            "properties": {
                "config_path": {"type": "string"},
                "repo": {"type": "string"},
                "harness": {"type": "string"},
                "live": {"type": "boolean"},
                "continue_run": {"type": "boolean"},
            },
        },
    },
    {
        "name": "captains_chair_usage",
        "description": "Report provider-reported token usage by model and workflow role.",
        "inputSchema": {
            "type": "object",
            "properties": {"config_path": {"type": "string"}, "repo": {"type": "string"}},
        },
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
                    "serverInfo": {"name": "captains-chair", "version": "0.1.0"},
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
