import datetime
import decimal
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import duckdb
from pydantic import BaseModel

from au_opendata_mcp.ckan.client import CkanClient
from au_opendata_mcp.ckan.models import CkanResource
from au_opendata_mcp.config import Settings
from au_opendata_mcp.errors import ResourceNotFound, SqlRejected
from au_opendata_mcp.models import Sourced
from au_opendata_mcp.safety import validate_sql, wrap_with_limit
from au_opendata_mcp.services.catalog import (
    CatalogService,
    DatasetDetail,
    ResourceInfo,
    assess_queryable,
)
from au_opendata_mcp.services.materialise import Materialiser

log = logging.getLogger(__name__)


class ColumnInfo(BaseModel):
    name: str
    type: str


class QueryResult(Sourced):
    resource_id: str
    portal: str
    sql: str
    columns: list[ColumnInfo]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool


class PreviewResult(Sourced):
    resource_id: str
    portal: str
    source: Literal["datastore", "materialised"]
    columns: list[ColumnInfo]
    rows: list[dict[str, Any]]
    total_rows: int | None


@dataclass(slots=True)
class ResolvedResource:
    resource: ResourceInfo
    dataset: DatasetDetail
    catalog_urls: set[str]
    last_modified: str | None


def jsonable(value: Any) -> Any:
    """Coerce DuckDB values into something both MCP and JSON can carry."""
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).hex()
    return value


def execute_in_sandbox(
    parquet: Path, sql: str, limit: int, memory_limit: str
) -> tuple[list[ColumnInfo], list[dict[str, Any]], bool]:
    """Run caller SQL against a locked-down in-memory DuckDB.

    THE ORDER OF THE FIRST THREE STATEMENTS IS THE SECURITY CONTROL (spec section 6.2).
    The table must be materialised BEFORE external access is revoked, because revoking it
    also disables read_parquet. Locking first and loading second fails outright; loading
    first and never locking leaves the filesystem open to caller SQL.
    Verified on DuckDB 1.5.5. Do not reorder. See tests/test_query_execute.py.
    """
    con = duckdb.connect()
    try:
        con.execute(f"SET memory_limit = '{memory_limit}'")
        con.execute("CREATE TABLE t AS SELECT * FROM read_parquet(?)", [str(parquet)])
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")

        cursor = con.execute(wrap_with_limit(sql, limit))
        description = cursor.description or []
        columns = [ColumnInfo(name=str(d[0]), type=str(d[1])) for d in description]
        names = [c.name for c in columns]
        fetched = cursor.fetchall()
    except duckdb.Error as exc:
        raise SqlRejected(f"the query could not be run: {exc}") from exc
    finally:
        con.close()

    truncated = len(fetched) > limit
    rows = [
        {name: jsonable(value) for name, value in zip(names, row, strict=True)}
        for row in fetched[:limit]
    ]
    return columns, rows, truncated


class QueryService:
    def __init__(
        self,
        clients: Mapping[str, CkanClient],
        catalog: CatalogService,
        materialiser: Materialiser,
        settings: Settings,
    ) -> None:
        self.clients = clients
        self.catalog = catalog
        self.materialiser = materialiser
        self.settings = settings

    async def resolve(self, resource_id: str, portal: str = "data.gov.au") -> ResolvedResource:
        raw = await self.clients[portal].action("resource_show", {"id": resource_id})
        package_id = raw.get("package_id")
        if not package_id:
            raise ResourceNotFound(
                f"resource {resource_id!r} has no parent dataset on {portal}",
                remedy_url=raw.get("url"),
            )
        dataset = await self.catalog.get_dataset(package_id, portal)
        listed = next((r for r in dataset.resources if r.resource_id == resource_id), None)
        if listed is None:
            raise ResourceNotFound(
                f"resource {resource_id!r} is not listed on dataset {dataset.dataset_id!r}",
                remedy_url=dataset.dataset_url,
            )
        # resource_show is the authoritative, freshly fetched source for THIS resource_id.
        # The matching entry inside the parent dataset's resources list (from package_show)
        # is only used above to confirm the resource is genuinely part of this dataset --
        # trusting its (potentially stale) format/url instead of resource_show's own answer
        # would let a resource whose format has since changed be classified from cached data.
        ckan_resource = CkanResource.model_validate(raw)
        verdict, reason = assess_queryable(ckan_resource, self.settings)
        resource = ResourceInfo(
            resource_id=ckan_resource.id,
            name=ckan_resource.name,
            format=ckan_resource.format,
            size_bytes=ckan_resource.size,
            url=ckan_resource.url,
            datastore_active=ckan_resource.datastore_active,
            queryable=verdict,
            queryable_reason=reason,
        )
        catalog_urls = {r.url for r in dataset.resources if r.url}
        if resource.url:
            # Portal-declared for this exact resource_id via resource_show -- same trust
            # boundary as the dataset's own resources list, so this only widens coverage
            # for the case above, never the set of hosts that could be trusted.
            catalog_urls.add(resource.url)
        return ResolvedResource(
            resource=resource,
            dataset=dataset,
            catalog_urls=catalog_urls,
            last_modified=raw.get("last_modified") or dataset.modified,
        )

    async def query_resource(
        self,
        resource_id: str,
        sql: str,
        portal: str = "data.gov.au",
        limit: int = 100,
    ) -> QueryResult:
        # Validate before resolving or downloading: a bad query must cost nothing.
        normalised = validate_sql(sql)
        capped = max(1, min(limit, self.settings.max_query_limit))

        resolved = await self.resolve(resource_id, portal)
        parquet = await self.materialiser.parquet_for(
            resolved.resource, resolved.catalog_urls, portal, resolved.last_modified
        )
        columns, rows, truncated = execute_in_sandbox(
            parquet, normalised, capped, self.settings.duckdb_memory_limit
        )
        log.info(
            "query_resource resource_id=%s rows=%d truncated=%s", resource_id, len(rows), truncated
        )
        return QueryResult(
            resource_id=resource_id,
            portal=portal,
            sql=normalised,
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            dataset_url=resolved.dataset.dataset_url,
            organisation=resolved.dataset.organisation,
            licence=resolved.dataset.licence,
        )
