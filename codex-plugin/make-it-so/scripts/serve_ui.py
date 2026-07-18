"""Serve the shared Make It So UI with a local sidecar-backed API."""

from __future__ import annotations

import argparse
import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from make_it_so.sidecar import SidecarServer

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
    configured = value or os.environ.get("MAKE_IT_SO_UI_ROOT")
    if not configured:
        raise ValueError("--ui-root or MAKE_IT_SO_UI_ROOT is required")
    root = Path(configured).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"UI build directory does not exist: {root}")
    return root


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("content-length", "0"))
    if length == 0:
        return {}
    if length < 0 or length > 1_048_576:
        raise ValueError("request body exceeds the 1 MiB API limit")
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
    _security_headers(handler)
    handler.send_header("content-type", content_type)
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _security_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("cache-control", "no-store")
    handler.send_header(
        "content-security-policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    )
    handler.send_header("referrer-policy", "no-referrer")
    handler.send_header("x-content-type-options", "nosniff")
    handler.send_header("x-frame-options", "DENY")


def build_handler(
    root: Path,
    sidecar: SidecarServer,
    access_token: str | None = None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _authorized(self, parsed: Any) -> bool:
            if access_token is None:
                return True
            supplied = self.headers.get("authorization", "").removeprefix("Bearer ")
            supplied = supplied or self.headers.get("x-make-it-so-token", "")
            cookies = dict(
                part.strip().split("=", 1)
                for part in self.headers.get("cookie", "").split(";")
                if "=" in part
            )
            supplied = supplied or cookies.get("make_it_so_token", "")
            supplied = supplied or parse_qs(parsed.query).get("token", [""])[0]
            return secrets.compare_digest(supplied, access_token)

        def _reject_unauthorized(self) -> None:
            self.send_response(401)
            _security_headers(self)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if not self._authorized(parsed):
                self._reject_unauthorized()
                return
            if access_token is not None and parse_qs(parsed.query).get("token"):
                self.send_response(303)
                _security_headers(self)
                self.send_header(
                    "set-cookie", f"make_it_so_token={access_token}; HttpOnly; SameSite=Strict; Path=/"
                )
                self.send_header("location", parsed.path or "/make-it-so/")
                self.end_headers()
                return
            path = parsed.path
            if path in {"/", "/make-it-so", "/make-it-so/"}:
                _serve_file(self, root, "/index.html")
                return
            if path.startswith("/make-it-so/assets/"):
                _serve_file(self, root, path.removeprefix("/make-it-so"))
                return
            if path.startswith("/assets/"):
                _serve_file(self, root, path)
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if not self._authorized(parsed):
                self._reject_unauthorized()
                return
            origin = self.headers.get("origin")
            if origin and urlparse(origin).netloc != self.headers.get("host"):
                self.send_error(403)
                return
            if self.headers.get_content_type() != "application/json":
                self.send_error(415)
                return
            prefix = "/make-it-so/api/"
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
                _security_headers(self)
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                body = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(500)
                _security_headers(self)
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return Handler


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the Make It So standalone UI")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--ui-root", type=Path)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", default=8787, type=int)
    parser.add_argument("--token", help="required bearer/cookie token for non-loopback binding")
    return parser


def main() -> int:
    args = _parser().parse_args()
    root = _ui_root(str(args.ui_root) if args.ui_root else None)
    token = args.token or os.environ.get("MAKE_IT_SO_UI_TOKEN")
    if token and (len(token) < 16 or any(character.isspace() or character in ";," for character in token)):
        raise ValueError(
            "UI token must be at least 16 characters and contain no whitespace, semicolons, or commas"
        )
    try:
        loopback = ip_address(args.bind).is_loopback
    except ValueError:
        loopback = args.bind.lower() == "localhost"
    if not loopback and not token:
        raise ValueError("non-loopback UI binding requires --token or MAKE_IT_SO_UI_TOKEN")
    server = ThreadingHTTPServer(
        (args.bind, args.port),
        build_handler(root, SidecarServer(args.config), token),
    )
    print(
        json.dumps({"url": f"http://{args.bind}:{server.server_port}/make-it-so/", "ui_root": str(root)}),
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
