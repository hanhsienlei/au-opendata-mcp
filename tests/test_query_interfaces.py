import httpx
import respx
from fastapi.testclient import TestClient

from au_opendata_mcp.main import create_app
from tests.test_query_service import CSV, PACKAGE_SHOW, RES_URL, RESOURCE_SHOW


def mock_catalog() -> None:
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/resource_show").mock(
        return_value=httpx.Response(200, json=RESOURCE_SHOW)
    )
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=PACKAGE_SHOW)
    )


@respx.mock
def test_rest_query_endpoint_returns_rows_and_attribution():
    mock_catalog()
    respx.get(RES_URL).mock(return_value=httpx.Response(200, content=CSV))
    with TestClient(create_app()) as c:
        r = c.post(
            "/api/resources/r1/query",
            json={"sql": "SELECT suburb, count(*) AS n FROM t GROUP BY suburb"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["licence"] == "CC BY 4.0"
    assert body["row_count"] == 2


@respx.mock
def test_rest_query_endpoint_rejects_bad_sql_with_400():
    mock_catalog()
    with TestClient(create_app()) as c:
        r = c.post("/api/resources/r1/query", json={"sql": "DROP TABLE t"})
    assert r.status_code == 400
    assert "SELECT" in r.json()["detail"]


@respx.mock
def test_rest_preview_endpoint_returns_columns():
    mock_catalog()
    respx.get(RES_URL).mock(return_value=httpx.Response(200, content=CSV))
    with TestClient(create_app()) as c:
        r = c.get("/api/resources/r1/preview")
    assert r.status_code == 200
    assert [col["name"] for col in r.json()["columns"]] == ["suburb", "price"]


def test_mcp_tool_descriptions_teach_the_dialect_and_table_name():
    """Spec section 5.3: the description is the prompt. A model that guesses costs a round trip."""
    from au_opendata_mcp.mcp_server import query_resource

    doc = query_resource.__doc__ or ""
    assert "DuckDB" in doc
    assert "`t`" in doc or " t " in doc
    assert "SELECT" in doc
    assert "FROM t" in doc  # a worked example is present
