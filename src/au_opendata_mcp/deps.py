from dataclasses import dataclass
from functools import lru_cache

import httpx

from au_opendata_mcp.cache import MetadataCache
from au_opendata_mcp.ckan.client import CkanClient
from au_opendata_mcp.config import Settings, get_settings
from au_opendata_mcp.portals import all_portals
from au_opendata_mcp.services.catalog import CatalogService


@dataclass(slots=True)
class Services:
    settings: Settings
    http: httpx.AsyncClient
    catalog: CatalogService


@lru_cache
def get_services() -> Services:
    settings = get_settings()
    http = httpx.AsyncClient(follow_redirects=False, headers={"User-Agent": "au-opendata-mcp/0.1"})
    # Shared across portals: safe because CkanClient's cache key includes the portal id,
    # so data.gov.au and data.sa.gov.au never serve each other's responses.
    metadata_cache = MetadataCache(
        max_entries=settings.metadata_cache_max_entries,
        ttl_seconds=settings.metadata_ttl_seconds,
    )
    clients = {p.id: CkanClient(p, http, settings, cache=metadata_cache) for p in all_portals()}
    return Services(settings=settings, http=http, catalog=CatalogService(clients, settings))


async def close_services() -> None:
    services = get_services()
    await services.http.aclose()
    get_services.cache_clear()
