"""Serve the shared Captain's Chair UI with a local sidecar-backed API."""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from captains_chair.sidecar import SidecarServer

ROUTES = {
    "/portfolio/status": "portfolio.status",
    "/repos/list": "repos.list",
    "/repos/register": "repo.register",
    "/repos/create": "repo.create",
    "/repos/update": "repo.update",
    "/models/validate": "models.validate",
    "/models/config": "models.config",
    "/models/update": "models.update",
    "/usage/config": "usage.config",
    "/usage/update": "usage.update",
    "/courses/list": "courses.list",
    "/course/get": "course.get",
    "/course/create": "course.create",
    "/course/readiness": "course.readiness",
    "/course/planning-session": "course.planning_session",
    "/course/requirement": "course.requirement",
    "/course/models": "course.models",
    "/course/approve": "course.approve",
    "/course/ready-work": "course.ready_work",
    "/course/checkpoint": "course.checkpoint",
    "/course/pause": "course.pause",
    "/course/resume": "course.resume",
    "/schedule/describe": "schedule.describe",
}


def _ui_root(value: str | None) -> Path:
    configured = value or os.environ.get("CAPTAINS_CHAIR_UI_ROOT")
    if not configured:
        raise ValueError("--ui-root or CAPTAINS_CHAIR_UI_ROOT is required")
    root = Path(configured).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"UI build directory does not exist: {root}")
    return root


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("content-length", "0"))
    if length == 0:
        return {}
    value = json.loads(handler.rfile.read(length))
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    return value


def _serve_file(handler: BaseHTTPRequestHandler, root: Path, relative: str) -> None:
    target = (root / unquote(relative.lstrip("/"))).resolve()
    if root not in target.parents and target != root:
        raise ValueError("requested UI path escapes the build directory")
    if not target.is_file():
        handler.send_error(404)
        return
    body = target.read_bytes()
    content_type = "text/html; charset=utf-8" if target.name == "index.html" else "application/octet-stream"
    if target.suffix == ".js":
        content_type = "text/javascript; charset=utf-8"
    elif target.suffix == ".css":
        content_type = "text/css; charset=utf-8"
    elif target.suffix == ".svg":
        content_type = "image/svg+xml"
    elif target.suffix == ".png":
        content_type = "image/png"
    handler.send_response(200)
    handler.send_header("content-type", content_type)
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def build_handler(root: Path, sidecar: SidecarServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if path in {"/", "/captains-chair", "/captains-chair/"}:
                _serve_file(self, root, "/index.html")
                return
            if path.startswith("/captains-chair/assets/"):
                _serve_file(self, root, path.removeprefix("/captains-chair"))
                return
            if path.startswith("/assets/"):
                _serve_file(self, root, path)
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            prefix = "/captains-chair/api/"
            if not parsed.path.startswith(prefix):
                self.send_error(404)
                return
            method = ROUTES.get("/" + parsed.path.removeprefix(prefix))
            if method is None:
                self.send_error(404)
                return
            try:
                result = sidecar.request(method, _read_json(self))
                body = json.dumps(result, default=str).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                body = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(500)
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return Handler


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the Captain's Chair standalone UI")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--ui-root", type=Path)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", default=8787, type=int)
    return parser


def main() -> int:
    args = _parser().parse_args()
    root = _ui_root(str(args.ui_root) if args.ui_root else None)
    server = ThreadingHTTPServer((args.bind, args.port), build_handler(root, SidecarServer(args.config)))
    print(
        json.dumps({"url": f"http://{args.bind}:{server.server_port}/captains-chair/", "ui_root": str(root)}),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
