"""Tests for MetadataCache wired into CkanClient.action().

These are separate from tests/test_cache.py (which tests the caches in isolation)
and from tests/test_ckan_client.py (whose call-count assertions must keep passing
unmodified, because CkanClient(portal, http, settings) with no cache argument
must still make one HTTP request per action() call).
"""

import json
import pathlib

import httpx
import pytest
import respx

from au_opendata_mcp.cache import MetadataCache
from au_opendata_mcp.ckan.client import CkanClient
from au_opendata_mcp.config import Settings
from au_opendata_mcp.errors import UpstreamUnavailable
from au_opendata_mcp.portals import get_portal

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "ckan"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@respx.mock
async def test_two_identical_calls_through_a_cache_hit_the_network_once():
    payload = load("data.gov.au__package_search.json")
    route = respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(200, json=payload)
    )
    cache = MetadataCache(max_entries=100, ttl_seconds=3600, clock=FakeClock())
    c = CkanClient(get_portal("data.gov.au"), httpx.AsyncClient(), Settings(), cache=cache)

    first = await c.action("package_search", {"q": "bus stops"})
    second = await c.action("package_search", {"q": "bus stops"})

    assert route.call_count == 1
    assert first == second


@respx.mock
async def test_parameter_order_does_not_affect_the_cache_hit():
    payload = load("data.gov.au__package_search.json")
    route = respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(200, json=payload)
    )
    cache = MetadataCache(max_entries=100, ttl_seconds=3600, clock=FakeClock())
    c = CkanClient(get_portal("data.gov.au"), httpx.AsyncClient(), Settings(), cache=cache)

    first = await c.action("package_search", {"q": "x", "rows": 5})
    second = await c.action("package_search", {"rows": 5, "q": "x"})

    assert route.call_count == 1
    assert first == second


@respx.mock
async def test_different_portals_do_not_share_a_cache_entry():
    gov_payload = load("data.gov.au__package_search.json")
    sa_payload = load("data.sa.gov.au__package_search.json")
    gov_route = respx.get(
        url__startswith="https://data.gov.au/data/api/3/action/package_search"
    ).mock(return_value=httpx.Response(200, json=gov_payload))
    sa_route = respx.get(
        url__startswith="https://data.sa.gov.au/data/api/3/action/package_search"
    ).mock(return_value=httpx.Response(200, json=sa_payload))

    cache = MetadataCache(max_entries=100, ttl_seconds=3600, clock=FakeClock())
    http = httpx.AsyncClient()
    settings = Settings()
    gov_client = CkanClient(get_portal("data.gov.au"), http, settings, cache=cache)
    sa_client = CkanClient(get_portal("data.sa.gov.au"), http, settings, cache=cache)

    gov_result = await gov_client.action("package_search", {"q": "x"})
    sa_result = await sa_client.action("package_search", {"q": "x"})

    assert gov_route.call_count == 1
    assert sa_route.call_count == 1
    assert gov_result != sa_result


@respx.mock
async def test_a_failing_call_is_not_cached_so_a_later_success_still_hits_the_network():
    payload = load("data.gov.au__package_search.json")
    route = respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search")
    route.side_effect = [
        httpx.Response(500, text="boom"),
        httpx.Response(200, json={"success": False, "error": {"message": "bad"}}),
        httpx.Response(200, json=payload),
    ]
    cache = MetadataCache(max_entries=100, ttl_seconds=3600, clock=FakeClock())
    settings = Settings(upstream_retries=0)
    c = CkanClient(get_portal("data.gov.au"), httpx.AsyncClient(), settings, cache=cache)

    with pytest.raises(UpstreamUnavailable):
        await c.action("package_search", {"q": "x"})
    with pytest.raises(UpstreamUnavailable):
        await c.action("package_search", {"q": "x"})

    result = await c.action("package_search", {"q": "x"})
    assert "results" in result
    assert route.call_count == 3


@respx.mock
async def test_a_transport_error_is_not_cached_so_a_later_success_still_hits_the_network():
    payload = load("data.gov.au__package_search.json")
    route = respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search")
    route.side_effect = [httpx.ConnectTimeout("timed out"), httpx.Response(200, json=payload)]
    cache = MetadataCache(max_entries=100, ttl_seconds=3600, clock=FakeClock())
    settings = Settings(upstream_retries=0)
    c = CkanClient(get_portal("data.gov.au"), httpx.AsyncClient(), settings, cache=cache)

    with pytest.raises(UpstreamUnavailable):
        await c.action("package_search", {"q": "x"})

    result = await c.action("package_search", {"q": "x"})
    assert "results" in result
    assert route.call_count == 2


@respx.mock
async def test_client_without_a_cache_makes_one_request_per_call():
    payload = load("data.gov.au__package_search.json")
    route = respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(200, json=payload)
    )
    c = CkanClient(get_portal("data.gov.au"), httpx.AsyncClient(), Settings())

    await c.action("package_search", {"q": "bus stops"})
    await c.action("package_search", {"q": "bus stops"})

    assert route.call_count == 2
