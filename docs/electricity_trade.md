# German electricity: monthly commercial trade

The Python extension `pipeline/src/data_pipelines/dashboards/german_electricity/trade.py`
exports one compact snapshot to `frontend/src/_data/germanElectricityTrade.json`.
It covers **January 2019–August 2026: 92 consecutive months** in the DE–LU bidding
zone. It uses the existing SMARD HTTP client, canonical serialization, strict JSON
parsing, bounded history reader, directory lock and atomic writer. All aggregation
is standard-library Python over already aggregated source values; no database,
dependency, dbt model, scheduler or deployment mechanism is added.

The uncommitted [partial-refresh contract](dashboard_partial_refresh.md) adds trade
v2 nullable monthly inputs and coordinated source-error retention/status reporting.
V1 remains readable; frozen rows/exports are not rewritten as part of feature
implementation. Feature live acceptance and manual code promotion remain pending.

## Commands and revision policy

Run from `pipeline/` with the existing Python 3.11 dashboard environment:

```bash
PYTHONPATH=. python -m src.data_pipelines.dashboards.german_electricity.trade backfill --start-year 2019 --end-year 2026
PYTHONPATH=. python -m src.data_pipelines.dashboards.german_electricity.trade refresh
PYTHONPATH=. python -m src.data_pipelines.dashboards.german_electricity.trade backfill --start-year 2025 --end-year 2025 --reconcile
PYTHONPATH=. python -m unittest discover -s tests -p 'test_german_electricity_trade.py' -v
```

Both commands accept `--as-of YYYY-MM-DD`, `--history-manifest PATH`, and
`--output PATH`. The defaults are the Berlin run date, the existing
`frontend/src/data-history/german-electricity/manifest.json`, and the trade export
above. The output parent must exist. An alternate manifest filename is supported;
its referenced history partitions must be alongside it. Future as-of dates fail.

1. **Cutoff:** validate the history manifest and every referenced partition/hash.
   Select the latest complete calendar month fully covered by `last_date`, capped
   at the preceding month of `--as-of`. History ending 9 September or 31 August
   allows August; history ending 30 August allows only July. An ahead-of-as-of
   history fails. Stale history can hold the trade cutoff back; it cannot cause a
   partial calendar month to be published. A covered month may contain explicit
   null inputs under v2, with null totals. An existing later trade watermark fails rather
   than moving backwards. `--as-of` does not reproduce a historical source vintage.
2. **Initial backfill:** must start in 2019, always uses DE–LU, and never includes
   a partial latest month. Fetch only selected missing years. Existing years are
   skipped, including an unfinished current year, unless `--reconcile` is supplied.
   The complete snapshot must remain contiguous from January 2019.
3. **Refresh:** request only the current year. Replace the latest **three completed
   months**, clipped to January of that year, and append newly completed months.
   For an August cutoff, June–August are eligible; January–May and every closed
   year retain their exact canonical row bytes. Downloaded earlier and partial
   future-month observations are still checked for invalid types, dates and signs.
4. **Missed window / year rollover:** if the old watermark precedes the month just
   before that correction window, or the prior year is missing December, fail with
   explicit `backfill --start-year YEAR --end-year YEAR --reconcile` instructions.
   January refresh never automatically fetches the previous year. Complete prior
   December explicitly, then refresh. Selected reconciliations may replace closed
   years but cannot truncate coverage or introduce a gap.

The single Git-tracked JSON is the durable refresh input, like the approved
[daily history](german_electricity_history.md). Corrupt or noncanonical existing
snapshots fail even during reconciliation. Filesystem writers lock the parent
directory inode; stale output and history-manifest bytes are compared again before
publication. Validate the entire proposed snapshot, then write/fsync a same-directory
temporary file and atomically replace the output. A failed replace leaves the old
file intact and removes the temporary file. No-change runs preserve bytes and mtime.
Cross-checkout publication uses serialized Git operations in the authorized
[daily data publisher](dashboard_publication.md). Trade's current-year correction
window is included after recent/history refresh; normal code and explicit closed-year
reconciliation retain manual release promotion after integrating latest release
ancestry into reviewed main and preserving newer snapshots. The release-first
implementation requires deliberate promotion to both branches and new live rollout
verification; the September 10 successful run covered the old both-ref design.

## Official source, definition, units and licensing

Attribution: **Bundesnetzagentur | SMARD.de**, **CC BY 4.0**, using the exact existing
`pipeline.py` `SOURCE` object, including its license and terms URLs. Modifications:
sum bilateral series, reverse import signs, convert MWh to GWh, derive net exports,
apply narrowly documented pre-trading structural zeros and startup daily fallback.

These are **scheduled commercial exchanges**, not physical flows or generation
minus load. All source URLs use **DE-LU**, beginning in 2019, after the DE–AT–LU
split. Austria is a cross-border counterpart, not part of the reporting zone.
The [official market-data configuration](https://www.smard.de/app/chart_configuration/market_data_configuration.json)
identifies the following gross export/import pairs; it supplies no gross total IDs:

| Counterpart | Export ID | Import ID |
| --- | ---: | ---: |
| DK1 | 4486 | 4504 |
| DK2 | 4487 | 4505 |
| France | 4488 | 4506 |
| Netherlands | 4489 | 4507 |
| Poland | 4490 | 4508 |
| SE4 | 4491 | 4509 |
| Switzerland | 4492 | 4510 |
| Czechia | 4493 | 4511 |
| Austria | 4494 | 4512 |
| NO2 | 4718 | 4720 |
| Belgium | 4706 | 4708 |

```text
https://www.smard.de/app/chart_data/{id}/DE-LU/index_month.json
https://www.smard.de/app/chart_data/{id}/DE-LU/{id}_DE-LU_month_{year_Jan1_epoch_ms}.json
```

Indices contain annual **Berlin 1 January midnight** timestamps in UTC epoch
milliseconds. Each chunk contains up to twelve Berlin month-start observations.
Every returned index and monthly timestamp is checked, including DST boundaries.
Reject duplicate keys/timestamps, wrong years/granularity, invalid numeric types,
nonfinite values and implausible magnitudes. Exports must be nonnegative; imports
must be nonpositive. Observed zero is valid and never marked as derived.

Monthly values are **MWh sums**. `exports_gwh = sum(exports) / 1000`;
`imports_gwh = -sum(imports) / 1000`; `net_exports_gwh = exports_gwh - imports_gwh`.
Use accurate summation and round output to eight decimal places. Do not multiply
monthly sums by hours or apply quarter-hour scaling. A 200 GW equivalent broad
sanity bound uses each month's actual Berlin hours, including leap years and DST.

The [official April 2026 handbook](https://www.smard.de/resource/blob/220052/9d526adf4b948599da4a956dfae6dab9/smard-benutzerhandbuch-04-2026-data.pdf)
describes aggregation and upstream interpolation rules. Complete monthly or daily
observations do not certify underlying hourly/quarter-hour completeness. This
extension preserves official aggregates and does not infer negative-price hours.

### Startup holes: bounded recovery and explicit null fallback

The [official trading announcement](https://www.smard.de/en/new-on-smard-data-on-trading-with-belgium-and-norway-204596)
states that commercial trading began **18 November 2020 for Belgium** and
**9 December 2020 for Norway**. Physical-flow trial dates differ and are irrelevant
to these commercial series.

- Missing Belgium before November 2020 and Norway before December 2020 may be
  derived as zero; list each affected ID in `structural_zero_series`.
- An observed source zero is preserved without that flag. Any nonzero before the
  applicable trading start fails. No other missing observation may become zero.
- The full monthly audit found **only four missing series-months** after commercial
  starts: Belgium 4706/4708 in November 2020 (monthly nulls), and Norway 4718/4720 in
  December 2020 (2020 monthly chunks return 404).

The initial investigation used **eight diagnostic requests**: one annual daily
chunk and one startup-week hourly chunk per affected ID. Results on 10 September:

| IDs | Monthly hole | Daily data in startup month | Startup hourly chunk |
| --- | --- | --- | --- |
| 4706, 4708 | November 2020 | 30 days, first 10 null; all 13 active trading days numeric | `1605481200000`: 168 hours, none null |
| 4718, 4720 | December 2020 | 31 days, first 7 null; all 23 active trading days numeric | `1607295600000`: 168 hours, first 24 null (7 December, before trading) |

Production recovery needs only **four daily requests**, and only during a selected
2020 backfill/reconcile when those monthly inputs are missing:

```text
https://www.smard.de/app/chart_data/{id}/DE-LU/{id}_DE-LU_day_1577833200000.json
```

Validate the entire annual daily payload for duplicate/midnight/year/type/sign
errors and nonzero pre-trading observations. Sum only after every day from the
documented commercial start through month-end is present and numeric. Earlier
days contribute structural zero under the official start-date justification.
This intra-month derivation is recorded here and in `revision_policy`; whole-month
`structural_zero_series` flags apply only to entirely pre-trading months.

Recovered source values (MWh, retaining source signs):

| Month | Export series / MWh | Import series / MWh |
| --- | ---: | ---: |
| 2020-11 | 4706: 51,871 | 4708: −29,263 |
| 2020-12 | 4718: 37,004 | 4720: −199,774 |

Thus the initial September 10 snapshot had **no null months**. If the daily fallback is
unavailable (404) or lacks an active day, the named monthly input stays missing.
That month must have all three totals `null` and the exact missing IDs. Malformed
fallbacks and transport failures fail this component; the coordinator retains its
original bytes as stale. No wider fallback search occurs. V1 keeps its historical
missing-series allowlist; v2 accepts new explicit null monthly observations with
their exact missing IDs and null totals. Omitted required months still fail source
acquisition. See [trade v2](dashboard_partial_refresh.md#monthly-trade-v2).

### Optional official net cross-check and historical discrepancies

ID **4629** is an independent net series, never a substitute for missing gross
inputs. Its 2019/2020 monthly observations are all null. Where its index/monthly
value is available and gross inputs are complete, compare it with the signed sum
of all 22 gross inputs. A missing net value does not make gross totals incomplete.

Tolerance is **0.12 MWh**: 22 gross terms rounded to 0.01 MWh plus one rounded net
term imply a maximum rounding uncertainty of `23 × 0.005 = 0.115 MWh`. This is not
a relative/percentage tolerance. The audit found four larger provider disparities:

| Month | Gross-derived net minus official monthly net (MWh) |
| --- | ---: |
| 2021-12 | −1,065.00 |
| 2022-01 | +1,438.75 |
| 2022-10 | −38.25 |
| 2022-12 | −2,153.50 |

These exact month/discrepancy pairs are explicitly recognized and reported in run
metrics. A changed discrepancy outside rounding tolerance, or any new discrepant
month, fails. If a provider correction restores agreement, it is accepted on an
explicit reconciliation. The export always derives net from gross components;
it does not silently substitute the provider's inconsistent net or loosen the
tolerance. Summing the monthly series yields a **−1,065 MWh discrepancy for 2021**
and **−753 MWh for 2022**. These comparisons concern monthly sums; the independent
annual-resolution endpoint has not been used to overwrite them.

Of 68 months with non-null official net in this backfill, 64 agree within 0.01 MWh;
all 2025 and January–August 2026 values do. Routine refresh checks the three months
being replaced against net, while validating syntax/signs across every full chunk.

## Exact JSON contract: schemas 1 and 2

The root has exactly these fields, with no creation timestamp. The example is v1;
v2 uses the same fields and permits newly reported missing gross series-months.
The producer emits v2 only when new gaps require it or when retaining existing v2:

```text
{
  schema_version: 1,
  kind: "german-electricity-trade",
  source: SOURCE,
  region: "DE-LU",
  timezone: "Europe/Berlin",
  first_month: "2019-01",
  last_month: "2026-08",
  revision_policy: "<exact trade.py POLICY text>",
  rows: [{
    month: "YYYY-MM",
    imports_gwh: number | null,
    exports_gwh: number | null,
    net_exports_gwh: number | null,
    missing_series: [integer IDs],
    structural_zero_series: [integer IDs]
  }, ...],
  content_hash: "<lowercase SHA-256>"
}
```

Rows ascend contiguously from January 2019. ID arrays are sorted and unique.
Imports/exports are positive magnitudes (including zero), net is signed. Missing
either direction makes **all three totals null**, maintaining comparable coverage.
`missing_series` lists only the actual absent inputs, not every counterpart.

Encoding is UTF-8, compact JSON with recursively sorted keys, finite numbers and
**no trailing newline**. `content_hash` is SHA-256 over the same canonical encoding
of the root after removing **only `content_hash`**. This uses the recent pipeline's
canonical algorithm; there is no timestamp to exclude. It is a semantic checksum,
not the checksum of the entire file containing itself.

Consumer integration must preserve numeric token spelling when hashing (Python
`0.0`, `-0.0` or exponent spellings can differ from `JSON.stringify`). The existing
frontend `verifySnapshot` is **recent-hourly-specific**: it validates different keys
and requires a newline, so it cannot be called directly for this trade contract.
Its token-preserving hash technique can be reused with trade-specific validation.

Period consumers must mark periods containing any missing month as **partial**, show
coverage, and never present a sum of known months as a full annual total. Current
2026 coverage is January–August/YTD, not a complete annual total. Annual net totals
derived here may differ from official net because of the documented disparities.

## Budgets, measured acceptance and verification

| Measure, 10 September 2026 | Initial live backfill | Live routine refresh |
| --- | ---: | ---: |
| Requests including indices/fallback | 205 | 46 |
| Response body bytes | 99,827 | 11,302 |
| Total time including history validation | 8.187 s | 1.859 s |
| Result | changed | unchanged, identical bytes |
| Export | 15,129 bytes / 92 rows | same |

The audit preceding backfill used 201 monthly/index requests plus eight finer
diagnostics (209 total, 114,131 bytes, 8.475 s). Production backfill used 23 indices,
178 monthly attempts (including the two known 404s), and four daily fallbacks.
Typical refresh uses **23 indices + 23 current-year monthly chunks**; indices are
fetched concurrently, not as 23 serial round trips. It made no fallback requests.

- At most **three concurrent requests**, including indices and fallback.
- **260 total attempts** for explicit backfill; **50** for refresh, including retries.
- Shared bounded transient retry behavior: at most three attempts per URL, 1/2-second
  backoff for transient errors; 404 is not retried. Other permanent errors fail.
- Refresh fetch deadline **15 seconds**, backfill **120 seconds**; socket timeout
  at most 15 seconds, clipped to remaining deadline. Deadline exhaustion fails the
  component (retained as stale in a coordinated run);
  scheduling/socket teardown can add small overhead, so this is not a guaranteed
  end-to-end SLA. Measured normal additional runtime is below the 10–15-second target.
- Body limits: **64 KB/response**, **2 MB total**; export limit **250 KB**. No raw
  downloads or persistent state DB are needed. Oversized/malformed sources fail.

Snapshot content hash:
`134552fef7dbc71fd1b0e763bc4b81fd31fcac77217aab35ea566a8f9ffe679a`.

Validation: **17 new trade tests** pass (0.286 s); **55 combined electricity tests**
pass (17.709 s), including the existing isolated dbt integrations. Coverage includes
network-mocked request selection/concurrency/budget, strict HTTP JSON, signs,
duplicates/nonfinite values, leap/DST/year boundaries, exact missing policies,
bounded daily recovery, net discrepancies, canonical checksum/corruption, validated
history cutoff, frozen row bytes, correction/append windows, reconciliation and
monotonicity, January rollover, no-change mtime, locks, stale writers and failed
atomic replacement.

The frontend now validates this contract and combines it with existing daily
history into a compact long-term summary (about 22 KB). The fixed dashboard section
shows annual renewable/coal/gas shares and generation mix, plus monthly/annual
commercial imports, exports and derived net exports. It does not download all
historical daily partitions to render these charts. Incomplete energy years are
visibly suppressed where a full annual total cannot be supported; current trade
years are explicitly labelled as partial years. No negative-price-hour inference
is made from monthly or daily averages.

The daily/manual workflow stages recent hourly data, independent daily history, then
monthly trade through [`.refresh`](dashboard_partial_refresh.md#coordinated-cli-failure-boundary-and-recovery).
Source failures retain the affected export and report embedded stale status; shared
errors, including available net contradictions, abort the bundle. Monthly describes
the source resolution; this bounded trade
refresh participates in the daily run. Its review artifact includes the trade
snapshot. Publishing requires the main event ref, then explicitly checks out release
and guards `HEAD == origin/releases/cloudflare`, ignoring main's position. Released
scripts/runtime/frontend validate allowlisted changes before a non-force release-only
push and public verification, including no-change runs. Verified release SHA output
alone permits separate sync: merge exact release ancestry into current main, run offline
electricity/script tests and frontend tests/lint/build without live source fetching,
recheck refs, then push main only without force. Sync failure makes the workflow red
but preserves verified production and future refreshes; no-change publishing retries
outstanding sync. There is no two-ref atomic promise. Manual dispatch defaults to
`publish=false`, refreshing/validating only the selected ref. December completion at
rollover still requires explicit reconciliation if absent; no previous-year fetch is automatic.
Frontend regression checks cover monthly advancement and year rollover without
hard-coded current snapshot dates. See [publication policy](dashboard_publication.md).

```bash
PYTHONPATH=. python -m unittest discover -s tests -p 'test_german_electricity*.py' -v
```
