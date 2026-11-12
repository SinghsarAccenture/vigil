"""Local web page for Vigil: shows the dashboard and lets you fetch today's filings with one click.

Run it with:  python src/vigil/serve.py   then open http://127.0.0.1:8000

The page is rebuilt from the database on every load. The "Get today's filings" button
does exactly what run_daily.py does (scrape, save, assess, report), in the background.
It listens on this computer only and is not meant to be exposed to a network.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil import run_daily  # noqa: E402
from vigil.report import dashboard  # noqa: E402
from vigil.storage import db  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8000


class RunState:
    """What the page can ask about: is a run going, and how did the last one end."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"running": False, "started_at": None, "finished_at": None,
                                      "exit_code": None, "report": ""}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def try_start(self) -> bool:
        with self._lock:
            if self._data["running"]:
                return False
            self._data.update(running=True, started_at=_now(), finished_at=None, exit_code=None, report="")
            return True

    def add_line(self, line: str) -> None:
        with self._lock:
            self._data["report"] += line + "\n"

    def finish(self, exit_code: int) -> None:
        with self._lock:
            self._data.update(running=False, finished_at=_now(), exit_code=exit_code)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def run_in_background(state: RunState, db_path: Path, dashboard_path: Path, max_pages: int | None) -> None:
    def work() -> None:
        try:
            code = run_daily.execute(db_path, max_pages=max_pages, dashboard_path=dashboard_path,
                                     out=state.add_line)
        except Exception as exc:  # never leave the page stuck on "running"
            state.add_line(f"FAILED   Unexpected error: {type(exc).__name__}: {exc}")
            code = 1
        state.finish(code)

    threading.Thread(target=work, daemon=True).start()


def make_handler(state: RunState, db_path: Path, dashboard_path: Path, max_pages: int | None, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _host_ok(self) -> bool:
            # Refuses requests addressed to any other name (blocks DNS-rebinding tricks).
            return self.headers.get("Host", "") in allowed_hosts

        def do_GET(self) -> None:
            if not self._host_ok():
                return self._json(403, {"error": "unexpected host"})
            if self.path in ("/", "/index.html"):
                conn = db.connect(db_path)
                try:
                    db.init_db(conn)
                    page = dashboard.render(dashboard.collect(conn), _now(), live=True)
                finally:
                    conn.close()
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if self.path == "/status":
                return self._json(200, state.snapshot())
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._host_ok():
                return self._json(403, {"error": "unexpected host"})
            origin = self.headers.get("Origin")
            if origin is not None and origin.removeprefix("http://") not in allowed_hosts:
                return self._json(403, {"error": "unexpected origin"})
            if self.path != "/run":
                return self._json(404, {"error": "not found"})
            if not state.try_start():
                return self._json(409, {"started": False, "reason": "a run is already in progress"})
            run_in_background(state, db_path, dashboard_path, max_pages)
            self._json(202, {"started": True})

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vigil local dashboard with a 'get filings' button.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--db", type=Path, help="database file (default: VIGIL_DB_PATH or vigil.db)")
    parser.add_argument("--max-pages", type=int, metavar="N",
                        help="only read the first N SEC pages per click (quicker demos; skips row-count checks)")
    args = parser.parse_args(argv)

    run_daily.load_env(run_daily.ENV_PATH)
    db_path = args.db or run_daily.default_db_path()
    dashboard_path = Path(db_path).with_name("dashboard.html")
    handler = make_handler(RunState(), db_path, dashboard_path, args.max_pages, args.port)
    try:
        server = ThreadingHTTPServer((HOST, args.port), handler)
    except OSError as exc:
        print(f"Could not start on port {args.port}: {exc}. Try --port 8001.")
        return 1
    print(f"Vigil dashboard: http://{HOST}:{args.port}   (database: {db_path})   Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
