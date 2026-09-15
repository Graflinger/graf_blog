# Databearer

**[blog.databearer.de](https://blog.databearer.de)**

A German data journalism blog covering energy, politics & society, and economics. Posts feature interactive charts and visualizations to make complex data accessible and understandable.

Built with [Eleventy](https://www.11ty.dev/) and [Apache ECharts](https://echarts.apache.org/).

## Daily dashboard publication

The authorized `dashboard-refresh.yml` runs daily at **06:00 UTC**; manual dispatch
defaults to **`publish=false`**, refreshing/validating the selected ref only.
Publishing requires the `main` **event ref**, then explicitly checks out
`releases/cloudflare` for released scripts, runtime, and frontend. The guard requires
`HEAD == origin/releases/cloudflare` before source fetching and ignores main's
position. Validated recent data, current-year history, and monthly trade alone may
be committed and pushed **release only, without force**. Publishing runs then poll
public snapshots/HTML for up to 240 seconds, including on no-change runs.

Only verified release SHA output enables a separate `sync-main` job using the
released script. It checks the exact release SHA, merges real release ancestry into
a worktree on current main, runs offline electricity/script tests and frontend
tests/lint/build on Node 20, then rechecks refs before a **main-only non-force push**.
Sync fetches no live source data, and bot main pushes cannot rely on push CI.
Conflicts/races or failed candidate checks make the workflow red while leaving
verified production intact and future refreshes possible. No-change publishing
runs retry outstanding sync. Production has a ten-minute budget including verification;
sync has its own ten-minute cap and extra validation/build cost. There is no two-ref
atomic promise.

Normal code/blog promotion remains manual: incorporate latest release ancestry into
reviewed main via sync or explicit real-merge reconciliation, preserve newer snapshots
without silently overwriting conflicts or changing schemas, pass checks, then
fast-forward release without force. Main and release need not routinely equal.

**New rollout verification is pending.** Deliberately promote this implementation to
**both branches** before the main schedule relies on released scripts. The
[September 10 manual run](https://github.com/Graflinger/databearer/actions/runs/34532806712)
succeeded under the old both-ref design with public verification in about 5m47s;
September 11/12 old guards stopped with main ahead. Those results do not verify the
new design. A push is not a deployment guarantee. See the
[publication runbook](../docs/dashboard_publication.md) and
[frontend dashboard checks](README-dashboard.md); tests/lint/build use Node 20.

The uncommitted [partial-refresh feature](../docs/dashboard_partial_refresh.md)
supports recent/history/trade v2 nulls and explicit coverage/statuses, retaining
validated last-good components on source errors. Shared errors abort the staged
bundle. This is an approved durable recent-export exception with no persistent DB.
Feature live acceptance/manual code promotion to both branches remains pending;
implementation/PR permission does not authorize production or frozen-export rewrites.

## Electricity dashboard: Ausbau, Speicher & Netze

The fixed bottom section before the main methodology adds five lazy charts: reported
solar/onshore/offshore capacity, recorded battery/pumped-storage power, annual
congestion energy, annual congestion costs, and monthly total/market-plant redispatch.
Only the monthly chart has an energy/cost toggle; it never changes the main period
selection. Axes keep GW, GWh and nominal million euros separate. Legends use visible
line styles; latest values and separate statutory target cards work without JS.
There are no additional tables or source requests.

`src/_data/germanElectricityProgressView.js` reads the producer's actual file bytes via
`src/data_ingestion/builders/electricityProgress.js`, independently verifying the exact
[schema-1 contract](../docs/electricity_progress.md), sources, license evidence, legal
baseline, dates, continuity, bounds and SHA-256. The existing `parseCanonical` helper
preserves Python numeric tokens when hashing; only `content_hash` is omitted.
The 150 KB cap is enforced before parsing. The current snapshot is about 11 KB and
is embedded once as script-safe JSON. `/data/german-electricity-progress.json` retains
the validated canonical bytes, including numeric spelling and license evidence.
Post-build checks reread the source and compare both download and embed.

Capacity stays pinned to year ends 2011–2025, provisional; the June 26, 2026 source
evaluation is a manually verified baseline, not an automatically updated timestamp.
Congestion dates follow the last actual annual/monthly rows (initially 2025 / May 2026).
New completed congestion years/months are supported without hardcoded display years.
Targets show 2030 and later milestones on their own statutory basis (PV DC module
power differs from observed net rated power), with no achievement ratio or overlay.
Storage excludes small systems below 13.2 kW and may include foreign grid-feeding
plants; it is neither all German storage nor GWh/duration. Congestion interventions
are not outages, renewable generation losses, or a stability indicator. The compact
method disclosure links licensing/provenance and explains deferred, unlicensed
auctions, grid projects, interconnectors and SAIDI. This source remains separately
refreshed manually/at most monthly and manually promoted, outside daily publication.

Checks: `npm test`, `npm run lint`, `npm run build`; inspect desktop/mobile and dark
mode, independent controls, viewport-only loading, and ECharts-disabled text fallback.

## Electricity dashboard: long-term comparisons

`src/dashboards/strom.njk` includes the permanent **Die Energiewende im
Langfristvergleich** section. Its two energy charts and annual/monthly trade charts
are independent of the recent-period and historical-year controls. Annual values
are rendered as accessible HTML tables; monthly trade values are in a disclosure.
ECharts initializes each long-term chart only near the viewport, using the existing
palette, with responsive resizing and dark-mode support.

Build-time data flow (filesystem only):

- `src/data_ingestion/builders/electricityHistory.js` validates all manifest-referenced
  daily partitions, their raw hashes and recent/hourly overlap.
- `electricityTrends.js` reads those validated partitions and the pipeline-owned
  `_data/germanElectricityTrade.json` snapshot. The shared browser/Node contract in
  `src/js/dashboards/electricity-trade.js` validates strict fields, continuity,
  signs, net identity, source, missing-series rules and completed-month cutoff.
  The build hashes the canonical trade preimage while preserving numeric token
  spelling (including Python `0.0`, `-0.0` and exponents). Only `content_hash` is omitted.
- The same builder validates `_data/germanElectricityAnnual.json` against the exact
  [annual supplement contract](../docs/electricity_annual.md): canonical raw JSON
  capped at 10 KB, token-preserving SHA-256, exact source/schema/region, sorted unique
  years restricted to 2016/2018, all twelve generation keys and finite annual bounds.
  An absent supplement retains daily-only gap handling; an invalid one fails the build.
- `_data/germanElectricityTrends.js` provides the compact summary, embedded as escaped
  non-executable JSON and available at `/data/german-electricity-trends.json`.
  It includes coverage, source notes, manifest/partition hashes, trade identity and
  `inputs.annual_content_hash` (null only when the supplement is absent).
  No daily history is sent for these charts and they make no fetch requests.
- Post-build checks compare the rendered JSON and embedded HTML against the validated
  inputs, catching concurrent input changes. Builds fail on invalid snapshots.

Energy charts include **completed calendar years only**. Shares use summed energy,
not mean daily percentages; coal combines lignite and hard coal, and the denominator
includes every generation source, including pumped storage and nuclear. The frozen
SMARD supplement supplies **all twelve annual category values** for 2016 and 2018,
replacing the entire annual mix and denominator; all completed years 2015–2025 now
have annual values. These rows carry `method: source_annual_aggregate` and tables and
tooltips say **SMARD-Jahreswerte**. Other years use `method: daily_sum`; missing daily
generation without a supplement leaves every annual series null. `days` and
`complete_days` always describe daily history: 2016 remains 365/366, 2018 remains
361/365. No residual is allocated to missing days, and selected-year/partial-year
views retain their gaps. Neither annual nor daily aggregates certify underlying
hourly/quarter-hour completeness. The incomplete latest year remains
available in the existing selected-year view; there is no additional YTD chart.

Trade is **scheduled commercial exchange for DE–LU**, not physical flows or a national
generation-minus-load balance. Imports and exports are positive magnitudes; net export
is export minus import (negative = net import). Annual sums use complete monthly values
from January 2019 onward. The latest partial year has a date-range label, lighter
dashed-border bars and an isolated net marker, with no projection. A missing month
invalidates all three annual totals, with coverage shown explicitly.

See [trade contract](../docs/electricity_trade.md) and
[history contract](../docs/german_electricity_history.md). The frontend does not modify
producer snapshots. Recent/history/trade participate in authorized daily data-only
publication; frozen annual supplements remain outside daily fetching and the write
allowlist. Normal code changes retain the manual release gate.

Verify from `frontend/`: `npm test`, `npm run lint`, `npm run build`, then inspect
`http://localhost:8080/dashboards/strom/` (including mobile, dark mode, chart loading,
and changing period controls while the long-term section stays fixed).
