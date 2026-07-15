from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import yaml

from tests.helpers import app_config, repo_config

PLUGIN_ROOT = Path(__file__).parents[1] / "codex-plugin" / "captains-chair"


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
    }


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
