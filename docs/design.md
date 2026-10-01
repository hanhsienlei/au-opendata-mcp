# Design — au-opendata-mcp

Date: 2026-09-23
Status: Implemented (v1)
Supersedes the open questions in `docs/prd.md`

## 1. Purpose and framing

`au-opendata-mcp` is a self-hostable MCP server that exposes Australian government
open data (data.gov.au and data.sa.gov.au) as tools an AI client can call directly.

It must be genuinely correct and genuinely useful, but where breadth competes with
legibility, legibility wins. Every feature
in this design earns its place by being either demonstrable in under a minute or
defensible in a code review. Anything that is neither has been cut.

## 2. Decisions that close the PRD's open questions

| PRD open question | Decision |
| --- | --- |
| Is v1 scope Phases 1–3? | Phases 1 and 2 in full; Phase 3 trimmed to `validate_abn` and `au_financial_year`. `validate_acn`, `postcode_to_state` and `validate_bsb` are cut as near-duplicates that pad the tool list without adding signal. |
| Include data.sa.gov.au, and confirm its endpoint? | Yes. Verified 2026-09-23: CKAN 2.9.12 at `https://data.sa.gov.au/data/api/3/action/`, with `datastore` and `xloader` extensions installed. One client covers both portals. |
| Local only, or deploy in v1? | Local plus a Dockerfile, deploy-ready but not deployed. The cache sits behind an interface so a stateless deployment later needs no redesign. |
| Final project name? | `au-opendata-mcp`. The directory is renamed to match. |
| Does `datastore_search_sql` need an API token? | No. Verified 2026-09-23: an anonymous SQL query against data.gov.au returned records. |

### 2.1 Correction to the PRD's query strategy

The PRD specifies that `query_rows` runs the caller's SQL against CKAN's DataStore
when the resource is loaded there, and against DuckDB otherwise. This is rejected.

CKAN's DataStore is PostgreSQL. The fallback is DuckDB. Under the PRD's design the
same tool executes caller-supplied SQL against two different engines with different
function names, casting rules and date handling — and the caller cannot know which
engine it will get before writing the SQL. A model would write a query that works
on one dataset and fails inexplicably on the next.

**This design uses one dialect, always: DuckDB executes every SQL query.** The
DataStore is retained only for the path it is genuinely best at — schema and sample
retrieval with no SQL and no download.

Cost accepted: a SQL query against a DataStore-backed resource pays a download it
could theoretically have avoided. This is mitigated by caching and by the preview
path, which covers the common "show me this data" case without downloading anything.
Uniform query semantics across heterogeneous sources is worth more than that saving.

## 3. Scope

### In scope

- CKAN catalog access across two portals: search, dataset detail, organisation listing.
- Tabular querying of resources via DuckDB, with DataStore-backed fast preview.
- Two deterministic Australian utility tools.
- Both MCP transports (stdio and streamable-http) and a REST interface.
- Dockerfile, CI, tests, README, and a written evaluation of the 20-question metric.

### Out of scope for v1

- Accounts, API keys, billing, quotas.
- Any web frontend or dashboard.
- A data mirror or scheduled crawl.
- ABS SDMX and additional state portals.
- Actual cloud deployment.
- An automated LLM evaluation harness.

## 4. Architecture

MCP and REST are two adapters over one service layer. All logic lives in services;
the adapters serialise results and translate a small set of domain exceptions.

```
Claude / Cursor  --MCP streamable-http or stdio-->  mcp_server.py  --\
                                                                     >--> services/ --> ckan client --> data.gov.au
Browser / curl   --REST-------------------------->  api.py        --/                --> DuckDB     --> data.sa.gov.au
                                                                                     --> au_tools
```

### 4.1 Module layout

A `src/` layout is used rather than the PRD's `app/`, so the package is genuinely
installable and tests import the installed package rather than a directory that
happens to be on `sys.path`.

```
src/au_opendata_mcp/
  config.py        # pydantic-settings: limits, TTLs, cache dir
  portals.py       # portal registry: id, label, api_base, site_url
  ckan/
    client.py      # async httpx CKAN Action API client: retries, timeouts
    models.py      # Dataset, Resource, Organization (pydantic)
  services/
    catalog.py     # search_datasets, get_dataset, list_organizations
    query.py       # preview_resource, query_resource
    materialise.py # resource URL -> local Parquet -> DuckDB table
    au_tools.py    # validate_abn, au_financial_year
  cache.py         # Cache protocol + DiskCache implementation
  safety.py        # URL allow-listing, SSRF guard, SQL validation
  mcp_server.py    # FastMCP tool registrations (thin)
  api.py           # FastAPI routes (thin)
  main.py          # app assembly, CLI entry point
tests/
  fixtures/ckan/   # recorded CKAN responses
evals/
  questions.md     # the 20-question metric
scripts/
  refresh_fixtures.py
```

### 4.2 The dependency rule

`services/` must not import from `mcp_server.py` or `api.py`, and must not raise
HTTP exceptions. Services accept plain arguments and return pydantic result models.

The adapters map these domain exceptions:

| Exception | REST status | MCP behaviour |
| --- | --- | --- |
| `ResourceNotFound` | 404 | tool error, message + portal site URL |
| `ResourceNotQueryable` | 422 | tool error, message + resource download URL |
| `ResourceTooLarge` | 413 | tool error, message + resource download URL |
| `SqlRejected` | 400 | tool error, naming the rule that rejected it |
| `UpstreamUnavailable` | 502 | tool error, naming the portal |
| `DownloadFailed` | 502 | tool error, message + resource download URL |

Every error carries a way forward. A caller that hits the size ceiling leaves with
a download link, not a dead end.

### 4.3 Attribution as a type

Every result model that surfaces **dataset content or dataset metadata** inherits from a
`Sourced` base carrying `dataset_url`, `organisation` and `licence`: `DatasetSummary`,
`DatasetDetail`, `PreviewResult` and `QueryResult`. There is no un-sourced version of these
types, so a tool cannot return portal data without attribution. The PRD's CC BY requirement
is therefore structural rather than a convention to be remembered in ten places.

`Sourced` deliberately does **not** apply to `PortalInfo` or `OrganizationSummary` — a list
of publisher names is not licensed data — nor to the §7 utility tools, whose results are
computed rather than retrieved. CC BY attaches to the data.

### 4.4 Transports and app assembly

Both MCP transports are supported from one set of tool registrations, selected by a
CLI flag: `stdio` for Claude Desktop, streamable-http for everything else.

FastMCP's HTTP app carries its own lifespan, which manages its session manager.
Mounting it into FastAPI without propagating that lifespan to the parent app is the
standard failure mode for this integration: the server starts, then every `/mcp`
request fails on a missing session manager. `main.py` constructs the FastAPI app
with `lifespan=mcp_app.lifespan` explicitly. An integration test boots the composed
app and exercises `/mcp`, `/api/datasets` and `/health` so a regression here cannot
ship silently.

## 5. Tool surface

The PRD's single `query_rows` is replaced by two tools. Under one-dialect-always,
a call with SQL and a call without have entirely different cost profiles — one is a
DataStore hit, the other may download 50 MB — and a single tool description cannot
honestly state its cost. More fundamentally, a model cannot write SQL for a resource
whose columns it has not seen, so schema discovery must precede querying.

The intended workflow is:

`search_datasets` -> `get_dataset` -> `preview_resource` -> `query_resource`

| Tool | Inputs | Returns |
| --- | --- | --- |
| `list_portals` | — | id, label, site URL, dataset count |
| `search_datasets` | `q`, `portal`, `organisation`, `format`, `limit` (≤ 50, default 20) | id, title, organisation, formats, modified, licence, dataset URL |
| `get_dataset` | `dataset_id`, `portal` | metadata, licence, resources with `queryable` flag |
| `list_organizations` | `portal`, `q` | name, title, dataset count |
| `preview_resource` | `resource_id`, `portal`, `limit` (≤ 20, default 20) | columns with types, sample rows, total row count, `source` |
| `query_resource` | `resource_id`, `portal`, `sql`, `limit` (≤ 100, default 100) | columns, rows, `truncated` |
| `validate_abn` | 11-digit string | `valid`, normalised `51 824 753 556`, failure reason |
| `au_financial_year` | date | `FY2025–26`, start date, end date |

`portal` defaults to `data.gov.au` and accepts `data.sa.gov.au`. `search_datasets`
and `list_organizations` additionally accept `portal="all"`, which fans out
concurrently and merges results, tagging each with its source portal.

### 5.1 The `queryable` flag

`get_dataset` returns, for every resource, whether `query_resource` will succeed
and — when it will not — why, in plain language:

- `"format 'PDF' is not tabular"`
- `"resource is 812 MB, over the 50 MB limit"`
- `"resource URL is unreachable"`

The caller learns this before attempting a call rather than by consuming an error.
This is the highest-leverage single item for the ≥95% tool-call success metric.

Determining `queryable` must not itself be expensive. It is computed from catalog
metadata alone — declared format and declared size — except that a resource with no
declared size gets a HEAD request, performed concurrently across the dataset's
resources and subject to a dedicated 5-second HEAD budget. This is deliberately
tighter than the 15-second general upstream timeout in §8, because `get_dataset` must
stay responsive and a size probe is not worth waiting on. If the HEAD does not resolve
within that budget the flag is reported as `"unknown"` with a reason, never as a
blocking wait.

### 5.2 DataStore response hygiene

CKAN's DataStore returns a `_full_text` column containing a PostgreSQL tsvector —
on a sampled data.gov.au resource this was several hundred characters of lexeme
noise per row. Returned to a model it would dominate every preview response and
consume caller context for zero information.

The CKAN client strips `_full_text` and normalises `_id` at the transport boundary.
No service above it ever observes these columns.

### 5.3 Tool descriptions are a deliverable

Tool descriptions are the prompt, and are specified as part of the work rather than
written incidentally. `query_resource`'s description must state that the dialect is
DuckDB, that the table is always named `t`, that only a single `SELECT` is permitted,
and must carry one worked example. A model that guesses wrong costs a round trip,
and the success metric is measured in successful first calls.

## 6. The query layer

### 6.1 Materialisation

The cached artifact is a Parquet file derived from the downloaded resource, not the
raw download. CSV sniffing — delimiter, encoding, header detection, type inference —
is the slow and failure-prone step; caching the raw file means paying it on every
query. Converting once yields typed columns, a schema readable without re-parsing,
and a substantially smaller cache footprint.

Pipeline for `query_resource`:

1. Resolve `resource_id` to resource metadata via the CKAN client (metadata cache, 1 hour TTL).
2. Guard: format is tabular (CSV, TSV, XLSX); declared size, where present, is ≤ 50 MB; URL passes the SSRF checks in §6.4. A missing or zero declared size is **not** treated as passing the check — it defers to the streaming ceiling in §6.4, which is the only authoritative enforcement point.
3. Check the materialisation cache for `portal + resource_id + last_modified`.
4. On miss: HEAD, then stream the download to a temporary file under a hard byte ceiling.
5. Convert to Parquet via DuckDB (`read_csv_auto` with lenient options, or the excel extension for XLSX) and store it in the cache.
6. Execute the query per §6.2.

Including `last_modified` in the cache key means an upstream refresh invalidates
itself rather than silently serving stale rows.

Malformed sources are handled leniently at conversion time (`ignore_errors`,
explicit encoding fallback to `latin-1` where UTF-8 decoding fails). If conversion
still fails, the tool returns `ResourceNotQueryable` with the parser's message and
the original download URL.

### 6.2 Execution, and why the order is the security control

```python
con = duckdb.connect()                                    # in-memory
con.execute("CREATE TABLE t AS SELECT * FROM read_parquet(?)", [path])
con.execute("SET enable_external_access = false")         # no further file or network access
con.execute("SET lock_configuration = true")              # and it cannot be re-enabled
# only now is caller SQL executed
```

The data must be loaded **before** external access is revoked, because revoking it
also disables `read_parquet`. Reversing these two steps produces either a server
that cannot read its own cache or one that leaves file access open to caller SQL.
A test asserts both properties on the same connection: `t` is queryable, and a query
attempting to read a local path fails.

A fresh connection is created per query. `memory_limit` is set from config.

### 6.3 SQL validation

Validation is performed by parsing with `sqlglot` (`read="duckdb"`). Regex-based
validation is rejected: it loses to comments, string literals and nested statements.

Rules, each of which produces a named `SqlRejected` reason:

- Exactly one statement.
- The statement is a `SELECT` (a CTE whose body is a `SELECT` is permitted).
- No DDL or DML anywhere in the tree.
- No `ATTACH`, `COPY`, `PRAGMA`, `INSTALL`, `LOAD`, `CALL`, `EXPORT`.
- No function from the denylist: `read_csv`, `read_csv_auto`, `read_parquet`, `read_json`, `glob`, `parquet_scan`.
- No table reference other than `t`, other than names bound by a CTE in the same query. `WITH x AS (SELECT * FROM t) SELECT * FROM x` is permitted; `SELECT * FROM other_table` is not. Resolution is done against the set of CTE names collected from the parsed tree, not by name matching.

Row limits are enforced by wrapping, not by rewriting the caller's query:

```sql
SELECT * FROM (<caller sql>) LIMIT <n + 1>
```

If `n + 1` rows are returned, `truncated` is `true` and only `n` are sent. No AST
surgery is performed on caller SQL.

### 6.4 Download safety

- Scheme must be `http` or `https`.
- The URL must appear verbatim in the catalog metadata already retrieved for that resource. Caller-supplied URLs are never fetched.
- Resolved IP addresses are checked against private, loopback, link-local and metadata-service ranges, for every hop.
- Redirects are followed manually with re-validation at each hop, to a maximum of 5.
- HEAD precedes GET, but `content-length` is treated as a hint only. The byte ceiling is enforced during streaming, and the download is aborted the moment it is exceeded.
- Download timeout is 30 seconds; total per-resource budget is 60 seconds.

### 6.5 Preview

`preview_resource` returns columns with types, sample rows, and a total row count.

- Resource is DataStore-backed (`datastore_active: true`): answered from `datastore_search`, no download. `source: "datastore"`.
- Otherwise: materialised via §6.1, schema and sample read from the Parquet. `source: "materialised"`.

Because materialisation is cached, a preview followed by a query costs **one**
download in total, not two. The expensive preview warms the cache for the query
that follows it.

Known and accepted wrinkle: types reported from the DataStore are CKAN's declared
types, while types reported after materialisation are DuckDB's inferred types.
These can diverge for the same resource. The `source` field tells the caller which
engine reported them; this is labelled rather than hidden.

### 6.6 Caching

Two caches, both behind a `Cache` protocol in `cache.py`:

| Cache | Key | TTL | Bound |
| --- | --- | --- | --- |
| Metadata | `portal + action + params` | 1 hour | 5,000 entries, LRU |
| Materialisation | `portal + resource_id + last_modified` | 24 hours | 1 GB on disk, LRU eviction |

The protocol exists so that a stateless deployment can later substitute a
memory-plus-tmpfs implementation without touching any service. This is the concrete
content of the "deploy-ready but not deployed" decision.

## 7. Australian utility tools

Both are pure functions with no I/O.

- `validate_abn`: the ATO's published modulus-89 checksum — subtract 1 from the first digit, apply the weighting `[10,1,3,5,7,9,11,13,15,17,19]`, sum, and require divisibility by 89. Returns validity, the normalised `NN NNN NNN NNN` form, and on failure a reason distinguishing wrong length, non-digits, and checksum failure.
- `au_financial_year`: maps a date to the Australian financial year, which runs 1 July to 30 June. Returns the label (`FY2025–26`), start date and end date.

These exist to demonstrate deterministic computation where a model would otherwise
guess. That is the entire justification, and it is why only two are kept.

## 8. Non-functional requirements

| Area | Requirement |
| --- | --- |
| Performance | Cached query < 200 ms; DataStore preview < 1 s; upstream calls 15 s timeout |
| Caching | Per §6.6 |
| Download limits | ≤ 50 MB per resource, 30 s download timeout, 60 s total per-resource budget |
| SSRF protection | Per §6.4 |
| SQL safety | Per §6.2 and §6.3 |
| Attribution | Structural, per §4.3 |
| Observability | Structured JSON logs: tool name, arguments digest, duration, upstream status, cache hit or miss |
| Quality | ≥ 80% coverage on `services/`, enforced as a CI gate; lint, format and typecheck all gating |

## 9. Testing

### 9.1 No network in the default suite

CKAN responses are recorded to `tests/fixtures/ckan/` as real captured JSON and
replayed with `respx` at the httpx transport layer, so the client's retry, timeout
and error-mapping code is exercised rather than mocked away.

`scripts/refresh_fixtures.py` re-records fixtures from live endpoints. This keeps
them from rotting and establishes that they came from the real API.

### 9.2 Upstream drift has its own lane

A small number of tests marked `@pytest.mark.live` hit real endpoints and assert
only the shape this project depends on:

- `datastore_search_sql` remains callable anonymously.
- `datastore_active` remains present on resource metadata.
- data.sa.gov.au remains CKAN at `/data/api/3/action/`.

Excluded from pull-request CI, run on a weekly schedule. This separates "my tests
pass" from "the thing still works" without making every pull request hostage to a
third party's uptime.

### 9.3 Security tests are first-class

Table-driven, not incidental.

SSRF rejection table: `file://` scheme; `http://169.254.169.254/`;
`http://localhost:8080`; `http://10.0.0.1/`; a public URL that redirects to a
private address; a URL not present in the catalog metadata.

SQL rejection table: `DROP TABLE t`; stacked statements; `ATTACH`;
`read_csv('/etc/passwd')`; a second statement concealed behind a comment; a
reference to a table other than `t`; `PRAGMA` and `INSTALL` forms.

Connection invariant: on a single prepared connection, `SELECT * FROM t` succeeds
and a local-file read fails.

### 9.4 Integration

One test boots the composed ASGI app and exercises `/mcp`, `/api/datasets` and
`/health`, guarding the lifespan propagation described in §4.4.

### 9.5 Tooling and CI

`uv` for dependency management; `ruff` for lint and format; `mypy` over `services/`
and `ckan/`; `pytest` with `--cov-fail-under=80` scoped to the service layer.

GitHub Actions on Python 3.12: sync, `ruff check`, `ruff format --check`, `mypy`,
`pytest`. A separate weekly workflow runs the `live` marker.

Implementation is test-first throughout: red, green, refactor, per behaviour.

## 10. Success metrics and how each is proven

| Metric | Proof |
| --- | --- |
| ≥ 95% tool-call success over 20 questions | `evals/questions.md` lists 20 questions with the tool sequence each should produce. `evals/run_eval.py` asks them of a real model through Claude Code's headless mode, with this server as its only tool source, and checks the tool sequence; answers are graded by hand. The results table and run date go in the README and `evals/questions.md`. |
| 3 reproducible demo scenarios | Catalog search, an aggregate query over a real resource, and ABN validation. Written into the README with exact prompts. |
| Local setup under 5 minutes | README quickstart: clone, `uv sync`, run, plus the Claude Desktop config block to paste. |
| ≥ 80% service-layer coverage | CI gate, not a claim. |

## 11. Risks

| Risk | Mitigation |
| --- | --- |
| Malformed CSVs | Lenient `read_csv_auto` options and an encoding fallback; on failure, `ResourceNotQueryable` with the parser message and the original link |
| Broken resource links | HEAD before download; `queryable: false` surfaced in `get_dataset` |
| Upstream rate limits, unknown | Caching, exponential backoff, a concurrency cap on fan-out |
| Large resources | 50 MB ceiling enforced during streaming; metadata returned with the download link |
| Licence variation | Licence returned structurally on every result; README states user responsibility |
| DataStore and DuckDB type divergence | Labelled via the `source` field rather than hidden (§6.5) |
| FastMCP lifespan regression | Integration test in §9.4 |
