from au_opendata_mcp.config import Settings


def test_limits_match_the_spec():
    s = Settings()
    assert s.max_resource_bytes == 50 * 1024 * 1024
    assert s.upstream_timeout_seconds == 15.0
    assert s.head_timeout_seconds == 5.0
    assert s.download_timeout_seconds == 30.0
    assert s.resource_budget_seconds == 60.0
    assert s.metadata_ttl_seconds == 3600
    assert s.materialisation_ttl_seconds == 86400
    assert s.max_query_limit == 100
    assert s.max_preview_limit == 20
    assert s.max_search_limit == 50
    assert s.query_timeout_seconds == 30.0


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("AU_OPENDATA_MAX_RESOURCE_BYTES", "1024")
    assert Settings().max_resource_bytes == 1024
