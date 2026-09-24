"""The HTTP transport must not write caller content to the log.

`configure_logging` silences the chatty client libraries, but until this test existed
nothing pinned the *server* side: uvicorn's own `uvicorn.access` logger writes the full
request line -- query string and all -- through its own handler with `propagate: False`,
so `configure_logging` cannot reach it. Two concrete leaks were observed live:

    INFO:  127.0.0.1:56130 - "GET /api/datasets?q=<a user's question>&portal=... HTTP/1.1" 200 OK
    INFO:  127.0.0.1:56132 - "GET /api/tools/abn/51824753556 HTTP/1.1" 200 OK

The second one is why the replacement logs the *route template* rather than the concrete
path: caller-supplied values live in the path as well as the query string.

These tests capture the real production log stream -- `configure_logging` plus its
JsonFormatter -- rather than a caplog-only view, so a leak from any logger in the process
(ours, httpx's, anything else) fails the assertion.
"""

import io
import json
import logging

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from au_opendata_mcp.main import cli, create_app
from au_opendata_mcp.observability import _NOISY_THIRD_PARTY_LOGGERS, configure_logging

# Distinctive enough that a substring match cannot be a coincidence.
MARKER = "MYSECRETQUESTION-ABOUT-DIVORCE"
ABN = "51824753556"

EMPTY_SEARCH = {"success": True, "result": {"count": 0, "results": []}}


@pytest.fixture
def log_output():
    """Install the server's real logging configuration, writing to a buffer.

    configure_logging replaces root's handlers wholesale and quiets the noisy
    third-party loggers; all of that is restored afterwards so no later test
    inherits it (same contract as test_observability.py's restore_root_logging).
    """
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_third_party = {name: logging.getLogger(name).level for name in _NOISY_THIRD_PARTY_LOGGERS}
    configure_logging("INFO")
    stream = io.StringIO()
    root.handlers[0].setStream(stream)
    try:
        yield stream
    finally:
        root.handlers = saved_handlers
        root.level = saved_level
        for name, level in saved_third_party.items():
            logging.getLogger(name).setLevel(level)


def records(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def requests_logged(stream: io.StringIO) -> list[dict]:
    return [r for r in records(stream) if r.get("event") == "request"]


@respx.mock
def test_no_caller_content_reaches_the_log_and_requests_are_still_recorded(log_output):
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(200, json=EMPTY_SEARCH)
    )
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(404, json={"success": False})
    )
    with TestClient(create_app()) as client:
        assert client.get("/api/datasets", params={"q": MARKER}).status_code == 200
        assert client.get(f"/api/tools/abn/{ABN}").status_code == 200
        # The dataset id is caller-supplied and sits in the path, not the query string.
        assert client.get(f"/api/datasets/{MARKER}").status_code == 404

    everything = log_output.getvalue()
    assert MARKER not in everything, "a caller's search text reached the log"
    assert ABN not in everything, "a caller-supplied ABN reached the log"

    seen = {(r["method"], r["route"], r["status"]) for r in requests_logged(log_output)}
    assert ("GET", "/api/datasets", 200) in seen
    assert ("GET", "/api/tools/abn/{abn}", 200) in seen
    assert ("GET", "/api/datasets/{dataset_id}", 404) in seen
    assert all(isinstance(r["duration_ms"], int | float) for r in requests_logged(log_output)), (
        "every request record carries a duration"
    )


@respx.mock
def test_an_unrouted_request_is_logged_without_crashing(log_output):
    with TestClient(create_app()) as client:
        assert client.get(f"/no-such-endpoint/{MARKER}").status_code == 404

    assert MARKER not in log_output.getvalue()
    assert ("GET", "<unrouted>", 404) in {
        (r["method"], r["route"], r["status"]) for r in requests_logged(log_output)
    }


def test_the_http_transport_turns_uvicorn_access_logging_off(monkeypatch, log_output):
    """uvicorn.access owns its own handler and does not propagate, so the only way to stop
    it writing the raw request line (query string included) is to not enable it."""
    captured: dict = {}

    def fake_run(app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)
    monkeypatch.setattr("sys.argv", ["au-opendata-mcp", "--transport", "http", "--port", "8123"])
    cli()

    assert captured["access_log"] is False
