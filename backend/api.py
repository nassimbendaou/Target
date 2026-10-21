"""Local HTTP routes and request/response safety checks."""

import json
import hmac
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

from authlib.common.errors import AuthlibBaseError
from joserfc.errors import JoseError
from requests import RequestException

from .auth import FLOW_COOKIE, ORIGIN, can_operate, cookie


def make_handler(app, auth):
    class Handler(BaseHTTPRequestHandler):
        def log_request(self, code="-", size="-"):
            self.log_message(
                '"%s %s" %s %s', self.command, urlsplit(self.path).path, code, size
            )

        def respond(
            self,
            status,
            body,
            content_type="application/json; charset=utf-8",
            headers=(),
        ):
            data = (
                json.dumps(body).encode()
                if content_type.startswith("application/json")
                else body
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for name, value in headers:
                self.send_header(name, value)
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(data)

        def allowed_host(self):
            return self.headers.get("Host") in {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }

        def do_GET(self):
            if not self.allowed_host():
                return self.respond(403, {"error": "Invalid host"})
            path = urlsplit(self.path).path
            if path in {"/api/auth/login", "/api/auth/callback"}:
                try:
                    if path == "/api/auth/login":
                        location, login_cookie = auth.begin_login()
                        headers = [("Location", location), ("Set-Cookie", login_cookie)]
                    else:
                        session_cookie = auth.complete_login(
                            urlsplit(self.path).query, self.headers.get("Cookie")
                        )
                        headers = [
                            ("Location", ORIGIN),
                            ("Set-Cookie", session_cookie),
                            ("Set-Cookie", cookie(FLOW_COOKIE, "", 0)),
                        ]
                    return self.respond(302, {}, headers=headers)
                except (
                    ValueError,
                    AuthlibBaseError,
                    JoseError,
                    RequestException,
                    KeyError,
                    TypeError,
                ):
                    return self.respond(
                        401,
                        {
                            "error": "Sign-in failed. Check local Keycloak and try again."
                        },
                    )
            session = auth.session(self.headers.get("Cookie"))
            if not session:
                return self.respond(401, {"error": "Sign in with Keycloak"})
            if path == "/api/auth/session":
                return self.respond(
                    200, {**session, "can_operate": can_operate(session)}
                )
            if path == "/api/catalog":
                return self.respond(200, app.catalog.describe())
            if path == "/api/jobs":
                return self.respond(200, app.jobs())
            if path.startswith("/api/jobs/"):
                job = app.job(path.removeprefix("/api/jobs/"))
                return (
                    self.respond(200, job)
                    if job
                    else self.respond(404, {"error": "Job not found"})
                )
            return self.respond(404, {"error": "Not found"})

        def do_POST(self):
            if not self.allowed_host():
                return self.respond(403, {"error": "Invalid host"})
            origin = self.headers.get("Origin")
            if origin not in {
                f"http://127.0.0.1:{self.server.server_port}",
                f"http://localhost:{self.server.server_port}",
                "http://127.0.0.1:5173",
                "http://localhost:5173",
                "http://127.0.0.1:4173",
                "http://localhost:4173",
            }:
                return self.respond(403, {"error": "Invalid origin"})
            path = urlsplit(self.path).path
            length = self.headers.get("Content-Length", "0")
            if (
                self.headers.get("Transfer-Encoding")
                or len(length) > 4
                or not length.isascii()
                or not length.isdigit()
                or int(length) > 1024
            ):
                return self.respond(400, {"error": "Unsupported request"})
            length = int(length)
            session = auth.session(self.headers.get("Cookie"))
            if not session:
                return self.respond(401, {"error": "Sign in with Keycloak"})
            if not hmac.compare_digest(
                self.headers.get("X-CSRF-Token", "").encode(), session["csrf"].encode()
            ):
                return self.respond(403, {"error": "Invalid CSRF token"})
            if path == "/api/auth/logout":
                if length:
                    return self.respond(400, {"error": "Unsupported request"})
                return self.respond(
                    200,
                    {"signed_out": True},
                    headers=[("Set-Cookie", auth.logout(self.headers.get("Cookie")))],
                )
            if not can_operate(session):
                return self.respond(403, {"error": "Operator role required"})
            actor = {key: session["user"][key] for key in ("issuer", "sub", "name")}
            if path == "/api/jobs":
                template_id = "local-smoke"
                if length:
                    if (
                        self.headers.get("Content-Type", "").split(";")[0].strip()
                        != "application/json"
                    ):
                        return self.respond(400, {"error": "Expected JSON"})
                    try:
                        self.connection.settimeout(5)
                        parameters = json.loads(self.rfile.read(length))
                    except (ValueError, UnicodeDecodeError, OSError):
                        return self.respond(400, {"error": "Invalid JSON"})
                    if (
                        not isinstance(parameters, dict)
                        or set(parameters) != {"template_id"}
                        or not isinstance(parameters["template_id"], str)
                    ):
                        return self.respond(
                            400, {"error": "Choose an approved template"}
                        )
                    template_id = parameters["template_id"]
                try:
                    job = app.launch(template_id, actor=actor)
                except KeyError:
                    return self.respond(404, {"error": "Unknown job template"})
                return (
                    self.respond(201, job)
                    if job
                    else self.respond(409, {"error": "A job is already running"})
                )
            if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                if length:
                    return self.respond(400, {"error": "Unsupported request"})
                job_id = path.removeprefix("/api/jobs/").removesuffix("/cancel")
                if not job_id or "/" in job_id:
                    return self.respond(400, {"error": "Unsupported request"})
                status, job = app.cancel(job_id, actor=actor)
                if status == 404:
                    return self.respond(404, {"error": "Job not found"})
                if status == 409:
                    return self.respond(409, {"error": "Job is no longer running"})
                return self.respond(202, job)
            return self.respond(400, {"error": "Unsupported request"})

    return Handler
