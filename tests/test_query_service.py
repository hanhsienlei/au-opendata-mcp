import httpx
import pytest
import respx

from au_opendata_mcp.cache import BlobCache
from au_opendata_mcp.ckan.client import CkanClient
from au_opendata_mcp.config import Settings
from au_opendata_mcp.errors import ResourceNotQueryable, SqlRejected
from au_opendata_mcp.portals import get_portal
from au_opendata_mcp.services.catalog import CatalogService
from au_opendata_mcp.services.materialise import Materialiser
from au_opendata_mcp.services.query import QueryService

CSV = b"suburb,price\nAdelaide,100\nAdelaide,300\nGlenelg,200\n"
RES_URL = "https://x.org/stops.csv"
SETTINGS = Settings()

RESOURCE_SHOW = {
    "success": True,
    "result": {
        "id": "r1",
        "name": "stops.csv",
        "format": "CSV",
        "url": RES_URL,
        "size": len(CSV),
        "datastore_active": False,
        "package_id": "pkg-1",
        "last_modified": "2026-08-01T00:00:00",
    },
}
PACKAGE_SHOW = {
    "success": True,
    "result": {
        "id": "pkg-1",
        "name": "sa-bus-stops",
        "title": "SA Bus Stops",
        "license_title": "CC BY 4.0",
        "metadata_modified": "2026-08-01T00:00:00",
        "organization": {"name": "dpti", "title": "DIT"},
        "resources": [RESOURCE_SHOW["result"]],
    },
}


def build(tmp_path) -> QueryService:
    http = httpx.AsyncClient(follow_redirects=False)
    clients = {
        p: CkanClient(get_portal(p), http, SETTINGS) for p in ("data.gov.au", "data.sa.gov.au")
    }
    catalog = CatalogService(clients, SETTINGS)
    blobs = BlobCache(
        tmp_path / "blobs", SETTINGS.materialisation_ttl_seconds, SETTINGS.cache_max_bytes
    )
    materialiser = Materialiser(http, SETTINGS, blobs)
    materialiser._resolve = lambda host: ["93.184.216.34"]
    return QueryService(clients, catalog, materialiser, SETTINGS)


def mock_catalog() -> None:
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/resource_show").mock(
        return_value=httpx.Response(200, json=RESOURCE_SHOW)
    )
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=PACKAGE_SHOW)
    )


@respx.mock
async def test_a_query_returns_rows_with_attribution(tmp_path):
    mock_catalog()
    respx.get(RES_URL).mock(return_value=httpx.Response(200, content=CSV))
    result = await build(tmp_path).query_resource(
        "r1", "SELECT suburb, avg(price) AS mean FROM t GROUP BY suburb ORDER BY suburb"
    )
    assert result.rows[0] == {"suburb": "Adelaide", "mean": 200.0}
    assert result.licence == "CC BY 4.0"
    assert result.organisation == "DIT"
    assert result.dataset_url == "https://data.gov.au/dataset/sa-bus-stops"
    assert result.row_count == 2
    assert result.truncated is False


@respx.mock
async def test_invalid_sql_is_rejected_before_anything_is_downloaded(tmp_path):
    mock_catalog()
    download = respx.get(RES_URL)
    with pytest.raises(SqlRejected):
        await build(tmp_path).query_resource("r1", "DROP TABLE t")
    assert download.call_count == 0


@respx.mock
async def test_limit_is_clamped_to_the_configured_maximum(tmp_path):
    mock_catalog()
    respx.get(RES_URL).mock(return_value=httpx.Response(200, content=CSV))
    result = await build(tmp_path).query_resource("r1", "SELECT * FROM t", limit=100_000)
    assert len(result.rows) <= SETTINGS.max_query_limit


@respx.mock
async def test_querying_a_non_tabular_resource_returns_the_download_link(tmp_path):
    pdf_resource = {
        "success": True,
        "result": {**RESOURCE_SHOW["result"], "format": "PDF", "url": "https://x.org/a.pdf"},
    }
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/resource_show").mock(
        return_value=httpx.Response(200, json=pdf_resource)
    )
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=PACKAGE_SHOW)
    )
    with pytest.raises(ResourceNotQueryable) as exc:
        await build(tmp_path).query_resource("r1", "SELECT * FROM t")
    assert exc.value.remedy_url == "https://x.org/a.pdf"


@respx.mock
async def test_the_second_query_reuses_the_materialised_cache(tmp_path):
    mock_catalog()
    download = respx.get(RES_URL).mock(return_value=httpx.Response(200, content=CSV))
    service = build(tmp_path)
    await service.query_resource("r1", "SELECT * FROM t")
    await service.query_resource("r1", "SELECT count(*) AS n FROM t")
    assert download.call_count == 1
