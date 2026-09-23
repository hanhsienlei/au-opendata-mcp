import httpx
import pytest

pytestmark = pytest.mark.live


def test_datastore_search_sql_is_still_anonymous():
    r = httpx.get(
        "https://data.gov.au/data/api/3/action/datastore_search_sql",
        params={"sql": 'SELECT * FROM "c4972df1-faeb-4d0f-8e79-d43d4c7bd30a" LIMIT 1'},
        timeout=30.0,
    )
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_sa_portal_is_still_ckan_at_the_expected_path():
    r = httpx.get("https://data.sa.gov.au/data/api/3/action/status_show", timeout=30.0)
    assert r.status_code == 200
    assert "datastore" in r.json()["result"]["extensions"]


def test_resource_metadata_still_carries_datastore_active():
    r = httpx.get(
        "https://data.gov.au/data/api/3/action/package_search",
        params={"q": "csv", "rows": 5},
        timeout=30.0,
    )
    resources = [res for d in r.json()["result"]["results"] for res in d.get("resources", [])]
    assert any("datastore_active" in res for res in resources)
