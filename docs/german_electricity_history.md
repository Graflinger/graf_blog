# German electricity: durable daily history

Implemented locally on 10 September 2026. The approved history extension stores
validated yearly JSON in Git, with rolling current-year corrections and frozen
closed years. This is an explicit exception to the default stateless-export rule
in [dashboard architecture](dashboard_architecture.md). It uses no persistent
database. The [daily publication workflow](dashboard_publication.md) now authorizes
current-year data-only updates from the release checkout, followed by public
verification and a separate validated ancestry sync to main. The release-first
implementation requires deliberate promotion to both branches and new live rollout
verification; the September 10 success covered the old both-ref design. Closed-year
reconciliation remains explicit and manually promoted.

The uncommitted [partial-refresh contract](dashboard_partial_refresh.md) adds nullable
v2 partitions, independent daily cutoffs and source-error retention in a coordinated
bundle. The manifest and closed historical v1 snapshots remain compatible; no frozen
exports are rewritten for this feature. Live feature acceptance is pending.

## Commands and update policy

Run from `pipeline/`, using the existing Python 3.11 dashboard environment:

```bash
PYTHONPATH=. python -m src.data_pipelines.dashboards.german_electricity.history backfill --start-year 2015 --end-year 2026
PYTHONPATH=. python -m src.data_pipelines.dashboards.german_electricity.history refresh
PYTHONPATH=. python -m src.data_pipelines.dashboards.german_electricity.history backfill --start-year 2018 --end-year 2018 --reconcile
PYTHONPATH=. python -m unittest discover -s tests -p 'test_german_electricity_history.py' -v
```

Both commands accept `--as-of YYYY-MM-DD`, `--output-dir PATH`, and
`--recent-snapshot PATH`. The default directory is
`frontend/src/data-history/german-electricity/`; the default recent snapshot is
`frontend/src/_data/germanElectricity.json`. Output directories are created when
needed. Future as-of dates and dates before source availability are rejected.

- **Backfill:** fetch selected missing years, as far back as 2015. Existing years
  are skipped unless `--reconcile` is explicitly supplied. Reconciliation rereads
  selected years, including frozen ones, and cannot truncate existing coverage.
  A subset backfill into an empty directory is allowed; the manifest describes its
  actual range. Adding partitions must produce one continuous date sequence.
- **Refresh:** validates all previous manifest/partition bytes first. Reads only
  the current year's daily source chunks, retains dates before the correction
  window verbatim, replaces the latest **35 completed calendar days** within that
  year, and appends newly complete days. The inclusive window is
  `[cutoff - 34 days, cutoff]`, clipped at 1 January. If the old watermark predates
  the preceding day, refresh fails with explicit backfill instructions.
- **Closed years never change during refresh, including in January.** They become
  `frozen: true` in the manifest; partition bytes stay unchanged. If 31 December
  was not captured before rollover, refresh deliberately fails. Run an explicit
  prior-year `backfill --reconcile` after source completion, then refresh. No
  automatic prior-year fetch/correction exception is hidden in routine refresh.
- `--as-of` is a Berlin run date, not an archived source vintage. With recent v1,
  current-year backfill/refresh retains the legacy recent-bound cutoff between
  `as_of - 4` and `as_of - 1`. With recent v2, routine refresh selects its own latest
  reported daily boundary within that range; explicit nulls count as reported slots.
  Current-year backfill starts from the previous-day limit with v2. Historical
  closed-year-only backfills need no recent snapshot. Use the coordinated
  [`.refresh` command](dashboard_partial_refresh.md#coordinated-cli-failure-boundary-and-recovery)
  for routine recent → history → trade staging and status updates.

The history module uses the standard library for already-daily source values:
strict parsing, date alignment, MWh→GWh conversion, historical price stitching,
validation, and deterministic export. There is no aggregation requiring another
dbt model or DuckDB staging copy. Available overlapping values are checked against
the fresh DuckDB + focused dbt hourly pipeline; daily availability is independent
under v2. Neither path opens a persistent DB.

## Official source, units, and coverage

Attribution and redistribution: **Bundesnetzagentur | SMARD.de**, **CC BY 4.0**.
Reuse the exact `SOURCE` object documented in [recent data](german_electricity_data.md).
Describe conversion to GWh, stitched price zones, and derived nuclear zeros as
modifications. Generation means net public-grid generation; load means network
load; pumped storage is discharge. Generation minus load is not a trade series.

- [Official SMARD handbook, April 2026](https://www.smard.de/resource/blob/220052/9d526adf4b948599da4a956dfae6dab9/smard-benutzerhandbuch-04-2026-data.pdf),
  “Auswahl einer Auflösung”: wholesale prices are averaged at the chosen
  resolution; other series, including generation and consumption, are aggregated.
  Daily generation/load endpoints therefore contain **MWh sums**, converted by
  dividing by 1,000 to **GWh**. Do not multiply these daily sums by 24 or divide by
  four. `hours` is 23/24/25 from Berlin midnight boundaries.
- The handbook also describes source-side chart interpolation under specified
  missing-data rules. A non-null daily value **does not prove that all underlying
  quarter-hours/hours existed**. Historical exports preserve the official daily
  values; historical underlying-hour completeness is not certified. Unknown
  partial sums/interpolation are a source limitation, not reconstructed data.
- [Official market-data configuration](https://www.smard.de/app/chart_configuration/market_data_configuration.json)
  identifies historical price ID **251**. Use `/DE` for both price series:
  **251 / DE–AT–LU** through 30 September 2018, **4169 / DE–LU** from 1 October 2018.
  The earliest available old-zone price is **5 January 2015**. Keep 1–4 January
  as explicit null prices, with the DE–AT–LU zone. Never substitute another zone.
- Nuclear ID **1224** has complete daily entries in 2015–2023, last nonzero
  **15 April 2023**, observed zero through **29 January 2024**, then null; no 2025
  or 2026 chunk. The [official nuclear shutdown account](https://www.bundesumweltministerium.de/themen/nukleare-sicherheit/aufsicht-ueber-atomkraftwerke/atomkraftwerke-in-deutschland)
  states the final three plants' operating authorizations expired at the end of
  15 April 2023. After that date only, fill absent/null nuclear with numeric zero
  and `nuclear_derived_zero: true`. Observed source zeros remain `false`.
  Any nonzero post-shutdown value fails and requires investigation. Missing
  pre-shutdown nuclear fails. The nuclear index is still checked during refresh,
  so a newly published current-year chunk is validated rather than ignored.

### Five historical missing energy values: v1 null policy

The complete live daily audit found precisely these five missing series-days.
Each was separately checked against its official weekly hourly chunk: **24
observations, all 24 null**. No hourly fallback can reconstruct them.

| Date | Series / ID | Hourly chunk timestamp |
| --- | --- | ---: |
| 2016-11-08 | other_renewables / 1228 | 1478473200000 |
| 2018-01-21 | pumped_storage / 4070 | 1515970800000 |
| 2018-08-02 | pumped_storage / 4070 | 1532901600000 |
| 2018-08-03 | pumped_storage / 4070 | 1532901600000 |
| 2018-08-23 | pumped_storage / 4070 | 1534716000000 |

**All five dates remain in the exported sequence.** In v1 only the named energy
field may be null. All other energy fields/dates require finite nonnegative numbers.
No imputation, generic missing→zero conversion, or hidden dropped days is allowed.
If the source eventually supplies these values, explicit reconciliation may store
the numeric correction. V1 rejects newly discovered gaps. V2 accepts explicit null
source observations under the [nullable partition contract](dashboard_partial_refresh.md#daily-history-partitions-v2-and-independent-cutoff);
omitted required dates and malformed values still fail source acquisition.

Read-only audit (182 daily requests + four deduplicated hourly diagnostic requests):

```bash
PYTHONPATH=. python -m src.data_pipelines.dashboards.german_electricity.audit_history --as-of 2026-09-10 --recent-snapshot /path/to/validated-recent.json
```

## Exact static JSON contract: manifest v1, partitions v1/v2

`manifest.json` has exactly:

```text
{
  schema_version: 1,
  kind: "german-electricity-history",
  timezone: "Europe/Berlin",
  source: SOURCE,
  first_date: "2015-01-01",
  last_date: "2026-09-08",
  years: [{
    year: 2015,
    url: "/data/history/german-electricity/2015.<sha256>.json",
    sha256: "<64 lowercase hex digits, hash of raw partition bytes>",
    first_date: "2015-01-01", last_date: "2015-12-31",
    days: 365, frozen: true
  }, ...],
  revision_policy: "<history.py POLICY text, including source gaps>"
}
```

Each partition has exactly the fields below. The example is frozen v1; nullable
partitions use `schema_version: 2` without changing the field layout:

```text
{
  schema_version: 1, year: 2015, timezone: "Europe/Berlin", source: SOURCE,
  rows: [{
    date: "2015-01-01", hours: 24,
    energy_gwh: {
      biomass, hydro, wind_offshore, wind_onshore, solar, other_renewables,
      lignite, hard_coal, gas, other_conventional, pumped_storage, nuclear, load
    },
    price_eur_mwh: null,
    price_zone: "DE-AT-LU",
    nuclear_derived_zero: false
  }, ...]
}
```

In v1 the energy keys above are numeric except the five narrowly allowlisted nulls;
`price_eur_mwh` is numeric except the four 2015 dates. V2 permits additional explicit
null energy/price observations, while retaining historical/nuclear policies and
the complete date sequence. Closed v1 partition bytes are retained.
`price_zone` is exactly `DE-AT-LU` or `DE-LU`; the boolean is always present.
Rows and years are ascending and contiguous, every partition starts 1 January,
and only the final partition may end before 31 December. Frozen can be true for
an unfinished final prior year; refresh then requires explicit reconciliation.

Encoding is UTF-8, minified JSON, sorted keys, finite numbers, **no trailing
newline**, no creation/check timestamps. GWh values are rounded to eight decimal
places; prices preserve the source number. SHA-256 covers the **exact raw file
bytes**, not reserialized JavaScript objects. Hash the fetched UTF-8 string/bytes
before parsing in browser/build code. Never rewrite JSON during static copying.

### Consumer rules

- Public mapping is `frontend/src/data-history/german-electricity/**` →
  `/data/history/german-electricity/**`. The frontend owns this passthrough/build
  integration. Load the small manifest and only requested yearly partitions.
- Verify raw SHA-256, schema, source, year, dates/counts, series and null policies.
  List only manifest-referenced files as current data; retained versions are not
  extra years. Do not manufacture an expected hash from the filename alone.
- History supports **days with negative daily mean prices**, not negative-hour
  counts or hourly minima/maxima. It cannot recover hourly extremes/distributions.
- Period price = `sum(known daily mean * hours) / sum(hours of known-price days)`.
  Report coverage; for 2015 this excludes four dates/96 hours, never zeros. Do not
  weight by load (that would be a different measure).
- For missing energy inputs, show gaps/incomplete coverage in derived generation
  totals or renewable shares. Aggregating available values must explicitly say
  “known values” and report coverage. A null must never become zero through JS
  arithmetic/coercion. Load is assessed independently from generation; it remains
  complete on these five historical dates. See [null-safe metrics](dashboard_partial_refresh.md#null-safe-frontend-metrics).
- Renewable sources exclude pumped storage and nuclear. Changes in price zone
  must be visible in historical methodology. Daily averages hide intraday peaks.

## Bounded acquisition and validation

```text
https://www.smard.de/app/chart_data/{id}/DE/index_day.json
https://www.smard.de/app/chart_data/{id}/DE/{id}_DE_day_{year_start_ms}.json
```

The index timestamps are annual **Berlin 1 January midnight** in UTC epoch ms.
The daily chunk is annual, not a weekly/hourly download. Reuse the original 13
series IDs, add nuclear 1224 and old price 251, select only the relevant price
zone's years (both chunks in 2018), and do not request absent nuclear chunks.

- Full 2015–2026 backfill: **15 indices + 167 annual chunks = 182 requests**.
- Typical 2026 refresh: **14 indices + 13 annual chunks = 27 requests**. The whole
  current-year chunk is downloaded because the endpoint cannot fetch just 35 days;
  dates outside the correction window are retained from validated prior history.
- Shared strict HTTP client: at most three concurrent requests, at most three
  attempts per URL with bounded transient retry/backoff, 15-second socket timeout,
  240-second fetch deadline. History hard cap is **200 total attempts including
  retries**, 8 MB total bodies, 256 KB per response. Exhaustion fails this component;
  coordinated refresh retains the original history and records stale status.
- Partition cap 250 KB; manifest cap 20 KB. The 12-year set is about 1.7 MB; this is
  an approved increase over the initial recent-only 1 MB target, served lazily.
- Validate all required daily entries, calendar continuity/leap/DST hours, types,
  finite bounds, duplicate timestamps/keys, all metadata, hashes, source gaps, and
  historical policies. Entire raw chunks are checked for malformed values and
  unexpected nonzero nuclear, including dates outside the selected export window.
- Every freshly fetched day overlapping the validated recent snapshot is compared
  per series where the daily value and all expected hourly values are numeric.
  Null/unavailable comparisons are skipped, not treated as equality. Rounding tolerance is
  `(hours + 1) * 0.005 / 1000 + 1e-8 GWh` and `0.011 EUR/MWh`. No percentage-based
  tolerance hides partial daily totals. Source revisions between recent and daily
  reads can hard-fail the entire coordinated bundle: regenerate through `.refresh`
  and investigate available contradictions; do not
  loosen tolerance. Older historical daily sums remain subject to the caveat above.

## Atomic publication, retention, recovery

The following describes the standalone history writer. The coordinated
[bundle writer](dashboard_partial_refresh.md#coordinated-cli-failure-boundary-and-recovery)
adds original-byte guards and handled rollback across recent/history/trade, with
recent component status promoted last. Source `ComponentUnavailable` retains the
original history; schema, consistency, state and storage errors abort the bundle.

A local advisory exclusive lock on the output directory inode serializes writers
(macOS/Linux). Readers do not need the lock. Cross-runner jobs must also serialize
their Git publication; filesystem locking cannot coordinate different checkouts.
Validate the prior manifest and every referenced file before requesting source
data, even for an explicit reconcile. Corrupt existing data is never overwritten.

Validate the full proposed set in memory, write/fsync new immutable
`YEAR.<raw-sha256>.json` files via same-directory temporary files, reread/validate
all referenced bytes, check the original manifest still matches, then atomically
replace `manifest.json` **last**. Existing files are never edited in place. A
handled pre-commit failure removes newly written artifacts, leaving the old set
intact. A process kill may leave unreferenced files but cannot advance a partial
manifest; this is process-failure atomicity, not a power-loss durability guarantee.

After a successful manifest replacement, remove only strictly named version files
not referenced by the new or immediately preceding manifest, within eligible years.
Routine refresh cleans only the current year according to `--as-of`; explicit
backfill/reconciliation may clean the selected years it fetched. Thus there are at
most **two versions per eligible year** after successful cleanup, not daily
accumulation. Closed years may retain both versions after reconciliation or rollover;
routine refresh never deletes either. Their cleanup waits for a subsequent explicit
manual reconciliation.
Cleanup errors are reported as deferred cleanup after successful publication.
No-change runs preserve bytes/mtime and perform no cleanup. Interrupted/failed
cleanup may leave extras until the next successful changed publication eligible to
clean that year.

Readers/builds must reload the manifest and retry a missing superseded partition;
they must not combine files from different manifests. Serve the manifest with
revalidation (`Cache-Control: no-cache`, or TTL at most five minutes), cache hashed
partitions immutably, and handle browser tabs holding a manifest across multiple
updates by reloading it on 404. Only one previous publication is retained; there
is **no indefinite stale-reader guarantee**. Git/site deployment history supplies
rollback of the complete set. Preserve the manifest and all its referenced blobs
together when restoring. Initial-backfill crash without a manifest requires an
empty alternate directory or inspected orphan cleanup, not trusting orphan data.

## Measured live acceptance, 10 September 2026

| Measurement | Backfill | Routine refresh |
| --- | ---: | ---: |
| Requests | 182 | 27 |
| Downloaded body bytes | 1,522,905 | 116,412 |
| Total time | 8.873 s | 2.349 s |
| Result | changed | unchanged (identical bytes) |
| Current exports including manifest | 1,694,558 bytes | same |

Coverage: **4,269 dates, 2015-01-01–2026-09-08**, 12 partitions, 2015–2025 frozen.
Four price nulls, five energy nulls, **953 flagged derived nuclear-zero days**.
Thirty recent overlap days checked; max energy delta **0.00006 GWh** (=0.06 MWh),
max price delta **0.00625 EUR/MWh**. The independent gap audit used 186 requests,
1,538,366 bytes including four hourly diagnostic chunks.

The tracked recent snapshot was already a slightly older source vintage:
8 September onshore wind differed by 0.00533 GWh, correctly failing validation.
An existing-hourly-pipeline run with `--as-of 2026-09-09` into an OS temporary
file provided a fresh complete comparison through 8 September (semantic hash
`0e46eedb16086672d94b8b78e052bc577f3314d1271af79cb34650f8ef014606`).
Backfill and refresh used `--as-of 2026-09-10 --recent-snapshot <that file>`.
The frozen requested cutoff is therefore 8 September; a separately checked newer
hourly run already reached 9 September. No recent frontend export was replaced by
this history task. Future operations should refresh their recent input first.

Verification: **21 history tests** passed in 1.593 s, **17 existing recent tests**
passed in 16.079 s, including real isolated dbt integrations. Tests cover daily
source selection/bounds, leap/DST, price transitions, all missing-value policies,
revisions and append, closed years/January, gap/corruption failures, no-change
bytes/mtime, concurrent/stale writers, manifest-last failure, and bounded versions.

### Integrated refresh and frontend verification

The recent frontend snapshot and history were subsequently refreshed **in that order**
on 10 September, advancing both through **9 September 2026**: 4,270 daily rows,
1,694,953 bytes of current history exports. Recent refresh took 8.767 seconds and
history refresh 2.602 seconds (27 history requests); all 30 overlap dates passed.
This replaces the mismatched initial recent/history source vintages described above.

The build verifies recent/history hashes and available overlapping values with the
same energy/price tolerances. Matching cutoffs remain a v1 rule; recent v2 permits
independent cutoffs and validates embedded history status against the manifest.
Individually valid hashes alone are not enough to publish an inconsistent pair.
The frontend starts on YTD, with recent 1/7/30-day views and a year selector back to
2015. Historical files are loaded on demand and verified by raw-byte SHA-256. Five
source gaps stay visible; affected energy summaries report covered days and partial
sums rather than presenting incomplete observations as full-year totals.

The daily/manual workflow tests all electricity pipeline paths, stages recent
first, then independent current-year history and monthly trade through `.refresh`,
validates the frontend, and retains snapshots and referenced history files in its
short-lived review artifact.
Publishing requires the main event ref, then explicitly checks out release and
guards `HEAD == origin/releases/cloudflare`, independent of main's position.
Released scripts/runtime/frontend validate allowlisted data before a release-only
non-force push and public verification, even when no data changed. Only verified
release SHA output enables separate sync: merge exact release ancestry into current
main, validate offline electricity/script and frontend tests/lint/build without live
source fetching, recheck refs, then push main only without force. Sync failure makes
the workflow red but leaves verified production and future refreshes intact;
no-change publishing runs retry outstanding sync. There is no two-ref atomic promise.
Manual dispatch defaults to `publish=false`, refreshing/validating only the selected
ref. Normal promotion first integrates latest release ancestry into reviewed main,
preserving newer snapshots, then fast-forwards release without force. See
[publication guards and recovery](dashboard_publication.md). Closed years are never
automatically reconciled; first rollover may need manual completion and promotion
of the prior year's history and December trade before daily runs can resume.
