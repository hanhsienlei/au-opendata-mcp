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


# --- A CKAN error message must never carry the caller's own text.
# `params` for package_search holds the user's raw `q`, and CKAN/Solr routinely echoes the
# submitted query back inside its own error text. Both were being interpolated into the
# exception message, which services/catalog.py then logged verbatim.

MARKER = "CONFIDENTIAL-TAX-EVASION-QUESTION"


@respx.mock
async def test_a_404_names_the_param_keys_but_never_their_values(client):
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(404, json={"success": False})
    )
    c = await client()
    with pytest.raises(ResourceNotFound) as exc:
        await c.action("package_search", {"q": MARKER, "rows": 20})
    message = str(exc.value)
    assert MARKER not in message
    # Still diagnosable: the portal, the action and which parameters were sent.
    assert "data.gov.au" in message
    assert "package_search" in message
    assert "rows" in message


@respx.mock
async def test_ckans_own_error_text_is_not_echoed_back(client):
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(
            200,
            json={"success": False, "error": {"message": f"undefined field {MARKER}"}},
        )
    )
    c = await client()
    with pytest.raises(UpstreamUnavailable) as exc:
        await c.action("package_search", {"q": MARKER})
    message = str(exc.value)
    assert MARKER not in message
    assert "data.gov.au" in message


@respx.mock
async def test_a_transport_failure_reports_its_type_not_the_requested_url(client):
    """httpx transport errors can stringify with the failing URL attached, and that URL
    carries `q=<the user's question>` in its query string."""
    route = respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search")
    route.side_effect = httpx.ConnectError(f"failed connecting while fetching q={MARKER}")
    c = await client()
    with pytest.raises(UpstreamUnavailable) as exc:
        await c.action("package_search", {"q": MARKER})
    message = str(exc.value)
    assert MARKER not in message
    assert "ConnectError" in message


# --- A portal outage must not be reported as the caller's mistake.
# A bare resp.json() on a 3xx, a 204, or a 200 carrying a WAF/CDN error page raised
# ValueError, which main.py's handler turned into HTTP 400 with the text
# "Expecting value: line 1 column 1 (char 0)".


@pytest.mark.parametrize(
    ("label", "response"),
    [
        ("redirect", {"status_code": 302, "headers": {"location": "https://elsewhere.test/"}}),
        ("no content", {"status_code": 204}),
        (
            "html error page",
            {
                "status_code": 200,
                "text": "<html><body>Access Denied</body></html>",
                "headers": {"content-type": "text/html"},
            },
        ),
    ],
)
@respx.mock
async def test_a_non_json_portal_response_is_an_upstream_outage(client, label, response):
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(**response)
    )
    c = await client()
    with pytest.raises(UpstreamUnavailable) as exc:
        await c.action("package_search", {"q": "x"})
    assert "data.gov.au" in str(exc.value)


@respx.mock
async def test_a_json_body_that_is_not_an_object_is_an_upstream_outage(client):
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(200, json=["not", "a", "ckan", "envelope"])
    )
    c = await client()
    with pytest.raises(UpstreamUnavailable):
        await c.action("package_search", {"q": "x"})
