from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

import make_it_so.sidecar as sidecar
from make_it_so.models import CourseKind, RepositoryProvisioningConfig
from make_it_so.sidecar import SidecarError
from tests.helpers import app_config, repo_config
from tests.test_courses import ready_course, rebind_readiness_review
from tests.test_sidecar import _sidecar  # pyright: ignore[reportPrivateUsage]


def test_sidecar_rejects_invalid_greenfield_intents(tmp_path: Path) -> None:
    server = _sidecar(tmp_path)
    assert server.request("usage.config")["usage"]["block_on_unknown"] is False

    with pytest.raises(SidecarError, match="requires full_name"):
        server.request("repo.create", {})
    with pytest.raises(SidecarError, match="already registered"):
        server.request(
            "repo.create",
            {
                "full_name": "example/project",
                "local_path": str(tmp_path / "new"),
                "course": ready_course().model_dump(mode="json"),
            },
        )

    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "file.txt").write_text("occupied", encoding="utf-8")
    with pytest.raises(SidecarError, match="empty directory"):
        server.request(
            "repo.create",
            {
                "full_name": "example/nonempty",
                "local_path": str(nonempty),
                "course": ready_course().model_dump(mode="json"),
            },
        )

    with pytest.raises(SidecarError, match="only valid for a greenfield"):
        server.request(
            "repo.create",
            {
                "full_name": "example/feature",
                "local_path": str(tmp_path / "feature"),
                "course": ready_course().model_dump(mode="json"),
            },
        )

    greenfield = ready_course().model_copy(update={"kind": CourseKind.GREENFIELD})
    with pytest.raises(SidecarError, match="visibility"):
        server.request(
            "repo.create",
            {
                "full_name": "example/greenfield",
                "local_path": str(tmp_path / "greenfield"),
                "visibility": "internal",
                "course": greenfield.model_dump(mode="json"),
            },
        )

    route = server.request(
        "models.validate",
        {
            "model_profiles": {
                "coder": {
                    "primary": {
                        "model": "verified-route",
                        "capability": {"structured_output": True},
                    }
                }
            }
        },
    )
    assert route["status"] == "valid"


def test_greenfield_approval_checks_provisioning_and_local_git_state(tmp_path: Path) -> None:
    pending = rebind_readiness_review(
        ready_course().model_copy(update={"key": "greenfield", "kind": CourseKind.GREENFIELD})
    )
    disabled_root = tmp_path / "disabled"
    disabled_root.mkdir()
    disabled = _sidecar(disabled_root)
    disabled.request(
        "course.create",
        {"full_name": "example/project", "course": pending.model_dump(mode="json")},
    )
    with pytest.raises(SidecarError, match="provisioning to be enabled"):
        disabled.request(
            "course.approve",
            {
                "full_name": "example/project",
                "course_key": "greenfield",
                "approved_by": "owner",
            },
        )

    git_root = tmp_path / "existing-git"
    (git_root / ".git").mkdir(parents=True)
    enabled_repo = repo_config(git_root).model_copy(
        update={"provisioning": RepositoryProvisioningConfig(enabled=True)}
    )
    enabled_root = tmp_path / "enabled"
    enabled_root.mkdir()
    existing = _sidecar(enabled_root, repo=enabled_repo)
    existing.request(
        "course.create",
        {"full_name": "example/project", "course": pending.model_dump(mode="json")},
    )
    with pytest.raises(SidecarError, match="already contains a Git repository"):
        existing.request(
            "course.approve",
            {
                "full_name": "example/project",
                "course_key": "greenfield",
                "approved_by": "owner",
            },
        )


def test_sidecar_readiness_review_normalizes_process_failures(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    server = _sidecar(tmp_path)
    server.request(
        "course.create",
        {"full_name": "example/project", "course": ready_course().model_dump(mode="json")},
    )
    params = {
        "full_name": "example/project",
        "course_key": "feature-search",
        "harness": "test",
    }

    def process(returncode: int, stdout: str, stderr: str = "") -> None:
        def run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], returncode, stdout, stderr)

        monkeypatch.setattr("make_it_so.sidecar.subprocess.run", run)

    process(2, "", "review failed")
    with pytest.raises(SidecarError, match="review failed"):
        server.request("course.readiness_review", params)
    process(0, "not-json")
    with pytest.raises(SidecarError, match="invalid JSON"):
        server.request("course.readiness_review", params)
    process(0, "[]")
    with pytest.raises(SidecarError, match="invalid response"):
        server.request("course.readiness_review", params)


def test_sidecar_stage_attention_and_schedule_validation_edges(tmp_path: Path) -> None:
    server = _sidecar(tmp_path)
    server.request(
        "course.create",
        {"full_name": "example/project", "course": ready_course().model_dump(mode="json")},
    )
    params = {"full_name": "example/project", "course_key": "feature-search"}

    with pytest.raises(SidecarError, match="requires stage_name"):
        server.request(
            "course.models",
            {
                **params,
                "layer": "stage",
                "stage_profile": {"primary": {"model": "test"}},
            },
        )
    with pytest.raises(SidecarError, match="work_package_key"):
        server.request(
            "course.models",
            {**params, "layer": "work_package", "model_profiles": {}},
        )
    with pytest.raises(SidecarError, match="work package is not defined"):
        server.request(
            "course.models",
            {
                **params,
                "layer": "stage",
                "stage_name": "review",
                "stage_scope": "work_package",
                "work_package_key": "missing",
                "stage_profile": {"primary": {"model": "test"}},
            },
        )
    with pytest.raises(SidecarError, match="attention.ack requires"):
        server.request("attention.ack", {})
    with pytest.raises(SidecarError, match="invalid schedule configuration"):
        server.request("schedule.configure", {"review_every": "not-a-duration"})


def test_sidecar_skips_repositories_excluded_from_schedules(tmp_path: Path) -> None:
    ready_repo = repo_config(tmp_path).model_copy(update={"schedule_enabled": False})
    server = _sidecar(tmp_path, repo=ready_repo)
    assert ready_repo.schedule_enabled is False
    result = server.request("run.once", {"kind": "reconcile"})
    assert result["status"] == "completed"
    assert result["execution"][0]["status"] == "skipped"


def test_sidecar_main_handles_blank_invalid_and_valid_stdio_requests(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = app_config(tmp_path, repo_config(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["make-it-so-sidecar", "--config", str(config_path)])
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO("\n[]\n" + json.dumps({"id": 7, "method": "health", "params": []}) + "\n"),
    )

    assert sidecar.main() == 0
    responses = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert responses[0]["error"]["code"] == "SIDECAR_ERROR"
    assert responses[1]["id"] == 7
    assert responses[1]["result"]["status"] == "healthy"


def test_sidecar_main_does_not_serialize_long_stdio_requests(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    started = threading.Event()
    release = threading.Event()

    class Server:
        def __init__(self, _path: Path) -> None:
            pass

        def request(self, method: str, _params: dict[str, Any]) -> dict[str, Any]:
            if method == "slow":
                started.set()
                assert release.wait(2), "fast RPC was not serviced while slow RPC was running"
            else:
                assert started.wait(2), "slow RPC did not start"
                release.set()
            return {"method": method}

    monkeypatch.setattr(sidecar, "SidecarServer", Server)
    monkeypatch.setattr(sys, "argv", ["make-it-so-sidecar", "--config", str(tmp_path / "config.yaml")])
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps({"id": "slow", "method": "slow", "params": {}})
            + "\n"
            + json.dumps({"id": "fast", "method": "fast", "params": {}})
            + "\n"
        ),
    )

    started_at = time.monotonic()
    assert sidecar.main() == 0
    assert time.monotonic() - started_at < 2
    responses = {json.loads(line)["id"]: json.loads(line) for line in capsys.readouterr().out.splitlines()}
    assert responses["slow"]["result"] == {"method": "slow"}
    assert responses["fast"]["result"] == {"method": "fast"}


@pytest.mark.parametrize(("status", "expected"), (("completed", 0), ("degraded", 2)))
def test_sidecar_main_once_propagates_run_health(
    tmp_path: Path,
    monkeypatch: Any,
    status: str,
    expected: int,
) -> None:
    class Server:
        def __init__(self, _path: Path) -> None:
            pass

        def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            assert method == "run.once"
            assert params == {"kind": "review"}
            return {"status": status}

    monkeypatch.setattr(sidecar, "SidecarServer", Server)
    monkeypatch.setattr(
        sys,
        "argv",
        ["make-it-so-sidecar", "--config", str(tmp_path / "config.yaml"), "--once", "review"],
    )
    assert sidecar.main() == expected
