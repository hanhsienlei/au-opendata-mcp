import httpx
import respx

from tests.test_query_service import CSV, PACKAGE_SHOW, RES_URL, RESOURCE_SHOW, build

DATASTORE_RESOURCE = {
    "success": True,
    "result": {**RESOURCE_SHOW["result"], "datastore_active": True},
}
DATASTORE_PACKAGE = {
    "success": True,
    "result": {**PACKAGE_SHOW["result"], "resources": [DATASTORE_RESOURCE["result"]]},
}
DATASTORE_SEARCH = {
    "success": True,
    "result": {
        "total": 4821,
        "fields": [
            {"id": "_id", "type": "int"},
            {"id": "suburb", "type": "text"},
            {"id": "price", "type": "numeric"},
        ],
        "records": [
            {"_id": 1, "_full_text": "'adelaide':1 '100':2", "suburb": "Adelaide", "price": 100},
            {"_id": 2, "_full_text": "'glenelg':1 '200':2", "suburb": "Glenelg", "price": 200},
        ],
    },
}


def mock_datastore_catalog() -> None:
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/resource_show").mock(
        return_value=httpx.Response(200, json=DATASTORE_RESOURCE)
    )
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=DATASTORE_PACKAGE)
    )


@respx.mock
async def test_a_datastore_resource_previews_without_any_download(tmp_path):
    mock_datastore_catalog()
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/datastore_search").mock(
        return_value=httpx.Response(200, json=DATASTORE_SEARCH)
    )
    download = respx.get(RES_URL)
    result = await build(tmp_path).preview_resource("r1")
    assert result.source == "datastore"
    assert download.call_count == 0
    assert result.total_rows == 4821
    assert [c.name for c in result.columns] == ["_id", "suburb", "price"]


@respx.mock
async def test_the_tsvector_column_never_reaches_the_caller(tmp_path):
    mock_datastore_catalog()
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/datastore_search").mock(
        return_value=httpx.Response(200, json=DATASTORE_SEARCH)
    )
    result = await build(tmp_path).preview_resource("r1")
    assert all("_full_text" not in row for row in result.rows)


@respx.mock
async def test_a_non_datastore_resource_materialises_and_says_so(tmp_path):
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/resource_show").mock(
        return_value=httpx.Response(200, json=RESOURCE_SHOW)
    )
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=PACKAGE_SHOW)
    )
    respx.get(RES_URL).mock(return_value=httpx.Response(200, content=CSV))
    result = await build(tmp_path).preview_resource("r1")
    assert result.source == "materialised"
    assert [c.name for c in result.columns] == ["suburb", "price"]
    assert result.total_rows == 3


@respx.mock
async def test_preview_then_query_costs_only_one_download(tmp_path):
    """The expensive preview warms the cache for the query that follows it (spec 6.5)."""
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/resource_show").mock(
        return_value=httpx.Response(200, json=RESOURCE_SHOW)
    )
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=PACKAGE_SHOW)
    )
    download = respx.get(RES_URL).mock(return_value=httpx.Response(200, content=CSV))
    service = build(tmp_path)
    await service.preview_resource("r1")
    await service.query_resource("r1", "SELECT count(*) AS n FROM t")
    assert download.call_count == 1


@respx.mock
async def test_preview_limit_is_clamped(tmp_path):
    mock_datastore_catalog()
    route = respx.get(
        url__startswith="https://data.gov.au/data/api/3/action/datastore_search"
    ).mock(return_value=httpx.Response(200, json=DATASTORE_SEARCH))
    await build(tmp_path).preview_resource("r1", limit=9999)
    assert route.calls[0].request.url.params["limit"] == "20"


@respx.mock
async def test_preview_falls_back_to_materialising_when_datastore_search_fails(tmp_path):
    mock_datastore_catalog()
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/datastore_search").mock(
        return_value=httpx.Response(500)
    )
    respx.get(RES_URL).mock(return_value=httpx.Response(200, content=CSV))
    result = await build(tmp_path).preview_resource("r1")
    assert result.source == "materialised"
