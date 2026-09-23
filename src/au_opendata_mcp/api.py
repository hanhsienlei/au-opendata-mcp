from typing import Any

from fastapi import APIRouter, Query

from au_opendata_mcp.deps import get_services
from au_opendata_mcp.errors import (
    AuOpenDataError,
    DownloadFailed,
    QueryTimeout,
    ResourceNotFound,
    ResourceNotQueryable,
    ResourceTooLarge,
    SqlRejected,
    UpstreamUnavailable,
)

STATUS_FOR: dict[type[AuOpenDataError], int] = {
    ResourceNotFound: 404,
    ResourceNotQueryable: 422,
    ResourceTooLarge: 413,
    SqlRejected: 400,
    UpstreamUnavailable: 502,
    DownloadFailed: 502,
    QueryTimeout: 504,
}

router = APIRouter(prefix="/api", tags=["catalog"])


@router.get("/portals")
async def portals() -> list[dict[str, Any]]:
    return [p.model_dump() for p in await get_services().catalog.list_portals()]


@router.get("/datasets")
async def datasets(
    q: str,
    portal: str = "data.gov.au",
    organisation: str | None = None,
    format: str | None = None,
    limit: int = Query(20, ge=1, le=50),
) -> list[dict[str, Any]]:
    results = await get_services().catalog.search_datasets(
        q=q, portal=portal, organisation=organisation, format=format, limit=limit
    )
    return [r.model_dump() for r in results]


@router.get("/datasets/{dataset_id}")
async def dataset(dataset_id: str, portal: str = "data.gov.au") -> dict[str, Any]:
    return (await get_services().catalog.get_dataset(dataset_id, portal)).model_dump()


@router.get("/organizations")
async def organizations(portal: str = "data.gov.au", q: str | None = None) -> list[dict[str, Any]]:
    orgs = await get_services().catalog.list_organizations(portal=portal, q=q)
    return [o.model_dump() for o in orgs]
