# Electricity dashboard: partial refresh operational contract

Status: documents the `feature/dashboard-partial-refresh` implementation
in September 2026. Implementation and PR preparation are authorized; **production
promotion/publication is not authorized by this feature task**. No live refresh or
deployment acceptance run of this feature has been performed yet. Existing frozen
exports are not rewritten as part of implementation. Offline fixtures are evidence
of contract behavior, not evidence that the feature is serving production.

This document is the current contract for partial refresh and supersedes the older
recent all-or-nothing/no-null and shared recent/history cutoff rules. Source IDs,
units, licensing and historical exceptions remain in [recent data](german_electricity_data.md),
[daily history](german_electricity_history.md) and [monthly trade](electricity_trade.md).
Publication guards and deployment recovery remain in [the publication runbook](dashboard_publication.md).

## Why: 13 September 2026 price regression

The motivating source regression is missing SMARD DE–LU day-ahead prices for
**13 September 2026**, represented by explicit null observations. Under the old
common-complete-window rule, a price gap could hold generation and load back too,
or fail the whole refresh once inside the selected window. The new regression
fixtures model that day as 24 null hourly prices while healthy energy advances.
This records the observed source-gap reason, not a claim about an established
upstream root cause or a live recovery test. Missing prices are neither zero prices
nor negative-price hours; no interpolation or substitute market area is introduced.

## Approved durable-state exception

The validated Git-tracked recent export is now an **authoritative per-component
last-good input**, alongside the existing durable history and trade snapshots.
This is an approved exception to the default stateless-export architecture, not
merely a change-detection cache. Retention requires those previous bytes; an expiring
Actions artifact or a runner cache is not the source of truth.

- Every run still creates and discards its own temporary DuckDB database. No
  persistent database, raw-response archive or new service is introduced.
- Successful recent components are reconstructed from bounded live source windows.
  Failed components retain only previous values at matching timestamps in the new
  display window. Expired timestamps disappear; newly uncovered hours are null.
- Successful explicit nulls replace old numbers. Retention is for failed acquisition,
  not a rule to fill every newly reported null with an older value.
- History retains dates outside the current-year 35-day correction window; trade
  retains months outside its current-year three-completed-month correction window.
  Closed years require explicit reconciliation, including at rollover.
- Identical source inputs, prior exports, reporting date and semantic statuses
  produce identical bytes. The prior export is now part of reproducibility inputs.
  Status changes can legitimately change a hash even when numeric rows do not.

## Recent JSON v2

`frontend/src/_data/germanElectricity.json` keeps all [v1 base fields](german_electricity_data.md#exact-frontend-contract-schema-versions-1-and-2),
column order, units and canonical encoding. `schema_version` is `2`; it adds exactly
`components` and `refresh_status`. V1 remains readable with its strict numeric rows.
The producer emits v2 on a successful new recent run; no bulk migration is required.

Rows retain a complete, ascending hourly timestamp grid over **30 Berlin calendar
days** (719/720/721 hours with DST). Every measurement is a finite number or JSON
`null`; timestamps and fields may not be omitted. Real zero remains numeric zero.
`window_start` is inclusive and `window_end` exclusive Berlin midnight, expressed
as canonical UTC seconds with `Z`; `data_through` still equals `window_end`.
That top-level boundary describes the aligned grid, **not the latest numeric
observation of every source**.

### Component fields and statuses

`components` has exactly the 13 measurement keys in `columns`: eleven generation
sources, `load`, and `price`. Each object has exactly:

| Field | Contract |
| --- | --- |
| `status` | `complete`, `partial`, `stale` or `unavailable`, as below |
| `known_hours` | Integer count of non-null values for this component in the exported 30-day grid |
| `expected_hours` | Integer equal to the number of exported hourly rows, including DST |
| `last_successful_window_end` | Exclusive Berlin-midnight cutoff of the last structurally valid acquired source window; canonical UTC timestamp, or null if never successful |
| `source_observed_through` | Exclusive end of the latest numeric hourly observation in that successful source window; canonical UTC timestamp, or null if that window had no numbers |

These are source coverage boundaries, **not fetch/check wall clocks**. In particular,
`source_observed_through` can precede `last_successful_window_end`, and neither proves
there are no internal gaps. Coverage counts describe the displayed window; retained
observation metadata can describe an older window even after its values age out.

| Status | Meaning |
| --- | --- |
| `complete` | Acquisition succeeded and every aligned output hour is numeric |
| `partial` | Acquisition succeeded, but some aligned hours are null, including explicit source nulls or hours outside this component's own window; all-null is permitted |
| `stale` | Acquisition failed and a validated successful prior component exists; retain its metadata and timestamp-aligned values, even if no retained numbers remain in the display window |
| `unavailable` | Acquisition failed with no successful prior component; every output value and both cutoffs are null |

A successful all-null component is `partial`, not `unavailable`: it has a successful
structural cutoff, zero known hours and a null observation cutoff. A failed component
may be `stale` even if its retained grid is still numerically complete. Thus status,
coverage and age must all be presented rather than inferred from one another.

Validators require exact series/metadata keys, coverage counts matching rows,
status/completeness consistency, valid midnight success cutoffs no later than the
grid end, and hourly observation cutoffs no later than success. If numeric values
are displayed, the observation cutoff must equal the end of the last numeric row.
Without displayed numbers, a retained non-null cutoff must be at or before the
grid start (the observation has aged out). An unavailable component cannot claim
observations. Existing corrupt or inconsistent exports are hard errors.

### Acquisition and alignment

`partial.py` fetches the same preceding **35 calendar days**, excluding today's
partial day, for the existing series allowlist. Each series independently selects
the latest structurally reported day from `as_of - 1` through `as_of - 4`.
Explicit null slots count as reported; omitted timestamps do not. Once selected,
every timestamp in that component's 30-day window must exist. An internal omission
fails that component rather than shifting the window again to conceal the hole.

The shared display end is the latest successful component cutoff or previous
validated snapshot end, whichever is later. A source cutoff regressing behind that
component's previous success is isolated as a source failure. Failed components
are aligned by timestamp, never by row index; retained export GW values survive the
temporary staging conversion unchanged in the final rows.

If all recent components fail but a validated previous snapshot exists, its window
and rows can remain while statuses become stale. History and trade can still run.
Without any successful component or validated previous recent snapshot there is no
valid display boundary: fail. Standalone recent can initialize from at least one
valid source; coordinated refresh requires an existing validated bundle.

## Embedded history/trade refresh status

Recent v2's `refresh_status` contains entries for `history` and `trade` after a
coordinated run. Each entry has exactly `status` and `data_through`:

| Entry | `data_through` | Status assessment |
| --- | --- | --- |
| `history` | Inclusive `YYYY-MM-DD`, exactly manifest `last_date` | `partial` if the latest partition contains any null price or energy value; otherwise `ok` |
| `trade` | `YYYY-MM`, exactly trade `last_month` | `partial` if any row in the as-of calendar year has `missing_series`; otherwise `ok` |

A caught `ComponentUnavailable` overrides that assessment with `stale`, retaining
the original component export and its cutoff. `ok` means the assessed data has no
such gaps, not that the job ran today or that every historical year is complete.
These component-level statuses use `ok/partial/stale`, not recent's four-status enum.
The schema accepts an empty/subset status object for standalone compatibility;
standalone recent carries previous entries forward without refreshing history/trade.

Both metadata objects participate in the recent semantic content hash; the only
excluded fields remain `content_hash` and `snapshot_created_at`. An unchanged
semantic snapshot preserves its original bytes, mtime and creation timestamp.
Error messages and check times stay in operational logs, not timestamp-only commits.

Build checks require embedded cutoffs to match their actual history/trade files.
HTML embeds escaped non-executable JSON in `electricity-component-data`; build and
public verification compare it with the intended recent component/status metadata.
The component report is available without JavaScript. Visible report labels, status,
coverage and cutoff markers are also checked by the verifier; see the
[frontend offline release gate](../frontend/README-dashboard.md#offline-partial-refresh-release-gate)
for its markup checks and limitations. Browser warnings include
non-complete/non-ok statuses and recent component observation age over 96 hours;
snapshot creation time must not masquerade as freshness or scheduler health.

## Daily history partitions v2 and independent cutoff

The history **manifest remains v1**. Individual partitions may be v1 or v2, with
the same fields, source metadata, calendar rows, GWh units, price zones, canonical
bytes and raw-byte SHA-256 naming. V2 allows explicit null daily energy/price
observations beyond the narrowly allowlisted v1 gaps. Missing required date slots
still fail acquisition; null permission does not invent missing days.

Closed v1 partitions keep their exact bytes and historical policies. Current-year
refresh against recent v2 emits a v2 partition. Existing v2 partitions retain that
contract during explicit reconciliation even when no recent snapshot is needed.
Historical price-zone rules, the four initial 2015 price nulls, and justified
post-shutdown nuclear zeros/flags remain enforced; nullable is no license to
fabricate nuclear generation. Frozen annual supplements complete annual charts
only and never fill daily cells.

The coordinator requests history with an independent day cutoff. Standalone history
also uses this nullable/independent availability policy with a v2 recent input;
a v1 input retains the legacy recent-bound cutoff behavior. For routine v2 refresh,
daily source timestamps select the latest reported current-year day within
`as_of - 1` through `as_of - 4`, independently of recent price/energy availability.
All required current daily series must report that boundary, with nulls allowed.
The correction window ends there and remains 35 days, clipped to 1 January.
No available boundary or a source coverage regression retains history as stale in
the coordinator. Missed correction windows, unfinished closed years and invalid
as-of/state are hard errors requiring the existing explicit recovery procedure.

Recent and history may end on different dates with recent v2. Compare only dates
shared by their declared ranges, and each series only where the daily value and
all expected hourly values are numeric. An uncheckable comparison is skipped,
never treated as zero or proof of equality. Available retained stale values are
still checked. Contradictions raise `ConsistencyError` and block the bundle:

- Energy tolerance: `(hours + 1) * 0.005 / 1000 + 1e-8` GWh.
- Price tolerance: `0.011` EUR/MWh against the hourly mean.

Calendar continuity, actual 23/24/25-hour days, hashes and metadata remain mandatory.
Frontend v1 pairs still require equal cutoffs and overlap; v2 permits independent
ranges, including no shared dates. Daily source aggregates remain no guarantee of
complete underlying quarter-hour/hour observations.

## Monthly trade v2

Trade keeps its v1 root/row fields and checksum encoding. V2 permits newly reported
null gross series-months; `missing_series` lists the exact missing gross IDs, sorted
and unique. If **any required gross input is missing, all three monthly totals**
(`imports_gwh`, `exports_gwh`, `net_exports_gwh`) **are null**. Never sum incomplete
counterparts into a purported total. Omitted required monthly slots still fail;
the existing documented startup-gap recovery/structural-zero exceptions remain.

The producer emits v2 when new gaps require it or when retaining an existing v2
snapshot. A healthy v1 snapshot need not migrate just because the code supports v2.
V1 readers' historical missing-series restrictions remain in v1 validation.
Closed-year rows are retained; the three-completed-month current-year window,
bounded startup fallback and explicit rollover reconciliation remain unchanged.

Trade still stops at the last completed calendar month covered by validated
history, capped before the as-of month. Daily nulls do not erase calendar coverage.
Retained stale history can constrain trade, but a history source error does not
prevent a trade attempt. Optional official net is checked only with available net
and complete gross inputs. New contradictions remain hard `ConsistencyError`s;
the known historical discrepancies/tolerance are not broadened.

## Null-safe frontend metrics

The shared helpers validate v1/v2 before aggregation. Numeric zero and negative
prices remain valid. Missing values display as `–` or chart gaps, never coerced
zero, `NaN` or infinity. Coverage always belongs to the selected period.

- **Recent generation:** any missing generation input in the selected period
  suppresses total generation, its overall average, renewable share and all mix
  shares. Per-source sums/means may use known hours, explicitly labelled as partial
  sums with each source's known/expected hours. Every stacked source has a gap at
  an incomplete-generation hour; healthy load/price charts remain independent.
- **Recent load:** sum known GW × duration; average over known-load duration;
  extrema use known values. An entirely missing load period yields null metrics.
- **Recent price:** `sum(price × known interval duration) / known-price duration`.
  Min/max and negative-hour counts use known prices only and report coverage.
  All-missing mean, extrema and negative count are null, not zero. A 24-hour null
  price day leaves 144/168 known hours in a normal seven-day view.
- **Daily generation:** YTD/year summaries use the same complete-generation days
  for all generation sources, mix and shares, explicitly reporting partial sums
  and covered days/hours. Load gaps alone do not exclude a generation day. No
  complete generation day means null generation/mix amounts and averages.
- **Daily load and price:** independently use known-load/known-price days and their
  actual durations. Price is `sum(daily mean × day hours) / known-price hours`;
  all-missing mean and negative-day count are null. Negative daily means do not
  establish negative hourly counts or hourly extremes.
- **Long-term annual generation:** suppress the annual total/mix/shares if required
  generation days are incomplete, except the separately frozen official 2016/2018
  annual supplements. Load-only gaps do not suppress generation trends.
- **Trade periods:** retain monthly gaps; any missing included month suppresses
  all annual/YTD trade totals. Report `complete_months/months` and label partial
  calendar years separately. Known YTD months are not extrapolated to a full year.

## Coordinated CLI, failure boundary and recovery

From `pipeline/`, with the existing Python 3.11 dashboard runtime and `PYTHONPATH=.`:

```bash
PYTHONPATH=. python -m src.data_pipelines.dashboards.german_electricity.refresh
# Optional explicit date and an existing coherent alternate bundle:
PYTHONPATH=. python -m src.data_pipelines.dashboards.german_electricity.refresh --as-of 2026-09-15 --output /existing/bundle/recent.json --history-directory /existing/bundle/history --trade-output /existing/bundle/trade.json
```

Defaults are the tracked recent, history directory and trade paths documented above.
The coordinator validates all existing inputs before fetching, copies them to a
temporary staging directory, then attempts recent → history → trade. A shared
failure is collected while independent stages are still attempted; any such error
aborts promotion. The resulting bundle is checked again before destination writes.

| Failure | Result |
| --- | --- |
| Recent transport, malformed/empty payload, invalid index, duplicate, omitted internal timestamp, exhausted source budget or regressed source coverage | Isolate affected series; retain validated previous values as stale, or unavailable if none |
| History/trade upstream acquisition or required-source-row failure (`ComponentUnavailable`) | Retain original component export bytes; mark embedded status stale; allow healthy stages to proceed |
| Invalid existing exports, schema/hash error, available overlap/net contradiction, missed correction/rollover guard, dbt/shared transformation, filesystem/lock/race or other shared validation failure | Hard failure; abort staged bundle promotion |
| Frontend test/lint/build or publication guard failure | No data push; local refresh outputs are not a deployed site |

Promotion locks destination directories in stable order and rechecks original recent,
manifest and trade bytes. It writes immutable history blobs/manifest, trade, then
recent with embedded statuses last. Handled write failures restore advanced original
files and remove newly introduced history blobs; old versions are preserved until
promotion completes. Afterwards retain the new and immediately preceding current-year
history references. Cleanup errors are deferred. This is not a cross-file crash or
power-loss transaction: a killed process can leave a mixed local bundle, which must
be inspected/restored and revalidated before publication. Git/build gates and full
site deployment remain the external publication boundary.

The existing standalone recent command, `history refresh/backfill` and
`trade refresh/backfill` remain supported for diagnostics, initialization and
explicit reconciliation. They publish only their own local output and do not
coordinate rollback or update all embedded statuses. After targeted recovery,
run the coordinated command against the coherent bundle to recompute statuses and
cutoffs before frontend checks/publication; stale embedded metadata is not repaired
by editing JSON. On entry only, the coordinator permits a validated standalone
history/trade export to be ahead of its old embedded status cutoff. It still rejects
metadata ahead of actual data, corrupt inputs, invalid overlap or trade/history
coverage. The final staged bundle requires exact status cutoff equality. Standalone
component source failures still exit nonzero.

On the next **bounded** coordinated run, recovered recent sources replace retained
values and recalculate status/coverage/cutoffs; history/trade retry their normal
correction windows and replace stale statuses with `ok` or `partial`. Source nulls
can remain `partial` until supplied, or age outside the correction/display window.
Older unresolved history/trade gaps then require explicit selected-year reconciliation.
An unchanged repeated outage does not advance a check clock or create a new commit.
Inspect JSON metrics/Actions component summaries for source errors; a successful
degraded refresh is not proof all sources are healthy. For hard failures, investigate
and restore a coherent validated bundle or reconcile the explicitly affected year;
never loosen tolerances or overwrite corrupt originals merely to resume publication.

## Runtime, verification and rollout

The schedule stays **06:00 UTC daily**, cron `0 6 * * *` (07:00 MEZ / 08:00 MESZ).
No extra polling, scheduled retries, annual refresh or monthly/manual progress
refresh is added. `partial.acquire` has **at most three workers**, each fetching
one series' index and required weeks sequentially. It retains the existing recent
78–91 normal request budget and **273 attempts**, 240-second fetch deadline,
256 KB/response and 24 MB total bodies. History retains its typical 27 requests,
200-attempt/240-second/8 MB bounds; trade retains its typical 46 requests and
50-attempt/15-second/2 MB routine bounds. Retry/backoff rules remain bounded.

Completing healthy stages after a source fails can cost more than the old early
abort. Staging/copying, full-bundle validation and additional offline regression
tests also add runtime. These are not measured live feature costs yet. Preserve
the ten-minute production job budget including the 240-second public verifier,
the separate ten-minute sync cap, and the 120-second recent dbt timeout. Component
deadlines are not additive entitlements beyond the job cap. Measure cold/warm runs
after authorized rollout. The [existing runtime/backlog notes](dashboard_refresh_improvements.md)
remain deferred; partial refresh does not implement their optimization proposals.

Offline regression coverage is in `pipeline/tests/test_german_electricity_partial.py`
and `frontend/tests/electricity-partial.test.js`, alongside existing electricity,
workflow and public-verifier tests. Fixtures cover September 13 null prices,
zero/negative values, all-null DST windows with real temporary dbt, timestamp-aligned
retention/recovery, source failure isolation, independent cutoffs, null monthly
totals, hard contradictions/shared errors, no-change bytes and handled rollback.
Cross-language fixtures pass Python-produced candidates to frontend validators.
Additional recovery fixtures cover standalone repair followed by coordinator status
reconciliation, regressed daily source retention, and closed v2 December reconciliation
while preserving closed v1 partitions.
These describe offline test coverage, not live source or deployment acceptance.

Required offline checks for implementation review (each in its indicated directory):

```bash
# pipeline/
PYTHONPATH=. python -m unittest discover -s tests -p 'test_german_electricity*.py' -v
# repository root
python -B -m unittest discover -s scripts/tests -p 'test_*.py' -v
# frontend/ (Node 20)
npm test -- --runInBand
npm run lint
npm run build
```

The additional frontend `npm run test:v2` gate injects nullable recent/history/trade
fixtures into a disposable copy, runs the full frontend tests/lint/build and checks
built public HTML/data with the verifier. It checks that original frontend files
remain unchanged. This is offline built-output verification, not a live deployment;
it also requires the existing Python runtime for the verifier. Frontend CI runs this
gate even while tracked exports remain v1, so v1 fixtures cannot hide a broken first
v2 production refresh. It is not added to every daily publishing/sync job.

Manual code promotion must put compatible producer, consumers, workflow and verifier
on **both `main` and `releases/cloudflare`** before the main schedule relies on the
new released `.refresh` entry point. Follow the runbook's real-ancestry reconciliation
and non-force fast-forward promotion, preserving newer production snapshots. Main-only
PR integration does not release the feature. V1 compatibility permits code promotion
without rewriting frozen exports; future bounded refreshes migrate only eligible
data. After explicit production authorization, record a live partial/recovery run,
public component status/coverage verification, no-change behavior, both job runtimes
and separate main-sync result. Existing September 10/14 runs predate this feature
and cannot substitute for that pending acceptance.
