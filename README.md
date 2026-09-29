# au-opendata-mcp

An MCP server that gives an AI client direct access to Australian government open data.
Search the catalogue, inspect a file's schema, then run real SQL over it — with the source
URL, publisher and licence attached to every answer.

Covers [data.gov.au](https://data.gov.au) (the federal catalogue) and
[data.sa.gov.au](https://data.sa.gov.au) (South Australia). Both run CKAN; `list_portals`
reports each portal's current dataset count.

## Quickstart

```bash
git clone https://github.com/<your-username>/au-opendata-mcp   # replace <your-username>
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

This runs the server over stdio, this server's default transport when `--transport` is not
passed (see `main.py`). Restart Claude Desktop after editing the config.

## Tools

| Tool | What it does |
|---|---|
| `list_portals` | The portals available, with dataset counts |
| `search_datasets` | Keyword search; one portal or both |
| `get_dataset` | A dataset's metadata and files, each flagged queryable or not |
| `list_organizations` | The publishers on a portal, with how many datasets each has |
| `preview_resource` | A file's columns, types, sample rows and row count |
| `query_resource` | A single DuckDB `SELECT` over the file, exposed as table `t` |
| `validate_abn` | ABN checksum, per the ATO algorithm |
| `au_financial_year` | Maps a date to the Australian FY (1 July - 30 June) |

The intended flow is `search_datasets` → `get_dataset` → `preview_resource` → `query_resource`.
You cannot write SQL for columns you have not seen, so `preview_resource` comes first.

Every tool has a matching REST endpoint under `/api` (`list_organizations` -> `GET
/api/organizations`, `query_resource` -> `POST /api/resources/{resource_id}/query`, and so
on), and both interfaces share one service layer — see `src/au_opendata_mcp/api.py` and
`src/au_opendata_mcp/mcp_server.py`.

## Design notes

**One SQL dialect, always.** CKAN's DataStore is PostgreSQL and the fallback engine is DuckDB.
Routing queries to whichever happens to be available would mean the same tool silently
changing dialect between datasets — different function names, different casting, different
date handling — with no way for the caller to know in advance. So every SQL query runs in
DuckDB. The DataStore is used only for download-free schema and sample retrieval.

**The query sandbox.** A resource is materialised to Parquet once, then loaded into a fresh
in-memory DuckDB. Only *after* the table exists is `enable_external_access` revoked and the
configuration locked — that order matters, because revoking access first also disables the
Parquet read that loads the table. Caller SQL is parsed with `sqlglot` and must be a single
`SELECT` over the table `t`; row caps are enforced by wrapping the query in an outer `SELECT
... LIMIT`, never by rewriting it. Queries also run under a timeout and are executed off the
event loop (`asyncio.to_thread`), so one slow or runaway query cannot stall every other
concurrent request.

**Outbound fetches are constrained.** Only URLs the portal itself published for that resource
are fetched, http(s) only. Every resolved address is checked for global routability (not just
private/loopback/link-local, but multicast and CGNAT ranges too). On the download path that
check runs again at every redirect hop. `get_dataset`'s HEAD size probe runs the same check
before the one request it makes, and deliberately does not follow redirects — there is no
second hop to check, and an unfollowed redirect just leaves the size unresolved rather than
reaching an address nothing validated. A 50 MB ceiling is enforced while streaming the
response body — never trusted from the `content-length` header, which is just a hint.

**Attribution is structural.** Every result model carrying portal data inherits a base
(`Sourced`) with `dataset_url`, `organisation` and `licence`. There is no un-sourced result
type to return.

## Security notes

This is a self-hosted tool, not a multi-tenant service, and its threat model is scoped
accordingly: the operator trusts the two configured portals but not the SQL or URLs an AI
client sends it. A few things worth knowing before you rely on it:

- **DNS TOCTOU in the download guard.** The SSRF check (`assert_safe_url`) resolves a
  resource's hostname itself and rejects it if any resolved address is not globally routable.
  But httpx then resolves the same hostname again, independently, when it actually opens the
  connection — and a hostile authoritative DNS server can answer differently between those two
  lookups (classic DNS rebinding). Re-validating on every redirect hop narrows this window —
  a long redirect chain gives an attacker more chances, not fewer, since each hop is checked
  again — but it does not close it. Closing it properly needs a custom transport that connects
  to the already-validated IP while still presenting the original hostname for TLS SNI, which
  was judged disproportionate for a tool that, in practice, only ever downloads from URLs a
  known government CKAN instance published. See the docstring on `Materialiser._download` in
  `src/au_opendata_mcp/services/materialise.py` for the full reasoning.
- **Caller SQL is parsed, not pattern-matched**, so comments, string literals and stacked
  statements cannot smuggle a second statement past the check — but the parser
  (`sqlglot`) is still trusted code. A parser bug would be a real hole.
- **The sandbox lockdown order is load-bearing.** `services/query.py::execute_in_sandbox`
  creates the table, *then* revokes `enable_external_access`, *then* locks the DuckDB
  configuration. Reordering any of these three statements either breaks the Parquet read or
  reopens the filesystem to caller SQL; this is covered by a comment and a dedicated test
  (`tests/test_query_execute.py`), not just convention.
- **The Docker image runs as root.** See the comment in the `Dockerfile` — this is a
  self-hosted tool, not a hardened production deployment, and no attempt has been made to
  build a non-root, distroless, or otherwise locked-down image.

If you spot something else, please open an issue rather than assume it was missed on purpose —
some of the above trade-offs are deliberate, but not all corners have necessarily been
checked.

## Licensing

Datasets carry their own licences, and they vary widely. Creative Commons variants are common
on both portals, but on data.gov.au roughly two-thirds of datasets specify no licence at all
(measured via the portal's own `package_search` licence facet, September 2026 — worth
re-checking rather than trusting, since it will drift). data.sa.gov.au is Creative
Commons-dominated, but it is a small fraction of the combined catalogue, so it does not set
the norm for the whole server.

Every result includes its `licence` field — treat an unspecified licence as "unknown," not
"free to use," and attributing the source when you reuse the data is your responsibility.

## Demo scenarios

Three prompts to paste into Claude Desktop once the config above is in place — catalog
search, an aggregate query over a real resource, and ABN validation. They name what the user
wants rather than which tool to call, so what you see is the tool selection a real user would
get, across the intended chain `search_datasets` → `get_dataset` → `preview_resource` →
`query_resource`.

These are scenarios to try, not a transcript. As with the evaluation below, they have not
been run and recorded here, so no output or success rate is reproduced.

**1. Catalog search.**

> Search data.gov.au for datasets about public toilets. Give me the five best matches, and
> for each one the publisher, the licence, and which file formats it comes in. Flag any
> where the licence is not specified.

The interesting part is the last sentence: roughly two-thirds of data.gov.au datasets carry
no licence, and every result model here has a `licence` field precisely so that is visible
rather than assumed.

**2. An aggregate query over a real resource.**

> On data.gov.au, find a CSV of Australian Government contract notices. Show me the columns
> first, then run one SQL query that gives me the ten agencies with the highest total
> contract value. Tell me the source URL and licence of the exact file you queried, and how
> many rows it has.

This is the full chain: `search_datasets` to find it, `get_dataset` to pick a file that comes
back `queryable: "yes"`, `preview_resource` to see the real column names — you cannot write
SQL for columns you have not seen — then a single `SELECT` through `query_resource`. The file
is downloaded once, converted to Parquet and cached, and the query runs in a locked-down
in-memory DuckDB over the table `t`.

**3. ABN validation.**

> Is the ABN 51 824 753 556 valid? And which Australian financial year does 2026-03-31 fall
> in?

`validate_abn` runs the ATO's weighted-modulus checksum (spacing and formatting are
tolerated), and `au_financial_year` maps the date to the 1 July – 30 June year — neither
needs a portal, so both answer even if data.gov.au is down.

## Evaluation

`evals/questions.md` has a 20-question tool-call evaluation harness: for each question, the
tool sequence a correct response should produce, run manually against Claude Desktop with the
server configured. It has not been run yet, so no score is claimed here — a number with no
run behind it is worse than no number. Run it yourself with the Claude Desktop config above,
then fill in the results table and this section.

## Development

```bash
uv sync
uv run pytest                  # unit tests, no network
uv run pytest -m live          # contract tests against the real portals
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Service-layer coverage (`src/au_opendata_mcp/services`) is gated at 80% in CI
(`.github/workflows/ci.yml`) and currently sits at 96%. The live contract tests run weekly
(`.github/workflows/upstream-drift.yml`) rather than per-PR, so upstream downtime cannot block
a pull request.

## Not in this version

User accounts, API key issuance, and quotas; a web frontend or dashboard; a full data mirror
or daily crawl; the ABS SDMX statistics API; the NSW, VIC and QLD portals; and actual cloud
deployment (a `Dockerfile` exists and has been built and run locally, but nothing has been
deployed to Cloud Run or any other host). See `docs/prd.md` and
`docs/superpowers/specs/2026-09-23-au-opendata-mcp-design.md` for the reasoning behind each.
