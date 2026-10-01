import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from au_opendata_mcp.main import create_app
from au_opendata_mcp.mcp_server import mcp

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def test_health_and_mcp_are_served_by_the_same_app():
    with TestClient(create_app()) as c:
        assert c.get("/health").json() == {"status": "ok"}
        r = c.post("/mcp/", json=INITIALIZE, headers=MCP_HEADERS)
        assert r.status_code == 200
        assert "serverInfo" in r.text


async def test_mcp_advertises_the_catalog_tools():
    """fastmcp.Client speaks MCP to the server in-process — no transport, no sockets."""
    from fastmcp import Client

    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert {"list_portals", "search_datasets", "get_dataset", "list_organizations"} <= names


async def test_every_tool_carries_a_description():
    """Tool descriptions are the prompt (spec 5.3). An undescribed tool is an unusable one."""
    from fastmcp import Client

    async with Client(mcp) as client:
        for tool in await client.list_tools():
            assert tool.description, f"tool {tool.name} has no description"


def test_omitting_the_fastmcp_lifespan_silently_breaks_only_mcp():
    """Regression guard for docs/design.md §4.4. REST keeps working, which is the trap."""
    mcp_app = mcp.http_app(path="/")
    broken = FastAPI()  # deliberately no lifespan=mcp_app.lifespan

    @broken.get("/health")
    def health():
        return {"status": "ok"}

    broken.mount("/mcp", mcp_app)

    with TestClient(broken) as c:
        assert c.get("/health").status_code == 200  # REST is fine — the trap
        with pytest.raises(RuntimeError, match="task group|Task group"):
            c.post("/mcp/", json=INITIALIZE, headers=MCP_HEADERS)
