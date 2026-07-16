from __future__ import annotations

import ast
import importlib
import json
import os
import runpy
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from captains_chair.direct_orchestrator import DirectOrchestrator
from captains_chair.models import CourseKind, OperationMode, RepositoryProvisioningConfig
from captains_chair.orchestration import QueueCardSpec, QueueStatus
from captains_chair.sidecar import SidecarServer
from captains_chair.state import StateStore
from tests.helpers import app_config, repo_config
from tests.test_courses import ready_course

PLUGIN_ROOT = Path(__file__).parents[1] / "codex-plugin" / "captains-chair"
CORE_ROOT = Path(__file__).parents[1] / "src" / "captains_chair"


def test_runtime_neutral_core_does_not_import_codex_plugin_modules() -> None:
    forbidden_prefixes = ("codex_plugin", "captains_chair_codex", "mcp_server", "serve_ui")

    for path in CORE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = [
            alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
        ]
        imports.extend(node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
        assert not any(imported.startswith(forbidden_prefixes) for imported in imports), (
            f"{path} imports a Codex plugin module"
        )


def test_codex_plugin_manifest_and_mcp_bridge_are_portable() -> None:
    manifest = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    mcp = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "captains-chair"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert mcp["mcpServers"]["captains-chair"]["args"] == ["scripts/mcp_server.py"]

    completed = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "scripts" / "mcp_server.py")],
        input=(
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
            '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert completed.returncode == 0
    assert responses[0]["result"]["serverInfo"]["name"] == "captains-chair"
    assert {tool["name"] for tool in responses[1]["result"]["tools"]} >= {
        "captains_chair_doctor",
        "captains_chair_baseline",
        "captains_chair_cycle",
        "captains_chair_planning_session",
        "captains_chair_usage",
        "captains_chair_course_create",
        "captains_chair_course_approve",
        "captains_chair_course_checkpoint",
        "captains_chair_attention_ack",
        "captains_chair_worker_discover",
        "captains_chair_worker_claim",
        "captains_chair_worker_heartbeat",
        "captains_chair_worker_complete",
        "captains_chair_worker_block",
    }
    for tool in responses[1]["result"]["tools"]:
        assert tool["inputSchema"]["additionalProperties"] is False


def test_codex_tools_create_and_control_every_course_type(tmp_path: Path) -> None:
    module = runpy.run_path(str(PLUGIN_ROOT / "scripts" / "mcp_server.py"))
    run_tool = module["_run_tool"]
    repo = repo_config(tmp_path, mode=OperationMode.SUPERVISED).model_copy(
        update={"provisioning": RepositoryProvisioningConfig(enabled=True)}
    )
    config = app_config(tmp_path, repo)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")

    class Provider:
        def provision_greenfield(self, repo: object, course: object) -> dict[str, object]:
            del repo, course
            return {"created": True, "url": "https://github.test/example/project"}

    def sidecar(path: Path) -> SidecarServer:
        return SidecarServer(path, github=cast(Any, Provider()))

    run_tool.__globals__["SidecarServer"] = sidecar

    for kind in CourseKind:
        course = ready_course().model_copy(
            update={"key": f"{kind.value}-course", "kind": kind, "title": f"{kind.value} course"}
        )
        review = getattr(course, "readiness_review", None)
        if review is not None:
            readiness_module = importlib.import_module("captains_chair.readiness")
            input_sha = cast(Any, readiness_module).readiness_input_sha(course)
            course = course.model_copy(
                update={"readiness_review": review.model_copy(update={"input_sha": input_sha})}
            )
        arguments = {
            "config_path": str(config_path),
            "repo": "example/project",
            "course": course.model_dump(mode="json"),
        }
        created = run_tool("captains_chair_course_create", arguments)
        assert created["result"]["course"]["kind"] == kind.value
        readiness = run_tool(
            "captains_chair_course_readiness",
            {"config_path": str(config_path), "repo": "example/project", "course_key": course.key},
        )
        assert readiness["result"]["readiness"]["ready"] is True
        approved = run_tool(
            "captains_chair_course_approve",
            {
                "config_path": str(config_path),
                "repo": "example/project",
                "course_key": course.key,
                "approved_by": "builder",
            },
        )
        assert approved["result"]["course"]["status"] == "engaged"
        paused = run_tool(
            "captains_chair_course_pause",
            {"config_path": str(config_path), "repo": "example/project", "course_key": course.key},
        )
        assert paused["result"]["course"]["status"] == "paused"
        resumed = run_tool(
            "captains_chair_course_resume",
            {"config_path": str(config_path), "repo": "example/project", "course_key": course.key},
        )
        assert resumed["result"]["course"]["status"] == "engaged"
        answered = run_tool(
            "captains_chair_course_answer",
            {
                "config_path": str(config_path),
                "repo": "example/project",
                "course_key": course.key,
                "requirement_key": "success",
                "status": "answered",
                "answer": "The ranked search flow passes.",
                "evidence": ["owner-answer"],
            },
        )
        assert answered["result"]["course"]["readiness"][0]["status"] == "answered"
        checkpoint = run_tool(
            "captains_chair_course_checkpoint",
            {
                "config_path": str(config_path),
                "repo": "example/project",
                "course_key": course.key,
                "checkpoint_key": "ui-demo",
                "status": "resolved",
                "resolved_by": "builder",
                "evidence": ["demo-approved"],
            },
        )
        assert checkpoint["result"]["course"]["checkpoints"][0]["status"] == "resolved"


def test_codex_tools_drive_direct_worker_lifecycle_without_a_board(tmp_path: Path) -> None:
    module = runpy.run_path(str(PLUGIN_ROOT / "scripts" / "mcp_server.py"))
    run_tool = module["_run_tool"]
    config = app_config(tmp_path, repo_config(tmp_path, mode=OperationMode.SUPERVISED))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    adapter = DirectOrchestrator(config.state_dir / "orchestrators" / "example-project.db")
    board_id = "captains-chair-direct-example-project"
    adapter.ensure_board(board_id, "Direct", "Portable direct work", tmp_path)
    card = adapter.create_card(
        board_id,
        QueueCardSpec(key="package-1", title="Package", notes="Implement it", status=QueueStatus.READY),
    )
    base = {
        "config_path": str(config_path),
        "repo": "example/project",
        "owner_id": "codex-worker",
        "claim_token": "opaque-claim",
    }

    discovered = run_tool(
        "captains_chair_worker_discover", {"config_path": str(config_path), "repo": "example/project"}
    )
    assert card.id in discovered["output"]
    claimed = run_tool("captains_chair_worker_claim", {**base, "card": card.id})
    assert '"status": "running"' in claimed["output"]
    heartbeat = run_tool(
        "captains_chair_worker_heartbeat", {**base, "card": card.id, "note": "tests running"}
    )
    assert "tests running" in heartbeat["output"]
    completed = run_tool(
        "captains_chair_worker_complete",
        {**base, "card": card.id, "summary": "Implemented", "proof_note": "pytest passed"},
    )
    assert '"status": "done"' in completed["output"]
    assert "pytest passed" in completed["output"]

    blocked_card = adapter.create_card(
        board_id,
        QueueCardSpec(key="package-2", title="Blocked package", notes="Try it", status=QueueStatus.READY),
    )
    blocked_base = {**base, "card": blocked_card.id, "claim_token": "second-claim"}
    run_tool("captains_chair_worker_claim", blocked_base)
    blocked = run_tool(
        "captains_chair_worker_block",
        {**blocked_base, "reason": "USER_SECRET: test credential is required"},
    )
    assert '"status": "blocked"' in blocked["output"]
    assert "USER_SECRET" in blocked["output"]

    state = StateStore(config.state_dir / "state.db")
    state.note_attention("example/project", "attention-1", "ATTENTION_REQUIRED")
    acknowledged = run_tool(
        "captains_chair_attention_ack",
        {"config_path": str(config_path), "repo": "example/project", "fingerprint": "attention-1"},
    )
    assert '"count": 1' in acknowledged["output"]


def test_standalone_server_serves_shared_ui_and_sidecar_api(tmp_path: Path) -> None:
    ui_root = tmp_path / "ui"
    ui_root.mkdir()
    (ui_root / "index.html").write_text("<html>Captain's Chair</html>", encoding="utf-8")
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).parents[1] / "src"), environment.get("PYTHONPATH", "")]
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(PLUGIN_ROOT / "scripts" / "serve_ui.py"),
            "--config",
            str(config_path),
            "--ui-root",
            str(ui_root),
            "--port",
            "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        assert process.stdout is not None
        announcement = json.loads(process.stdout.readline())
        base_url = str(announcement["url"]).rstrip("/")
        with urllib.request.urlopen(f"{base_url}/", timeout=5) as response:
            html = response.read().decode("utf-8")
            assert response.headers["content-security-policy"].startswith("default-src 'self'")
        assert "Captain's Chair" in html
        request = urllib.request.Request(
            f"{base_url}/api/portfolio/status",
            data=b"{}",
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
        assert payload["repos"][0]["full_name"] == "example/project"
        model_request = urllib.request.Request(
            f"{base_url}/api/models/config",
            data=b"{}",
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(model_request, timeout=5) as response:
            model_payload = json.loads(response.read())
        assert "global_profiles" in model_payload
        usage_request = urllib.request.Request(
            f"{base_url}/api/usage/update",
            data=json.dumps(
                {
                    "daily_token_limit": 1000,
                    "model_daily_token_limits": {"codex/gpt-5.3-codex-spark": 600},
                    "block_on_unknown": True,
                }
            ).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(usage_request, timeout=5) as response:
            usage_payload = json.loads(response.read())
        assert usage_payload["usage"]["daily_token_limit"] == 1000
    finally:
        process.terminate()
        process.wait(timeout=10)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def test_standalone_server_requires_and_enforces_token_for_remote_binding(tmp_path: Path) -> None:
    ui_root = tmp_path / "ui"
    ui_root.mkdir()
    (ui_root / "index.html").write_text("<html>Captain's Chair</html>", encoding="utf-8")
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(Path(__file__).parents[1] / "src"), os.environ.get("PYTHONPATH", "")]
        ),
    }
    missing = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "scripts" / "serve_ui.py"),
            "--config",
            str(config_path),
            "--ui-root",
            str(ui_root),
            "--bind",
            "0.0.0.0",
            "--port",
            "0",
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
        check=False,
    )
    assert missing.returncode != 0
    assert "requires --token" in missing.stderr

    process = subprocess.Popen(
        [
            sys.executable,
            str(PLUGIN_ROOT / "scripts" / "serve_ui.py"),
            "--config",
            str(config_path),
            "--ui-root",
            str(ui_root),
            "--token",
            "test-secret-12345678",
            "--port",
            "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        assert process.stdout is not None
        base_url = str(json.loads(process.stdout.readline())["url"]).rstrip("/")
        with pytest.raises(urllib.error.HTTPError) as unauthorized:
            urllib.request.urlopen(f"{base_url}/", timeout=5)
        assert unauthorized.value.code == 401
        unauthorized.value.close()
        request = urllib.request.Request(
            f"{base_url}/api/portfolio/status",
            data=b"{}",
            headers={
                "content-type": "application/json",
                "authorization": "Bearer test-secret-12345678",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert json.loads(response.read())["repos"][0]["full_name"] == "example/project"
    finally:
        process.terminate()
        process.wait(timeout=10)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
