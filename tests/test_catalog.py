import json
import pathlib

import httpx
import pytest
import respx

from au_opendata_mcp.ckan.client import CkanClient
from au_opendata_mcp.ckan.models import CkanResource
from au_opendata_mcp.config import Settings
from au_opendata_mcp.errors import ResourceNotFound
from au_opendata_mcp.portals import get_portal
from au_opendata_mcp.services.catalog import CatalogService, assess_queryable

SETTINGS = Settings()
FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "ckan"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def build_service() -> CatalogService:
    http = httpx.AsyncClient()
    clients = {
        p: CkanClient(get_portal(p), http, SETTINGS) for p in ("data.gov.au", "data.sa.gov.au")
    }
    return CatalogService(clients, SETTINGS)


def package_search_payload(*datasets: dict) -> dict:
    return {"success": True, "result": {"count": len(datasets), "results": list(datasets)}}


DATASET = {
    "id": "abc-123",
    "name": "sa-bus-stops",
    "title": "SA Bus Stops",
    "notes": "Every bus stop in South Australia.",
    "license_title": "Creative Commons Attribution 4.0",
    "metadata_modified": "2026-08-01T00:00:00",
    "organization": {"name": "dpti", "title": "Dept for Infrastructure and Transport"},
    "resources": [
        {
            "id": "r1",
            "name": "stops.csv",
            "format": "CSV",
            "url": "https://x.org/stops.csv",
            "size": 2048,
            "datastore_active": True,
            "last_modified": "2026-08-01T00:00:00",
        },
        {
            "id": "r2",
            "name": "guide.pdf",
            "format": "PDF",
            "url": "https://x.org/guide.pdf",
            "size": 100,
            "datastore_active": False,
        },
    ],
}


# --- assess_queryable: the flag that stops the model burning a call on an error ---


def test_tabular_resource_under_the_ceiling_is_queryable():
    r = CkanResource(id="r", format="CSV", size=1024, url="https://x/a.csv")
    assert assess_queryable(r, SETTINGS) == ("yes", None)


def test_non_tabular_format_is_not_queryable_and_says_why():
    r = CkanResource(id="r", format="PDF", size=1024, url="https://x/a.pdf")
    verdict, reason = assess_queryable(r, SETTINGS)
    assert verdict == "no"
    assert "PDF" in reason and "not tabular" in reason


def test_oversized_resource_is_not_queryable_and_states_both_numbers():
    r = CkanResource(id="r", format="CSV", size=812 * 1024 * 1024, url="https://x/a.csv")
    verdict, reason = assess_queryable(r, SETTINGS)
    assert verdict == "no"
    assert "812 MB" in reason and "50 MB" in reason


def test_missing_size_is_unknown_not_a_silent_pass():
    r = CkanResource(id="r", format="CSV", size=None, url="https://x/a.csv")
    verdict, reason = assess_queryable(r, SETTINGS)
    assert verdict == "unknown"
    assert "size" in reason.lower()


def test_missing_format_is_not_queryable():
    r = CkanResource(id="r", format=None, size=10, url="https://x/a")
    assert assess_queryable(r, SETTINGS)[0] == "no"


# --- correction 1: data.gov.au's "unknown size" sentinel is -1, not None ---
# 6 of 29 resources in the recorded package_search fixture carry size=-1. Any
# non-positive size must normalise to "unknown" so Task 4's HEAD probe fires.


def test_sentinel_size_negative_one_is_unknown_not_silently_queryable():
    r = CkanResource(id="r", format="CSV", size=-1, url="https://x/a.csv")
    verdict, reason = assess_queryable(r, SETTINGS)
    assert verdict == "unknown"
    assert "size" in reason.lower()


def test_zero_size_is_unknown_not_silently_queryable():
    r = CkanResource(id="r", format="CSV", size=0, url="https://x/a.csv")
    verdict, reason = assess_queryable(r, SETTINGS)
    assert verdict == "unknown"
    assert "size" in reason.lower()


# --- search_datasets ---


@respx.mock
async def test_search_returns_sourced_summaries():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(200, json=package_search_payload(DATASET))
    )
    results = await build_service().search_datasets("bus stops")
    assert len(results) == 1
    hit = results[0]
    assert hit.dataset_id == "sa-bus-stops"
    assert hit.title == "SA Bus Stops"
    assert hit.organisation == "Dept for Infrastructure and Transport"
    assert hit.licence == "Creative Commons Attribution 4.0"
    assert hit.dataset_url == "https://data.gov.au/dataset/sa-bus-stops"
    assert sorted(hit.formats) == ["CSV", "PDF"]


@respx.mock
async def test_search_limit_is_clamped_to_the_configured_maximum():
    route = respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(200, json=package_search_payload())
    )
    await build_service().search_datasets("x", limit=5000)
    assert route.calls[0].request.url.params["rows"] == str(SETTINGS.max_search_limit)


@respx.mock
async def test_search_across_all_portals_merges_and_tags_each_result():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(200, json=package_search_payload(DATASET))
    )
    respx.get(url__startswith="https://data.sa.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(
            200, json=package_search_payload({**DATASET, "name": "sa-only"})
        )
    )
    results = await build_service().search_datasets("bus", portal="all")
    assert {r.portal for r in results} == {"data.gov.au", "data.sa.gov.au"}


@respx.mock
async def test_organisation_and_format_become_ckan_filter_queries():
    route = respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(200, json=package_search_payload())
    )
    await build_service().search_datasets("x", organisation="dpti", format="CSV")
    fq = route.calls[0].request.url.params["fq"]
    assert 'organization:"dpti"' in fq
    assert 'res_format:"CSV"' in fq


# --- correction 2: parse a RECORDED fixture end-to-end, not just the synthetic DATASET ---


@respx.mock
async def test_search_parses_the_recorded_data_gov_au_fixture():
    payload = load_fixture("data.gov.au__package_search.json")
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(200, json=payload)
    )
    results = await build_service().search_datasets("bus stops")

    raw = payload["result"]["results"]
    assert len(results) == len(raw)

    by_id = {r.dataset_id: r for r in results}
    first = raw[0]
    hit = by_id[first["name"]]
    assert hit.title == first["title"]
    assert hit.organisation == first["organization"]["title"]
    assert hit.licence == first["license_title"]
    assert hit.dataset_url == f"https://data.gov.au/dataset/{first['name']}"
    assert hit.modified == first["metadata_modified"]

    for hit in results:
        assert hit.dataset_url
        assert hit.organisation
        assert hit.licence


# --- get_dataset ---


@respx.mock
async def test_get_dataset_marks_each_resource_queryable_or_not():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json={"success": True, "result": DATASET})
    )
    detail = await build_service().get_dataset("sa-bus-stops")
    by_id = {r.resource_id: r for r in detail.resources}
    assert by_id["r1"].queryable == "yes"
    assert by_id["r2"].queryable == "no"
    assert "PDF" in by_id["r2"].queryable_reason
    assert detail.licence == "Creative Commons Attribution 4.0"


@respx.mock
async def test_missing_dataset_raises_resource_not_found():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(404, json={"success": False})
    )
    with pytest.raises(ResourceNotFound):
        await build_service().get_dataset("nope")


# --- correction 2: parse a RECORDED fixture end-to-end for get_dataset too ---


@respx.mock
async def test_get_dataset_parses_the_recorded_data_gov_au_fixture():
    payload = load_fixture("data.gov.au__package_show.json")
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=payload)
    )
    detail = await build_service().get_dataset("national-public-toilet-map")

    raw = payload["result"]
    assert detail.dataset_id == raw["name"]
    assert detail.title == raw["title"]
    assert detail.organisation == raw["organization"]["title"]
    assert detail.licence == raw["license_title"]
    assert detail.dataset_url == f"https://data.gov.au/dataset/{raw['name']}"
    assert detail.dataset_url
    assert detail.organisation
    assert detail.licence
    assert len(detail.resources) == len(raw["resources"])

    by_id = {r.resource_id: r for r in detail.resources}
    csv_resource = next(r for r in raw["resources"] if r["format"] == "CSV")
    pdf_resource = next(r for r in raw["resources"] if r["format"] == "PDF")
    assert by_id[csv_resource["id"]].queryable == "yes"
    assert by_id[pdf_resource["id"]].queryable == "no"


# --- list_organizations and list_portals ---


@respx.mock
async def test_list_organizations_returns_names_and_counts():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/organization_list").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "result": [
                    {
                        "name": "abs",
                        "title": "Australian Bureau of Statistics",
                        "package_count": 412,
                    },
                ],
            },
        )
    )
    orgs = await build_service().list_organizations()
    assert orgs[0].name == "abs"
    assert orgs[0].dataset_count == 412


@respx.mock
async def test_list_organizations_filters_by_substring_case_insensitively():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/organization_list").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "result": [
                    {"name": "abs", "title": "Australian Bureau of Statistics", "package_count": 4},
                    {"name": "aemo", "title": "Energy Market Operator", "package_count": 9},
                ],
            },
        )
    )
    orgs = await build_service().list_organizations(q="bureau")
    assert [o.name for o in orgs] == ["abs"]


# --- correction 3: list_organizations must also support portal="all" ---


@respx.mock
async def test_list_organizations_across_all_portals_tags_each_by_portal():
    for host in ("data.gov.au", "data.sa.gov.au"):
        respx.get(url__startswith=f"https://{host}/data/api/3/action/organization_list").mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [
                        {"name": f"org-{host}", "title": "Org", "package_count": 1},
                    ],
                },
            )
        )
    orgs = await build_service().list_organizations(portal="all")
    assert {o.portal for o in orgs} == {"data.gov.au", "data.sa.gov.au"}


@respx.mock
async def test_list_portals_reports_a_dataset_count_per_portal():
    for host in ("data.gov.au", "data.sa.gov.au"):
        respx.get(url__startswith=f"https://{host}/data/api/3/action/package_search").mock(
            return_value=httpx.Response(
                200, json={"success": True, "result": {"count": 70123, "results": []}}
            )
        )
    portals = await build_service().list_portals()
    assert {p.id for p in portals} == {"data.gov.au", "data.sa.gov.au"}
    assert all(p.dataset_count == 70123 for p in portals)


@respx.mock
async def test_list_portals_degrades_to_none_when_a_portal_is_down():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(500)
    )
    respx.get(url__startswith="https://data.sa.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(
            200, json={"success": True, "result": {"count": 1200, "results": []}}
        )
    )
    counts = {p.id: p.dataset_count for p in await build_service().list_portals()}
    assert counts["data.gov.au"] is None
    assert counts["data.sa.gov.au"] == 1200
