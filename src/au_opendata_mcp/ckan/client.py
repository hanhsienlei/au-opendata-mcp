import asyncio
import json
import logging
from typing import Any

import httpx

from au_opendata_mcp.cache import MetadataCache
from au_opendata_mcp.config import Settings
from au_opendata_mcp.errors import ResourceNotFound, UpstreamUnavailable
from au_opendata_mcp.portals import Portal

log = logging.getLogger(__name__)

INTERNAL_COLUMNS = {"_full_text"}


def strip_internal_columns(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop CKAN's tsvector column. Left in, it dominates every response for zero information."""
    return [{k: v for k, v in r.items() if k not in INTERNAL_COLUMNS} for r in records]


class CkanClient:
    def __init__(
        self,
        portal: Portal,
        http: httpx.AsyncClient,
        settings: Settings,
        cache: MetadataCache | None = None,
    ) -> None:
        self.portal = portal
        self.http = http
        self.settings = settings
        self.cache = cache

    def _cache_key(self, name: str, params: dict[str, Any]) -> str:
        return f"{self.portal.id}:{name}:{json.dumps(params, sort_keys=True, default=str)}"

    async def action(self, name: str, params: dict[str, Any]) -> Any:
        cache_key = self._cache_key(name, params) if self.cache is not None else None
        if self.cache is not None and cache_key is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        url = f"{self.portal.api_base}/{name}"
        last_exc: Exception | None = None

        for attempt in range(self.settings.upstream_retries + 1):
            try:
                resp = await self.http.get(
                    url, params=params, timeout=self.settings.upstream_timeout_seconds
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < self.settings.upstream_retries:
                    await asyncio.sleep(0.2 * (2**attempt))
                    continue
                break

            if resp.status_code == 404:
                raise ResourceNotFound(
                    f"{name}({params}) not found on {self.portal.id}",
                    remedy_url=self.portal.site_url,
                )
            if resp.status_code >= 500:
                last_exc = UpstreamUnavailable(f"{self.portal.id} returned {resp.status_code}")
                if attempt < self.settings.upstream_retries:
                    await asyncio.sleep(0.2 * (2**attempt))
                    continue
                break
            if resp.status_code >= 400:
                raise UpstreamUnavailable(
                    f"{self.portal.id} rejected {name}: HTTP {resp.status_code}",
                    remedy_url=self.portal.site_url,
                )

            body = resp.json()
            if not body.get("success"):
                message = (body.get("error") or {}).get("message", "unknown CKAN error")
                raise UpstreamUnavailable(
                    f"{self.portal.id} reported failure for {name}: {message}",
                    remedy_url=self.portal.site_url,
                )
            result = body["result"]
            if self.cache is not None and cache_key is not None:
                self.cache.set(cache_key, result)
            return result

        raise UpstreamUnavailable(
            f"{self.portal.id} unreachable for {name}: {last_exc}",
            remedy_url=self.portal.site_url,
        )
