# Germany electricity dashboard data

Status: pipeline and real-data snapshot implemented. **Release-first daily data-only
publication is implemented; deliberate promotion to both `main` and
`releases/cloudflare` and new live rollout verification are pending.** The September 10
successful manual publication used the old both-ref design. See
[publication and recovery](dashboard_publication.md) for historical evidence and new rollout.
This document describes the bounded
recent hourly snapshot. The approved, separately implemented
[daily history extension](german_electricity_history.md) covers 2015 onward with
durable yearly JSON, frozen closed years, and rolling corrections. Architecture:
[stateless dashboard](dashboard_architecture.md) and [Plan B](plan_b.md).

The uncommitted [partial-refresh contract](dashboard_partial_refresh.md) adds recent
v2 nullable observations, per-component last-good retention and independent cutoffs.
It supersedes the old common-complete-window rule; feature live acceptance and manual
code promotion to both branches remain pending. Earlier measurements below describe v1.

## Source, permission, and methodology

Verified 10 September 2026:

- Provider/mandatory attribution: **Bundesnetzagentur | SMARD.de**.
- [Market data](https://www.smard.de/home/marktdaten).
- [Official terms](https://www.smard.de/home/datennutzung): market data may be
  downloaded free of charge, stored, shared, combined, and modified under
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), with that attribution.
  This permission covers the market **data**; it does not grant rights to provider
  logos or other branding. Retain attribution and the license link in presentation,
  and identify the conversion to hourly average GW as a modification.
- [Official SMARD handbook, April 2026](https://www.smard.de/resource/blob/220052/9d526adf4b948599da4a956dfae6dab9/smard-benutzerhandbuch-04-2026-data.pdf):
  data preparation, chart resolution, actual generation, actual consumption, and
  wholesale prices. Hour timestamps denote interval starts; quarter-hour energy
  values in MWh are **summed** to hourly energy. Generation/load from the `hour`
  endpoints are MWh, not instantaneous MW. For a one-hour interval:
  `average GW = MWh / 1 hour / 1000`. No division by four is applied again.
- [Official generation explanation](https://www.smard.de/page/home/wiki-article/446/636/stromerzeugung):
  net generation fed into the public grid, excluding power-station own use.
  Industrial/closed grids, railway generation, and behind-the-meter PV own use are
  outside this scope. Coverage and extrapolation methods differ by energy source
  and control area; upstream revisions are possible.
- [Official 2026 Q2 definitions](https://www.smard.de/page/home/topic-article/219708/221002/erzeugung-und-verbrauch-leicht-gestiegen):
  `load` is **Gesamt (Netzlast)**, including grid losses but excluding pumped-storage
  charging and power-station own use. It is not gross national electricity demand.
  `pumped_storage` is generation/discharge, not charging or net storage flow. Keep
  it separate from renewables. Hydro excludes pumped-storage generation.
- `price` is the **DE–LU day-ahead wholesale price**, EUR/MWh, using SMARD's hourly
  resolution. It is not a household tariff or an intraday price. Products have been
  quarter-hourly since October 2025; the hourly presentation hides within-hour
  price variation ([official 2026 Q1 explanation](https://www.smard.de/page/home/topic-article/444/220298/nettoexport-von-strom-im-ersten-quartal)).
  Retain SMARD's hourly price unchanged; negative prices are legitimate.
- [Official nuclear phase-out account](https://www.smard.de/page/home/topic-article/444/211756/der-strommarkt-im-jahr-2023):
  Germany's last nuclear power stations shut down on **15 April 2023**. The nuclear
  endpoint (1224) is discontinued/404 for current 2026 data. It is never requested
  or replaced with fabricated observations. Both fetched and displayed windows
  must be post-2023 (start on/after 1 January 2024).

The difference between generation and load must **not** be labelled imports or
exports: the series have different coverage and storage treatment. This snapshot
contains no trade flows, inferred imports, emissions, or invented nuclear series.

### HTTP surface and series allowlist

The public chart endpoints require no account or key. The
[community OpenAPI schema](https://raw.githubusercontent.com/bundesAPI/smard-api/main/openapi.yaml)
documents the transport and IDs; it is not the authority for licensing or a
provider API-availability guarantee.

```text
https://www.smard.de/app/chart_data/{id}/DE/index_hour.json
https://www.smard.de/app/chart_data/{id}/DE/{id}_DE_hour_{week_ms}.json
```

Index entries are UTC epoch milliseconds for **Monday 00:00 Europe/Berlin**.
They are not UTC Mondays; calculate the next week using local calendar dates to
handle the DST offset change. Each weekly payload is `series: [[epoch_ms, value]]`.

| JSON column (ordered) | ID | Export unit |
| --- | ---: | --- |
| biomass | 4066 | GW |
| hydro | 1226 | GW |
| wind_offshore | 1225 | GW |
| wind_onshore | 4067 | GW |
| solar | 4068 | GW |
| other_renewables | 1228 | GW |
| lignite | 1223 | GW |
| hard_coal | 4069 | GW |
| gas | 4071 | GW |
| other_conventional | 1227 | GW |
| pumped_storage | 4070 | GW |
| load | 410 | GW |
| price | 4169 | EUR/MWh |

## Window, budgets, and failures

`--as-of` means the Berlin run date (default: today's Berlin date), not the last
observation date. Fetch the preceding **35 calendar days**, exclusive of as-of
midnight. Weekly HTTP payloads can include adjacent out-of-window hours; retain
only the requested 35-day range for source-window selection, then stage the aligned
30-day output grid. Fetch every index once and only the
five or six intersecting weeks per series: **78–91 successful HTTP requests**.

Each series selects its latest structurally reported day from `as_of - 1` through
`as_of - 4`, then validates its 30-day timestamp grid. Explicit source nulls are
allowed in v2; omitted internal hours fail that component. Align successful and
retained components to the latest successful or previous snapshot cutoff. See
[window alignment and statuses](dashboard_partial_refresh.md#recent-json-v2) for
failure retention and all-unavailable behavior. Never fill gaps with zero or shift
a window again to conceal an internal omission. An explicit historical as-of still
reads today's source vintage.

Build the expected timestamp sequence from Berlin midnight boundaries in UTC
one-hour increments. This yields 719/720/721 rows around spring/ordinary/autumn
30-day windows and properly represents 23/25-hour days. Repeated local 02:00
hours in autumn have distinct epoch timestamps.

Runtime bounds:

- At most three component workers; each fetches its index and required weeks sequentially.
- At most three attempts per URL, retrying transient connection failures and HTTP
  429/500/502/503/504, with 1/2-second backoff. Permanent HTTP errors fail immediately.
- Socket timeout 15 seconds; total fetch deadline 240 seconds, checked while reading
  and before requests (an in-flight socket read may take up to its timeout to stop).
- 256,000 bytes per response, 24,000,000 total response-body bytes, 273 request
  attempts maximum. Counters include retries and partial reads; HTTP headers/TLS
  overhead are not included. No raw-response archive or cache is used.
- dbt subprocess timeout 120 seconds, one thread, no downloaded DuckDB extensions.
- Final JSON at most **1,000,000 bytes** including newline.

V1 requires numeric measurements; v2 permits explicit nulls while rejecting omitted
required timestamp slots. Validation rejects duplicate keys (even identical duplicates),
bad timestamps/granularity, unexpected series, nonnumeric values (including numeric
strings and booleans), NaN/infinity, malformed/empty responses and incomplete
timestamp coverage. Source acquisition errors are isolated per component; shared
validation/dbt/storage failures remain hard errors. Broad sanity limits: each
generation/load series 0–200 GW; prices
−10,000 to +10,000 EUR/MWh. These are corruption guards, not forecasts or asserted
market price limits; out-of-range upstream changes require explicit investigation.

## Temporary-database implementation and local commands

From `pipeline/`, use Python 3.11 and the pinned narrow runtime:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-dashboard.txt
PYTHONPATH=. .venv/bin/python -m src.data_pipelines.dashboards.german_electricity
PYTHONPATH=. .venv/bin/python -m src.data_pipelines.dashboards.german_electricity.refresh
PYTHONPATH=. .venv/bin/python -m src.data_pipelines.dashboards.german_electricity --as-of 2026-09-10 --output /existing/directory/germanElectricity.json
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -p 'test_german_electricity*.py' -v
```

Direct runtime dependencies match the main pipeline pins: dbt-core 1.8.8,
dbt-duckdb 1.9.0, DuckDB 1.1.1. HTTP, calendar handling, hashing, and JSON use the
standard library. Transitive dependencies are resolved by pip, not fully locked.
The host needs the Europe/Berlin timezone database. Output parent must exist.
The first module is the compatible standalone recent command; `.refresh` coordinates
an existing recent/history/trade bundle. See [CLI and recovery](dashboard_partial_refresh.md#coordinated-cli-failure-boundary-and-recovery).
Previous validated recent values are an approved durable per-component input, not
only a no-change comparison; the database remains temporary.

Each run creates a new `TemporaryDirectory` and a dedicated
`german_electricity.duckdb`. It never connects to `pipeline/.data/duckdb.db`.
The dedicated profile requires `GERMAN_ELECTRICITY_DB`, which the entry point sets
only for its dbt subprocess. The main dbt project is reused with this exact narrow
selection:

```text
dbt build --target dashboard --select +german_electricity_hourly
```

The entry point also supplies absolute `--project-dir`, dashboard `--profiles-dir`,
temporary `--target-path`/`--log-path`, `--no-partial-parse`, and `--fail-fast`.
dbt usage telemetry is disabled. Raw tables are `staging.smard_hourly` and
`staging.smard_window`; selected models are
`dashboard_cleaned.smard_hourly_cleaned` and
`dashboard_curated.german_electricity_hourly`. Two real models and eight dbt tests
run. Dashboard sources/models/tests are enabled only for the dashboard target;
ordinary blog targets do not acquire a dependency on dashboard staging.

The final snapshot is validated in memory and again in a same-directory temporary
file before atomic replacement. A failure retains the previous destination. An
existing corrupt snapshot also causes an explicit failure, rather than trusting
its claimed hash or overwriting it. Investigate it and restore a known-good version
or choose a separate output path. Temporary files/databases/artifacts are cleaned
on normal exit and handled exceptions; forced process termination may leave OS-temp
files, which are never used as input by a later run.

## Exact frontend contract (schema versions 1 and 2)

Default generated destination: `frontend/src/_data/germanElectricity.json`.
One compact JSON object. The base fields are below; v2 additionally requires
`components` and `refresh_status` with the exact [partial-refresh metadata contract](dashboard_partial_refresh.md#recent-json-v2).

| Field | Value / meaning |
| --- | --- |
| schema_version | integer `1` (compatible existing snapshots) or `2` (new recent producer) |
| source | `{name: "Bundesnetzagentur \| SMARD.de", url: "https://www.smard.de/home/marktdaten", license: "CC BY 4.0", license_url: "https://creativecommons.org/licenses/by/4.0/", terms_url: "https://www.smard.de/home/datennutzung"}` |
| timezone | `Europe/Berlin` |
| window_start | ISO UTC seconds with `Z`, inclusive Berlin midnight |
| window_end | ISO UTC seconds with `Z`, exclusive Berlin midnight |
| data_through | exactly `window_end`; exclusive coverage boundary |
| snapshot_created_at | ISO UTC seconds with `Z`; creation time of the changed snapshot |
| content_hash | lowercase SHA-256 hex of canonical semantic content |
| columns | `['timestamp','biomass','hydro','wind_offshore','wind_onshore','solar','other_renewables','lignite','hard_coal','gas','other_conventional','pumped_storage','load','price']` |
| rows | sorted arrays `[integerEpochMillis, 12 GW measurements, EUR/MWh price]`; finite numbers in v1, finite numbers or null in v2 |
| units | `{power: "GW", price: "EUR/MWh"}` |
| expected_update | `daily` |
| stale_after_hours | integer `96`; grid age uses exclusive `data_through`; v2 also reports per-component status/observation age |

Canonical encoding: Python `json.dumps(sort_keys=True, separators=(',', ':'),
ensure_ascii=False, allow_nan=False)`, UTF-8. The hash covers **all** fields except
`content_hash` and `snapshot_created_at`, with no trailing newline. Output adds one
newline. Numeric values use finite Python/DuckDB doubles with no extra rounding;
the fixed runtime gives stable serialization. Source observation order and run
times do not affect semantic content. If it is unchanged, preserve the previous
file bytes, modification time, and `snapshot_created_at`. That timestamp is not
“last checked”. Metrics record successful checks separately.

## Verification and initial measured run

10 September 2026, clean temporary Python 3.11 environment and no previous database:

| Measurement | Initial live run |
| --- | ---: |
| HTTP requests | 91 |
| Downloaded response bodies | 418,831 bytes |
| Fetch duration | 4.732 s |
| Transformation + validation | 7.423 s |
| Total refresh | 12.168 s |
| Rows | 720 |
| Export size | 107,447 bytes |
| dbt result | 2 models + 8 tests passed |

Selected Berlin dates: **10 August–8 September 2026**, inclusive. UTC interval:
`[2026-08-09T22:00:00Z, 2026-09-08T22:00:00Z)`. Generation on 9 September remained
partial (some series stopped around 19:00/20:00), so that day was ineligible.
The observed semantic hash was
`d39305bb9c527512678d751fe6f761e3ee61c46086d3ce61ff1eb165402ba4bf`.

The generated-destination run completed in **8.452 s** (4.401 s fetch, 4.039 s
transform/validate), downloading 418,837 bytes in 91 requests with the same hash.
The subsequent live rerun completed in **8.510 s** and reported `unchanged`,
preserving the existing snapshot timestamp and bytes. The small download-size
change outside the selected complete window did not change the semantic snapshot.

Final offline test result: **15 tests passed in 16.875 s**. The deliberate duplicate
injection produces an expected dbt failure which the test asserts. A separate
`dbt ls --target prod --select tag:german_electricity` returned **no enabled nodes**,
confirming ordinary blog-target isolation. dbt 1.8 emits a non-fatal legacy `tests`
configuration deprecation notice when parsing this project's schema YAML.

Offline unittest fixtures exercise DST, deterministic real dbt reruns, missing
periods, latest-common-day lag, duplicate rejection before pivoting, numeric types,
negative prices, bounded requests/retries/bytes, malformed input, revision detection,
corrupt snapshots, and atomic-replacement failure. Synthetic fixtures stay in tests
and OS temporary directories; the generated frontend snapshot comes from live SMARD.
Recent v2 prints JSON metrics with change status, per-series failures/components,
request/byte counts, rows, export size and total duration. Coordinated status and
the additional offline regression fixtures are documented in [partial refresh](dashboard_partial_refresh.md#runtime-verification-and-rollout).

## Frontend and workflow integration

The standalone dashboard is `/dashboards/strom/`; its downloadable snapshot is
`/data/german-electricity.json`. See [frontend implementation and checks](../frontend/README-dashboard.md).
One shared JSON export replaces per-chart CSV duplication for synchronized period
controls, while retaining Eleventy, ECharts, and static hosting. Historical blog
datasets are unchanged. Production rendering checks the snapshot's semantic hash.

`.github/workflows/dashboard-refresh.yml` schedules **06:00 UTC daily**, plus manual
dispatch with boolean **`publish=false`** by default to refresh/validate only the
selected ref, without publication or sync. It runs all offline electricity and script
tests, runs the coordinated `.refresh` entry point (recent → independent current-year
history with observed overlap checks → monthly trade), then frontend tests/lint/build
on Node 20. Source failures may produce validated, visibly degraded components;
shared errors abort the staged bundle. Annual supplements stay frozen;
capacity/congestion remains a separate manual/monthly refresh. Review artifacts are
retained for seven days. The ten-minute production job budget includes up to 240
seconds of public verification; package caches never contain the DuckDB database.
The workflow also validates/builds unchanged data.

Publishing requires the `main` **event ref**, followed by an explicit checkout of
`releases/cloudflare` using its released scripts, runtime, and frontend. The guard
requires `HEAD == origin/releases/cloudflare` before source fetching and ignores
main's position. With `contents: write`, only validated recent/current-year-history/trade
allowlist changes may be committed and pushed **to release only, without force**.
Public data and HTML are then verified, including on no-change publishing runs;
a review artifact or successful push is not proof of deployment.

Only verified release SHA output enables the separate `sync-main` job. It checks
the exact release SHA, merges real ancestry into a worktree on current main, and
validates the candidate with offline electricity/script tests plus frontend
tests/lint/build before rechecking refs and pushing **main only, without force**.
Sync does not fetch live source data or change schemas; bot main pushes cannot rely
on push CI. It has its own ten-minute cap and extra validation/build cost. Conflicts,
validation failures, or races make the workflow red but leave verified production
intact and future refreshes possible. No-change publishing runs retry outstanding
sync; there is no two-ref atomic promise.

Normal code/blog releases retain manual promotion: incorporate latest release
ancestry through sync or reviewed real-merge reconciliation, preserve newer snapshots,
pass checks, then fast-forward release to reviewed main without force. Main and
release need not routinely equal. See [the publication runbook](dashboard_publication.md)
for new rollout and distinct recovery paths for public failure versus sync conflict/race.

Local verification passed 17 pipeline tests (including real dbt integration), 84
frontend Jest tests, `npm run lint`, and `npm run build`. Browser checks cover period
controls, negative prices, mobile/dark mode, JavaScript disabled, missing JSON and
missing ECharts. A successful local refresh does not establish scheduled-job health
or successful site publication.
