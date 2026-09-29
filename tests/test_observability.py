import io
import json
import logging

import httpx
import pytest
import respx
from fastmcp import Client, FastMCP

from au_opendata_mcp.errors import UpstreamUnavailable
from au_opendata_mcp.observability import (
    _NOISY_THIRD_PARTY_LOGGERS,
    JsonFormatter,
    ToolTimingMiddleware,
    arguments_digest,
    configure_logging,
)


def capture(caplog) -> list[dict]:
    """Render captured records through JsonFormatter and parse them back."""
    formatter = JsonFormatter()
    return [json.loads(formatter.format(r)) for r in caplog.records]


@pytest.fixture
def restore_root_logging():
    """configure_logging mutates global logging state destructively: it replaces
    root's handlers wholesale and quiets the noisy third-party loggers. Any test
    that calls it directly (not through caplog, which installs its own per-test
    handler) must put all of that back the way it found it, or every test
    collected afterwards inherits the JSON handler and the silenced loggers."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_third_party = {name: logging.getLogger(name).level for name in _NOISY_THIRD_PARTY_LOGGERS}
    try:
        yield root
    finally:
        root.handlers = saved_handlers
        root.level = saved_level
        for name, level in saved_third_party.items():
            logging.getLogger(name).setLevel(level)


def test_the_formatter_emits_one_json_object_per_record(caplog):
    with caplog.at_level(logging.INFO):
        logging.getLogger("t").info("hello")
    entry = capture(caplog)[0]
    assert entry["message"] == "hello"
    assert entry["level"] == "INFO"
    assert "timestamp" in entry


def test_extra_fields_are_merged_into_the_json(caplog):
    with caplog.at_level(logging.INFO):
        logging.getLogger("t").info("done", extra={"tool": "query_resource", "duration_ms": 12})
    entry = capture(caplog)[0]
    assert entry["tool"] == "query_resource"
    assert entry["duration_ms"] == 12


def test_the_formatter_output_is_always_parseable_even_with_odd_values(caplog):
    with caplog.at_level(logging.INFO):
        logging.getLogger("t").info("x", extra={"weird": {1, 2}})
    json.loads(JsonFormatter().format(caplog.records[0]))  # must not raise


def test_the_formatter_never_raises_on_a_circular_extra_value(caplog):
    # json's cycle detector fires while walking the container, before `default`
    # is ever consulted -- default=str alone does not save this case.
    circular: dict = {}
    circular["self"] = circular
    with caplog.at_level(logging.INFO):
        logging.getLogger("t").info("x", extra={"weird": circular})
    entry = json.loads(JsonFormatter().format(caplog.records[0]))  # must not raise
    assert entry["level"] == "INFO"


def test_the_formatter_never_raises_when_str_itself_raises(caplog):
    # default=str calls str(value); if that itself raises, the exception
    # propagates straight out of json.dumps unless the formatter catches it.
    class Unstringable:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    with caplog.at_level(logging.INFO):
        logging.getLogger("t").info("x", extra={"weird": Unstringable()})
    entry = json.loads(JsonFormatter().format(caplog.records[0]))  # must not raise
    assert entry["level"] == "INFO"


def test_the_digest_is_stable_and_order_independent():
    assert arguments_digest({"a": 1, "b": 2}) == arguments_digest({"b": 2, "a": 1})
    assert len(arguments_digest({"a": 1})) == 12


def test_the_digest_does_not_leak_the_argument_values():
    sql = "SELECT * FROM t WHERE email = 'someone@example.org'"
    digest = arguments_digest({"sql": sql})
    assert "example.org" not in digest
    assert "SELECT" not in digest


def test_different_arguments_produce_different_digests():
    assert arguments_digest({"q": "a"}) != arguments_digest({"q": "b"})


def test_none_arguments_are_handled():
    assert arguments_digest(None)


async def test_a_successful_tool_call_is_logged_with_its_duration(caplog):
    mcp = FastMCP("t")
    mcp.add_middleware(ToolTimingMiddleware())

    @mcp.tool
    def ping(x: str) -> dict:
        """Ping."""
        return {"x": x}

    with caplog.at_level(logging.INFO):
        async with Client(mcp) as c:
            await c.call_tool("ping", {"x": "hi"})

    entries = [e for e in capture(caplog) if e.get("event") == "tool_call"]
    assert len(entries) == 1
    assert entries[0]["tool"] == "ping"
    assert entries[0]["outcome"] == "ok"
    assert entries[0]["duration_ms"] >= 0
    assert entries[0]["args_digest"] == arguments_digest({"x": "hi"})


async def test_a_failing_tool_call_is_logged_as_an_error_and_still_raises(caplog):
    mcp = FastMCP("t")
    mcp.add_middleware(ToolTimingMiddleware())

    @mcp.tool
    def boom() -> dict:
        """Boom."""
        raise UpstreamUnavailable("data.gov.au is down")

    with caplog.at_level(logging.INFO):
        async with Client(mcp) as c:
            with pytest.raises(Exception):  # noqa: B017 -- fastmcp's Client wraps tool errors
                await c.call_tool("boom", {})

    entries = [e for e in capture(caplog) if e.get("event") == "tool_call"]
    assert entries[0]["outcome"] == "error"
    assert entries[0]["error_type"] == "UpstreamUnavailable"


def test_configure_logging_installs_the_json_formatter(restore_root_logging):
    configure_logging("INFO")
    assert any(isinstance(h.formatter, JsonFormatter) for h in restore_root_logging.handlers)


@respx.mock
async def test_httpx_request_logs_never_leak_a_query_string(restore_root_logging):
    """search_datasets sends `q` -- a user's question -- as a GET query string.
    httpx logs every request at INFO, including the full URL, and that logger
    propagates straight through root by default. configure_logging must quiet it,
    or the one thing arguments_digest exists to keep out of the log file (the
    argument content) leaks back in through a client library's own logging."""
    marker = "TOP-SECRET-SEARCH-TERM-abc123"
    respx.get("https://example.org/search").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    configure_logging("INFO")
    buffer = io.StringIO()
    restore_root_logging.handlers[0].setStream(buffer)

    async with httpx.AsyncClient() as client:
        await client.get("https://example.org/search", params={"q": marker})

    output = buffer.getvalue()
    assert marker not in output
