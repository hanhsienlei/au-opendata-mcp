# Tool-call evaluation

Target: >= 95% of questions produce the expected tool sequence and a correct, sourced answer.
Run manually against Claude Desktop with the server configured (see the README's "Claude
Desktop" section). Record the date and paste the results into the README.

**Model tool-selection run:** not yet run. This is the metric above — whether a *model*,
given only the tool descriptions, picks the expected sequence. It needs the server wired into
Claude Desktop and a human working through the questions. No score is claimed anywhere in this
repo until that happens.

**Server-side sequence check:** 20/20, run 2026-09-29 against the live data.gov.au and
data.sa.gov.au APIs. This is a *different and weaker* claim: it executes each question's
**expected** tool sequence directly against the service layer and checks the call succeeds and
returns a correct, sourced answer. It proves the tools work on real data today — it proves
nothing about whether a model would choose them. The two must not be conflated.

Notable from that run, and worth watching when the model run happens: for question 6, CKAN's
relevance ranking returns `national-public-toilet-map` (CSV + PDF) only **third**, behind two
ArcGIS REST services. A caller that takes the first hit lands on a dataset with nothing
queryable. Choosing correctly means reading the `formats` field in the search results — which
is why `search_datasets` returns it. This is a plausible place for a model to go wrong, so it
is worth noting which dataset it picks rather than only whether the final answer is right.

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
have said to prevent it. Tool descriptions are the prompt (docs/design.md §5.3); a systematic
failure here is a description bug, not a model bug.

_Not yet run — no failures recorded._
