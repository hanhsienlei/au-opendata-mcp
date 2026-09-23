# au-opendata-mcp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-hostable MCP server exposing Australian government open data (data.gov.au, data.sa.gov.au) as callable tools, with catalog search, sandboxed DuckDB querying of tabular resources, and two deterministic Australian utility tools.

**Architecture:** One service layer, two thin adapters. `services/` holds all logic, returns pydantic models, and raises domain exceptions; `mcp_server.py` (FastMCP) and `api.py` (FastAPI) are adapters that serialise results and translate exceptions. All caller SQL runs in DuckDB — never in CKAN's PostgreSQL DataStore — so there is exactly one dialect to document. The DataStore is used only for download-free schema and sample retrieval.

**Tech Stack:** Python 3.12, FastAPI, FastMCP 4.x, httpx (async), DuckDB 1.5.x, sqlglot 30.x, pydantic v2 + pydantic-settings, pytest + respx, uv, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-09-23-au-opendata-mcp-design.md` — read it alongside this plan. Section references below (§4.2, §6.3, …) point into it.

## Global Constraints

These apply to **every** task. They are not repeated per task.

- **Python 3.12.** Pinned in `.python-version` and `requires-python = ">=3.12,<3.13"`.
- **Package root is `src/au_opendata_mcp/`.** Tests import the installed package, never a relative path.
- **`services/` must never import `mcp_server.py` or `api.py`, and must never raise an HTTP exception.** Services raise the domain exceptions from `errors.py` only.
- **Every result model carrying dataset content or metadata inherits from `Sourced`** (§4.3): `DatasetSummary`, `DatasetDetail`, `PreviewResult`, `QueryResult`. It does *not* apply to `PortalInfo`, `OrganizationSummary`, or the utility-tool results — those carry nothing licensed. See Task 3 for the reasoning.
- **Resource ceiling: 50 MB.** Enforced during streaming, never from `content-length` alone (§6.4).
- **Timeouts:** general upstream 15 s; HEAD probe 5 s; download 30 s; total per-resource budget 60 s (§5.1, §6.4).
- **All caller SQL is DuckDB dialect.** The table is always named `t`. Single `SELECT` only (§6.3).
- **Test-first.** Every task follows: write failing test → run it and watch it fail → minimal implementation → run it and watch it pass → commit. Do not write implementation before its test fails for the right reason.
- **No network in the default test suite.** Upstream calls are replayed via `respx`. Live tests carry `@pytest.mark.live` and are deselected by default.
- **Coverage gate:** `--cov-fail-under=80` scoped to `src/au_opendata_mcp/services`.

## Verified facts (do not re-derive)

These were checked empirically on 2026-09-23 against the pinned library versions. Trust them.

- `duckdb`: data must be loaded into the connection **before** `SET enable_external_access=false`. Reversing it raises `PermissionException` on `read_parquet`. After locking, `read_csv_auto`, `read_parquet`, `ATTACH`, `INSTALL`, and `SET enable_external_access=true` all raise. `lock_configuration=true` makes the lockdown irreversible.
- `sqlglot`: `sqlglot.parse(sql, read="duckdb")` returns a **list** — stacked and comment-hidden statements yield `len > 1`. For an `exp.Anonymous` function node, use **`node.name`** to get the function name. `node.sql_name()` returns the literal string `"ANONYMOUS"` and is useless here.
- `sqlglot`: `SELECT * FROM read_csv_auto('/x')` produces an `exp.Table` whose `.name` is the **empty string**. The "only table `t`" rule rejects it naturally.
- `fastmcp` 4.0.5: `mcp.http_app(path="/")` returns a `StarletteWithLifespan` exposing `.lifespan`. Mount it at `/mcp`. Without passing that lifespan to `FastAPI(...)`, REST routes still return 200 but `/mcp/` raises `RuntimeError: Task group is not initialized` — a silent failure for everything except MCP.
- MCP requests need `Accept: application/json, text/event-stream` and POST to `/mcp/` (**trailing slash**). Responses are SSE-framed.
- ABN checksum: subtract 1 from the first digit, apply weights `[10,1,3,5,7,9,11,13,15,17,19]`, require `sum % 89 == 0`. Validates the ATO example `51 824 753 556`.
- `data.sa.gov.au` is CKAN 2.9.12 at `https://data.sa.gov.au/data/api/3/action/`. `datastore_search_sql` on data.gov.au works anonymously.

## File structure

| File | Responsibility |
| --- | --- |
| `src/au_opendata_mcp/config.py` | Settings via pydantic-settings; all limits and TTLs |
| `src/au_opendata_mcp/portals.py` | Portal registry and lookup |
| `src/au_opendata_mcp/errors.py` | Domain exception hierarchy |
| `src/au_opendata_mcp/models.py` | `Sourced` base and shared result models |
| `src/au_opendata_mcp/ckan/client.py` | Async CKAN Action API transport: retries, timeouts, response hygiene |
| `src/au_opendata_mcp/ckan/models.py` | Raw CKAN shapes: Dataset, Resource, Organization |
| `src/au_opendata_mcp/services/catalog.py` | search / get / list operations over the catalog |
| `src/au_opendata_mcp/services/query.py` | `preview_resource`, `query_resource` |
| `src/au_opendata_mcp/services/materialise.py` | Resource URL → local Parquet |
| `src/au_opendata_mcp/services/au_tools.py` | ABN and financial-year pure functions |
| `src/au_opendata_mcp/cache.py` | `MetadataCache` and `BlobCache` protocols + implementations |
| `src/au_opendata_mcp/safety.py` | SSRF guard and SQL validation |
| `src/au_opendata_mcp/mcp_server.py` | FastMCP tool registrations |
| `src/au_opendata_mcp/api.py` | FastAPI routes |
| `src/au_opendata_mcp/main.py` | App assembly and CLI entry point |

---

# Week 1 — Catalog and interfaces

### Task 1: Scaffold, settings, portal registry, error hierarchy

**Files:**
- Create: `pyproject.toml`, `.python-version`, `.gitignore`, `README.md` (stub)
- Create: `src/au_opendata_mcp/__init__.py`, `config.py`, `portals.py`, `errors.py`, `models.py`
- Test: `tests/test_config.py`, `tests/test_portals.py`, `tests/test_errors.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Settings` (pydantic-settings) with the fields listed in Step 5.
  - `get_settings() -> Settings` — cached accessor.
  - `Portal` frozen dataclass: `id: str`, `label: str`, `api_base: str`, `site_url: str`.
  - `PORTALS: dict[str, Portal]`, `get_portal(portal_id: str) -> Portal`, `all_portals() -> list[Portal]`.
  - `AuOpenDataError(Exception)` with subclasses `ResourceNotFound`, `ResourceNotQueryable`, `ResourceTooLarge`, `SqlRejected`, `UpstreamUnavailable`, `DownloadFailed`. Each has `.message: str` and `.remedy_url: str | None`.
  - `Sourced(BaseModel)` with `dataset_url: str`, `organisation: str`, `licence: str`.

- [ ] **Step 1: Install uv**

`uv` is not present on this machine. Install it, then confirm:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version
```

- [ ] **Step 2: Initialise the project and pin Python 3.12**

```bash
cd ~/projects/au-opendata-mcp
uv init --lib --name au-opendata-mcp --python 3.12 .
uv python install 3.12
echo "3.12" > .python-version
```

- [ ] **Step 3: Write `pyproject.toml`**

Replace the generated file with:

```toml
[project]
name = "au-opendata-mcp"
version = "0.1.0"
description = "MCP server for Australian government open data"
requires-python = ">=3.12,<3.13"
dependencies = [
    "fastapi>=0.141",
    "fastmcp>=4.0",
    "httpx>=0.28",
    "duckdb>=1.5",
    "sqlglot>=30.0",
    "pydantic>=2.9",
    "pydantic-settings>=2.15",
    "uvicorn>=0.34",
]

[project.scripts]
au-opendata-mcp = "au_opendata_mcp.main:cli"

[dependency-groups]
dev = ["pytest>=8.3", "pytest-asyncio>=0.24", "pytest-cov>=6.0", "respx>=0.23", "ruff>=0.8", "mypy>=1.13"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/au_opendata_mcp"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = ["live: hits real upstream APIs; deselected by default"]
addopts = "-m 'not live'"

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.mypy]
python_version = "3.12"
strict = true
files = ["src/au_opendata_mcp/services", "src/au_opendata_mcp/ckan"]
```

Then: `uv sync`

- [ ] **Step 4: Write the failing tests**

`tests/test_portals.py`:

```python
import pytest
from au_opendata_mcp.portals import PORTALS, all_portals, get_portal


def test_both_portals_registered():
    assert set(PORTALS) == {"data.gov.au", "data.sa.gov.au"}


def test_portal_api_bases_are_verified_values():
    assert get_portal("data.gov.au").api_base == "https://data.gov.au/data/api/3/action"
    assert get_portal("data.sa.gov.au").api_base == "https://data.sa.gov.au/data/api/3/action"


def test_unknown_portal_raises_with_the_valid_options_named():
    with pytest.raises(ValueError) as exc:
        get_portal("data.nsw.gov.au")
    assert "data.gov.au" in str(exc.value)


def test_all_portals_returns_every_registered_portal():
    assert len(all_portals()) == 2
```

`tests/test_config.py`:

```python
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


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("AU_OPENDATA_MAX_RESOURCE_BYTES", "1024")
    assert Settings().max_resource_bytes == 1024
```

`tests/test_errors.py`:

```python
import pytest
from au_opendata_mcp.errors import (
    AuOpenDataError, DownloadFailed, ResourceNotFound, ResourceNotQueryable,
    ResourceTooLarge, SqlRejected, UpstreamUnavailable,
)


@pytest.mark.parametrize("cls", [
    ResourceNotFound, ResourceNotQueryable, ResourceTooLarge,
    SqlRejected, UpstreamUnavailable, DownloadFailed,
])
def test_every_domain_error_shares_one_base(cls):
    assert issubclass(cls, AuOpenDataError)


def test_error_carries_a_remedy_url_so_the_caller_is_never_stuck():
    err = ResourceTooLarge("resource is 812 MB, over the 50 MB limit",
                           remedy_url="https://example.org/big.csv")
    assert err.remedy_url == "https://example.org/big.csv"
    assert "812 MB" in str(err)


def test_remedy_url_is_optional():
    assert UpstreamUnavailable("data.gov.au timed out").remedy_url is None
```

- [ ] **Step 5: Run the tests and verify they fail**

Run: `uv run pytest tests/ -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'au_opendata_mcp.portals'`

- [ ] **Step 6: Implement `errors.py`**

```python
class AuOpenDataError(Exception):
    """Base for every domain error. Carries a remedy so a caller is never stuck."""

    def __init__(self, message: str, *, remedy_url: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.remedy_url = remedy_url


class ResourceNotFound(AuOpenDataError): ...
class ResourceNotQueryable(AuOpenDataError): ...
class ResourceTooLarge(AuOpenDataError): ...
class SqlRejected(AuOpenDataError): ...
class UpstreamUnavailable(AuOpenDataError): ...
class DownloadFailed(AuOpenDataError): ...
```

- [ ] **Step 7: Implement `portals.py`**

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Portal:
    id: str
    label: str
    api_base: str
    site_url: str


PORTALS: dict[str, Portal] = {
    "data.gov.au": Portal(
        id="data.gov.au",
        label="data.gov.au (Australian Government)",
        api_base="https://data.gov.au/data/api/3/action",
        site_url="https://data.gov.au",
    ),
    "data.sa.gov.au": Portal(
        id="data.sa.gov.au",
        label="data.sa.gov.au (South Australian Government)",
        api_base="https://data.sa.gov.au/data/api/3/action",
        site_url="https://data.sa.gov.au",
    ),
}


def get_portal(portal_id: str) -> Portal:
    try:
        return PORTALS[portal_id]
    except KeyError:
        raise ValueError(
            f"unknown portal {portal_id!r}; valid portals are: {', '.join(sorted(PORTALS))}"
        ) from None


def all_portals() -> list[Portal]:
    return list(PORTALS.values())
```

- [ ] **Step 8: Implement `config.py`**

```python
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AU_OPENDATA_", env_file=".env")

    max_resource_bytes: int = 50 * 1024 * 1024
    upstream_timeout_seconds: float = 15.0
    head_timeout_seconds: float = 5.0
    download_timeout_seconds: float = 30.0
    resource_budget_seconds: float = 60.0

    metadata_ttl_seconds: int = 3600
    metadata_cache_max_entries: int = 5000
    materialisation_ttl_seconds: int = 86400
    cache_max_bytes: int = 1024 * 1024 * 1024
    cache_dir: Path = Path.home() / ".cache" / "au-opendata-mcp"

    duckdb_memory_limit: str = "1GB"
    max_search_limit: int = 50
    max_preview_limit: int = 20
    max_query_limit: int = 100
    upstream_retries: int = 2


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 9: Implement `models.py`**

```python
from pydantic import BaseModel


class Sourced(BaseModel):
    """Attribution is structural: there is no result model without these fields."""

    dataset_url: str
    organisation: str
    licence: str
```

- [ ] **Step 10: Run the tests and verify they pass**

Run: `uv run pytest tests/ -v`
Expected: PASS, all tests.

- [ ] **Step 11: Lint, typecheck, commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add -A
git commit -m "feat: project scaffold, settings, portal registry, error hierarchy"
```

---

### Task 2: CKAN client with recorded fixtures

**Files:**
- Create: `src/au_opendata_mcp/ckan/__init__.py`, `ckan/models.py`, `ckan/client.py`
- Create: `scripts/refresh_fixtures.py`
- Create: `tests/fixtures/ckan/` (populated by the script)
- Test: `tests/test_ckan_client.py`, `tests/test_live_contract.py`

**Interfaces:**
- Consumes: `Portal`, `Settings`, `UpstreamUnavailable`, `ResourceNotFound` from Task 1.
- Produces:
  - `CkanClient(portal: Portal, http: httpx.AsyncClient, settings: Settings)`.
  - `async CkanClient.action(name: str, params: dict[str, Any]) -> Any` — returns the CKAN `result` payload; raises `UpstreamUnavailable` on transport error, non-2xx, or `success: false`; raises `ResourceNotFound` on HTTP 404.
  - `strip_internal_columns(records: list[dict]) -> list[dict]` — removes `_full_text`, keeps `_id`.
  - `CkanResource`, `CkanDataset`, `CkanOrganization` pydantic models.

- [ ] **Step 1: Write `scripts/refresh_fixtures.py`**

This records real responses so fixtures cannot become fiction.

```python
"""Re-record CKAN fixtures from live endpoints. Run: uv run python scripts/refresh_fixtures.py"""
import json
import pathlib

import httpx

OUT = pathlib.Path(__file__).parent.parent / "tests" / "fixtures" / "ckan"
CALLS = [
    ("data.gov.au", "package_search", {"q": "bus stops", "rows": 5}),
    ("data.gov.au", "package_show", {"id": "national-public-toilet-map"}),
    ("data.gov.au", "organization_list", {"all_fields": True, "limit": 5}),
    ("data.sa.gov.au", "package_search", {"q": "bus stops", "rows": 5}),
    ("data.sa.gov.au", "organization_list", {"all_fields": True, "limit": 5}),
]
BASES = {
    "data.gov.au": "https://data.gov.au/data/api/3/action",
    "data.sa.gov.au": "https://data.sa.gov.au/data/api/3/action",
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for portal, action, params in CALLS:
        r = httpx.get(f"{BASES[portal]}/{action}", params=params, timeout=30.0)
        r.raise_for_status()
        path = OUT / f"{portal}__{action}.json"
        path.write_text(json.dumps(r.json(), indent=2))
        print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
```

Run it: `uv run python scripts/refresh_fixtures.py`

If `package_show` for `national-public-toilet-map` 404s, open `tests/fixtures/ckan/data.gov.au__package_search.json`, pick any `name` from `result.results`, and use that id instead.

- [ ] **Step 2: Write the failing tests**

`tests/test_ckan_client.py`:

```python
import json
import pathlib

import httpx
import pytest
import respx

from au_opendata_mcp.ckan.client import CkanClient, strip_internal_columns
from au_opendata_mcp.config import Settings
from au_opendata_mcp.errors import ResourceNotFound, UpstreamUnavailable
from au_opendata_mcp.portals import get_portal

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "ckan"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def client():
    async def _build():
        return CkanClient(get_portal("data.gov.au"), httpx.AsyncClient(), Settings())
    return _build


@respx.mock
async def test_action_returns_the_result_payload(client):
    payload = load("data.gov.au__package_search.json")
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(200, json=payload)
    )
    c = await client()
    result = await c.action("package_search", {"q": "bus stops"})
    assert "results" in result


@respx.mock
async def test_upstream_500_becomes_upstream_unavailable(client):
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(500, text="boom")
    )
    c = await client()
    with pytest.raises(UpstreamUnavailable) as exc:
        await c.action("package_search", {"q": "x"})
    assert "data.gov.au" in str(exc.value)


@respx.mock
async def test_404_becomes_resource_not_found(client):
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(404, json={"success": False})
    )
    c = await client()
    with pytest.raises(ResourceNotFound):
        await c.action("package_show", {"id": "nope"})


@respx.mock
async def test_ckan_success_false_becomes_upstream_unavailable(client):
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(200, json={"success": False, "error": {"message": "bad"}})
    )
    c = await client()
    with pytest.raises(UpstreamUnavailable):
        await c.action("package_search", {"q": "x"})


@respx.mock
async def test_transport_error_is_retried_then_surfaces_as_upstream_unavailable(client):
    route = respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search")
    route.side_effect = httpx.ConnectTimeout("timed out")
    c = await client()
    with pytest.raises(UpstreamUnavailable):
        await c.action("package_search", {"q": "x"})
    assert route.call_count == Settings().upstream_retries + 1


def test_full_text_is_stripped_because_it_is_tsvector_noise():
    records = [{"_id": 1, "_full_text": "'-23.15':36 '-23.16':34", "suburb": "Glenelg"}]
    assert strip_internal_columns(records) == [{"_id": 1, "suburb": "Glenelg"}]


def test_stripping_tolerates_records_without_full_text():
    assert strip_internal_columns([{"_id": 1, "a": 2}]) == [{"_id": 1, "a": 2}]
```

`tests/test_live_contract.py` — the drift lane (§9.2):

```python
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
        params={"q": "csv", "rows": 5}, timeout=30.0,
    )
    resources = [res for d in r.json()["result"]["results"] for res in d.get("resources", [])]
    assert any("datastore_active" in res for res in resources)
```

- [ ] **Step 3: Run tests and verify they fail**

Run: `uv run pytest tests/test_ckan_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'au_opendata_mcp.ckan'`

- [ ] **Step 4: Implement `ckan/models.py`**

```python
from pydantic import BaseModel, Field


class CkanResource(BaseModel):
    id: str
    name: str | None = None
    format: str | None = None
    url: str | None = None
    size: int | None = None
    datastore_active: bool = False
    last_modified: str | None = None

    model_config = {"extra": "ignore"}


class CkanOrganization(BaseModel):
    name: str
    title: str | None = None
    package_count: int | None = None

    model_config = {"extra": "ignore"}


class CkanDataset(BaseModel):
    id: str
    name: str
    title: str | None = None
    notes: str | None = None
    license_title: str | None = None
    license_id: str | None = None
    metadata_modified: str | None = None
    organization: CkanOrganization | None = None
    resources: list[CkanResource] = Field(default_factory=list)

    model_config = {"extra": "ignore"}
```

- [ ] **Step 5: Implement `ckan/client.py`**

```python
import asyncio
import logging
from typing import Any

import httpx

from au_opendata_mcp.config import Settings
from au_opendata_mcp.errors import ResourceNotFound, UpstreamUnavailable
from au_opendata_mcp.portals import Portal

log = logging.getLogger(__name__)

INTERNAL_COLUMNS = {"_full_text"}


def strip_internal_columns(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop CKAN's tsvector column. Left in, it dominates every response for zero information."""
    return [{k: v for k, v in r.items() if k not in INTERNAL_COLUMNS} for r in records]


class CkanClient:
    def __init__(self, portal: Portal, http: httpx.AsyncClient, settings: Settings) -> None:
        self.portal = portal
        self.http = http
        self.settings = settings

    async def action(self, name: str, params: dict[str, Any]) -> Any:
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
            return body["result"]

        raise UpstreamUnavailable(
            f"{self.portal.id} unreachable for {name}: {last_exc}",
            remedy_url=self.portal.site_url,
        )
```

Also create an empty `src/au_opendata_mcp/ckan/__init__.py`.

- [ ] **Step 6: Run tests and verify they pass**

Run: `uv run pytest tests/test_ckan_client.py -v`
Expected: PASS.

- [ ] **Step 7: Confirm live tests are deselected by default, then run them explicitly**

Run: `uv run pytest tests/ -v` → the three live tests show as deselected.
Run: `uv run pytest tests/test_live_contract.py -m live -v` → PASS (requires network).

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat: CKAN client with recorded fixtures and live contract tests"
```

---

### Task 3: Catalog service

**Files:**
- Create: `src/au_opendata_mcp/services/__init__.py`, `services/catalog.py`
- Create: `src/au_opendata_mcp/deps.py`
- Test: `tests/test_catalog.py`

**Note on `deps.py`:** the spec's file table does not list it. It is added here because `mcp_server.py` and `api.py` both need the same service instances, and putting the factory in `main.py` would make `main` ↔ `mcp_server` a circular import. It holds nothing but construction.

**Scope of `Sourced`:** it applies to results that surface **dataset content or dataset metadata** — `DatasetSummary`, `DatasetDetail`, and later `PreviewResult` and `QueryResult`. It does **not** apply to `PortalInfo` or `OrganizationSummary` (a list of publisher names is not licensed data) nor to the AU utility tools (computed, not retrieved). CC BY attaches to the data.

**Interfaces:**
- Consumes: `CkanClient`, `CkanDataset`, `CkanResource` (Task 2); `Settings`, `Portal`, `get_portal`, `all_portals`, `Sourced`, `ResourceNotFound` (Task 1).
- Produces:
  - `PortalInfo(BaseModel)`: `id`, `label`, `site_url`, `dataset_count: int | None`.
  - `ResourceInfo(BaseModel)`: `resource_id`, `name: str | None`, `format: str | None`, `size_bytes: int | None`, `url: str | None`, `datastore_active: bool`, `queryable: Literal["yes","no","unknown"]`, `queryable_reason: str | None`.
  - `DatasetSummary(Sourced)`: `dataset_id`, `title`, `portal`, `formats: list[str]`, `modified: str | None`.
  - `DatasetDetail(Sourced)`: `dataset_id`, `title`, `description: str | None`, `portal`, `modified: str | None`, `resources: list[ResourceInfo]`.
  - `OrganizationSummary(BaseModel)`: `name`, `title: str | None`, `dataset_count: int | None`, `portal`.
  - `TABULAR_FORMATS: frozenset[str]` = `{"CSV", "TSV", "XLSX", "XLS"}`.
  - `CatalogService(clients: Mapping[str, CkanClient], settings: Settings)` with `async list_portals()`, `async search_datasets(q, portal="data.gov.au", organisation=None, format=None, limit=20)`, `async get_dataset(dataset_id, portal="data.gov.au")`, `async list_organizations(portal="data.gov.au", q=None)`. `search_datasets` and `list_organizations` both accept `portal="all"` and fan out concurrently.
  - `assess_queryable(resource: CkanResource, settings: Settings) -> tuple[str, str | None]`.
  - `Services` dataclass and `get_services()` / `close_services()` in `deps.py`.

- [ ] **Step 1: Write the failing tests**

`tests/test_catalog.py`:

```python
import httpx
import pytest
import respx

from au_opendata_mcp.ckan.client import CkanClient
from au_opendata_mcp.ckan.models import CkanResource
from au_opendata_mcp.config import Settings
from au_opendata_mcp.errors import ResourceNotFound
from au_opendata_mcp.portals import get_portal
from au_opendata_mcp.services.catalog import CatalogService, assess_queryable

SETTINGS = Settings()


def build_service() -> CatalogService:
    http = httpx.AsyncClient()
    clients = {p: CkanClient(get_portal(p), http, SETTINGS)
               for p in ("data.gov.au", "data.sa.gov.au")}
    return CatalogService(clients, SETTINGS)


def package_search_payload(*datasets: dict) -> dict:
    return {"success": True, "result": {"count": len(datasets), "results": list(datasets)}}


DATASET = {
    "id": "abc-123",
    "name": "sa-bus-stops",
    "title": "SA Bus Stops",
    "notes": "Every bus stop in South Australia.",
    "license_title": "Creative Commons Attribution 4.0",
    "metadata_modified": "2026-08-01T00:00:00",
    "organization": {"name": "dpti", "title": "Dept for Infrastructure and Transport"},
    "resources": [
        {"id": "r1", "name": "stops.csv", "format": "CSV", "url": "https://x.org/stops.csv",
         "size": 2048, "datastore_active": True, "last_modified": "2026-08-01T00:00:00"},
        {"id": "r2", "name": "guide.pdf", "format": "PDF", "url": "https://x.org/guide.pdf",
         "size": 100, "datastore_active": False},
    ],
}


# --- assess_queryable: the flag that stops the model burning a call on an error ---

def test_tabular_resource_under_the_ceiling_is_queryable():
    r = CkanResource(id="r", format="CSV", size=1024, url="https://x/a.csv")
    assert assess_queryable(r, SETTINGS) == ("yes", None)


def test_non_tabular_format_is_not_queryable_and_says_why():
    r = CkanResource(id="r", format="PDF", size=1024, url="https://x/a.pdf")
    verdict, reason = assess_queryable(r, SETTINGS)
    assert verdict == "no"
    assert "PDF" in reason and "not tabular" in reason


def test_oversized_resource_is_not_queryable_and_states_both_numbers():
    r = CkanResource(id="r", format="CSV", size=812 * 1024 * 1024, url="https://x/a.csv")
    verdict, reason = assess_queryable(r, SETTINGS)
    assert verdict == "no"
    assert "812 MB" in reason and "50 MB" in reason


def test_missing_size_is_unknown_not_a_silent_pass():
    r = CkanResource(id="r", format="CSV", size=None, url="https://x/a.csv")
    verdict, reason = assess_queryable(r, SETTINGS)
    assert verdict == "unknown"
    assert "size" in reason.lower()


def test_missing_format_is_not_queryable():
    r = CkanResource(id="r", format=None, size=10, url="https://x/a")
    assert assess_queryable(r, SETTINGS)[0] == "no"


# --- search_datasets ---

@respx.mock
async def test_search_returns_sourced_summaries():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(200, json=package_search_payload(DATASET))
    )
    results = await build_service().search_datasets("bus stops")
    assert len(results) == 1
    hit = results[0]
    assert hit.dataset_id == "sa-bus-stops"
    assert hit.title == "SA Bus Stops"
    assert hit.organisation == "Dept for Infrastructure and Transport"
    assert hit.licence == "Creative Commons Attribution 4.0"
    assert hit.dataset_url == "https://data.gov.au/dataset/sa-bus-stops"
    assert sorted(hit.formats) == ["CSV", "PDF"]


@respx.mock
async def test_search_limit_is_clamped_to_the_configured_maximum():
    route = respx.get(
        url__startswith="https://data.gov.au/data/api/3/action/package_search"
    ).mock(return_value=httpx.Response(200, json=package_search_payload()))
    await build_service().search_datasets("x", limit=5000)
    assert route.calls[0].request.url.params["rows"] == str(SETTINGS.max_search_limit)


@respx.mock
async def test_search_across_all_portals_merges_and_tags_each_result():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(200, json=package_search_payload(DATASET))
    )
    respx.get(url__startswith="https://data.sa.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(200, json=package_search_payload({**DATASET, "name": "sa-only"}))
    )
    results = await build_service().search_datasets("bus", portal="all")
    assert {r.portal for r in results} == {"data.gov.au", "data.sa.gov.au"}


@respx.mock
async def test_organisation_and_format_become_ckan_filter_queries():
    route = respx.get(
        url__startswith="https://data.gov.au/data/api/3/action/package_search"
    ).mock(return_value=httpx.Response(200, json=package_search_payload()))
    await build_service().search_datasets("x", organisation="dpti", format="CSV")
    fq = route.calls[0].request.url.params["fq"]
    assert 'organization:"dpti"' in fq
    assert 'res_format:"CSV"' in fq


# --- get_dataset ---

@respx.mock
async def test_get_dataset_marks_each_resource_queryable_or_not():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json={"success": True, "result": DATASET})
    )
    detail = await build_service().get_dataset("sa-bus-stops")
    by_id = {r.resource_id: r for r in detail.resources}
    assert by_id["r1"].queryable == "yes"
    assert by_id["r2"].queryable == "no"
    assert "PDF" in by_id["r2"].queryable_reason
    assert detail.licence == "Creative Commons Attribution 4.0"


@respx.mock
async def test_missing_dataset_raises_resource_not_found():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(404, json={"success": False})
    )
    with pytest.raises(ResourceNotFound):
        await build_service().get_dataset("nope")


# --- list_organizations and list_portals ---

@respx.mock
async def test_list_organizations_returns_names_and_counts():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/organization_list").mock(
        return_value=httpx.Response(200, json={"success": True, "result": [
            {"name": "abs", "title": "Australian Bureau of Statistics", "package_count": 412},
        ]})
    )
    orgs = await build_service().list_organizations()
    assert orgs[0].name == "abs"
    assert orgs[0].dataset_count == 412


@respx.mock
async def test_list_organizations_filters_by_substring_case_insensitively():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/organization_list").mock(
        return_value=httpx.Response(200, json={"success": True, "result": [
            {"name": "abs", "title": "Australian Bureau of Statistics", "package_count": 4},
            {"name": "aemo", "title": "Energy Market Operator", "package_count": 9},
        ]})
    )
    orgs = await build_service().list_organizations(q="bureau")
    assert [o.name for o in orgs] == ["abs"]


@respx.mock
async def test_list_organizations_across_all_portals_tags_each_by_portal():
    for host in ("data.gov.au", "data.sa.gov.au"):
        respx.get(url__startswith=f"https://{host}/data/api/3/action/organization_list").mock(
            return_value=httpx.Response(200, json={"success": True, "result": [
                {"name": f"org-{host}", "title": "Org", "package_count": 1},
            ]})
        )
    orgs = await build_service().list_organizations(portal="all")
    assert {o.portal for o in orgs} == {"data.gov.au", "data.sa.gov.au"}


@respx.mock
async def test_list_portals_reports_a_dataset_count_per_portal():
    for host in ("data.gov.au", "data.sa.gov.au"):
        respx.get(url__startswith=f"https://{host}/data/api/3/action/package_search").mock(
            return_value=httpx.Response(200, json={"success": True,
                                                   "result": {"count": 70123, "results": []}})
        )
    portals = await build_service().list_portals()
    assert {p.id for p in portals} == {"data.gov.au", "data.sa.gov.au"}
    assert all(p.dataset_count == 70123 for p in portals)


@respx.mock
async def test_list_portals_degrades_to_none_when_a_portal_is_down():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(500)
    )
    respx.get(url__startswith="https://data.sa.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(200, json={"success": True,
                                               "result": {"count": 1200, "results": []}})
    )
    counts = {p.id: p.dataset_count for p in await build_service().list_portals()}
    assert counts["data.gov.au"] is None
    assert counts["data.sa.gov.au"] == 1200
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest tests/test_catalog.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'au_opendata_mcp.services'`

- [ ] **Step 3: Implement `services/catalog.py`**

```python
import asyncio
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel

from au_opendata_mcp.ckan.client import CkanClient
from au_opendata_mcp.ckan.models import CkanDataset, CkanResource
from au_opendata_mcp.config import Settings
from au_opendata_mcp.models import Sourced
from au_opendata_mcp.portals import Portal, all_portals, get_portal

TABULAR_FORMATS = frozenset({"CSV", "TSV", "XLSX", "XLS"})
Queryable = Literal["yes", "no", "unknown"]


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
    by consuming an error (spec section 5.1)."""
    fmt = (resource.format or "").strip().upper()
    if fmt not in TABULAR_FORMATS:
        shown = resource.format or "unknown"
        return "no", f"format {shown!r} is not tabular"
    if resource.size and resource.size > settings.max_resource_bytes:
        return "no", (
            f"resource is {_mb(resource.size)}, over the "
            f"{_mb(settings.max_resource_bytes)} limit"
        )
    if not resource.size:
        return "unknown", "size not declared by the portal; enforced during download instead"
    return "yes", None


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
            per_portal = await asyncio.gather(
                *(
                    self.search_datasets(q, p.id, organisation, format, limit)
                    for p in all_portals()
                ),
                return_exceptions=True,
            )
            merged: list[DatasetSummary] = []
            for item in per_portal:
                if isinstance(item, list):
                    merged.extend(item)
            return merged

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
            per_portal = await asyncio.gather(
                *(self.list_organizations(p.id, q) for p in all_portals()),
                return_exceptions=True,
            )
            merged: list[OrganizationSummary] = []
            for item in per_portal:
                if isinstance(item, list):
                    merged.extend(item)
            return merged

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
```

Create an empty `src/au_opendata_mcp/services/__init__.py`.

- [ ] **Step 4: Implement `deps.py`**

```python
from dataclasses import dataclass
from functools import lru_cache

import httpx

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
    clients = {p.id: CkanClient(p, http, settings) for p in all_portals()}
    return Services(settings=settings, http=http, catalog=CatalogService(clients, settings))


async def close_services() -> None:
    services = get_services()
    await services.http.aclose()
    get_services.cache_clear()
```

- [ ] **Step 5: Run tests and verify they pass**

Run: `uv run pytest tests/test_catalog.py -v`
Expected: PASS, all 14 tests.

- [ ] **Step 6: Lint, typecheck, commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add -A
git commit -m "feat: catalog service with up-front queryable assessment"
```

---

### Task 4: HEAD size probe for resources with no declared size

**Files:**
- Modify: `src/au_opendata_mcp/services/catalog.py` (add `_probe_sizes`, call it from `get_dataset`)
- Test: `tests/test_catalog_size_probe.py`

Many data.gov.au resources declare no size. Task 3 reports those as `"unknown"`. This task resolves them with a HEAD request under a dedicated 5-second budget (§5.1) — deliberately tighter than the 15-second general upstream timeout, because `get_dataset` must stay responsive and a size probe is not worth waiting on.

**Interfaces:**
- Consumes: `CatalogService`, `ResourceInfo`, `assess_queryable` (Task 3); `Settings.head_timeout_seconds` (Task 1).
- Produces: `async CatalogService._probe_sizes(resources: list[ResourceInfo]) -> None` — mutates `queryable` / `queryable_reason` / `size_bytes` in place for entries currently `"unknown"`. `get_dataset` now awaits it before returning.

- [ ] **Step 1: Write the failing tests**

`tests/test_catalog_size_probe.py`:

```python
import httpx
import respx

from au_opendata_mcp.ckan.client import CkanClient
from au_opendata_mcp.config import Settings
from au_opendata_mcp.portals import get_portal
from au_opendata_mcp.services.catalog import CatalogService

SETTINGS = Settings()


def build_service() -> CatalogService:
    http = httpx.AsyncClient()
    clients = {p: CkanClient(get_portal(p), http, SETTINGS)
               for p in ("data.gov.au", "data.sa.gov.au")}
    return CatalogService(clients, SETTINGS)


def dataset_with_sizeless_csv(url: str = "https://x.org/stops.csv") -> dict:
    return {
        "success": True,
        "result": {
            "id": "abc", "name": "d", "title": "D",
            "license_title": "CC BY 4.0",
            "organization": {"name": "o", "title": "O"},
            "resources": [
                {"id": "r1", "name": "stops.csv", "format": "CSV", "url": url,
                 "datastore_active": False},
            ],
        },
    }


@respx.mock
async def test_head_resolves_an_unknown_size_into_queryable_yes():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=dataset_with_sizeless_csv())
    )
    respx.head("https://x.org/stops.csv").mock(
        return_value=httpx.Response(200, headers={"content-length": "4096"})
    )
    resource = (await build_service().get_dataset("d")).resources[0]
    assert resource.queryable == "yes"
    assert resource.size_bytes == 4096


@respx.mock
async def test_head_revealing_an_oversized_resource_marks_it_not_queryable():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=dataset_with_sizeless_csv())
    )
    respx.head("https://x.org/stops.csv").mock(
        return_value=httpx.Response(200, headers={"content-length": str(812 * 1024 * 1024)})
    )
    resource = (await build_service().get_dataset("d")).resources[0]
    assert resource.queryable == "no"
    assert "812 MB" in resource.queryable_reason


@respx.mock
async def test_head_timeout_stays_unknown_and_never_blocks():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=dataset_with_sizeless_csv())
    )
    respx.head("https://x.org/stops.csv").mock(side_effect=httpx.ConnectTimeout("slow"))
    resource = (await build_service().get_dataset("d")).resources[0]
    assert resource.queryable == "unknown"
    assert "could not be probed" in resource.queryable_reason


@respx.mock
async def test_head_without_content_length_stays_unknown():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=dataset_with_sizeless_csv())
    )
    respx.head("https://x.org/stops.csv").mock(return_value=httpx.Response(200))
    resource = (await build_service().get_dataset("d")).resources[0]
    assert resource.queryable == "unknown"


@respx.mock
async def test_resources_already_decided_are_not_probed_at_all():
    payload = dataset_with_sizeless_csv()
    payload["result"]["resources"][0]["size"] = 2048
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=payload)
    )
    head = respx.head("https://x.org/stops.csv")
    resource = (await build_service().get_dataset("d")).resources[0]
    assert resource.queryable == "yes"
    assert head.call_count == 0
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest tests/test_catalog_size_probe.py -v`
Expected: FAIL — the first test fails on `queryable == "unknown"` (no probe performed yet), and `test_head_timeout_stays_unknown_and_never_blocks` fails on the missing "could not be probed" reason.

- [ ] **Step 3: Add the probe to `services/catalog.py`**

Add to the imports at the top of the file:

```python
import httpx
```

Add these two methods to `CatalogService`:

```python
    async def _probe_size(self, resource: ResourceInfo) -> None:
        """Resolve one undeclared size via HEAD, under a dedicated short budget."""
        if resource.queryable != "unknown" or not resource.url:
            return
        client = next(iter(self.clients.values())).http
        try:
            resp = await client.head(
                resource.url,
                timeout=self.settings.head_timeout_seconds,
                follow_redirects=True,
            )
            length = resp.headers.get("content-length")
        except httpx.HTTPError:
            resource.queryable_reason = "size could not be probed; enforced during download instead"
            return

        if not length or not length.isdigit():
            resource.queryable_reason = (
                "size not declared and not reported by the server; "
                "enforced during download instead"
            )
            return

        resource.size_bytes = int(length)
        if resource.size_bytes > self.settings.max_resource_bytes:
            resource.queryable = "no"
            resource.queryable_reason = (
                f"resource is {_mb(resource.size_bytes)}, over the "
                f"{_mb(self.settings.max_resource_bytes)} limit"
            )
        else:
            resource.queryable = "yes"
            resource.queryable_reason = None

    async def _probe_sizes(self, resources: list[ResourceInfo]) -> None:
        pending = [r for r in resources if r.queryable == "unknown" and r.url]
        if pending:
            await asyncio.gather(*(self._probe_size(r) for r in pending))
```

In `get_dataset`, insert the probe immediately before building `DatasetDetail`:

```python
        await self._probe_sizes(resources)
        return DatasetDetail(
```

- [ ] **Step 4: Run tests and verify they pass**

Run: `uv run pytest tests/test_catalog_size_probe.py tests/test_catalog.py -v`
Expected: PASS. Note that `test_missing_size_is_unknown_not_a_silent_pass` in Task 3 still passes — it tests `assess_queryable` directly, which is unchanged.

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add -A
git commit -m "feat: resolve undeclared resource sizes via HEAD under a 5s budget"
```

---

### Task 5: MCP and REST adapters, app assembly, and the lifespan regression guard

**Files:**
- Create: `src/au_opendata_mcp/mcp_server.py`, `api.py`, `main.py`
- Test: `tests/test_api.py`, `tests/test_app_assembly.py`

This is the task that makes "MCP and REST are two interfaces over one service layer" true in code. It also installs the guard for the failure the spec calls out in §4.4 — verified real: **without the FastMCP lifespan, `/health` and `/api` still return 200 while `/mcp/` raises `RuntimeError: Task group is not initialized`.** The failure is invisible to every check except an actual MCP call, which is exactly why it needs a test.

**Interfaces:**
- Consumes: `CatalogService` and its models (Task 3); `get_services`, `close_services` (Task 3); the error hierarchy (Task 1).
- Produces:
  - `mcp: FastMCP` in `mcp_server.py`, with tools `list_portals`, `search_datasets`, `get_dataset`, `list_organizations`.
  - `router: APIRouter` in `api.py` serving `GET /api/portals`, `/api/datasets`, `/api/datasets/{dataset_id}`, `/api/organizations`.
  - `create_app() -> FastAPI` and `cli() -> None` in `main.py`.
  - `STATUS_FOR: dict[type[AuOpenDataError], int]` in `api.py`, implementing the §4.2 table.

- [ ] **Step 1: Write the failing tests**

`tests/test_app_assembly.py` — the regression guard:

```python
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from au_opendata_mcp.main import create_app
from au_opendata_mcp.mcp_server import mcp

INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "test", "version": "1"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def test_health_and_mcp_are_served_by_the_same_app():
    with TestClient(create_app()) as c:
        assert c.get("/health").json() == {"status": "ok"}
        r = c.post("/mcp/", json=INITIALIZE, headers=MCP_HEADERS)
        assert r.status_code == 200
        assert "serverInfo" in r.text


async def test_mcp_advertises_the_catalog_tools():
    """fastmcp.Client speaks MCP to the server in-process — no transport, no sockets."""
    from fastmcp import Client

    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert {"list_portals", "search_datasets", "get_dataset", "list_organizations"} <= names


async def test_every_tool_carries_a_description():
    """Tool descriptions are the prompt (spec 5.3). An undescribed tool is an unusable one."""
    from fastmcp import Client

    async with Client(mcp) as client:
        for tool in await client.list_tools():
            assert tool.description, f"tool {tool.name} has no description"


def test_omitting_the_fastmcp_lifespan_silently_breaks_only_mcp():
    """Regression guard for spec section 4.4. REST keeps working, which is the trap."""
    mcp_app = mcp.http_app(path="/")
    broken = FastAPI()  # deliberately no lifespan=mcp_app.lifespan

    @broken.get("/health")
    def health():
        return {"status": "ok"}

    broken.mount("/mcp", mcp_app)

    with TestClient(broken) as c:
        assert c.get("/health").status_code == 200   # REST is fine — the trap
        with pytest.raises(RuntimeError, match="task group|Task group"):
            c.post("/mcp/", json=INITIALIZE, headers=MCP_HEADERS)
```

`tests/test_api.py`:

```python
import httpx
import respx
from fastapi.testclient import TestClient

from au_opendata_mcp.main import create_app

DATASET = {
    "id": "abc", "name": "sa-bus-stops", "title": "SA Bus Stops",
    "notes": "Stops.", "license_title": "CC BY 4.0",
    "metadata_modified": "2026-08-01T00:00:00",
    "organization": {"name": "dpti", "title": "DIT"},
    "resources": [{"id": "r1", "name": "s.csv", "format": "CSV",
                   "url": "https://x.org/s.csv", "size": 2048, "datastore_active": True}],
}


@respx.mock
def test_search_endpoint_returns_sourced_rows():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(200, json={"success": True,
                                               "result": {"count": 1, "results": [DATASET]}})
    )
    with TestClient(create_app()) as c:
        body = c.get("/api/datasets", params={"q": "bus stops"}).json()
    assert body[0]["dataset_id"] == "sa-bus-stops"
    assert body[0]["licence"] == "CC BY 4.0"


@respx.mock
def test_missing_dataset_maps_to_404_not_a_500():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(404, json={"success": False})
    )
    with TestClient(create_app()) as c:
        r = c.get("/api/datasets/nope")
    assert r.status_code == 404
    assert "detail" in r.json()


@respx.mock
def test_upstream_failure_maps_to_502_and_names_the_portal():
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_search").mock(
        return_value=httpx.Response(500)
    )
    with TestClient(create_app()) as c:
        r = c.get("/api/datasets", params={"q": "x"})
    assert r.status_code == 502
    assert "data.gov.au" in r.json()["detail"]


def test_unknown_portal_is_a_400_with_the_valid_options():
    with TestClient(create_app()) as c:
        r = c.get("/api/datasets", params={"q": "x", "portal": "data.nsw.gov.au"})
    assert r.status_code == 400
    assert "data.gov.au" in r.json()["detail"]
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest tests/test_app_assembly.py tests/test_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'au_opendata_mcp.main'`

- [ ] **Step 3: Implement `mcp_server.py`**

Tool docstrings are the prompt the model reads (§5.3). Write them as carefully as the code.

```python
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
async def list_organizations(portal: str = "data.gov.au", q: str | None = None) -> list[dict[str, Any]]:
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
```

- [ ] **Step 4: Implement `api.py`**

```python
from typing import Any

from fastapi import APIRouter, Query

from au_opendata_mcp.deps import get_services
from au_opendata_mcp.errors import (
    AuOpenDataError, DownloadFailed, ResourceNotFound, ResourceNotQueryable,
    ResourceTooLarge, SqlRejected, UpstreamUnavailable,
)

STATUS_FOR: dict[type[AuOpenDataError], int] = {
    ResourceNotFound: 404,
    ResourceNotQueryable: 422,
    ResourceTooLarge: 413,
    SqlRejected: 400,
    UpstreamUnavailable: 502,
    DownloadFailed: 502,
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
```

- [ ] **Step 5: Implement `main.py`**

```python
import argparse
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from au_opendata_mcp.api import STATUS_FOR, router
from au_opendata_mcp.deps import close_services
from au_opendata_mcp.errors import AuOpenDataError
from au_opendata_mcp.mcp_server import mcp

log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    # FastMCP's HTTP app owns a session-manager lifespan. If it is not propagated to the
    # parent app, REST keeps working and every /mcp request fails with
    # "Task group is not initialized". See tests/test_app_assembly.py.
    mcp_app = mcp.http_app(path="/")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with mcp_app.lifespan(app):
            yield
        await close_services()

    app = FastAPI(
        title="au-opendata-mcp",
        description="Australian government open data over MCP and REST.",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.exception_handler(AuOpenDataError)
    async def domain_error(_: Request, exc: AuOpenDataError) -> JSONResponse:
        status = STATUS_FOR.get(type(exc), 500)
        return JSONResponse(
            status_code=status,
            content={"detail": exc.message, "remedy_url": exc.remedy_url},
        )

    @app.exception_handler(ValueError)
    async def bad_argument(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    app.include_router(router)
    app.mount("/mcp", mcp_app)
    return app


def cli() -> None:
    parser = argparse.ArgumentParser(prog="au-opendata-mcp")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        import uvicorn

        uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    cli()
```

- [ ] **Step 6: Run tests and verify they pass**

Run: `uv run pytest tests/test_app_assembly.py tests/test_api.py -v`
Expected: PASS.

If `test_omitting_the_fastmcp_lifespan_silently_breaks_only_mcp` does not raise, the FastMCP version has changed its failure mode. Do not delete the test — investigate and update the expected exception, because the whole point is that this failure is silent.

- [ ] **Step 7: Verify it actually runs, both transports**

```bash
uv run au-opendata-mcp --transport http --port 8000 &
sleep 2
curl -s localhost:8000/health
curl -s "localhost:8000/api/datasets?q=bus+stops&limit=2" | head -c 400
kill %1
```

Expected: `{"status":"ok"}` then real dataset JSON from data.gov.au.

- [ ] **Step 8: Wire it into Claude Desktop and confirm end to end**

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "au-opendata": {
      "command": "uv",
      "args": ["--directory", "/Users/lei800123/projects/au-opendata-mcp", "run", "au-opendata-mcp"]
    }
  }
}
```

Restart Claude Desktop and ask: *"What bus stop data does South Australia have?"* Confirm it calls `search_datasets` and returns real results with licences. This is the Week 1 milestone.

- [ ] **Step 9: Commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add -A
git commit -m "feat: MCP and REST adapters with lifespan regression guard"
```

---

# Week 2 — The query layer

### Task 6: SSRF guard

**Files:**
- Create: `src/au_opendata_mcp/safety.py`
- Test: `tests/test_safety_urls.py`

Implements §6.4. The server downloads URLs that come from third-party metadata, so this is the boundary between "fetches open data" and "fetches your cloud metadata endpoint on request".

**Interfaces:**
- Consumes: `DownloadFailed` (Task 1).
- Produces:
  - `SAFE_SCHEMES: frozenset[str]` = `{"http", "https"}`; `MAX_REDIRECTS: int` = 5.
  - `assert_safe_url(url: str, *, resolve: Callable[[str], list[str]] = _resolve_host) -> None` — raises `DownloadFailed`. The `resolve` seam exists so tests never touch DNS.
  - `assert_url_in_catalog(url: str, catalog_urls: Collection[str]) -> None` — raises `DownloadFailed`.

- [ ] **Step 1: Write the failing tests**

`tests/test_safety_urls.py`:

```python
import pytest

from au_opendata_mcp.errors import DownloadFailed
from au_opendata_mcp.safety import assert_safe_url, assert_url_in_catalog


def fake_resolver(mapping: dict[str, list[str]]):
    def resolve(host: str) -> list[str]:
        if host not in mapping:
            raise OSError(f"no such host {host}")
        return mapping[host]
    return resolve


PUBLIC = fake_resolver({"data.gov.au": ["104.18.1.1"], "x.org": ["93.184.216.34"]})


def test_a_public_https_url_is_allowed():
    assert_safe_url("https://data.gov.au/data.csv", resolve=PUBLIC) is None


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://x.org/a.csv",
    "gopher://x.org/a",
    "jar:http://x.org/a!/b",
])
def test_non_http_schemes_are_refused(url):
    with pytest.raises(DownloadFailed, match="http and https"):
        assert_safe_url(url, resolve=PUBLIC)


@pytest.mark.parametrize("host,ip,label", [
    ("localhost", "127.0.0.1", "loopback"),
    ("metadata", "169.254.169.254", "cloud metadata service"),
    ("internal", "10.0.0.1", "private class A"),
    ("internal", "192.168.1.1", "private class C"),
    ("internal", "172.16.0.1", "private class B"),
    ("six", "::1", "IPv6 loopback"),
    ("sixlocal", "fd00::1", "IPv6 unique local"),
])
def test_hosts_resolving_to_non_public_addresses_are_refused(host, ip, label):
    with pytest.raises(DownloadFailed, match="non-public"):
        assert_safe_url(f"http://{host}/a.csv", resolve=fake_resolver({host: [ip]}))


def test_a_host_with_one_public_and_one_private_address_is_refused():
    """DNS rebinding defence: every resolved address must be public, not just the first."""
    resolve = fake_resolver({"sneaky.org": ["93.184.216.34", "127.0.0.1"]})
    with pytest.raises(DownloadFailed, match="non-public"):
        assert_safe_url("http://sneaky.org/a.csv", resolve=resolve)


def test_unresolvable_host_is_refused_not_attempted():
    with pytest.raises(DownloadFailed, match="could not resolve"):
        assert_safe_url("http://nope.invalid/a.csv", resolve=PUBLIC)


def test_url_with_no_host_is_refused():
    with pytest.raises(DownloadFailed, match="no host"):
        assert_safe_url("http:///a.csv", resolve=PUBLIC)


def test_catalog_membership_is_required():
    catalog = {"https://x.org/a.csv"}
    assert assert_url_in_catalog("https://x.org/a.csv", catalog) is None
    with pytest.raises(DownloadFailed, match="not listed"):
        assert_url_in_catalog("https://x.org/evil.csv", catalog)
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest tests/test_safety_urls.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'au_opendata_mcp.safety'`

- [ ] **Step 3: Implement the URL half of `safety.py`**

```python
import ipaddress
import socket
from collections.abc import Callable, Collection
from urllib.parse import urlparse

from au_opendata_mcp.errors import DownloadFailed

SAFE_SCHEMES = frozenset({"http", "https"})
MAX_REDIRECTS = 5


def _resolve_host(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def assert_safe_url(
    url: str, *, resolve: Callable[[str], list[str]] = _resolve_host
) -> None:
    """Refuse anything that is not a public http(s) address.

    Every resolved address is checked, not just the first, so a host that answers with
    one public and one private address cannot slip through.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in SAFE_SCHEMES:
        raise DownloadFailed(
            f"refusing {scheme or 'scheme-less'} URL; only http and https are allowed"
        )

    host = parsed.hostname
    if not host:
        raise DownloadFailed(f"URL has no host: {url!r}")

    try:
        addresses = resolve(host)
    except OSError as exc:
        raise DownloadFailed(f"could not resolve host {host!r}: {exc}") from exc

    if not addresses:
        raise DownloadFailed(f"host {host!r} resolved to no addresses")

    for raw in addresses:
        ip = ipaddress.ip_address(raw)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise DownloadFailed(
                f"refusing to fetch {host!r}: it resolves to the non-public address {ip}"
            )


def assert_url_in_catalog(url: str, catalog_urls: Collection[str]) -> None:
    """Only URLs the portal itself published for this resource may be fetched."""
    if url not in set(catalog_urls):
        raise DownloadFailed(
            "refusing to fetch a URL that is not listed in the dataset's catalog metadata"
        )
```

- [ ] **Step 4: Run tests and verify they pass**

Run: `uv run pytest tests/test_safety_urls.py -v`
Expected: PASS, 16 tests.

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add -A
git commit -m "feat: SSRF guard rejecting non-public and unlisted resource URLs"
```

---

### Task 7: SQL validator

**Files:**
- Modify: `src/au_opendata_mcp/safety.py` (append the SQL half)
- Test: `tests/test_safety_sql.py`

Implements §6.3. Parsing, not regex — regex on SQL loses to comments, string literals and nested statements, and the tests below include exactly those cases.

**Verified:** `isinstance(tree, exp.Query)` is the correct single gate. It is `True` for `Select`, `Union` and `Subquery`, and `False` for `Drop`, `Insert`, `Update`, `Create`, `Attach`, `Pragma` and `Copy`. Do not hand-roll a denylist of statement types.

**Interfaces:**
- Consumes: `SqlRejected` (Task 1).
- Produces:
  - `QUERY_TABLE: str` = `"t"`; `DENIED_FUNCTIONS: frozenset[str]`.
  - `validate_sql(sql: str) -> str` — returns the normalised DuckDB SQL; raises `SqlRejected` naming the rule that fired.
  - `wrap_with_limit(sql: str, limit: int) -> str` — returns `SELECT * FROM (<sql>) LIMIT <limit+1>`.

- [ ] **Step 1: Write the failing tests**

`tests/test_safety_sql.py`:

```python
import pytest

from au_opendata_mcp.errors import SqlRejected
from au_opendata_mcp.safety import validate_sql, wrap_with_limit


@pytest.mark.parametrize("sql", [
    "SELECT * FROM t",
    "SELECT suburb, AVG(price) FROM t GROUP BY suburb",
    "SELECT * FROM t WHERE price > 100 ORDER BY price DESC",
    "WITH x AS (SELECT * FROM t) SELECT count(*) FROM x",
    "WITH a AS (SELECT * FROM t), b AS (SELECT * FROM a) SELECT * FROM b",
    "SELECT a FROM t UNION SELECT b FROM t",
    "SELECT strftime(d, '%Y') AS yr, count(*) FROM t GROUP BY 1",
])
def test_legitimate_read_only_queries_are_accepted(sql):
    assert validate_sql(sql)


@pytest.mark.parametrize("sql,expected", [
    ("DROP TABLE t", "only SELECT"),
    ("DELETE FROM t", "only SELECT"),
    ("UPDATE t SET a = 1", "only SELECT"),
    ("INSERT INTO t VALUES (1)", "only SELECT"),
    ("CREATE TABLE z AS SELECT * FROM t", "only SELECT"),
    ("ATTACH '/tmp/x.db' AS x", "only SELECT"),
    ("PRAGMA database_list", "only SELECT"),
    ("INSTALL httpfs", "only SELECT"),
    ("COPY t TO '/tmp/out.csv'", "only SELECT"),
])
def test_non_select_statements_are_rejected(sql, expected):
    with pytest.raises(SqlRejected, match=expected):
        validate_sql(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM t; DROP TABLE t",
    "SELECT * FROM t -- harmless\n; DELETE FROM t",
    "SELECT * FROM t;;",
])
def test_stacked_statements_are_rejected_including_comment_hidden_ones(sql):
    with pytest.raises(SqlRejected, match="exactly one statement"):
        validate_sql(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_csv_auto('/etc/passwd')",
    "SELECT * FROM read_parquet('/etc/shadow')",
    "SELECT * FROM glob('/**')",
    "SELECT * FROM other_table",
    "SELECT * FROM t JOIN secrets ON 1=1",
])
def test_reading_anything_other_than_t_is_rejected(sql):
    with pytest.raises(SqlRejected, match="only the table"):
        validate_sql(sql)


def test_a_denied_function_in_a_projection_is_rejected():
    with pytest.raises(SqlRejected, match="not allowed"):
        validate_sql("SELECT read_text('/etc/passwd') FROM t")


def test_unparseable_sql_is_rejected_with_the_parser_message():
    with pytest.raises(SqlRejected, match="could not parse"):
        validate_sql("SELECT FROM WHERE ***")


def test_empty_sql_is_rejected():
    with pytest.raises(SqlRejected):
        validate_sql("   ")


def test_limit_is_enforced_by_wrapping_not_by_rewriting():
    wrapped = wrap_with_limit("SELECT * FROM t LIMIT 999", 100)
    assert wrapped == "SELECT * FROM (SELECT * FROM t LIMIT 999) LIMIT 101"
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest tests/test_safety_sql.py -v`
Expected: FAIL — `ImportError: cannot import name 'validate_sql'`

- [ ] **Step 3: Append the SQL half to `safety.py`**

Add these imports at the top of the file:

```python
import sqlglot
from sqlglot import exp

from au_opendata_mcp.errors import SqlRejected
```

Then append:

```python
QUERY_TABLE = "t"

# Table-valued and file-reading functions. The DuckDB lockdown in services/query.py is the
# real defence; this makes the rejection message specific instead of a PermissionException.
DENIED_FUNCTIONS = frozenset({
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
    "read_ndjson", "read_text", "read_blob", "glob", "parquet_scan", "csv_scan",
})


def validate_sql(sql: str) -> str:
    """Accept a single read-only DuckDB query over the table `t`. Reject everything else.

    Parsing rather than pattern matching, because comments, string literals and stacked
    statements all defeat pattern matching.
    """
    if not sql or not sql.strip():
        raise SqlRejected("SQL is empty")

    try:
        parsed = sqlglot.parse(sql, read="duckdb")
    except sqlglot.ParseError as exc:
        raise SqlRejected(f"could not parse SQL as DuckDB: {exc}") from exc

    statements = [s for s in parsed if s is not None]
    if len(statements) != 1:
        raise SqlRejected(
            f"expected exactly one statement, found {len(statements)}; "
            "submit a single SELECT"
        )

    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise SqlRejected(
            f"only SELECT queries are allowed; this is a "
            f"{type(tree).__name__.upper()} statement"
        )

    allowed_tables = {QUERY_TABLE} | {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if table.name not in allowed_tables:
            shown = table.name or "a table-valued function"
            raise SqlRejected(
                f"only the table {QUERY_TABLE!r} may be queried; found {shown!r}"
            )

    for func in tree.find_all(exp.Anonymous):
        # Use .name — .sql_name() returns the literal string "ANONYMOUS" and is useless here.
        if func.name.lower() in DENIED_FUNCTIONS:
            raise SqlRejected(f"the function {func.name}() is not allowed")

    return tree.sql(dialect="duckdb")


def wrap_with_limit(sql: str, limit: int) -> str:
    """Enforce the row cap by wrapping. The caller's query is never rewritten.

    One extra row is requested so truncation can be detected.
    """
    return f"SELECT * FROM ({sql}) LIMIT {limit + 1}"
```

- [ ] **Step 4: Run tests and verify they pass**

Run: `uv run pytest tests/test_safety_sql.py -v`
Expected: PASS, 30 tests.

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add -A
git commit -m "feat: parse-based SQL validation restricting callers to one SELECT over t"
```

---

### Task 8: Two-tier cache

**Files:**
- Create: `src/au_opendata_mcp/cache.py`
- Test: `tests/test_cache.py`

Implements §6.6. Two caches with genuinely different shapes, so two types rather than one over-general one: metadata is small objects with a short TTL; materialised Parquet is large files with a disk budget.

Both take an injected `clock` so TTL and eviction are tested deterministically rather than with sleeps.

**Interfaces:**
- Consumes: nothing beyond the standard library.
- Produces:
  - `MetadataCache(max_entries: int, ttl_seconds: int, clock: Callable[[], float] = time.monotonic)` with `get(key) -> Any | None` and `set(key, value) -> None`. LRU by access, TTL from write.
  - `BlobCache(root: Path, ttl_seconds: int, max_bytes: int, clock: Callable[[], float] = time.time)` with `path_for(key) -> Path | None`, `store(key, source: Path) -> Path`, `total_bytes() -> int`.
  - TTL uses file **mtime** (set at write); LRU eviction uses **atime** (touched on read). Keeping them on separate stat fields is what lets a cache hit extend an entry's life without extending its freshness.

- [ ] **Step 1: Write the failing tests**

`tests/test_cache.py`:

```python
import os
from pathlib import Path

from au_opendata_mcp.cache import BlobCache, MetadataCache


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- MetadataCache ---

def test_set_then_get_returns_the_value():
    c = MetadataCache(max_entries=10, ttl_seconds=60, clock=FakeClock())
    c.set("k", {"a": 1})
    assert c.get("k") == {"a": 1}


def test_missing_key_returns_none():
    assert MetadataCache(10, 60, clock=FakeClock()).get("nope") is None


def test_entry_expires_after_its_ttl():
    clock = FakeClock()
    c = MetadataCache(10, 60, clock=clock)
    c.set("k", "v")
    clock.advance(59)
    assert c.get("k") == "v"
    clock.advance(2)
    assert c.get("k") is None


def test_least_recently_used_entry_is_evicted_when_full():
    c = MetadataCache(max_entries=2, ttl_seconds=600, clock=FakeClock())
    c.set("a", 1)
    c.set("b", 2)
    c.get("a")            # 'a' is now the most recently used
    c.set("c", 3)         # evicts 'b'
    assert c.get("a") == 1
    assert c.get("b") is None
    assert c.get("c") == 3


def test_a_cache_hit_does_not_extend_freshness():
    clock = FakeClock()
    c = MetadataCache(10, 60, clock=clock)
    c.set("k", "v")
    clock.advance(40)
    assert c.get("k") == "v"   # a hit
    clock.advance(30)          # 70s since write, still only 60s TTL
    assert c.get("k") is None


# --- BlobCache ---

def write_blob(tmp_path: Path, name: str, size: int) -> Path:
    p = tmp_path / name
    p.write_bytes(b"x" * size)
    return p


def test_store_then_path_for_returns_the_cached_file(tmp_path):
    cache = BlobCache(tmp_path / "cache", ttl_seconds=3600, max_bytes=10_000, clock=FakeClock())
    stored = cache.store("key1", write_blob(tmp_path, "a.parquet", 100))
    assert stored.exists()
    assert cache.path_for("key1") == stored
    assert stored.read_bytes() == b"x" * 100


def test_path_for_an_unknown_key_is_none(tmp_path):
    cache = BlobCache(tmp_path / "cache", 3600, 10_000, clock=FakeClock())
    assert cache.path_for("nope") is None


def test_expired_blob_is_removed_and_reported_as_a_miss(tmp_path):
    clock = FakeClock()
    cache = BlobCache(tmp_path / "cache", ttl_seconds=100, max_bytes=10_000, clock=clock)
    stored = cache.store("k", write_blob(tmp_path, "a.parquet", 50))
    clock.advance(101)
    assert cache.path_for("k") is None
    assert not stored.exists()


def test_disk_budget_evicts_least_recently_accessed_first(tmp_path):
    clock = FakeClock()
    cache = BlobCache(tmp_path / "cache", ttl_seconds=3600, max_bytes=250, clock=clock)
    a = cache.store("a", write_blob(tmp_path, "a.parquet", 100))
    b = cache.store("b", write_blob(tmp_path, "b.parquet", 100))
    # Make 'a' look older by access time than 'b'.
    os.utime(a, (clock.now - 500, a.stat().st_mtime))
    os.utime(b, (clock.now - 10, b.stat().st_mtime))
    cache.store("c", write_blob(tmp_path, "c.parquet", 100))   # total 300 > 250
    assert cache.total_bytes() <= 250
    assert not a.exists()
    assert b.exists()


def test_different_keys_do_not_collide(tmp_path):
    cache = BlobCache(tmp_path / "cache", 3600, 10_000, clock=FakeClock())
    cache.store("k1", write_blob(tmp_path, "one.parquet", 10))
    cache.store("k2", write_blob(tmp_path, "two.parquet", 20))
    assert cache.path_for("k1") != cache.path_for("k2")
    assert cache.path_for("k1").stat().st_size == 10
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest tests/test_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'au_opendata_mcp.cache'`

- [ ] **Step 3: Implement `cache.py`**

```python
import hashlib
import os
import shutil
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any


class MetadataCache:
    """Small objects, short TTL, LRU by access. TTL is measured from write, so a hit
    extends an entry's position in the LRU order but never its freshness."""

    def __init__(
        self,
        max_entries: int,
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._entries: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        self._clock = clock

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        written_at, value = entry
        if self._clock() - written_at > self._ttl:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return value

    def set(self, key: str, value: Any) -> None:
        self._entries[key] = (self._clock(), value)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


class BlobCache:
    """Materialised Parquet files under a disk budget.

    TTL uses mtime (written once). LRU eviction uses atime (touched on every read).
    Separate stat fields so a read can extend usefulness without extending freshness.
    """

    def __init__(
        self,
        root: Path,
        ttl_seconds: int,
        max_bytes: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = root
        self._ttl = ttl_seconds
        self._max_bytes = max_bytes
        self._clock = clock
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.parquet"

    def path_for(self, key: str) -> Path | None:
        path = self._path(key)
        if not path.exists():
            return None
        stat = path.stat()
        if self._clock() - stat.st_mtime > self._ttl:
            path.unlink(missing_ok=True)
            return None
        os.utime(path, (self._clock(), stat.st_mtime))  # touch atime only
        return path

    def store(self, key: str, source: Path) -> Path:
        dest = self._path(key)
        shutil.move(str(source), str(dest))
        now = self._clock()
        os.utime(dest, (now, now))
        self._evict()
        return dest

    def total_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.root.glob("*.parquet"))

    def _evict(self) -> None:
        files = sorted(self.root.glob("*.parquet"), key=lambda p: p.stat().st_atime)
        total = sum(p.stat().st_size for p in files)
        for path in files:
            if total <= self._max_bytes:
                return
            total -= path.stat().st_size
            path.unlink(missing_ok=True)
```

- [ ] **Step 4: Run tests and verify they pass**

Run: `uv run pytest tests/test_cache.py -v`
Expected: PASS, 10 tests.

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add -A
git commit -m "feat: metadata and blob caches with deterministic TTL and eviction tests"
```

---

### Task 9: Materialisation — download and convert to Parquet

**Files:**
- Create: `src/au_opendata_mcp/services/materialise.py`
- Test: `tests/test_materialise.py`

Implements §6.1 and the download half of §6.4. The cached artifact is Parquet, not the raw download, so CSV sniffing is paid once per resource version rather than once per query.

**Verified:** `read_csv_auto(path, ignore_errors=true, encoding='latin-1')` skips a malformed row and still decodes `Café` correctly on DuckDB 1.5.5. XLSX needs `INSTALL excel`, which reaches the network on first use — so the install is lazy and its failure degrades to `ResourceNotQueryable` with the download link. The Dockerfile in Task 15 pre-installs it.

**Interfaces:**
- Consumes: `assert_safe_url`, `assert_url_in_catalog`, `MAX_REDIRECTS` (Task 6); `BlobCache` (Task 8); `ResourceInfo` (Task 3); `Settings` (Task 1); `ResourceTooLarge`, `ResourceNotQueryable`, `DownloadFailed` (Task 1).
- Produces:
  - `TABULAR_READERS: dict[str, str]` mapping upper-case format to a reader kind (`"csv"` or `"excel"`).
  - `Materialiser(http: httpx.AsyncClient, settings: Settings, blobs: BlobCache)`.
  - `async Materialiser.parquet_for(resource: ResourceInfo, catalog_urls: set[str], portal: str = "data.gov.au", last_modified: str | None = None) -> Path` — returns a cached or freshly built Parquet path.
  - `Materialiser.cache_key(resource: ResourceInfo, portal: str, last_modified: str | None) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/test_materialise.py`:

```python
import httpx
import pytest
import respx

from au_opendata_mcp.cache import BlobCache
from au_opendata_mcp.config import Settings
from au_opendata_mcp.errors import DownloadFailed, ResourceNotQueryable, ResourceTooLarge
from au_opendata_mcp.services.catalog import ResourceInfo
from au_opendata_mcp.services.materialise import Materialiser

CSV = b"suburb,price\nAdelaide,100\nAdelaide,300\nGlenelg,200\n"
URL = "https://x.org/stops.csv"


def resource(url: str = URL, fmt: str = "CSV", size: int | None = len(CSV)) -> ResourceInfo:
    return ResourceInfo(
        resource_id="r1", name="stops.csv", format=fmt, size_bytes=size, url=url,
        datastore_active=False, queryable="yes", queryable_reason=None,
    )


def build(tmp_path, settings: Settings | None = None) -> Materialiser:
    s = settings or Settings()
    blobs = BlobCache(tmp_path / "blobs", s.materialisation_ttl_seconds, s.cache_max_bytes)
    # Public IPs only in tests; the SSRF resolver is exercised in test_safety_urls.py.
    m = Materialiser(httpx.AsyncClient(follow_redirects=False), s, blobs)
    m._resolve = lambda host: ["93.184.216.34"]
    return m


@respx.mock
async def test_csv_is_downloaded_and_converted_to_parquet(tmp_path):
    respx.get(URL).mock(return_value=httpx.Response(200, content=CSV))
    path = await build(tmp_path).parquet_for(resource(), {URL})
    assert path.suffix == ".parquet"
    assert path.stat().st_size > 0


@respx.mock
async def test_a_second_call_is_served_from_cache_without_another_download(tmp_path):
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=CSV))
    m = build(tmp_path)
    first = await m.parquet_for(resource(), {URL})
    second = await m.parquet_for(resource(), {URL})
    assert first == second
    assert route.call_count == 1


@respx.mock
async def test_a_changed_last_modified_invalidates_the_cache(tmp_path):
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=CSV))
    m = build(tmp_path)
    r1 = resource()
    await m.parquet_for(r1, {URL})
    m_key_old = m.cache_key(r1, "data.gov.au", "2026-01-01")
    m_key_new = m.cache_key(r1, "data.gov.au", "2026-09-01")
    assert m_key_old != m_key_new
    assert route.call_count == 1


@respx.mock
async def test_a_url_not_in_the_catalog_is_refused(tmp_path):
    respx.get("https://evil.org/x.csv").mock(return_value=httpx.Response(200, content=CSV))
    with pytest.raises(DownloadFailed, match="not listed"):
        await build(tmp_path).parquet_for(resource(url="https://evil.org/x.csv"), {URL})


@respx.mock
async def test_streaming_stops_the_moment_the_ceiling_is_crossed(tmp_path):
    settings = Settings(max_resource_bytes=100)
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"y" * 5000))
    with pytest.raises(ResourceTooLarge):
        await build(tmp_path, settings).parquet_for(resource(size=None), {URL})


@respx.mock
async def test_a_lying_content_length_does_not_get_past_the_streaming_cap(tmp_path):
    """content-length is attacker-controlled, so it is a hint and never the limit."""
    settings = Settings(max_resource_bytes=100)
    respx.get(URL).mock(
        return_value=httpx.Response(200, content=b"y" * 5000, headers={"content-length": "10"})
    )
    with pytest.raises(ResourceTooLarge):
        await build(tmp_path, settings).parquet_for(resource(size=None), {URL})


@respx.mock
async def test_a_redirect_to_a_private_address_is_refused_at_the_hop(tmp_path):
    respx.get(URL).mock(
        return_value=httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})
    )
    m = build(tmp_path)
    m._resolve = lambda host: ["169.254.169.254"] if host == "169.254.169.254" else ["93.184.216.34"]
    with pytest.raises(DownloadFailed, match="non-public"):
        await m.parquet_for(resource(), {URL})


@respx.mock
async def test_a_redirect_loop_is_bounded(tmp_path):
    respx.get(URL).mock(return_value=httpx.Response(302, headers={"location": URL}))
    with pytest.raises(DownloadFailed, match="too many redirects"):
        await build(tmp_path).parquet_for(resource(), {URL})


@respx.mock
async def test_a_non_tabular_format_is_rejected_before_any_download(tmp_path):
    route = respx.get("https://x.org/a.pdf")
    with pytest.raises(ResourceNotQueryable, match="not tabular"):
        await build(tmp_path).parquet_for(
            resource(url="https://x.org/a.pdf", fmt="PDF"), {"https://x.org/a.pdf"}
        )
    assert route.call_count == 0


@respx.mock
async def test_a_malformed_csv_row_is_skipped_rather_than_failing_the_query(tmp_path):
    messy = "name,qty\nCafé,3\nbroken,row,extra\nOk,5\n".encode("latin-1")
    respx.get(URL).mock(return_value=httpx.Response(200, content=messy))
    path = await build(tmp_path).parquet_for(resource(size=None), {URL})
    import duckdb
    rows = duckdb.connect().execute(f"SELECT * FROM read_parquet('{path}')").fetchall()
    assert len(rows) == 2


@respx.mock
async def test_an_unparseable_body_becomes_resource_not_queryable_with_the_link(tmp_path):
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"\x00\x01\x02\x03"))
    with pytest.raises(ResourceNotQueryable) as exc:
        await build(tmp_path).parquet_for(resource(size=None), {URL})
    assert exc.value.remedy_url == URL


@respx.mock
async def test_an_upstream_error_on_download_becomes_download_failed(tmp_path):
    respx.get(URL).mock(return_value=httpx.Response(503))
    with pytest.raises(DownloadFailed):
        await build(tmp_path).parquet_for(resource(), {URL})
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest tests/test_materialise.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'au_opendata_mcp.services.materialise'`

- [ ] **Step 3: Implement `services/materialise.py`**

```python
import hashlib
import logging
import tempfile
from collections.abc import Callable
from pathlib import Path

import duckdb
import httpx

from au_opendata_mcp.cache import BlobCache
from au_opendata_mcp.config import Settings
from au_opendata_mcp.errors import DownloadFailed, ResourceNotQueryable, ResourceTooLarge
from au_opendata_mcp.safety import MAX_REDIRECTS, _resolve_host, assert_safe_url, assert_url_in_catalog
from au_opendata_mcp.services.catalog import ResourceInfo

log = logging.getLogger(__name__)

TABULAR_READERS: dict[str, str] = {
    "CSV": "csv",
    "TSV": "csv",
    "XLSX": "excel",
    "XLS": "excel",
}


class Materialiser:
    def __init__(self, http: httpx.AsyncClient, settings: Settings, blobs: BlobCache) -> None:
        self.http = http
        self.settings = settings
        self.blobs = blobs
        self._resolve: Callable[[str], list[str]] = _resolve_host

    def cache_key(
        self, resource: ResourceInfo, portal: str, last_modified: str | None
    ) -> str:
        raw = f"{portal}|{resource.resource_id}|{last_modified or ''}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def parquet_for(
        self,
        resource: ResourceInfo,
        catalog_urls: set[str],
        portal: str = "data.gov.au",
        last_modified: str | None = None,
    ) -> Path:
        fmt = (resource.format or "").strip().upper()
        reader = TABULAR_READERS.get(fmt)
        if reader is None:
            raise ResourceNotQueryable(
                f"format {resource.format or 'unknown'!r} is not tabular",
                remedy_url=resource.url,
            )
        if not resource.url:
            raise ResourceNotQueryable(
                "resource has no download URL in the catalog", remedy_url=None
            )

        key = self.cache_key(resource, portal, last_modified)
        cached = self.blobs.path_for(key)
        if cached is not None:
            log.info("materialise cache hit resource_id=%s", resource.resource_id)
            return cached

        assert_url_in_catalog(resource.url, catalog_urls)
        downloaded = await self._download(resource.url)
        try:
            parquet = self._convert(downloaded, reader, resource.url)
        finally:
            downloaded.unlink(missing_ok=True)
        return self.blobs.store(key, parquet)

    async def _download(self, url: str) -> Path:
        """Stream to a temp file under a hard byte ceiling, re-validating every redirect hop."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            assert_safe_url(current, resolve=self._resolve)
            try:
                async with self.http.stream(
                    "GET", current, timeout=self.settings.download_timeout_seconds
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise DownloadFailed(f"redirect from {current} had no location header")
                        current = str(httpx.URL(current).join(location))
                        continue
                    if response.status_code >= 400:
                        raise DownloadFailed(
                            f"download of {current} failed with HTTP {response.status_code}",
                            remedy_url=url,
                        )

                    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".download")
                    written = 0
                    try:
                        async for chunk in response.aiter_bytes():
                            written += len(chunk)
                            # content-length is a hint. This is the limit.
                            if written > self.settings.max_resource_bytes:
                                raise ResourceTooLarge(
                                    f"resource exceeds the "
                                    f"{self.settings.max_resource_bytes // (1024 * 1024)} MB limit",
                                    remedy_url=url,
                                )
                            handle.write(chunk)
                    finally:
                        handle.close()
                    return Path(handle.name)
            except httpx.HTTPError as exc:
                raise DownloadFailed(f"download of {current} failed: {exc}", remedy_url=url) from exc

        raise DownloadFailed(f"too many redirects fetching {url}", remedy_url=url)

    def _convert(self, source: Path, reader: str, remedy_url: str) -> Path:
        dest = Path(tempfile.mkdtemp()) / "materialised.parquet"
        con = duckdb.connect()
        con.execute(f"SET memory_limit = '{self.settings.duckdb_memory_limit}'")

        if reader == "excel":
            try:
                con.execute("INSTALL excel")
                con.execute("LOAD excel")
            except duckdb.Error as exc:
                raise ResourceNotQueryable(
                    "XLSX support needs DuckDB's excel extension, which could not be "
                    f"loaded: {exc}",
                    remedy_url=remedy_url,
                ) from exc
            expression = f"SELECT * FROM read_xlsx('{source}')"
        else:
            expression = (
                f"SELECT * FROM read_csv_auto('{source}', ignore_errors=true, "
                "sample_size=-1)"
            )

        try:
            con.execute(f"COPY ({expression}) TO '{dest}' (FORMAT PARQUET)")
        except duckdb.Error:
            # One retry with an explicit single-byte encoding, for files that are not UTF-8.
            if reader != "csv":
                raise ResourceNotQueryable(
                    "resource could not be parsed as a table", remedy_url=remedy_url
                ) from None
            try:
                con.execute(
                    f"COPY (SELECT * FROM read_csv_auto('{source}', ignore_errors=true, "
                    f"sample_size=-1, encoding='latin-1')) TO '{dest}' (FORMAT PARQUET)"
                )
            except duckdb.Error as exc:
                raise ResourceNotQueryable(
                    f"resource could not be parsed as a table: {exc}", remedy_url=remedy_url
                ) from exc
        finally:
            con.close()

        return dest
```

- [ ] **Step 4: Run tests and verify they pass**

Run: `uv run pytest tests/test_materialise.py -v`
Expected: PASS, 12 tests.

If `test_an_unparseable_body_becomes_resource_not_queryable_with_the_link` passes conversion instead of failing (DuckDB is very tolerant), change the body to something that cannot be coerced — e.g. a single 2 MB line of random bytes with no delimiter — rather than relaxing the assertion. A resource that cannot be parsed must not silently become a one-column table.

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add -A
git commit -m "feat: materialise tabular resources to cached Parquet with a streaming byte cap"
```

---

### Task 10: `query_resource` and the DuckDB sandbox

**Files:**
- Create: `src/au_opendata_mcp/services/query.py`
- Test: `tests/test_query_execute.py`, `tests/test_query_service.py`

Implements §6.2. This is the centrepiece and the one place where getting two lines in the wrong order is a security bug rather than a bug.

**Verified on DuckDB 1.5.5** — do not reorder:
- Load into the connection, **then** `SET enable_external_access = false`, **then** `SET lock_configuration = true`.
- After lockdown: `read_csv_auto`, `read_parquet`, `ATTACH`, `INSTALL`, and `SET enable_external_access = true` all raise.
- Locking first and loading second fails with `PermissionException` on `read_parquet`.

**Interfaces:**
- Consumes: `validate_sql`, `wrap_with_limit` (Task 7); `Materialiser` (Task 9); `CatalogService`, `ResourceInfo`, `DatasetDetail` (Tasks 3–4); `CkanClient` (Task 2); `Sourced`, `Settings`, errors (Task 1).
- Produces:
  - `ColumnInfo(BaseModel)`: `name: str`, `type: str`.
  - `QueryResult(Sourced)`: `resource_id`, `portal`, `sql`, `columns: list[ColumnInfo]`, `rows: list[dict]`, `row_count: int`, `truncated: bool`.
  - `ResolvedResource` dataclass: `resource: ResourceInfo`, `dataset: DatasetDetail`, `catalog_urls: set[str]`, `last_modified: str | None`.
  - `jsonable(value: Any) -> Any` — coerces dates, decimals and bytes into JSON-safe values.
  - `execute_in_sandbox(parquet: Path, sql: str, limit: int, memory_limit: str) -> tuple[list[ColumnInfo], list[dict], bool]`.
  - `QueryService(clients, catalog, materialiser, settings)` with `async resolve(resource_id, portal)` and `async query_resource(resource_id, sql, portal="data.gov.au", limit=100)`.

- [ ] **Step 1: Write the failing sandbox tests**

`tests/test_query_execute.py`:

```python
import datetime
import decimal
from pathlib import Path

import duckdb
import pytest

from au_opendata_mcp.errors import SqlRejected
from au_opendata_mcp.services.query import execute_in_sandbox, jsonable


@pytest.fixture
def parquet(tmp_path) -> Path:
    csv = tmp_path / "s.csv"
    csv.write_text("suburb,price\nAdelaide,100\nAdelaide,300\nGlenelg,200\n")
    out = tmp_path / "s.parquet"
    duckdb.connect().execute(
        f"COPY (SELECT * FROM read_csv_auto('{csv}')) TO '{out}' (FORMAT PARQUET)"
    )
    return out


def test_a_normal_aggregate_query_works(parquet):
    columns, rows, truncated = execute_in_sandbox(
        parquet, "SELECT suburb, avg(price) AS mean FROM t GROUP BY suburb ORDER BY suburb",
        limit=100, memory_limit="1GB",
    )
    assert [c.name for c in columns] == ["suburb", "mean"]
    assert rows == [{"suburb": "Adelaide", "mean": 200.0}, {"suburb": "Glenelg", "mean": 200.0}]
    assert truncated is False


def test_the_sandbox_keeps_t_readable_and_the_filesystem_closed(parquet):
    """Spec section 6.2: both properties must hold on the same connection. This test is the
    reason the two SET statements come after the CREATE TABLE, and must never be relaxed."""
    _, rows, _ = execute_in_sandbox(parquet, "SELECT count(*) AS n FROM t", 100, "1GB")
    assert rows == [{"n": 3}]

    for hostile in [
        "SELECT * FROM read_csv_auto('/etc/passwd')",
        f"SELECT * FROM read_parquet('{parquet}')",
        "SELECT * FROM glob('/**')",
    ]:
        with pytest.raises(SqlRejected):
            execute_in_sandbox(parquet, hostile, 100, "1GB")


def test_the_lockdown_cannot_be_reversed_by_caller_sql(parquet):
    with pytest.raises(SqlRejected):
        execute_in_sandbox(parquet, "SET enable_external_access = true", 100, "1GB")


def test_truncation_is_reported_without_returning_the_extra_row(parquet):
    columns, rows, truncated = execute_in_sandbox(parquet, "SELECT * FROM t", limit=2,
                                                  memory_limit="1GB")
    assert len(rows) == 2
    assert truncated is True


def test_a_result_at_exactly_the_limit_is_not_reported_as_truncated(parquet):
    _, rows, truncated = execute_in_sandbox(parquet, "SELECT * FROM t", limit=3,
                                            memory_limit="1GB")
    assert len(rows) == 3
    assert truncated is False


def test_a_runtime_sql_error_becomes_sql_rejected_with_the_engine_message(parquet):
    with pytest.raises(SqlRejected, match="no_such_column|not found|Binder"):
        execute_in_sandbox(parquet, "SELECT no_such_column FROM t", 100, "1GB")


# --- jsonable: rows must survive JSON serialisation for both MCP and REST ---

def test_dates_and_times_become_iso_strings():
    assert jsonable(datetime.date(2026, 7, 1)) == "2026-07-01"
    assert jsonable(datetime.datetime(2026, 7, 1, 9, 30)) == "2026-07-01T09:30:00"


def test_decimals_become_floats():
    assert jsonable(decimal.Decimal("12.50")) == 12.5


def test_bytes_become_hex():
    assert jsonable(b"\x01\x02") == "0102"


def test_ordinary_values_pass_through():
    assert jsonable("Adelaide") == "Adelaide"
    assert jsonable(None) is None
    assert jsonable(42) == 42


def test_rows_containing_dates_are_serialisable(tmp_path):
    csv = tmp_path / "d.csv"
    csv.write_text("when,n\n2026-07-01,1\n")
    out = tmp_path / "d.parquet"
    duckdb.connect().execute(
        f"COPY (SELECT * FROM read_csv_auto('{csv}')) TO '{out}' (FORMAT PARQUET)"
    )
    import json
    _, rows, _ = execute_in_sandbox(out, "SELECT * FROM t", 100, "1GB")
    json.dumps(rows)  # must not raise
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest tests/test_query_execute.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'au_opendata_mcp.services.query'`

- [ ] **Step 3: Implement the sandbox in `services/query.py`**

```python
import datetime
import decimal
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import duckdb
from pydantic import BaseModel

from au_opendata_mcp.ckan.client import CkanClient, strip_internal_columns
from au_opendata_mcp.config import Settings
from au_opendata_mcp.errors import ResourceNotFound, SqlRejected
from au_opendata_mcp.models import Sourced
from au_opendata_mcp.safety import validate_sql, wrap_with_limit
from au_opendata_mcp.services.catalog import CatalogService, DatasetDetail, ResourceInfo
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
```

- [ ] **Step 4: Run the sandbox tests and verify they pass**

Run: `uv run pytest tests/test_query_execute.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 5: Write the failing service tests**

`tests/test_query_service.py`:

```python
import httpx
import pytest
import respx

from au_opendata_mcp.cache import BlobCache
from au_opendata_mcp.ckan.client import CkanClient
from au_opendata_mcp.config import Settings
from au_opendata_mcp.errors import ResourceNotQueryable, SqlRejected
from au_opendata_mcp.portals import get_portal
from au_opendata_mcp.services.catalog import CatalogService
from au_opendata_mcp.services.materialise import Materialiser
from au_opendata_mcp.services.query import QueryService

CSV = b"suburb,price\nAdelaide,100\nAdelaide,300\nGlenelg,200\n"
RES_URL = "https://x.org/stops.csv"
SETTINGS = Settings()

RESOURCE_SHOW = {"success": True, "result": {
    "id": "r1", "name": "stops.csv", "format": "CSV", "url": RES_URL,
    "size": len(CSV), "datastore_active": False, "package_id": "pkg-1",
    "last_modified": "2026-08-01T00:00:00",
}}
PACKAGE_SHOW = {"success": True, "result": {
    "id": "pkg-1", "name": "sa-bus-stops", "title": "SA Bus Stops",
    "license_title": "CC BY 4.0", "metadata_modified": "2026-08-01T00:00:00",
    "organization": {"name": "dpti", "title": "DIT"},
    "resources": [RESOURCE_SHOW["result"]],
}}


def build(tmp_path) -> QueryService:
    http = httpx.AsyncClient(follow_redirects=False)
    clients = {p: CkanClient(get_portal(p), http, SETTINGS)
               for p in ("data.gov.au", "data.sa.gov.au")}
    catalog = CatalogService(clients, SETTINGS)
    blobs = BlobCache(tmp_path / "blobs", SETTINGS.materialisation_ttl_seconds,
                      SETTINGS.cache_max_bytes)
    materialiser = Materialiser(http, SETTINGS, blobs)
    materialiser._resolve = lambda host: ["93.184.216.34"]
    return QueryService(clients, catalog, materialiser, SETTINGS)


def mock_catalog() -> None:
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/resource_show").mock(
        return_value=httpx.Response(200, json=RESOURCE_SHOW)
    )
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=PACKAGE_SHOW)
    )


@respx.mock
async def test_a_query_returns_rows_with_attribution(tmp_path):
    mock_catalog()
    respx.get(RES_URL).mock(return_value=httpx.Response(200, content=CSV))
    result = await build(tmp_path).query_resource(
        "r1", "SELECT suburb, avg(price) AS mean FROM t GROUP BY suburb ORDER BY suburb"
    )
    assert result.rows[0] == {"suburb": "Adelaide", "mean": 200.0}
    assert result.licence == "CC BY 4.0"
    assert result.organisation == "DIT"
    assert result.dataset_url == "https://data.gov.au/dataset/sa-bus-stops"
    assert result.row_count == 2
    assert result.truncated is False


@respx.mock
async def test_invalid_sql_is_rejected_before_anything_is_downloaded(tmp_path):
    mock_catalog()
    download = respx.get(RES_URL)
    with pytest.raises(SqlRejected):
        await build(tmp_path).query_resource("r1", "DROP TABLE t")
    assert download.call_count == 0


@respx.mock
async def test_limit_is_clamped_to_the_configured_maximum(tmp_path):
    mock_catalog()
    respx.get(RES_URL).mock(return_value=httpx.Response(200, content=CSV))
    result = await build(tmp_path).query_resource("r1", "SELECT * FROM t", limit=100_000)
    assert len(result.rows) <= SETTINGS.max_query_limit


@respx.mock
async def test_querying_a_non_tabular_resource_returns_the_download_link(tmp_path):
    pdf_resource = {"success": True, "result": {
        **RESOURCE_SHOW["result"], "format": "PDF", "url": "https://x.org/a.pdf"}}
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/resource_show").mock(
        return_value=httpx.Response(200, json=pdf_resource)
    )
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=PACKAGE_SHOW)
    )
    with pytest.raises(ResourceNotQueryable) as exc:
        await build(tmp_path).query_resource("r1", "SELECT * FROM t")
    assert exc.value.remedy_url == "https://x.org/a.pdf"


@respx.mock
async def test_the_second_query_reuses_the_materialised_cache(tmp_path):
    mock_catalog()
    download = respx.get(RES_URL).mock(return_value=httpx.Response(200, content=CSV))
    service = build(tmp_path)
    await service.query_resource("r1", "SELECT * FROM t")
    await service.query_resource("r1", "SELECT count(*) AS n FROM t")
    assert download.call_count == 1
```

- [ ] **Step 6: Run tests and verify they fail**

Run: `uv run pytest tests/test_query_service.py -v`
Expected: FAIL — `ImportError: cannot import name 'QueryService'`

- [ ] **Step 7: Append `QueryService` to `services/query.py`**

```python
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
        match = next((r for r in dataset.resources if r.resource_id == resource_id), None)
        if match is None:
            raise ResourceNotFound(
                f"resource {resource_id!r} is not listed on dataset {dataset.dataset_id!r}",
                remedy_url=dataset.dataset_url,
            )
        return ResolvedResource(
            resource=match,
            dataset=dataset,
            catalog_urls={r.url for r in dataset.resources if r.url},
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
```

- [ ] **Step 8: Run tests and verify they pass**

Run: `uv run pytest tests/test_query_service.py tests/test_query_execute.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add -A
git commit -m "feat: query_resource with a locked-down DuckDB sandbox"
```

---

### Task 11: `preview_resource`

**Files:**
- Modify: `src/au_opendata_mcp/services/query.py` (add `preview_resource`)
- Test: `tests/test_query_preview.py`

Implements §6.5. Schema discovery, which must happen before a model can write SQL. DataStore-backed resources answer with no download; everything else materialises — and because materialisation is cached, a preview followed by a query costs **one** download in total.

**Interfaces:**
- Consumes: everything from Task 10, plus `strip_internal_columns` (Task 2).
- Produces: `async QueryService.preview_resource(resource_id, portal="data.gov.au", limit=20) -> PreviewResult`.

- [ ] **Step 1: Write the failing tests**

`tests/test_query_preview.py`:

```python
import httpx
import respx

from tests.test_query_service import CSV, PACKAGE_SHOW, RES_URL, RESOURCE_SHOW, build

DATASTORE_RESOURCE = {"success": True, "result": {
    **RESOURCE_SHOW["result"], "datastore_active": True}}
DATASTORE_PACKAGE = {"success": True, "result": {
    **PACKAGE_SHOW["result"], "resources": [DATASTORE_RESOURCE["result"]]}}
DATASTORE_SEARCH = {"success": True, "result": {
    "total": 4821,
    "fields": [{"id": "_id", "type": "int"}, {"id": "suburb", "type": "text"},
               {"id": "price", "type": "numeric"}],
    "records": [
        {"_id": 1, "_full_text": "'adelaide':1 '100':2", "suburb": "Adelaide", "price": 100},
        {"_id": 2, "_full_text": "'glenelg':1 '200':2", "suburb": "Glenelg", "price": 200},
    ],
}}


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
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest tests/test_query_preview.py -v`
Expected: FAIL — `AttributeError: 'QueryService' object has no attribute 'preview_resource'`

- [ ] **Step 3: Add `preview_resource` to `QueryService`**

```python
    async def preview_resource(
        self, resource_id: str, portal: str = "data.gov.au", limit: int = 20
    ) -> PreviewResult:
        capped = max(1, min(limit, self.settings.max_preview_limit))
        resolved = await self.resolve(resource_id, portal)

        if resolved.resource.datastore_active:
            try:
                return await self._preview_from_datastore(resolved, portal, capped)
            except Exception as exc:  # DataStore is a fast path, not a hard dependency
                log.info("datastore preview failed for %s, materialising: %s", resource_id, exc)

        return await self._preview_from_parquet(resolved, portal, capped)

    async def _preview_from_datastore(
        self, resolved: ResolvedResource, portal: str, limit: int
    ) -> PreviewResult:
        result = await self.clients[portal].action(
            "datastore_search", {"resource_id": resolved.resource.resource_id, "limit": limit}
        )
        fields = result.get("fields", [])
        rows = strip_internal_columns(result.get("records", []))
        return PreviewResult(
            resource_id=resolved.resource.resource_id,
            portal=portal,
            source="datastore",
            columns=[
                ColumnInfo(name=str(f["id"]), type=str(f.get("type", "unknown"))) for f in fields
            ],
            rows=[{k: jsonable(v) for k, v in row.items()} for row in rows],
            total_rows=result.get("total"),
            dataset_url=resolved.dataset.dataset_url,
            organisation=resolved.dataset.organisation,
            licence=resolved.dataset.licence,
        )

    async def _preview_from_parquet(
        self, resolved: ResolvedResource, portal: str, limit: int
    ) -> PreviewResult:
        parquet = await self.materialiser.parquet_for(
            resolved.resource, resolved.catalog_urls, portal, resolved.last_modified
        )
        columns, rows, _ = execute_in_sandbox(
            parquet, "SELECT * FROM t", limit, self.settings.duckdb_memory_limit
        )
        count_rows = execute_in_sandbox(
            parquet, "SELECT count(*) AS n FROM t", 1, self.settings.duckdb_memory_limit
        )[1]
        return PreviewResult(
            resource_id=resolved.resource.resource_id,
            portal=portal,
            source="materialised",
            columns=columns,
            rows=rows,
            total_rows=int(count_rows[0]["n"]) if count_rows else None,
            dataset_url=resolved.dataset.dataset_url,
            organisation=resolved.dataset.organisation,
            licence=resolved.dataset.licence,
        )
```

- [ ] **Step 4: Run tests and verify they pass**

Run: `uv run pytest tests/test_query_preview.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add -A
git commit -m "feat: preview_resource with a download-free DataStore fast path"
```

---

### Task 12: Expose the query tools over MCP and REST

**Files:**
- Modify: `src/au_opendata_mcp/deps.py` (build `QueryService`)
- Modify: `src/au_opendata_mcp/mcp_server.py` (add two tools)
- Modify: `src/au_opendata_mcp/api.py` (add two endpoints)
- Test: `tests/test_query_interfaces.py`

**Interfaces:**
- Consumes: `QueryService`, `PreviewResult`, `QueryResult` (Tasks 10–11); `BlobCache` (Task 8); `Materialiser` (Task 9).
- Produces:
  - `Services.query: QueryService` and `Services.blobs: BlobCache` added to the dataclass.
  - MCP tools `preview_resource` and `query_resource`.
  - `GET /api/resources/{resource_id}/preview` and `POST /api/resources/{resource_id}/query`.

- [ ] **Step 1: Write the failing tests**

`tests/test_query_interfaces.py`:

```python
import httpx
import respx
from fastapi.testclient import TestClient

from au_opendata_mcp.main import create_app
from tests.test_query_service import CSV, PACKAGE_SHOW, RES_URL, RESOURCE_SHOW


def mock_catalog() -> None:
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/resource_show").mock(
        return_value=httpx.Response(200, json=RESOURCE_SHOW)
    )
    respx.get(url__startswith="https://data.gov.au/data/api/3/action/package_show").mock(
        return_value=httpx.Response(200, json=PACKAGE_SHOW)
    )


@respx.mock
def test_rest_query_endpoint_returns_rows_and_attribution():
    mock_catalog()
    respx.get(RES_URL).mock(return_value=httpx.Response(200, content=CSV))
    with TestClient(create_app()) as c:
        r = c.post("/api/resources/r1/query",
                   json={"sql": "SELECT suburb, count(*) AS n FROM t GROUP BY suburb"})
    assert r.status_code == 200
    body = r.json()
    assert body["licence"] == "CC BY 4.0"
    assert body["row_count"] == 2


@respx.mock
def test_rest_query_endpoint_rejects_bad_sql_with_400():
    mock_catalog()
    with TestClient(create_app()) as c:
        r = c.post("/api/resources/r1/query", json={"sql": "DROP TABLE t"})
    assert r.status_code == 400
    assert "SELECT" in r.json()["detail"]


@respx.mock
def test_rest_preview_endpoint_returns_columns():
    mock_catalog()
    respx.get(RES_URL).mock(return_value=httpx.Response(200, content=CSV))
    with TestClient(create_app()) as c:
        r = c.get("/api/resources/r1/preview")
    assert r.status_code == 200
    assert [col["name"] for col in r.json()["columns"]] == ["suburb", "price"]


def test_mcp_tool_descriptions_teach_the_dialect_and_table_name():
    """Spec section 5.3: the description is the prompt. A model that guesses costs a round trip."""
    from au_opendata_mcp.mcp_server import query_resource
    doc = query_resource.__doc__ or ""
    assert "DuckDB" in doc
    assert "`t`" in doc or " t " in doc
    assert "SELECT" in doc
    assert "FROM t" in doc          # a worked example is present
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest tests/test_query_interfaces.py -v`
Expected: FAIL — 404 on the new routes, and `ImportError` on `query_resource` from `mcp_server`.

- [ ] **Step 3: Extend `deps.py`**

Replace `get_services` and the `Services` dataclass with:

```python
@dataclass(slots=True)
class Services:
    settings: Settings
    http: httpx.AsyncClient
    catalog: CatalogService
    query: QueryService
    blobs: BlobCache


@lru_cache
def get_services() -> Services:
    settings = get_settings()
    http = httpx.AsyncClient(follow_redirects=False, headers={"User-Agent": "au-opendata-mcp/0.1"})
    clients = {p.id: CkanClient(p, http, settings) for p in all_portals()}
    catalog = CatalogService(clients, settings)
    blobs = BlobCache(
        settings.cache_dir / "blobs",
        settings.materialisation_ttl_seconds,
        settings.cache_max_bytes,
    )
    materialiser = Materialiser(http, settings, blobs)
    return Services(
        settings=settings,
        http=http,
        catalog=catalog,
        query=QueryService(clients, catalog, materialiser, settings),
        blobs=blobs,
    )
```

Add the imports: `from au_opendata_mcp.cache import BlobCache`, `from au_opendata_mcp.services.materialise import Materialiser`, `from au_opendata_mcp.services.query import QueryService`.

- [ ] **Step 4: Add the two MCP tools to `mcp_server.py`**

```python
@mcp.tool
async def preview_resource(
    resource_id: str, portal: str = "data.gov.au", limit: int = 20
) -> dict[str, Any]:
    """Show a resource's columns, their types, a sample of rows, and the total row count.

    ALWAYS call this before query_resource: you cannot write correct SQL without first
    seeing the column names. Costs nothing for resources held in the portal's DataStore;
    for others it downloads and caches the file, which then makes the follow-up query free.

    Args:
        resource_id: From get_dataset's resources list.
        portal: The portal the resource belongs to.
        limit: Sample rows to return, capped at 20."""
    try:
        result = await get_services().query.preview_resource(resource_id, portal, limit)
        return result.model_dump()
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except AuOpenDataError as exc:
        raise _fail(exc) from exc


@mcp.tool
async def query_resource(
    resource_id: str, sql: str, portal: str = "data.gov.au", limit: int = 100
) -> dict[str, Any]:
    """Run a read-only SQL query over one tabular resource and get the rows back.

    The SQL dialect is DuckDB. The resource is always exposed as the table `t`,
    whatever the file was called. Only a single SELECT statement is permitted:
    no DDL, no DML, no reading other tables or files.

    Example:
        sql = "SELECT suburb, AVG(price) AS mean FROM t GROUP BY suburb ORDER BY mean DESC"

    Call preview_resource first to learn the column names. At most 100 rows are returned;
    `truncated` tells you whether there were more.

    Args:
        resource_id: From get_dataset's resources list. Must have queryable == "yes".
        sql: A single DuckDB SELECT over the table `t`.
        portal: The portal the resource belongs to.
        limit: Maximum rows to return, capped at 100."""
    try:
        result = await get_services().query.query_resource(resource_id, sql, portal, limit)
        return result.model_dump()
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except AuOpenDataError as exc:
        raise _fail(exc) from exc
```

- [ ] **Step 5: Add the two REST endpoints to `api.py`**

```python
from pydantic import BaseModel


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
```

- [ ] **Step 6: Run the whole suite and verify it passes**

Run: `uv run pytest -v`
Expected: PASS, everything. This is the Week 2 milestone.

- [ ] **Step 7: Commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add -A
git commit -m "feat: expose preview_resource and query_resource over MCP and REST"
```

---

# Week 3 — Australian tools, packaging, and the portfolio surface

### Task 13: Australian utility tools

**Files:**
- Create: `src/au_opendata_mcp/services/au_tools.py`
- Modify: `src/au_opendata_mcp/mcp_server.py`, `api.py`
- Test: `tests/test_au_tools.py`

Implements §7. Pure functions, no I/O. These exist to demonstrate deterministic computation where a model would otherwise guess — that is the whole justification, and it is why only two survive from the PRD's five.

`Sourced` does **not** apply here: these results are computed, not retrieved, so there is no licence to attribute.

**Verified:** the checksum below validates the ATO's published example `51 824 753 556` and rejects a single-digit mutation.

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `ABN_WEIGHTS: tuple[int, ...]` = `(10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)`.
  - `AbnResult(BaseModel)`: `valid: bool`, `normalised: str | None`, `reason: str | None`.
  - `FinancialYearResult(BaseModel)`: `label: str`, `start_date: str`, `end_date: str`.
  - `validate_abn(abn: str) -> AbnResult`.
  - `au_financial_year(date: str) -> FinancialYearResult` — raises `ValueError` on an unparseable date.

- [ ] **Step 1: Write the failing tests**

`tests/test_au_tools.py`:

```python
import pytest

from au_opendata_mcp.services.au_tools import au_financial_year, validate_abn


# --- validate_abn ---

@pytest.mark.parametrize("abn", [
    "51824753556",        # the ATO's published example
    "51 824 753 556",     # the same, in its conventional spacing
    "51-824-753-556",     # hyphenated
    " 51824753556 ",      # padded
])
def test_a_valid_abn_is_accepted_in_any_conventional_formatting(abn):
    result = validate_abn(abn)
    assert result.valid is True
    assert result.normalised == "51 824 753 556"
    assert result.reason is None


def test_a_single_digit_mutation_fails_the_checksum():
    result = validate_abn("51824753557")
    assert result.valid is False
    assert "checksum" in result.reason.lower()
    assert result.normalised is None


def test_a_random_eleven_digit_number_fails():
    assert validate_abn("12345678901").valid is False


@pytest.mark.parametrize("abn,expected", [
    ("5182475355", "11 digits"),
    ("518247535567", "11 digits"),
    ("", "11 digits"),
])
def test_wrong_length_is_reported_as_a_length_problem(abn, expected):
    result = validate_abn(abn)
    assert result.valid is False
    assert expected in result.reason


def test_non_digit_characters_are_reported_distinctly():
    result = validate_abn("5182475355X")
    assert result.valid is False
    assert "digit" in result.reason.lower()


# --- au_financial_year ---

@pytest.mark.parametrize("date,label,start,end", [
    ("2026-09-23", "FY2026–27", "2026-07-01", "2027-06-30"),
    ("2026-07-01", "FY2026–27", "2026-07-01", "2027-06-30"),   # first day of the FY
    ("2026-06-30", "FY2025–26", "2025-07-01", "2026-06-30"),   # last day of the prior FY
    ("2026-01-15", "FY2025–26", "2025-07-01", "2026-06-30"),
    ("2025-12-31", "FY2025–26", "2025-07-01", "2026-06-30"),
])
def test_the_financial_year_boundary_is_the_first_of_july(date, label, start, end):
    result = au_financial_year(date)
    assert result.label == label
    assert result.start_date == start
    assert result.end_date == end


def test_a_century_boundary_formats_the_end_year_correctly():
    assert au_financial_year("2099-08-01").label == "FY2099–00"


def test_an_unparseable_date_raises_value_error():
    with pytest.raises(ValueError, match="could not be read as a date"):
        au_financial_year("not a date")


def test_a_datetime_string_is_accepted():
    assert au_financial_year("2026-09-23T14:30:00").label == "FY2026–27"
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest tests/test_au_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'au_opendata_mcp.services.au_tools'`

- [ ] **Step 3: Implement `services/au_tools.py`**

```python
import datetime
import re

from pydantic import BaseModel

# The ATO's published modulus-89 weighting.
ABN_WEIGHTS: tuple[int, ...] = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)
_SEPARATORS = re.compile(r"[\s\-]")


class AbnResult(BaseModel):
    valid: bool
    normalised: str | None
    reason: str | None


class FinancialYearResult(BaseModel):
    label: str
    start_date: str
    end_date: str


def validate_abn(abn: str) -> AbnResult:
    """Check an ABN against the ATO's modulus-89 checksum.

    Deterministic by design: a model asked to 'check this ABN' would otherwise guess.
    """
    stripped = _SEPARATORS.sub("", abn or "")

    if len(stripped) != 11:
        return AbnResult(
            valid=False,
            normalised=None,
            reason=f"an ABN must be 11 digits; this has {len(stripped)}",
        )
    if not stripped.isdigit():
        return AbnResult(
            valid=False, normalised=None, reason="an ABN must contain only digits"
        )

    digits = [int(c) for c in stripped]
    digits[0] -= 1  # the ATO's first step
    total = sum(d * w for d, w in zip(digits, ABN_WEIGHTS, strict=True))

    if total % 89 != 0:
        return AbnResult(
            valid=False, normalised=None, reason="the ABN failed its checksum"
        )

    normalised = f"{stripped[:2]} {stripped[2:5]} {stripped[5:8]} {stripped[8:]}"
    return AbnResult(valid=True, normalised=normalised, reason=None)


def au_financial_year(date: str) -> FinancialYearResult:
    """Map a date to the Australian financial year, which runs 1 July to 30 June."""
    try:
        parsed = datetime.date.fromisoformat(date.strip()[:10])
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{date!r} could not be read as a date; use YYYY-MM-DD") from exc

    start_year = parsed.year if parsed.month >= 7 else parsed.year - 1
    end_year = start_year + 1
    return FinancialYearResult(
        label=f"FY{start_year}–{end_year % 100:02d}",
        start_date=f"{start_year}-07-01",
        end_date=f"{end_year}-06-30",
    )
```

- [ ] **Step 4: Run tests and verify they pass**

Run: `uv run pytest tests/test_au_tools.py -v`
Expected: PASS, 15 tests.

- [ ] **Step 5: Expose both over MCP**

Add to `mcp_server.py`:

```python
from au_opendata_mcp.services import au_tools


@mcp.tool
def validate_abn(abn: str) -> dict[str, Any]:
    """Validate an Australian Business Number against the ATO's official checksum.

    Use this instead of judging an ABN yourself — the check is a deterministic
    modulus-89 calculation and guessing gets it wrong.

    Args:
        abn: 11 digits, with or without spaces or hyphens.

    Returns whether it is valid, the conventional "NN NNN NNN NNN" formatting if so,
    and the specific reason if not."""
    return au_tools.validate_abn(abn).model_dump()


@mcp.tool
def au_financial_year(date: str) -> dict[str, Any]:
    """Map a date to the Australian financial year, which runs 1 July to 30 June.

    Use this whenever a question involves an Australian financial year, rather than
    assuming a calendar year.

    Args:
        date: A date as YYYY-MM-DD.

    Returns e.g. label "FY2026-27" with its start and end dates."""
    try:
        return au_tools.au_financial_year(date).model_dump()
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
```

- [ ] **Step 6: Expose both over REST**

Add to `api.py`:

```python
from au_opendata_mcp.services import au_tools as au_tools_service

tools_router = APIRouter(prefix="/api/tools", tags=["australian tools"])


@tools_router.get("/abn/{abn}")
async def abn(abn: str) -> dict[str, Any]:
    return au_tools_service.validate_abn(abn).model_dump()


@tools_router.get("/financial-year")
async def financial_year(date: str) -> dict[str, Any]:
    return au_tools_service.au_financial_year(date).model_dump()
```

And in `main.py`, alongside the existing `app.include_router(router)`:

```python
    app.include_router(tools_router)
```

updating the import to `from au_opendata_mcp.api import STATUS_FOR, router, tools_router`.

- [ ] **Step 7: Add the interface test**

Append to `tests/test_query_interfaces.py`:

```python
def test_abn_and_financial_year_are_reachable_over_rest():
    with TestClient(create_app()) as c:
        assert c.get("/api/tools/abn/51824753556").json()["valid"] is True
        fy = c.get("/api/tools/financial-year", params={"date": "2026-09-23"}).json()
        assert fy["label"] == "FY2026–27"


def test_an_unparseable_date_is_a_400_not_a_500():
    with TestClient(create_app()) as c:
        assert c.get("/api/tools/financial-year", params={"date": "nope"}).status_code == 400
```

Run: `uv run pytest tests/test_query_interfaces.py tests/test_au_tools.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
git add -A
git commit -m "feat: ABN validation and Australian financial-year tools"
```

---

### Task 14: Dockerfile

**Files:**
- Create: `Dockerfile`, `.dockerignore`
- Test: manual — build and run

Implements the "deploy-ready but not deployed" decision. The DuckDB excel extension is baked in at build time so a container never reaches the network to fetch it (see Task 9).

- [ ] **Step 1: Write `.dockerignore`**

```
.git
.venv
__pycache__
*.pyc
.pytest_cache
.mypy_cache
.ruff_cache
docs
tests
htmlcov
.coverage
```

- [ ] **Step 2: Write `Dockerfile`**

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Dependencies first, so a source change does not invalidate the dependency layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
COPY README.md ./
RUN uv sync --frozen --no-dev

# Bake in DuckDB's excel extension so XLSX works without runtime network access.
RUN uv run python -c "import duckdb; c = duckdb.connect(); c.execute('INSTALL excel')"

ENV AU_OPENDATA_CACHE_DIR=/var/cache/au-opendata-mcp
RUN mkdir -p /var/cache/au-opendata-mcp

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uv", "run", "au-opendata-mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 3: Build and verify it actually serves**

```bash
docker build -t au-opendata-mcp .
docker run -d --name au-test -p 8001:8000 au-opendata-mcp
sleep 5
curl -s localhost:8001/health
curl -s "localhost:8001/api/datasets?q=bus+stops&limit=2" | head -c 300
docker rm -f au-test
```

Expected: `{"status":"ok"}` followed by real dataset JSON.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "build: Dockerfile with the DuckDB excel extension baked in"
```

---

### Task 15: CI, with the coverage gate and the drift lane

**Files:**
- Create: `.github/workflows/ci.yml`, `.github/workflows/upstream-drift.yml`
- Modify: `pyproject.toml` (coverage configuration)

Implements §9.5. The coverage number in the README must be a gate, not a claim (§10).

- [ ] **Step 1: Add coverage configuration to `pyproject.toml`**

```toml
[tool.coverage.run]
source = ["src/au_opendata_mcp/services"]
branch = true

[tool.coverage.report]
show_missing = true
skip_covered = true
```

- [ ] **Step 2: Write `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true

      - name: Install the pinned toolchain
        run: uv python install 3.12

      - name: Sync dependencies
        run: uv sync --frozen

      - name: Lint
        run: uv run ruff check .

      - name: Check formatting
        run: uv run ruff format --check .

      - name: Typecheck
        run: uv run mypy

      - name: Test with the coverage gate
        run: uv run pytest --cov --cov-fail-under=80
```

- [ ] **Step 3: Write `.github/workflows/upstream-drift.yml`**

```yaml
name: Upstream drift

# The live contract tests hit data.gov.au and data.sa.gov.au. They are kept out of PR CI
# so a third party's downtime cannot block a pull request, but they must still run, because
# "my tests pass" is not the same claim as "the thing still works".
on:
  schedule:
    - cron: "0 20 * * 0"   # Mondays, 07:00 Adelaide time
  workflow_dispatch:

jobs:
  contract:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv python install 3.12
      - run: uv sync --frozen
      - name: Run the live contract tests
        run: uv run pytest -m live -v
```

- [ ] **Step 4: Verify the CI commands pass locally, exactly as CI runs them**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov --cov-fail-under=80
```

All four must pass. If coverage is below 80% on `services/`, add tests for the uncovered branches the report names — do not lower the gate.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "ci: lint, typecheck and coverage gate, with a weekly upstream drift lane"
```

---

### Task 16: README, demo scenarios, and the 20-question evaluation

**Files:**
- Modify: `README.md`
- Create: `evals/questions.md`

Implements §10. Each success metric needs an artifact behind it, because a metric with nothing behind it is what an interviewer pokes first.

- [ ] **Step 1: Write `evals/questions.md`**

Twenty questions with the tool sequence each should produce. Fill in the Result column by running them.

```markdown
# Tool-call evaluation

Target: >= 95% of questions produce the expected tool sequence and a correct, sourced answer.
Run manually against Claude Desktop with the server configured. Record the date and paste
the results into the README.

**Run date:** _not yet run_
**Result:** _/20

| # | Question | Expected tool sequence | Pass? |
|---|---|---|---|
| 1 | What open data portals can you search? | `list_portals` | |
| 2 | What bus stop data does South Australia have? | `search_datasets(portal="data.sa.gov.au")` | |
| 3 | Find federal datasets about rainfall. | `search_datasets` | |
| 4 | Who publishes the most datasets on data.gov.au? | `list_organizations` | |
| 5 | Is there ABS data about population by postcode? | `list_organizations` → `search_datasets` | |
| 6 | Show me the files in the national public toilet map dataset. | `search_datasets` → `get_dataset` | |
| 7 | Which files in that dataset can you actually query? | `get_dataset` (reads `queryable`) | |
| 8 | What columns does that CSV have? | `get_dataset` → `preview_resource` | |
| 9 | How many rows are in it? | `preview_resource` | |
| 10 | Show me the first few rows. | `preview_resource` | |
| 11 | Count the records by state in that file. | `preview_resource` → `query_resource` | |
| 12 | What's the average of <numeric column> by <category column>? | `preview_resource` → `query_resource` | |
| 13 | Which 5 categories have the most records? | `query_resource` with ORDER BY + LIMIT | |
| 14 | Are there any rows with a missing value in <column>? | `query_resource` with IS NULL | |
| 15 | Try to query the PDF in that dataset. | `get_dataset` → refuses, citing `queryable: no` | |
| 16 | Delete all the rows from that table. | `query_resource` → `SqlRejected`, explained | |
| 17 | Is 51 824 753 556 a valid ABN? | `validate_abn` → valid | |
| 18 | Is 51 824 753 557 a valid ABN? | `validate_abn` → invalid, checksum | |
| 19 | What Australian financial year is 23 September 2026 in? | `au_financial_year` → FY2026-27 | |
| 20 | Compare bus stop dataset counts between the federal and SA portals. | `search_datasets(portal="all")` | |

## Notes on failures

Record, for each failure, which tool the model reached for and what the description should
have said to prevent it. Tool descriptions are the prompt (spec section 5.3); a systematic
failure here is a description bug, not a model bug.
```

- [ ] **Step 2: Write `README.md`**

```markdown
# au-opendata-mcp

An MCP server that gives an AI client direct access to Australian government open data.
Search the catalogue, inspect a file's schema, then run real SQL over it — with the source
URL, publisher and licence attached to every answer.

Covers [data.gov.au](https://data.gov.au) (federal, ~70,000 datasets) and
[data.sa.gov.au](https://data.sa.gov.au) (South Australia). Both run CKAN.

## Quickstart

```bash
git clone https://github.com/<you>/au-opendata-mcp
cd au-opendata-mcp
uv sync
uv run au-opendata-mcp --transport http --port 8000   # REST + MCP over HTTP
curl "localhost:8000/api/datasets?q=bus+stops&limit=3"
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "au-opendata": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/au-opendata-mcp", "run", "au-opendata-mcp"]
    }
  }
}
```

## Tools

| Tool | What it does |
|---|---|
| `list_portals` | The portals available, with dataset counts |
| `search_datasets` | Keyword search; one portal or both |
| `get_dataset` | A dataset's metadata and files, each flagged queryable or not |
| `preview_resource` | A file's columns, types, sample rows and row count |
| `query_resource` | A single DuckDB `SELECT` over the file, exposed as table `t` |
| `validate_abn` | ABN checksum, per the ATO algorithm |
| `au_financial_year` | Maps a date to the Australian FY (1 July - 30 June) |

The intended flow is `search_datasets` → `get_dataset` → `preview_resource` → `query_resource`.
You cannot write SQL for columns you have not seen, so `preview_resource` comes first.

## Design notes

**One SQL dialect, always.** CKAN's DataStore is PostgreSQL and the fallback engine is DuckDB.
Routing queries to whichever happens to be available would mean the same tool silently
changing dialect between datasets — different function names, different casting, different
date handling — with no way for the caller to know in advance. So every SQL query runs in
DuckDB. The DataStore is used only for download-free schema and sample retrieval.

**The query sandbox.** A resource is materialised to Parquet once, then loaded into a fresh
in-memory DuckDB. Only *after* the table exists is `enable_external_access` revoked and the
configuration locked — that order matters, because revoking access also disables the Parquet
read. Caller SQL is parsed with `sqlglot` and must be a single `SELECT` over the table `t`.
Row caps are applied by wrapping the query, never by rewriting it.

**Downloads are constrained.** Only URLs the portal itself published for that resource,
http(s) only, every resolved address checked against private and link-local ranges at every
redirect hop, and a 50 MB ceiling enforced while streaming rather than trusted from
`content-length`.

**Attribution is structural.** Every result model carrying portal data inherits a base with
`dataset_url`, `organisation` and `licence`. There is no un-sourced result type to return.

## Licensing

Datasets carry their own licences, most commonly CC BY 4.0. Every result includes its
`licence` field. Attributing the source when you reuse the data is your responsibility.

## Development

```bash
uv sync
uv run pytest                  # unit tests, no network
uv run pytest -m live          # contract tests against the real portals
uv run ruff check . && uv run mypy
```

Service-layer coverage is gated at 80% in CI. The live contract tests run weekly rather
than per-PR, so upstream downtime cannot block a pull request.

## Not in this version

Accounts, API keys and quotas; a web frontend; a data mirror; the ABS SDMX API; NSW, VIC and
QLD portals. The design doc in `docs/superpowers/specs/` explains each decision.
```

- [ ] **Step 3: Run the three demo scenarios and confirm each works end to end**

In Claude Desktop, with the server configured:

1. *"What bus stop data does South Australia have?"* → `search_datasets` returns real datasets with licences.
2. *"Pick a CSV from one of those and tell me the record count by suburb."* → `get_dataset` → `preview_resource` → `query_resource`, with a sourced answer.
3. *"Is 51 824 753 556 a valid ABN?"* → `validate_abn` → valid.

If any scenario needs more than one retry, the fix belongs in the tool description, not in the prompt.

- [ ] **Step 4: Run the 20-question evaluation and record the result**

Work through `evals/questions.md`, marking each pass or fail. Paste the score and date into
the README under a `## Evaluation` heading, e.g.:

```markdown
## Evaluation

19/20 tool-call sequences correct, run 2026-10-14 against Claude Desktop.
See `evals/questions.md` for the questions and the one failure.
```

Report the real number. A 20/20 that took prompt coaching is worth less than an honest 18/20
with a note on what failed.

- [ ] **Step 5: Final verification, then commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest --cov --cov-fail-under=80
git add -A
git commit -m "docs: README, demo scenarios and the 20-question evaluation"
```

---

### Task 17: Structured observability

**Files:**
- Create: `src/au_opendata_mcp/observability.py`
- Modify: `src/au_opendata_mcp/mcp_server.py` (register the middleware), `main.py` (configure logging), `services/materialise.py` (structured cache events)
- Test: `tests/test_observability.py`

Implements the Observability row of §8: *structured JSON logs carrying tool name, arguments digest, duration, upstream status, and cache hit or miss.* This can be done any time after Task 12; it is last because it instruments tools that must exist first.

Arguments are logged as a **digest, not verbatim**. A `query_resource` call carries caller SQL and a `search_datasets` call carries a user's question; neither belongs in a log file, and the digest is enough to correlate repeated calls.

**Verified on FastMCP 4.0.5:** `Middleware.on_call_tool(context, call_next)` is the hook, `mcp.add_middleware(m)` registers it, and the call payload is at `context.message`, with `.name` and `.arguments`.

**Interfaces:**
- Consumes: `mcp` (Task 5); `AuOpenDataError` (Task 1).
- Produces:
  - `JsonFormatter(logging.Formatter)` — emits one JSON object per record, merging anything passed via `extra`.
  - `configure_logging(level: str = "INFO") -> None`.
  - `arguments_digest(arguments: Mapping[str, Any] | None) -> str` — first 12 hex characters of a SHA-256 over the sorted arguments.
  - `ToolTimingMiddleware(Middleware)` — logs `event="tool_call"` with `tool`, `args_digest`, `duration_ms`, `outcome`.

- [ ] **Step 1: Write the failing tests**

`tests/test_observability.py`:

```python
import json
import logging

import pytest
from fastmcp import Client, FastMCP

from au_opendata_mcp.errors import UpstreamUnavailable
from au_opendata_mcp.observability import (
    JsonFormatter, ToolTimingMiddleware, arguments_digest, configure_logging,
)


def capture(caplog) -> list[dict]:
    """Render captured records through JsonFormatter and parse them back."""
    formatter = JsonFormatter()
    return [json.loads(formatter.format(r)) for r in caplog.records]


def test_the_formatter_emits_one_json_object_per_record(caplog):
    with caplog.at_level(logging.INFO):
        logging.getLogger("t").info("hello")
    entry = capture(caplog)[0]
    assert entry["message"] == "hello"
    assert entry["level"] == "INFO"
    assert "timestamp" in entry


def test_extra_fields_are_merged_into_the_json(caplog):
    with caplog.at_level(logging.INFO):
        logging.getLogger("t").info("done", extra={"tool": "query_resource", "duration_ms": 12})
    entry = capture(caplog)[0]
    assert entry["tool"] == "query_resource"
    assert entry["duration_ms"] == 12


def test_the_formatter_output_is_always_parseable_even_with_odd_values(caplog):
    with caplog.at_level(logging.INFO):
        logging.getLogger("t").info("x", extra={"weird": {1, 2}})
    json.loads(JsonFormatter().format(caplog.records[0]))  # must not raise


def test_the_digest_is_stable_and_order_independent():
    assert arguments_digest({"a": 1, "b": 2}) == arguments_digest({"b": 2, "a": 1})
    assert len(arguments_digest({"a": 1})) == 12


def test_the_digest_does_not_leak_the_argument_values():
    sql = "SELECT * FROM t WHERE email = 'someone@example.org'"
    digest = arguments_digest({"sql": sql})
    assert "example.org" not in digest
    assert "SELECT" not in digest


def test_different_arguments_produce_different_digests():
    assert arguments_digest({"q": "a"}) != arguments_digest({"q": "b"})


def test_none_arguments_are_handled():
    assert arguments_digest(None)


async def test_a_successful_tool_call_is_logged_with_its_duration(caplog):
    mcp = FastMCP("t")
    mcp.add_middleware(ToolTimingMiddleware())

    @mcp.tool
    def ping(x: str) -> dict:
        """Ping."""
        return {"x": x}

    with caplog.at_level(logging.INFO):
        async with Client(mcp) as c:
            await c.call_tool("ping", {"x": "hi"})

    entries = [e for e in capture(caplog) if e.get("event") == "tool_call"]
    assert len(entries) == 1
    assert entries[0]["tool"] == "ping"
    assert entries[0]["outcome"] == "ok"
    assert entries[0]["duration_ms"] >= 0
    assert entries[0]["args_digest"] == arguments_digest({"x": "hi"})


async def test_a_failing_tool_call_is_logged_as_an_error_and_still_raises(caplog):
    mcp = FastMCP("t")
    mcp.add_middleware(ToolTimingMiddleware())

    @mcp.tool
    def boom() -> dict:
        """Boom."""
        raise UpstreamUnavailable("data.gov.au is down")

    with caplog.at_level(logging.INFO):
        async with Client(mcp) as c:
            with pytest.raises(Exception):
                await c.call_tool("boom", {})

    entries = [e for e in capture(caplog) if e.get("event") == "tool_call"]
    assert entries[0]["outcome"] == "error"
    assert entries[0]["error_type"] == "UpstreamUnavailable"


def test_configure_logging_installs_the_json_formatter():
    configure_logging("INFO")
    root = logging.getLogger()
    assert any(isinstance(h.formatter, JsonFormatter) for h in root.handlers)
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `uv run pytest tests/test_observability.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'au_opendata_mcp.observability'`

- [ ] **Step 3: Implement `observability.py`**

```python
import hashlib
import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from fastmcp.server.middleware import Middleware

log = logging.getLogger("au_opendata_mcp")

# Attributes LogRecord always carries. Anything else came from `extra` and belongs in the JSON.
_STANDARD_FIELDS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()) | {
    "message", "asctime", "taskName",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, merging whatever was passed via `extra`."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_FIELDS:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def arguments_digest(arguments: Mapping[str, Any] | None) -> str:
    """A short, stable fingerprint of a call's arguments.

    Arguments are never logged verbatim: query_resource carries caller SQL and
    search_datasets carries a user's question. The digest is enough to correlate
    repeated calls without recording their content.
    """
    canonical = json.dumps(arguments or {}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


class ToolTimingMiddleware(Middleware):
    """Log every MCP tool call with its duration and outcome."""

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        message = getattr(context, "message", None)
        tool = getattr(message, "name", "unknown")
        digest = arguments_digest(getattr(message, "arguments", None))
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            log.warning(
                "tool call failed",
                extra={
                    "event": "tool_call",
                    "tool": tool,
                    "args_digest": digest,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "outcome": "error",
                    "error_type": type(exc).__name__,
                },
            )
            raise
        log.info(
            "tool call completed",
            extra={
                "event": "tool_call",
                "tool": tool,
                "args_digest": digest,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "outcome": "ok",
            },
        )
        return result
```

- [ ] **Step 4: Register the middleware and configure logging**

In `mcp_server.py`, directly after the `mcp = FastMCP(...)` construction:

```python
from au_opendata_mcp.observability import ToolTimingMiddleware

mcp.add_middleware(ToolTimingMiddleware())
```

In `main.py`, replace `logging.basicConfig(level=logging.INFO)` in `cli()` with:

```python
    configure_logging("INFO")
```

adding `from au_opendata_mcp.observability import configure_logging` and dropping the now-unused `logging` import if ruff flags it.

- [ ] **Step 5: Make the cache and upstream events structured**

In `services/materialise.py`, replace the cache-hit log line:

```python
            log.info("materialise cache hit resource_id=%s", resource.resource_id)
```

with:

```python
            log.info(
                "materialisation served from cache",
                extra={"event": "cache", "outcome": "hit", "resource_id": resource.resource_id},
            )
```

and add a miss event immediately before `assert_url_in_catalog(resource.url, catalog_urls)`:

```python
        log.info(
            "materialising resource",
            extra={"event": "cache", "outcome": "miss", "resource_id": resource.resource_id},
        )
```

In `ckan/client.py`, inside `action`, immediately after a response is received and before the status checks:

```python
            log.info(
                "upstream call",
                extra={
                    "event": "upstream",
                    "portal": self.portal.id,
                    "action": name,
                    "status": resp.status_code,
                },
            )
```

- [ ] **Step 6: Run the whole suite and verify it passes**

Run: `uv run pytest -v`
Expected: PASS, everything.

- [ ] **Step 7: Confirm the logs are actually JSON when the server runs**

```bash
uv run au-opendata-mcp --transport http --port 8000 > /tmp/au.log 2>&1 &
sleep 2
curl -s "localhost:8000/api/datasets?q=bus+stops&limit=1" > /dev/null
kill %1
grep '"event": "upstream"' /tmp/au.log | head -2 | python3 -m json.tool
```

Expected: a parseable JSON object containing `portal`, `action` and `status`.

- [ ] **Step 8: Commit**

```bash
uv run ruff check . && uv run ruff format . && uv run mypy
uv run pytest --cov --cov-fail-under=80
git add -A
git commit -m "feat: structured JSON logging for tool calls, upstream calls and cache events"
```
