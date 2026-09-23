import hashlib
import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from fastmcp.server.middleware import Middleware

log = logging.getLogger("au_opendata_mcp")

# Attributes LogRecord always carries. Anything else came from `extra` and belongs in the JSON.
_STANDARD_FIELDS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, merging whatever was passed via `extra`."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_FIELDS:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def arguments_digest(arguments: Mapping[str, Any] | None) -> str:
    """A short, stable fingerprint of a call's arguments.

    Arguments are never logged verbatim: query_resource carries caller SQL and
    search_datasets carries a user's question. The digest is enough to correlate
    repeated calls without recording their content.
    """
    canonical = json.dumps(arguments or {}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


class ToolTimingMiddleware(Middleware):
    """Log every MCP tool call with its duration and outcome."""

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        message = getattr(context, "message", None)
        tool = getattr(message, "name", "unknown")
        digest = arguments_digest(getattr(message, "arguments", None))
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            # FastMCP wraps every tool-body exception in ToolError before it reaches
            # middleware (masked or not -- see server.py's call_tool), so
            # type(exc).__name__ is always "ToolError" and never the domain error
            # (UpstreamUnavailable, SqlRejected, ...) that actually happened. The
            # original is preserved via `raise ToolError(...) from e`, so walk the
            # cause chain to report the type that is actually useful for correlating
            # failures in the logs.
            root: BaseException = exc
            while root.__cause__ is not None:
                root = root.__cause__
            log.warning(
                "tool call failed",
                extra={
                    "event": "tool_call",
                    "tool": tool,
                    "args_digest": digest,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "outcome": "error",
                    "error_type": type(root).__name__,
                },
            )
            raise
        log.info(
            "tool call completed",
            extra={
                "event": "tool_call",
                "tool": tool,
                "args_digest": digest,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "outcome": "ok",
            },
        )
        return result
