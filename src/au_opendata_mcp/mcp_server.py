from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from au_opendata_mcp.deps import get_services
from au_opendata_mcp.errors import AuOpenDataError

mcp: FastMCP = FastMCP(
    name="au-opendata-mcp",
    instructions=(
        "Australian government open data. Workflow: search_datasets to find a dataset, "
        "get_dataset to list its resources and see which are queryable, preview_resource "
        "to learn a resource's columns, then query_resource to ask the real question. "
        "Every result carries its source URL, publisher and licence — cite them."
    ),
)


def _fail(exc: AuOpenDataError) -> ToolError:
    suffix = f" You can download the source directly: {exc.remedy_url}" if exc.remedy_url else ""
    return ToolError(f"{exc.message}.{suffix}")


@mcp.tool
async def list_portals() -> list[dict[str, Any]]:
    """List the Australian open data portals this server can query, with dataset counts.

    Call this when the user asks what data sources are available."""
    try:
        return [p.model_dump() for p in await get_services().catalog.list_portals()]
    except AuOpenDataError as exc:
        raise _fail(exc) from exc


@mcp.tool
async def search_datasets(
    q: str,
    portal: str = "data.gov.au",
    organisation: str | None = None,
    format: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search Australian government datasets by keyword.

    Args:
        q: Keywords, e.g. "bus stops", "rainfall", "hospital waiting times".
        portal: "data.gov.au" (federal, default), "data.sa.gov.au" (South Australia),
            or "all" to search both.
        organisation: Optional publisher name to filter by, as returned by list_organizations.
        format: Optional resource format filter, e.g. "CSV".
        limit: Maximum datasets to return, capped at 50.

    Returns dataset id, title, publisher, licence, available formats and the dataset URL.
    Follow up with get_dataset to see a dataset's individual resources."""
    try:
        results = await get_services().catalog.search_datasets(
            q=q, portal=portal, organisation=organisation, format=format, limit=limit
        )
        return [r.model_dump() for r in results]
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except AuOpenDataError as exc:
        raise _fail(exc) from exc


@mcp.tool
async def get_dataset(dataset_id: str, portal: str = "data.gov.au") -> dict[str, Any]:
    """Get one dataset's full metadata and its list of resources (files).

    Each resource reports `queryable`: "yes" means query_resource will work on it,
    "no" gives the reason (wrong format, or too large), "unknown" means the size could
    not be determined in advance. Use the resource_id of a queryable resource with
    preview_resource and query_resource.

    Args:
        dataset_id: The dataset name/id from search_datasets.
        portal: The portal the dataset came from."""
    try:
        return (await get_services().catalog.get_dataset(dataset_id, portal)).model_dump()
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except AuOpenDataError as exc:
        raise _fail(exc) from exc


@mcp.tool
async def list_organizations(
    portal: str = "data.gov.au", q: str | None = None
) -> list[dict[str, Any]]:
    """List the publishers on a portal, with how many datasets each has.

    Args:
        portal: The portal to list publishers for.
        q: Optional case-insensitive substring filter, e.g. "bureau"."""
    try:
        orgs = await get_services().catalog.list_organizations(portal=portal, q=q)
        return [o.model_dump() for o in orgs]
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except AuOpenDataError as exc:
        raise _fail(exc) from exc
