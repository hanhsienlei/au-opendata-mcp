import httpx
import respx

from au_opendata_mcp.ckan.client import CkanClient
from au_opendata_mcp.config import Settings
from au_opendata_mcp.portals import get_portal
from au_opendata_mcp.services.catalog import CatalogService

SETTINGS = Settings()


def build_service() -> CatalogService:
    http = httpx.AsyncClient()
    clients = {
        p: CkanClient(get_portal(p), http, SETTINGS) for p in ("data.gov.au", "data.sa.gov.au")
    }
    return CatalogService(clients, SETTINGS)


def dataset_with_sizeless_csv(url: str = "https://x.org/stops.csv") -> dict:
    return {
        "success": True,
        "result": {
            "id": "abc",
            "name": "d",
            "title": "D",
            "license_title": "CC BY 4.0",
            "organization": {"name": "o", "title": "O"},
            "resources": [
                {
                    "id": "r1",
                    "name": "stops.csv",
                    "format": "CSV",
                    "url": url,
                    "datastore_active": False,
                },
            ],
        },
    }


@respx.mock
async def test_head_resolves_an_unknown_size_into_queryable_yes():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=dataset_with_sizeless_csv())
    )
    respx.head("https://x.org/stops.csv").mock(
        return_value=httpx.Response(200, headers={"content-length": "4096"})
    )
    resource = (await build_service().get_dataset("d")).resources[0]
    assert resource.queryable == "yes"
    assert resource.size_bytes == 4096
    assert resource.queryable_reason is None


@respx.mock
async def test_head_revealing_an_oversized_resource_marks_it_not_queryable():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=dataset_with_sizeless_csv())
    )
    respx.head("https://x.org/stops.csv").mock(
        return_value=httpx.Response(200, headers={"content-length": str(812 * 1024 * 1024)})
    )
    resource = (await build_service().get_dataset("d")).resources[0]
    assert resource.queryable == "no"
    assert "812 MB" in resource.queryable_reason


@respx.mock
async def test_head_timeout_stays_unknown_and_never_blocks():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=dataset_with_sizeless_csv())
    )
    respx.head("https://x.org/stops.csv").mock(side_effect=httpx.ConnectTimeout("slow"))
    resource = (await build_service().get_dataset("d")).resources[0]
    assert resource.queryable == "unknown"
    assert "could not be probed" in resource.queryable_reason


@respx.mock
async def test_head_without_content_length_stays_unknown():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=dataset_with_sizeless_csv())
    )
    respx.head("https://x.org/stops.csv").mock(return_value=httpx.Response(200))
    resource = (await build_service().get_dataset("d")).resources[0]
    assert resource.queryable == "unknown"


@respx.mock
async def test_resources_already_decided_are_not_probed_at_all():
    payload = dataset_with_sizeless_csv()
    payload["result"]["resources"][0]["size"] = 2048
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=payload)
    )
    head = respx.head("https://x.org/stops.csv")
    resource = (await build_service().get_dataset("d")).resources[0]
    assert resource.queryable == "yes"
    assert head.call_count == 0


@respx.mock
async def test_size_minus_one_is_probed_and_resolved():
    """data.gov.au uses size: -1 as its unknown size sentinel.
    Task 3 normalises it to "unknown", so the probe should resolve it."""
    payload = dataset_with_sizeless_csv()
    payload["result"]["resources"][0]["size"] = -1
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=payload)
    )
    respx.head("https://x.org/stops.csv").mock(
        return_value=httpx.Response(200, headers={"content-length": "4096"})
    )
    resource = (await build_service().get_dataset("d")).resources[0]
    assert resource.queryable == "yes"
    assert resource.size_bytes == 4096
    assert resource.queryable_reason is None


@respx.mock
async def test_head_uses_the_dedicated_5_second_timeout():
    """Verify the HEAD probe uses head_timeout_seconds (5s), not upstream_timeout_seconds (15s).
    This is critical for keepingget_dataset responsive when probing stalled resources."""
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=dataset_with_sizeless_csv())
    )
    head_route = respx.head("https://x.org/stops.csv").mock(
        return_value=httpx.Response(200, headers={"content-length": "4096"})
    )
    await build_service().get_dataset("d")
    # Verify the HEAD request was made and had the correct timeout
    assert head_route.call_count == 1
    timeout = head_route.calls[0].request.extensions["timeout"]
    assert timeout["connect"] == SETTINGS.head_timeout_seconds
    assert timeout["read"] == SETTINGS.head_timeout_seconds
    assert timeout["write"] == SETTINGS.head_timeout_seconds
    assert timeout["pool"] == SETTINGS.head_timeout_seconds
