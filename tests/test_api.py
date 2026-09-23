import httpx
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
            200, json={"success": True, "result": {"count": 1, "results": [DATASET]}}
        )
    )
    with TestClient(create_app()) as c:
        body = c.get("/api/datasets", params={"q": "bus stops"}).json()
    assert body[0]["dataset_id"] == "sa-bus-stops"
    assert body[0]["licence"] == "CC BY 4.0"


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
