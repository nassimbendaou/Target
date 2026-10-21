"""Start the single-process local Target API."""

import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .api import make_handler
from .jobs import TargetApp

ROOT = Path(__file__).resolve().parent.parent


def main():
    ansible_bin = shutil.which("ansible-playbook")
    if not ansible_bin:
        raise SystemExit(
            "ansible-playbook is required; run with nix develop --command python server.py"
        )
    server = ThreadingHTTPServer(("127.0.0.1", 8000), BaseHTTPRequestHandler)
    app = TargetApp(ROOT / ".data" / "target.sqlite3", ansible_bin)
    server.RequestHandlerClass = make_handler(app)
    print("Target API is ready at http://127.0.0.1:8000", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
