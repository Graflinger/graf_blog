# German electricity frontend

Route: `/dashboards/strom/` (standalone, outside post collections).
The menu opens `/dashboards/`, which lists pages tagged `dashboard` automatically.
Optional `dashboardImage` and `dashboardImageAlt` frontmatter provide a card
illustration. Cards form compact horizontal rows with 25–27% image width; the
text-free SVG supports a 4:3 or square crop on smaller screens. The electricity SVG is an original decorative illustration, not a
plot of source observations. Cards without images still render normally.
Add `dashboardTopic`, `dashboardSummary`, and `dashboardCadence` frontmatter to new
dashboard pages for their overview cards. The overview itself is not tagged.
Dashboard chrome uses the site's gold/neutral palette, with a darker gold for
accessible text on light backgrounds; chart source colours remain distinct.
Input contracts: [recent hourly v1/v2](../docs/german_electricity_data.md),
[daily history manifest v1 / partitions v1/v2](../docs/german_electricity_history.md)
and [trade v1/v2](../docs/electricity_trade.md). The authoritative
[partial-refresh contract](../docs/dashboard_partial_refresh.md) covers exact component
fields/statuses, nullable metrics, independent cutoffs and source-error recovery.
The uncommitted feature has no live refresh/deployment acceptance yet; manual code
promotion to both branches is required and frozen exports are not rewritten.
The pipeline owns
`src/_data/germanElectricity.json` and `src/data-history/german-electricity/`.

## Offline partial-refresh release gate

Before promoting partial-refresh code, run these commands from `frontend/` with
the installed dependencies (Node 20 in CI):

```bash
npm test -- --runInBand
npm run lint
npm run build
npm run test:v2
```

`test:v2` copies the frontend and verifier into a disposable temporary workspace,
reuses installed dependencies, and injects valid, hashed v2 recent/history/trade
artifacts into that copy. It exercises missing latest-day prices, partial generation
and load, retained gas/history, a missing trade month, and a closed-year generation
gap outside the frozen annual supplements. It runs **every frontend test**, lint,
the production build, and the public verifier against the built files using local
reads. It makes no network requests. The temporary copy is removed on success or
failure, and original frontend file fingerprints must remain identical.

Production and template tests share `electricityFilters.js`. Visible report markers
(`data-component`, `data-refresh`, `data-component-field`) bind reader-facing labels,
status, coverage and cutoffs to the v2 metadata. The public verifier checks the
actual text and rejects missing, duplicate, hidden/inert or mismatched report markup;
partial/retained reports must be expanded and have a visible warning. It does not
evaluate external CSS. Verifier regression tests run from the repository root:

```bash
python3 -B -m unittest discover -s scripts/tests -p 'test_verify_dashboard_deployment.py' -v
```

## Static rendering and shared controls

Eleventy validates the real snapshot and the entire daily history. The default HTML
renders **YTD for the latest history year**, from 1 January through its final date,
with KPIs, source-mix table, chart text summaries, coverage and provenance.
The recent helper supports v1/v2 with the same 1/7/30-day selections.
`src/js/dashboards/electricity-data.js` is a dependency-free CommonJS/browser helper
used by both the build and the interactive page. It validates schema, metadata,
units, bounded values, complete hourly timestamp grids, calendar days and future
timestamps; v2 also validates nullable observations and per-component metadata.
At the build boundary, `src/data_ingestion/builders/electricitySnapshot.js` verifies
the actual SHA-256 against the raw artifact. It preserves Python's numeric tokens
(including `0.0`, `-0.0` and exponent notation), checks compact sorted-key JSON with
one trailing newline, rejects duplicate keys, and hashes all root fields except
`content_hash` and `snapshot_created_at`. No Python runtime or extra dependency is
needed. Changing an observation while retaining its hash fails both rendering
filters. Parsed Eleventy data must also match the verified file, which is reread
for each render so watch builds cannot reuse an old verification.

The browser matches the HTML/JSON hash, coverage and creation timestamp to reject
mixed builds. A candidate becomes shared controller state only after all checks
pass, so a later theme change cannot render rejected data.

`src/data/german-electricity.njk` publishes the same snapshot at
`/data/german-electricity.json`. It is a standalone escaped JSON response, not an
interpolated executable script. The browser fetches this same-origin prepared
snapshot (15-second timeout) to enable the hourly controls. The standard local
ECharts library is reused.

This dashboard deliberately bypasses the CSV-to-generated-JS chart builder: one
shared JSON snapshot and one controller keep 1/7/30-day KPIs, table, stacked hourly
generation, separate load chart and signed price bars synchronized. Reusing the
per-chart CSV builders would duplicate data and calendar/aggregation logic. There
is no new framework, runtime server, upstream browser fetch or generated dashboard
JavaScript. Normal CSV chart discovery still reads only direct `.js` configs;
explicit nested filenames remain supported. Loader, generator and Eleventy hook
now propagate failures, including missing configs/data and write errors.

All period selections use Berlin calendar dates and actual UTC interval durations,
including 23/25-hour DST days. Tooltips distinguish repeated hours with GMT offsets;
the exclusive coverage boundary is never displayed as the last observed day.
Renewable shares use renewable energy / all generation energy, including pumped
storage in the denominator. Prices are time-weighted, not load-weighted. Source mix
bars, KPIs and summaries remain available without charts; without JavaScript the
initial YTD view remains readable and prepared JSON remains downloadable.

The grid freshness warning uses `data_through`, not snapshot creation time, and updates
in the browser every minute and when the tab becomes visible. Future timestamps
are rejected. This is not scheduler monitoring. Public copy describes automatic
daily updates and points to actual observation dates; capacity/congestion retains
its separate manual/monthly cadence and statutory targets their manual review date.
V2 also renders a static component report with status, known/expected hours,
`source_observed_through` and `last_successful_window_end`. The escaped
`electricity-component-data` JSON embeds those components and history/trade
`refresh_status`; build/public checks match it to the recent snapshot. Warnings
account for partial/stale/unavailable components, history/trade status and component
observation age. A successful fetch clock is not an observation cutoff.

The [publication workflow](../docs/dashboard_publication.md) authorizes daily
06:00 UTC recent/history/trade publication using released scripts/runtime/frontend.
Manual dispatch defaults to `publish=false`, refreshing/validating only the selected
ref. Publishing requires the `main` event ref, followed by explicit
`releases/cloudflare` checkout. Before source fetching, the guard requires
`HEAD == origin/releases/cloudflare` and ignores main; unpublished main changes do
not block production refreshes. Changed validated allowlisted exports alone advance
**release only, without force**.
`verify_dashboard_deployment.py` checks public recent data, history manifest/latest
partition, trends/progress and HTML for up to 240 seconds, including no-change
publishing runs. A push or validation artifact is not proof of Cloudflare deployment;
the verifier does not automatically retry an external build.

Only the production job's verified release SHA output enables `sync-main` using the
released sync script. It checks the exact release SHA and prepares a worktree on
current main with real release ancestry merged. Changed candidates must pass offline
electricity/script tests and frontend tests/lint/build on Node 20 without live source
fetching before rechecking refs and pushing **main only, without force**. Bot main
pushes cannot rely on push CI. Conflicts/races and validation failures make the
workflow red but leave verified production intact and future refreshes possible;
no-change publishing runs retry outstanding sync. There is no two-ref atomic promise.
Production retains its ten-minute budget including the verifier; sync has its own
ten-minute cap and extra validation/build cost. The runbook distinguishes recovery
for public verification failure from sync conflict/race recovery.

Normal code/blog promotion remains manual: incorporate latest release ancestry into
reviewed main through sync or explicit real-merge reconciliation, preserve newer
snapshots, pass checks, then fast-forward release without force. Do not silently
overwrite main conflicts or change schemas to make integration pass. Main and release
need not routinely equal. **New rollout is pending:** deliberately promote this
implementation to **both branches** before the main schedule uses released scripts.
The September 10 manual run succeeded under the old both-ref design (including public
verification, about 5m47s); September 11/12 old guards stopped with main ahead. See
the runbook for the historical run link and pending new rollout checks.

## YTD and yearly daily history

`src/js/dashboards/electricity-history.js` is the separate browser/CommonJS history
helper. `src/_data/germanElectricityHistory.js` calls the build validator in
`src/data_ingestion/builders/electricityHistory.js`, rereading the manifest and **all
referenced partitions**, including years that are not selected on the page. It checks:

- Exact keys, schema, source/license, revision policy, years and same-origin URLs.
- Contiguous dates/counts, leap days, Berlin 23/24/25-hour days, finite bounded values.
- Version-specific null rules: narrow documented v1 gaps or nullable v2 observations;
  historical price zones, nuclear shutdown zeros and explicit derived-zero flags.
- Compact sorted-key UTF-8 JSON with no trailing newline or duplicate keys. Numeric
  tokens retain their Python spelling; SHA-256 hashes **raw bytes**, using Node
  crypto at build time and WebCrypto in the browser.

At the build boundary, the helper also reads the exact recent producer file through
`electricitySnapshot.verifySnapshot`. Recent v1/history must end on the same Berlin
calendar date (recent's `data_through` is exclusive); recent v2 permits independent
cutoffs. Embedded history status must match manifest `last_date`, and trade status
must match trade `last_month`. Every date shared by their
declared ranges must exist with the same actual day hours. Daily energy is compared
to the hourly GW sum with tolerance `(hours + 1) * 0.005 / 1000 + 1e-8` GWh;
daily price is compared to the hourly mean with tolerance `0.011` EUR/MWh, matching
the pipeline. In v2 a series/day comparison requires a numeric daily value and all
expected numeric hourly values; otherwise it is skipped, not treated as equality.
Missing observations never become zero. Nuclear has no recent
series and is covered by the separate history shutdown/schema checks.
January overlap includes the preceding year's partition; a subset history starting
on 1 January compares only shared dates. Older dates outside the recent window
retain their documented source limitations. A mismatch fails the build even if
each artifact has a valid hash: use coordinated `.refresh` and investigate available
contradictions. V2 allows no overlap when independently retained ranges have diverged.
The after-build check repeats overlap validation and checks that the public recent
JSON matches the validated producer snapshot. Tests can supply an alternate raw
recent snapshot to `readHistory(directory, recentRaw)`; it is always fully verified.

Eleventy passthrough maps `src/data-history/german-electricity/` to
`/data/history/german-electricity/`. The public `manifest.json` is ordinary JSON
copied byte-for-byte; it does not hash itself. An after-build check validates the
published set, compares each referenced file and manifest byte-for-byte with the
inputs, and verifies the HTML's embedded manifest matches. Unreferenced retained
versions may be copied but never become extra selectable years.
`src/_headers` sets the public manifest to `Cache-Control: no-cache` and hashed
year files to immutable one-year caching on Cloudflare Pages. The local Eleventy
server does not apply these hosting rules; deployment headers need host-side
verification during the new release-first rollout. Browser manifest retries explicitly
request revalidation; partition hashes are always verified, even on cache hits.

The small validated manifest is safely escaped into a non-executable
`application/json` script in HTML. Its URLs and hashes pin that page to one data
publication. On initial load only the latest year is requested; choosing a previous
year requests only that partition. Verified years and in-flight requests are cached
in memory. A 404 revalidates the public manifest and retries the partition only if
the full manifest still matches HTML; a changed publication requires a page reload.
Network/HTTP, timeout, hash, schema and mixed-deployment failures retain the previous
view. The controller commits a candidate only if it is still the latest requested
selection. Theme changes render only committed data and preserve loading/error text.
The dropdown and `aria-pressed` buttons describe the displayed view while requests
are pending; `aria-busy` and a live status announce loading and failure.

### Aggregation and interpretation

- **Generation:** exclude a date from generation, renewable share and mix summary
  calculations when any required generation field is null. Use the same complete-
  generation-day hours denominator for those averages. Load is independent and
  uses its own known days/hours; a load gap does not erase generation. Actual source gaps yield
  **365/366 complete days in 2016** and **361/365 in 2018**; these are explicitly
  partial sums, not annual totals. Never coerce missing values to zero.
- **Charts:** daily generation/load power is GWh / actual day hours, in GW. All
  stacked generation sources have gaps on incomplete-generation dates to avoid a
  misleading total; observed load and prices remain visible independently. Price
  bars show daily means with null gaps, not hourly extremes.
- **Prices:** sum daily mean × actual hours, divided by known-price hours, independent
  of energy coverage. In 2015, 1–4 January remain unknown: 361/365 priced days and
  96 excluded hours. Count negative **daily mean days**, never negative hours.
  All-missing mean and negative-day count remain null.
- **Market areas:** DE–AT–LU through 30 September 2018, DE–LU from 1 October. The 2018
  blended annual price is labeled with both market areas; comparisons need care.
- **Nuclear:** a twelfth history-only generation/table/legend source, excluded from
  renewables. After 15 April 2023 absent nuclear values are structural zeros flagged
  by the producer; observed zeros remain unflagged. Recent charts retain 11 sources.
- **Provenance:** running-year corrections cover the last 35 completed days; closed
  years are frozen, changed only through explicit reconciliation. Selected dates,
  frozen/revisable status, partial-year coverage and derived-zero counts are visible.
  Source daily sums may contain upstream partial data/interpolation. “Complete day”
  means all required *daily* observations exist, not certified underlying hours.

History freshness uses the **latest manifest date**, never a selected closed year,
with the same 96-hour delay after the next Berlin midnight. The separate recent
warning continues to describe current hourly data. Both advance in open tabs.
The year selector is sufficient for historical exploration; there is no all-years
chart that would require downloading the entire history.

### Recent and trade partial metrics

Recent total generation, average and shares are suppressed when any generation
input in the selected period is missing. Per-source known sums/means carry coverage;
all stacked sources have a gap at an incomplete generation hour. Load and price use
their own known durations; all-missing metrics, including negative-hour counts and
extrema, stay null. Trade missing inputs require three null monthly totals; a missing
included month suppresses annual/YTD trade sums. Long-term annual generation keeps
its full-year suppression rule and separate frozen 2016/2018 supplements. See
[exact null-safe formulas and coverage](../docs/dashboard_partial_refresh.md#null-safe-frontend-metrics).

## Verification

Run from `frontend/` using Node 20:

```sh
npm test -- --runInBand
npm run build
npm run lint
npm start
```

Open `http://localhost:8080/dashboards/strom/`. Check YTD, all previous years, all
three hourly periods, negative prices, mobile layout, light/dark mode, JavaScript
disabled, blocked JSON and blocked ECharts. Inspect 2015 price gaps, 2016/2018 energy
coverage, 2018 price-zone labeling, and history-only nuclear. Race two selections,
switch to recent while history is pending, then fail a request and change theme.
Check `/themen/energie/` discovery and the Dashboard navigation.
Jest covers aggregation, DST, validation, stale/future dates, controller fallbacks,
period consistency, and chart-generation failure propagation.
`tests/electricity-history.test.js` adds real-file Node/WebCrypto validation, strict
contract mutations, missing/corrupt unselected partitions, daily aggregation, lazy
caching, 404/mixed manifests and timeout recovery. `tests/electricity-history-dashboard.test.js`
adds static YTD, every year, daily chart gaps/units, dynamic nuclear, independent
freshness and asynchronous controller regression coverage.
`tests/electricity-partial.test.js` adds September 13 price-null fixtures, independent
generation/load/price coverage, all-null DST metrics, v2 metadata mutations, nullable
trade totals and independent-cutoff overlap checks. Pipeline cross-language fixtures
validate Python-produced v2 bundles with these frontend helpers. Test coverage is
not a live source-recovery or deployment acceptance claim.

`src/scss/pages/_electricity.scss` holds scoped theme tokens and dashboard styles;
Sass emits the existing tracked `src/css/style.css` (include intentional dashboard styles). Generated legacy chart rewrites
are build artifacts, not dashboard source changes.
