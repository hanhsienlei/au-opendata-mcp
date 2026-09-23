from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

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


class QueryRequest(BaseModel):
    sql: str
    portal: str = "data.gov.au"
    limit: int = 100


@router.get("/resources/{resource_id}/preview")
async def preview(
    resource_id: str,
    portal: str = "data.gov.au",
    limit: int = Query(20, ge=1, le=20),
) -> dict[str, Any]:
    result = await get_services().query.preview_resource(resource_id, portal, limit)
    return result.model_dump()


@router.post("/resources/{resource_id}/query")
async def query(resource_id: str, body: QueryRequest) -> dict[str, Any]:
    result = await get_services().query.query_resource(
        resource_id, body.sql, body.portal, body.limit
    )
    return result.model_dump()
