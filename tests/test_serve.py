import http.client
import json
import socket
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from vigil import run_daily, serve


@pytest.fixture
def server(tmp_path, monkeypatch):
    release = threading.Event()
    calls = []

    def fake_execute(db_path, *, max_pages=None, dashboard_path=None, out=print, **kw):
        calls.append(max_pages)
        out("fake run started")
        release.wait(5)
        out("RESULT: OK")
        return 0

    monkeypatch.setattr(run_daily, "execute", fake_execute)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    handler = serve.make_handler(serve.RunState(), tmp_path / "t.db", tmp_path / "dashboard.html", 2, port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield port, release, calls
    release.set()
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, host=None, origin=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if origin:
        headers["Origin"] = origin
    conn.request(method, path, headers=headers)
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    return resp.status, body


def wait_until_done(port):
    for _ in range(50):
        status = json.loads(call(port, "GET", "/status")[1])
        if not status["running"]:
            return status
        time.sleep(0.1)
    raise AssertionError("run never finished")


def test_page_has_the_button(server):
    port, _, _ = server
    status, body = call(port, "GET", "/")
    assert status == 200 and "Get today's filings" in body


def test_clicking_runs_the_scrape_and_reports_the_result(server):
    port, release, calls = server
    assert call(port, "POST", "/run")[0] == 202
    release.set()
    status = wait_until_done(port)
    assert calls == [2] and status["exit_code"] == 0 and "RESULT: OK" in status["report"]


def test_a_second_click_while_running_is_refused(server):
    port, release, calls = server
    assert call(port, "POST", "/run")[0] == 202
    assert call(port, "POST", "/run")[0] == 409
    release.set()
    wait_until_done(port)
    assert len(calls) == 1


def test_a_run_can_start_again_after_one_finishes(server):
    port, release, calls = server
    release.set()
    call(port, "POST", "/run")
    wait_until_done(port)
    assert call(port, "POST", "/run")[0] == 202
    wait_until_done(port)
    assert len(calls) == 2


def test_requests_for_another_host_name_are_refused(server):
    port, _, _ = server
    assert call(port, "GET", "/", host="evil.example")[0] == 403
    assert call(port, "POST", "/run", host="evil.example")[0] == 403


def test_a_run_triggered_from_another_website_is_refused(server):
    port, _, calls = server
    assert call(port, "POST", "/run", origin="http://evil.example")[0] == 403
    assert calls == []


def test_a_crash_in_the_run_is_reported_not_left_hanging(tmp_path, monkeypatch):
    monkeypatch.setattr(run_daily, "execute", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    state = serve.RunState()
    assert state.try_start()
    serve.run_in_background(state, tmp_path / "t.db", tmp_path / "d.html", None)
    for _ in range(50):
        if not state.snapshot()["running"]:
            break
        time.sleep(0.1)
    snap = state.snapshot()
    assert snap["running"] is False and snap["exit_code"] == 1 and "boom" in snap["report"]
