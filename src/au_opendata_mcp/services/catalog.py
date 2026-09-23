import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Literal, TypeVar

from pydantic import BaseModel

from au_opendata_mcp.ckan.client import CkanClient
from au_opendata_mcp.ckan.models import CkanDataset, CkanResource
from au_opendata_mcp.config import Settings
from au_opendata_mcp.errors import UpstreamUnavailable
from au_opendata_mcp.models import Sourced
from au_opendata_mcp.portals import Portal, all_portals, get_portal

log = logging.getLogger(__name__)

TABULAR_FORMATS = frozenset({"CSV", "TSV", "XLSX", "XLS"})
Queryable = Literal["yes", "no", "unknown"]
T = TypeVar("T")


class PortalInfo(BaseModel):
    id: str
    label: str
    site_url: str
    dataset_count: int | None


class ResourceInfo(BaseModel):
    resource_id: str
    name: str | None
    format: str | None
    size_bytes: int | None
    url: str | None
    datastore_active: bool
    queryable: Queryable
    queryable_reason: str | None


class DatasetSummary(Sourced):
    dataset_id: str
    title: str
    portal: str
    formats: list[str]
    modified: str | None


class DatasetDetail(Sourced):
    dataset_id: str
    title: str
    description: str | None
    portal: str
    modified: str | None
    resources: list[ResourceInfo]


class OrganizationSummary(BaseModel):
    name: str
    title: str | None
    dataset_count: int | None
    portal: str


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.0f} MB"


def assess_queryable(resource: CkanResource, settings: Settings) -> tuple[Queryable, str | None]:
    """Decide up front whether query_resource can work, so the caller need not discover it
    by consuming an error (spec section 5.1).

    data.gov.au uses ``size: -1`` as its "unknown size" sentinel, and CKAN's absent-size
    case is ``None``. Both, along with a reported ``0``, mean the same thing: the portal
    has not told us how big the resource is. Any non-positive size is normalised to
    "unknown" so Task 4's HEAD probe (which only fires on "unknown") gets a chance to
    resolve the real size instead of the resource being wrongly advertised as queryable.
    """
    fmt = (resource.format or "").strip().upper()
    if fmt not in TABULAR_FORMATS:
        shown = resource.format or "unknown"
        return "no", f"format {shown!r} is not tabular"
    size = resource.size
    if size is not None and size > 0:
        if size > settings.max_resource_bytes:
            return "no", (
                f"resource is {_mb(size)}, over the {_mb(settings.max_resource_bytes)} limit"
            )
        return "yes", None
    return "unknown", "size not declared by the portal; enforced during download instead"


def _licence(ds: CkanDataset) -> str:
    return ds.license_title or ds.license_id or "not specified"


def _organisation(ds: CkanDataset) -> str:
    if ds.organization is None:
        return "unknown"
    return ds.organization.title or ds.organization.name


def _dataset_url(portal: Portal, ds: CkanDataset) -> str:
    return f"{portal.site_url}/dataset/{ds.name}"


class CatalogService:
    def __init__(self, clients: Mapping[str, CkanClient], settings: Settings) -> None:
        self.clients = clients
        self.settings = settings

    def _client(self, portal_id: str) -> CkanClient:
        get_portal(portal_id)
        return self.clients[portal_id]

    async def _fan_out_all_portals(
        self, op: str, make_call: Callable[[Portal], Awaitable[list[T]]]
    ) -> list[T]:
        """Run `make_call` concurrently against every portal and merge the successes.

        A per-portal failure is logged at WARNING (naming the portal and the
        exception) and otherwise silently excluded from the merge — that is the
        intended "partial degradation" behaviour. But if *every* portal failed,
        returning an empty list would be indistinguishable from "no results",
        and the consumer here is a language model that reports the answer to a
        user in prose: it must not be able to confuse "nothing matched" with
        "we could not reach anything". So a total outage raises instead.
        """
        portals = all_portals()
        results = await asyncio.gather(*(make_call(p) for p in portals), return_exceptions=True)
        merged: list[T] = []
        failed: list[str] = []
        for p, item in zip(portals, results, strict=True):
            if isinstance(item, BaseException):
                log.warning("%s failed for portal %s: %s", op, p.id, item)
                failed.append(p.id)
            else:
                merged.extend(item)
        if portals and len(failed) == len(portals):
            raise UpstreamUnavailable(f"all portals unreachable for {op}: {', '.join(failed)}")
        return merged

    async def list_portals(self) -> list[PortalInfo]:
        async def count(portal: Portal) -> int | None:
            try:
                result = await self._client(portal.id).action("package_search", {"rows": 0})
                return int(result["count"])
            except Exception:
                return None  # a portal being down must not fail the whole listing

        portals = all_portals()
        counts = await asyncio.gather(*(count(p) for p in portals))
        return [
            PortalInfo(id=p.id, label=p.label, site_url=p.site_url, dataset_count=c)
            for p, c in zip(portals, counts, strict=True)
        ]

    async def search_datasets(
        self,
        q: str,
        portal: str = "data.gov.au",
        organisation: str | None = None,
        format: str | None = None,
        limit: int = 20,
    ) -> list[DatasetSummary]:
        if portal == "all":
            return await self._fan_out_all_portals(
                "search_datasets",
                lambda p: self.search_datasets(q, p.id, organisation, format, limit),
            )

        rows = max(1, min(limit, self.settings.max_search_limit))
        params: dict[str, object] = {"q": q, "rows": rows}
        filters = []
        if organisation:
            filters.append(f'organization:"{organisation}"')
        if format:
            filters.append(f'res_format:"{format.upper()}"')
        if filters:
            params["fq"] = " ".join(filters)

        result = await self._client(portal).action("package_search", params)
        p = get_portal(portal)
        summaries = []
        for raw in result.get("results", []):
            ds = CkanDataset.model_validate(raw)
            formats = sorted({r.format.upper() for r in ds.resources if r.format})
            summaries.append(
                DatasetSummary(
                    dataset_id=ds.name,
                    title=ds.title or ds.name,
                    portal=portal,
                    formats=formats,
                    modified=ds.metadata_modified,
                    dataset_url=_dataset_url(p, ds),
                    organisation=_organisation(ds),
                    licence=_licence(ds),
                )
            )
        return summaries

    async def get_dataset(self, dataset_id: str, portal: str = "data.gov.au") -> DatasetDetail:
        result = await self._client(portal).action("package_show", {"id": dataset_id})
        ds = CkanDataset.model_validate(result)
        p = get_portal(portal)
        resources = []
        for r in ds.resources:
            verdict, reason = assess_queryable(r, self.settings)
            resources.append(
                ResourceInfo(
                    resource_id=r.id,
                    name=r.name,
                    format=r.format,
                    size_bytes=r.size,
                    url=r.url,
                    datastore_active=r.datastore_active,
                    queryable=verdict,
                    queryable_reason=reason,
                )
            )
        return DatasetDetail(
            dataset_id=ds.name,
            title=ds.title or ds.name,
            description=ds.notes,
            portal=portal,
            modified=ds.metadata_modified,
            resources=resources,
            dataset_url=_dataset_url(p, ds),
            organisation=_organisation(ds),
            licence=_licence(ds),
        )

    async def list_organizations(
        self, portal: str = "data.gov.au", q: str | None = None
    ) -> list[OrganizationSummary]:
        if portal == "all":
            return await self._fan_out_all_portals(
                "list_organizations",
                lambda p: self.list_organizations(p.id, q),
            )

        result = await self._client(portal).action(
            "organization_list", {"all_fields": True, "limit": 1000}
        )
        orgs = []
        for raw in result:
            name = raw.get("name", "")
            title = raw.get("title")
            if q and q.lower() not in f"{name} {title or ''}".lower():
                continue
            orgs.append(
                OrganizationSummary(
                    name=name,
                    title=title,
                    dataset_count=raw.get("package_count"),
                    portal=portal,
                )
            )
        return orgs
