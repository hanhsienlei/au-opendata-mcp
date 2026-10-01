import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from au_opendata_mcp.main import create_app

DATASET = {
    "id": "abc",
    "name": "sa-bus-stops",
    "title": "SA Bus Stops",
    "notes": "Stops.",
    "license_title": "CC BY 4.0",
    "metadata_modified": "2026-08-01T00:00:00",
    "organization": {"name": "dpti", "title": "DIT"},
    "resources": [
        {
            "id": "r1",
            "name": "s.csv",
            "format": "CSV",
            "url": "https://x.org/s.csv",
            "size": 2048,
            "datastore_active": True,
        }
    ],
}


@respx.mock
def test_search_endpoint_returns_sourced_rows():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(
            200, json={"success": True, "result": {"count": 40, "results": [DATASET]}}
        )
    )
    with TestClient(create_app()) as c:
        body = c.get("/api/datasets", params={"q": "bus stops"}).json()
    assert body["total_matches"] == {"data.gov.au": 40}
    assert body["results"][0]["dataset_id"] == "sa-bus-stops"
    assert body["results"][0]["licence"] == "CC BY 4.0"


@respx.mock
def test_missing_dataset_maps_to_404_not_a_500():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(404, json={"success": False})
    )
    with TestClient(create_app()) as c:
        r = c.get("/api/datasets/nope")
    assert r.status_code == 404
    assert "detail" in r.json()


@respx.mock
def test_upstream_failure_maps_to_502_and_names_the_portal():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(500)
    )
    with TestClient(create_app()) as c:
        r = c.get("/api/datasets", params={"q": "x"})
    assert r.status_code == 502
    assert "data.gov.au" in r.json()["detail"]


def test_unknown_portal_is_a_400_with_the_valid_options():
    with TestClient(create_app()) as c:
        r = c.get("/api/datasets", params={"q": "x", "portal": "data.nsw.gov.au"})
    assert r.status_code == 400
    assert "data.gov.au" in r.json()["detail"]


# --- A portal outage must surface as 502 (docs/design.md §4.2), not 400.
# Before the fix, a bare resp.json() in ckan/client.py raised ValueError on all three of
# these and main.py's ValueError handler returned
# HTTP 400 {"detail":"Expecting value: line 1 column 1 (char 0)"} -- blaming the caller for
# an upstream outage, and handing an MCP model a message it will retry against forever.


@pytest.mark.parametrize(
    ("label", "response"),
    [
        ("redirect", {"status_code": 302, "headers": {"location": "https://moved.test/"}}),
        ("no content", {"status_code": 204}),
        (
            "waf block page",
            {
                "status_code": 200,
                "text": "<html><body>Request blocked</body></html>",
                "headers": {"content-type": "text/html"},
            },
        ),
    ],
)
@respx.mock
def test_a_portal_outage_is_a_502_not_a_400(label, response):
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(**response)
    )
    with TestClient(create_app()) as c:
        r = c.get("/api/datasets", params={"q": "bus stops"})
    assert r.status_code == 502, f"{label} was reported as {r.status_code}"
    assert "Expecting value" not in r.json()["detail"]
    assert "data.gov.au" in r.json()["detail"]


# --- REST used to hardcode its own limit ceilings (and leave
# QueryRequest.limit unbounded) while the service clamped against Settings, so the two
# adapters disagreed about who owns the limit. The REST bounds are now derived from Settings.


def test_the_rest_limit_bounds_are_the_ones_settings_declares():
    from au_opendata_mcp.config import Settings

    settings = Settings()
    schema = create_app().openapi()

    def query_param(path: str, method: str, name: str) -> dict:
        params = schema["paths"][path][method]["parameters"]
        return next(p for p in params if p["name"] == name)["schema"]

    assert query_param("/api/datasets", "get", "limit")["maximum"] == settings.max_search_limit
    preview = query_param("/api/resources/{resource_id}/preview", "get", "limit")
    assert preview["maximum"] == settings.max_preview_limit
    orgs = query_param("/api/organizations", "get", "limit")
    assert orgs["maximum"] == settings.max_org_limit

    body = schema["components"]["schemas"]["QueryRequest"]["properties"]["limit"]
    assert body["maximum"] == settings.max_query_limit, "the POST body limit was unbounded"
    assert body["minimum"] == 1


@respx.mock
def test_a_query_limit_over_the_settings_ceiling_is_rejected_by_rest():
    with TestClient(create_app()) as c:
        r = c.post(
            "/api/resources/r1/query",
            json={"sql": "SELECT 1", "limit": 100_000},
        )
    assert r.status_code == 422
