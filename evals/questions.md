# Tool-call evaluation

Target: >= 95% of questions produce the expected tool sequence and a correct, sourced answer.

`evals/run_eval.py` asks these questions of a real model through Claude Code's headless mode
(`claude -p`), with this server over stdio as its only tool source: no built-in tools, no other
MCP servers, no user settings or plugins, and the one-line system prompt "You are a helpful
assistant with access to tools." Questions 6–16 run as one conversation; every other question
gets a fresh session. Questions 12 and 14 are templates, filled in with real columns of the
toilet map CSV (`Latitude`, `State`, `OpeningHours`), the way a human tester would after
seeing the preview.

A question passes when both hold:

1. **Sequence.** The expected tools are called in order (extra calls are allowed). In the
   chained questions, a `get_dataset` or `preview_resource` result already in the conversation
   counts, because fetching it again would not make the answer more right. Queries do not carry
   over: a question that needs a query run needs it run. The runner checks this.
2. **Answer.** The answer is correct and, where it reports portal data, sourced. Graded by hand
   from each run's `summary.json`.

## Results

**Model tool-selection run, 2026-10-01.** Current code: Claude Sonnet 5.5 **18/20 (90%)**,
Claude Haiku 4.5 **18/20 (90%)**. The target is not met. Each run is in `evals/runs/`:

| Runs | Server | Sonnet 5.5 | Haiku 4.5 |
|---|---|---|---|
| `125242` / `125244` | First run | 16/20 | 17/20 |
| `130244` / `130247` | After the `list_organizations` fix | 17/20 | 18/20 |
| `132207` / `132210` | After the `search_datasets` total-count fix (current) | 18/20 | 18/20 |

Haiku's Q5 passed in the second run and missed in the third, with that question's tools
unchanged in between. The model is not deterministic, so a one-question difference between
runs is noise.

**Server-side sequence check, 2026-09-29: 20/20.** Each question's **expected** tool sequence
was executed directly against the service layer, against the live data.gov.au and
data.sa.gov.au APIs, checking each call succeeds and returns a correct, sourced answer. This is
a *different and weaker* claim than the model run. It shows the tools work on real data. It
shows nothing about whether a model would choose them.

Columns below are for the current-code runs, `132207` / `132210`.

| # | Question | Expected tool sequence | Sonnet 5.5 | Haiku 4.5 |
|---|---|---|---|---|
| 1 | What open data portals can you search? | `list_portals` | pass | pass |
| 2 | What bus stop data does South Australia have? | `search_datasets(portal="data.sa.gov.au")` | pass | pass |
| 3 | Find federal datasets about rainfall. | `search_datasets` | pass | pass, see notes |
| 4 | Who publishes the most datasets on data.gov.au? | `list_organizations` | pass | pass |
| 5 | Is there ABS data about population by postcode? | `list_organizations` → `search_datasets` | **miss**: sequence | **miss**: sequence |
| 6 | Show me the files in the national public toilet map dataset. | `search_datasets` → `get_dataset` | pass | pass |
| 7 | Which files in that dataset can you actually query? | `get_dataset` (reads `queryable`) | pass | pass |
| 8 | What columns does that CSV have? | `get_dataset` → `preview_resource` | pass | pass |
| 9 | How many rows are in it? | `preview_resource` | pass | pass |
| 10 | Show me the first few rows. | `preview_resource` | pass | pass |
| 11 | Count the records by state in that file. | `preview_resource` → `query_resource` | pass | pass |
| 12 | What's the average of <numeric column> by <category column>? | `preview_resource` → `query_resource` | pass | pass |
| 13 | Which 5 categories have the most records? | `query_resource` with ORDER BY + LIMIT | pass | pass |
| 14 | Are there any rows with a missing value in <column>? | `query_resource` with IS NULL | pass | pass |
| 15 | Try to query the PDF in that dataset. | `get_dataset` → refuses, citing `queryable: no` | pass, see notes | pass, see notes |
| 16 | Delete all the rows from that table. | `query_resource` → `SqlRejected`, explained | **miss**: sequence | **miss**: sequence |
| 17 | Is 51 824 753 556 a valid ABN? | `validate_abn` → valid | pass | pass |
| 18 | Is 51 824 753 557 a valid ABN? | `validate_abn` → invalid, checksum | pass | pass |
| 19 | What Australian financial year is 23 September 2026 in? | `au_financial_year` → FY2026-27 | pass | pass |
| 20 | Compare bus stop dataset counts between the federal and SA portals. | `search_datasets(portal="all")` | pass | pass, see notes |

## Notes on failures

Tool descriptions are the prompt (docs/design.md §5.3), so a systematic failure here is a
description or tool bug before it is a model bug.

- **Q4, first run: a server bug, now fixed.** `list_organizations` called CKAN's
  `organization_list` with `limit=1000`, but CKAN caps that action at 25 rows whenever
  `all_fields` is set. On data.gov.au that meant 25 of 1,142 publishers, alphabetically from
  "3D Ecology Mapping" to "API NSW". Sonnet made five calls, then said the list looked
  truncated. Haiku made one call and confidently answered "ACT Government Open Data, 531
  datasets". The fix reads publishers from the `organization` facet of a zero-row
  `package_search`, sorted largest first. A re-run then hit a second problem: 1,012 publishers
  is more than an MCP client takes as one tool result, and Claude Code spilled it to a file the
  model could not read. `list_organizations` now takes a `limit` (default 20). On current code
  both models answer Geoscience Australia, 42,411 datasets, in one call.
- **Q20, first two runs: a missing feature, now added.** `search_datasets` returned at most 50
  summaries and dropped CKAN's `count` of total matches, so the two portals' counts could not
  be compared. Both models searched each portal with `limit=50`, reported "50 (the limit, so
  likely more)" against 5, and said the federal figure was capped. That was honest, but it did
  not answer the question. `search_datasets` now returns `total_matches` per portal next to
  `results`. On current code both models answer 469 against 5. Sonnet adds that these are
  keyword matches, and that only about 15–20 of the first 50 federal hits are really bus-stop
  datasets. Haiku calls all 469 "bus stop datasets", which overstates it; that passes, because
  the count itself is right.
- **Q16: a sequence miss with a correct answer.** Both models refused the DELETE from the
  `query_resource` description ("a single SELECT") without calling it, so `SqlRejected` never
  fired. Refusing up front is arguably better behaviour. The server-side rejection is covered by
  `tests/test_safety_sql.py` either way. Counted as a miss because the expectation was set
  before the run.
- **Q5 (both): a sequence miss with a correct answer.** Both models searched for ABS
  population data directly instead of looking up the publisher first. Both correctly said
  data.gov.au holds no postcode-level ABS population table. Its ABS population data there is by
  SA2, LGA and GCCSA, which was checked against the live portal. Haiku did call
  `list_organizations` in the previous run, which shows how much this one varies. The expected sequence may be
  over-specified: the ABS appears under four publisher names on data.gov.au, so filtering by
  any single one would miss data.
- **Q15 (both): passed, but not the expected way.** Neither model read `queryable: no` from the
  `get_dataset` result already in context. Both called `query_resource` on the PDF, and the
  server refused with "format 'PDF' is not tabular". The user asked them to try, and the
  refusal was explained, so this passes.
- **Q3 (Haiku): passed with a caveat.** data.gov.au's top "rainfall" results are Tasmanian and
  NSW state datasets. Sonnet said so, and with the new total it now reports about 1,860 matches
  before picking out the Bureau of Meteorology and CSIRO ones. Haiku called all 1,861 "federal
  rainfall datasets".
- **Q14 (Sonnet, second run):** the first `query_resource` call had empty arguments. The tool
  rejected it, and the retry was correct.
- **Not a failure, worth knowing.** In `scripts/demo.py`, asked "which state has the most
  public toilets?", Sonnet usually skipped `preview_resource` and guessed the `State` column
  from the dataset description. The guess was right, but this server's "preview before you
  query" advice is not always taken when a column name looks obvious.
