import httpx
import respx

from au_opendata_mcp.ckan.client import CkanClient
from au_opendata_mcp.config import Settings
from au_opendata_mcp.portals import get_portal
from au_opendata_mcp.services.catalog import CatalogService

SETTINGS = Settings()


def build_service(resolve=lambda host: ["93.184.216.34"]) -> CatalogService:
    http = httpx.AsyncClient()
    clients = {
        p: CkanClient(get_portal(p), http, SETTINGS) for p in ("data.gov.au", "data.sa.gov.au")
    }
    service = CatalogService(clients, SETTINGS, http)
    # Public IPs by default; the SSRF resolver itself is exercised in test_safety_urls.py.
    service._resolve = resolve
    return service


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


# --- fix (I1): the HEAD probe fetched any portal-named address, including link-local,
# with redirects on. Proven before the fix: a resource whose url was
# http://169.254.169.254/latest/meta-data/ was HEADed and reported queryable: "yes".
# Worse, follow_redirects=True was passed on the *same shared client* whose
# follow_redirects=False invariant Materialiser.__init__ raises a ValueError to protect.

LINK_LOCAL = "http://169.254.169.254/latest/meta-data/"


@respx.mock
async def test_a_link_local_resource_url_is_refused_and_never_head_requested():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=dataset_with_sizeless_csv(LINK_LOCAL))
    )
    head = respx.head(LINK_LOCAL).mock(
        return_value=httpx.Response(200, headers={"content-length": "4096"})
    )
    service = build_service(resolve=lambda host: ["169.254.169.254"])
    resource = (await service.get_dataset("d")).resources[0]

    assert head.call_count == 0, "the server must not fetch a link-local address at all"
    assert resource.queryable == "no"
    assert "refused" in resource.queryable_reason
    assert "169.254.169.254" in resource.queryable_reason
    assert resource.size_bytes is None


@respx.mock
async def test_a_refused_resource_does_not_fail_the_whole_dataset():
    """One bad resource among many must degrade to a reason, not blow up get_dataset."""
    payload = dataset_with_sizeless_csv(LINK_LOCAL)
    payload["result"]["resources"].append(
        {
            "id": "r2",
            "name": "ok.csv",
            "format": "CSV",
            "url": "https://x.org/ok.csv",
            "size": 2048,
            "datastore_active": False,
        }
    )
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=payload)
    )

    def resolve(host: str) -> list[str]:
        return ["169.254.169.254"] if host == "169.254.169.254" else ["93.184.216.34"]

    detail = await build_service(resolve=resolve).get_dataset("d")
    by_id = {r.resource_id: r for r in detail.resources}
    assert by_id["r1"].queryable == "no"
    assert by_id["r2"].queryable == "yes"


@respx.mock
async def test_the_probe_never_follows_a_redirect_to_an_unvalidated_address():
    """With follow_redirects=True (the bug) httpx would chase this hop itself, and the
    post-redirect address would never be checked -- the exact bypass
    Materialiser.__init__ refuses to allow on this shared client."""
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=dataset_with_sizeless_csv())
    )
    respx.head("https://x.org/stops.csv").mock(
        return_value=httpx.Response(302, headers={"location": LINK_LOCAL})
    )
    redirected_to = respx.head(LINK_LOCAL).mock(
        return_value=httpx.Response(200, headers={"content-length": "4096"})
    )
    resource = (await build_service().get_dataset("d")).resources[0]

    assert redirected_to.call_count == 0
    assert resource.queryable == "unknown"
    assert resource.size_bytes is None


@respx.mock
async def test_head_redirect_with_content_length_does_not_confuse_size():
    """data.gov.au resources commonly 302 to S3 or Azure blob storage. The redirect
    response carries its own Content-Length for the redirect body, not the actual resource.
    This regresses if we read content-length without checking the status code first."""
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=dataset_with_sizeless_csv())
    )
    respx.head("https://x.org/stops.csv").mock(
        return_value=httpx.Response(302, headers={"content-length": "138"})
    )
    resource = (await build_service().get_dataset("d")).resources[0]

    assert resource.queryable == "unknown", "redirect should not reveal queryability"
    assert resource.size_bytes is None, "redirect content-length should not be used as size"
    assert "could not be probed" in resource.queryable_reason


@respx.mock
async def test_head_error_with_content_length_does_not_confuse_size():
    """Error responses (4xx, 5xx) like 405 Method Not Allowed also carry their own
    Content-Length for the error page, not the actual resource. Same issue as redirects."""
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=dataset_with_sizeless_csv())
    )
    respx.head("https://x.org/stops.csv").mock(
        return_value=httpx.Response(405, headers={"content-length": "512"})
    )
    resource = (await build_service().get_dataset("d")).resources[0]

    assert resource.queryable == "unknown", "error should not reveal queryability"
    assert resource.size_bytes is None, "error content-length should not be used as size"
    assert "could not be probed" in resource.queryable_reason
