import json
import pathlib

import httpx
import pytest
import respx

from au_opendata_mcp.ckan.client import CkanClient, strip_internal_columns
from au_opendata_mcp.config import Settings
from au_opendata_mcp.errors import ResourceNotFound, UpstreamUnavailable
from au_opendata_mcp.portals import get_portal

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "ckan"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def client():
    async def _build():
        return CkanClient(get_portal("data.gov.au"), httpx.AsyncClient(), Settings())

    return _build


@respx.mock
async def test_action_returns_the_result_payload(client):
    payload = load("data.gov.au__package_search.json")
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(200, json=payload)
    )
    c = await client()
    result = await c.action("package_search", {"q": "bus stops"})
    assert "results" in result


@respx.mock
async def test_upstream_500_becomes_upstream_unavailable(client):
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(500, text="boom")
    )
    c = await client()
    with pytest.raises(UpstreamUnavailable) as exc:
        await c.action("package_search", {"q": "x"})
    assert "data.gov.au" in str(exc.value)


@respx.mock
async def test_404_becomes_resource_not_found(client):
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(404, json={"success": False})
    )
    c = await client()
    with pytest.raises(ResourceNotFound):
        await c.action("package_show", {"id": "nope"})


@respx.mock
async def test_ckan_success_false_becomes_upstream_unavailable(client):
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(200, json={"success": False, "error": {"message": "bad"}})
    )
    c = await client()
    with pytest.raises(UpstreamUnavailable):
        await c.action("package_search", {"q": "x"})


@respx.mock
async def test_transport_error_is_retried_then_surfaces_as_upstream_unavailable(client):
    route = respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search")
    route.side_effect = httpx.ConnectTimeout("timed out")
    c = await client()
    with pytest.raises(UpstreamUnavailable):
        await c.action("package_search", {"q": "x"})
    assert route.call_count == Settings().upstream_retries + 1


def test_full_text_is_stripped_because_it_is_tsvector_noise():
    records = [{"_id": 1, "_full_text": "'-23.15':36 '-23.16':34", "suburb": "Glenelg"}]
    assert strip_internal_columns(records) == [{"_id": 1, "suburb": "Glenelg"}]


def test_stripping_tolerates_records_without_full_text():
    assert strip_internal_columns([{"_id": 1, "a": 2}]) == [{"_id": 1, "a": 2}]
