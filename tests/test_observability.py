import json
import logging

import pytest
from fastmcp import Client, FastMCP

from au_opendata_mcp.errors import UpstreamUnavailable
from au_opendata_mcp.observability import (
    JsonFormatter,
    ToolTimingMiddleware,
    arguments_digest,
    configure_logging,
)


def capture(caplog) -> list[dict]:
    """Render captured records through JsonFormatter and parse them back."""
    formatter = JsonFormatter()
    return [json.loads(formatter.format(r)) for r in caplog.records]


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


def test_configure_logging_installs_the_json_formatter():
    configure_logging("INFO")
    root = logging.getLogger()
    assert any(isinstance(h.formatter, JsonFormatter) for h in root.handlers)
