"""Local HTTP routes and request/response safety checks."""

import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, body, content_type="application/json; charset=utf-8"):
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
            if origin and origin not in {
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
                    job = app.launch(template_id)
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
                status, job = app.cancel(job_id)
                if status == 404:
                    return self.respond(404, {"error": "Job not found"})
                if status == 409:
                    return self.respond(409, {"error": "Job is no longer running"})
                return self.respond(202, job)
            return self.respond(400, {"error": "Unsupported request"})

    return Handler
