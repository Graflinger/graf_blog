# Dashboard refresh: timing findings and deferred improvements

Research date: 14 September 2026. **Backlog only:** the current runtime is accepted
for now. This document does not authorize schedule changes, additional polling,
new infrastructure, relaxed validation, or production publication.

The later [partial-refresh feature](dashboard_partial_refresh.md) supersedes the
old common-cutoff behavior described in the historical baseline below. It keeps
the actual `0 6 * * *` schedule (06:00 UTC); the runtime/optimization backlog in
this document remains deferred. This baseline is not live acceptance of that feature.

## Measured baseline

[Release-first manual run 34879363165](https://github.com/Graflinger/databearer/actions/runs/34879363165)
succeeded on 14 September 2026. Production and its public verification completed in
**4m24s**; separate main synchronization completed in **2m26s**. Including runner
handoff, workflow creation to completion was approximately **6m57s**. The site did
not wait for main synchronization to finish.

| Stage | Measured time |
| --- | ---: |
| Production offline electricity tests | 69s |
| Production publisher/sync/verifier tests | 16s |
| Recent hourly source refresh and dbt validation | 39s |
| Current-year daily history refresh and overlap validation | 13s |
| Monthly commercial trade refresh | 11s |
| Production frontend installation/tests/lint/build | 31s |
| Public deployment polling and verification | 53s |
| Other production setup, artifact, publication, cleanup | 32s |
| Main candidate Python dependency installation | 15s |
| Main candidate offline electricity/script tests | 84s |
| Main candidate frontend installation/tests/lint/build | 34s |
| Other sync setup, merge, push, cleanup | 13s |

Merge preparation itself took about one second: the overall duration was not a
one-time migration cost. Changed daily snapshots normally require validation on
both released code and the main candidate. No-change publishing still validates
production and checks the public site, but already-integrated release ancestry
skips candidate validation and the main push. This single measurement is not a
runtime guarantee; collect several runs before setting performance targets.

## When does SMARD update?

Official references checked:

- [SMARD: Über SMARD](https://www.smard.de/home/ueber-smard): data is fetched
  automatically from ENTSO-E, checked for correctness/completeness, processed, and
  published. The overview's market data updates continuously. The RSS feed announces
  editorial articles/ticker entries, not a documented data-completeness event.
- [SMARD user manual, April 2026](https://www.smard.de/resource/blob/220052/9d526adf4b948599da4a956dfae6dab9/smard-benutzerhandbuch-04-2026-data.pdf):
  section B.1.3 describes revisions as new information arrives, with no fixed
  revision process; B.2 says aggregation requires all constituent data. Section
  D.1.1 says actual generation must be reported to ENTSO-E no later than one hour
  after the operating period. The homepage section describes its current-data tile
  with a three-hour delay. **Neither the reporting deadline nor the tile delay is
  a completeness guarantee for our 13 hourly API series.**
- [SMARD: Großhandelspreise](https://www.smard.de/blueprint/servlet/page/home/wiki-article/446/562):
  the price series represents day-ahead trading, not a common daily publication
  event for actual generation and load.
- [SMARD March 2026 data-gap notice](https://www.smard.de/home/update-info-zu-datenluecken-auf-smard-219610):
  interruptions in the upstream reporting chain can leave gaps until backfilled.

No fixed daily SMARD release clock or guaranteed completion time for the exact
series used here was established from these sources. Do not equate reporting
obligations, observation timestamps, API/cache modification times, or our snapshot
creation time with the instant a complete dataset first became available.

### Our completeness policy matters more than a nominal source clock

The pre-partial-refresh recent pipeline measured here, in
`pipeline/src/data_pipelines/dashboards/german_electricity/pipeline.py`:

- Uses the current **Europe/Berlin date** as `as_of`.
- Excludes today's partial data. Yesterday is eligible immediately; there is no
  mandatory two-day waiting period.
- Chooses the latest complete day across **all 13 hourly series** (11 generation
  categories, load, and price). It can fall back as far as `as_of - 4` if later
  days are incomplete. An internal gap in the resulting 30-day window fails validation.
- Exports `data_through` as an **exclusive boundary**, equal to `window_end`.
  History ends on the preceding Berlin date; this is not another extra day's lag.

In the successful September 14 run, the snapshot created at 18:14:38 UTC had
`data_through = 2026-09-12T22:00:00Z`: midnight at the start of September 13 in
Berlin. Therefore the **last included complete day was September 12**, and at least
one required hourly series was incomplete for September 13 at fetch time, assuming
the released default cutoff behavior. The export does not identify the missing
series/hour or when it later became available. An evening run still falling back
means an earlier morning schedule cannot be assumed to solve this delay.

Monthly trade includes only fully history-covered completed months. Annual
supplements are frozen; capacity/congestion progress remains separately monthly/manual.
Those datasets should not drive an intraday schedule for recent hourly data.

### Scheduling recommendation (deferred)

Keep the actual **06:00 UTC daily** schedule (`0 6 * * *`) unchanged. GitHub's
scheduled start is best-effort, not a guaranteed 06:00 execution. Distinguish:

1. **Upstream completeness lag** until all required source series are ready.
2. **Sampling/scheduler delay** between readiness and our next actual fetch.
3. **Processing/deployment delay** until the validated snapshot is publicly available.

Before picking a new hour, collect approximately 1–2 weeks of readiness evidence:

- First add diagnostics from already-fetched data: expected previous day, selected
  day, missing hourly counts by series, actual fetch/check time, and public cutoff.
  This need not add source requests or alter exported schemas.
- One observation per day cannot reveal the completion hour. If closer alignment
  is still needed, explicitly approve a temporary, bounded read-only availability
  probe at several candidate times. Fetch only the weekly chunks needed for the
  target completed day, not the full 35-day refresh/history/trade pipeline. Bound
  request count, retries, run duration, and total experiment length; do not deploy
  on each probe. Check daily-history readiness/overlap too before assuming hourly
  readiness alone permits publication.
- Use observed completion times (including weekends and late outliers) to choose
  the earliest reliable daily slot with a modest buffer. A 04:00–06:00 Berlin
  observation slot could test the overnight reporting hypothesis; it is **not an
  established optimal production time**. Account for CET/CEST explicitly.
- If availability varies too much, consider one bounded later catch-up only when
  the earlier run lacked yesterday. This would change the current one-daily-run
  policy and needs separate agreement on source requests, builds, and cost. Do not
  silently introduce hourly full refreshes or fill missing values.

## Deferred runtime backlog, in priority order

1. **Measure individual test and dbt setup durations.** Offline tests account for
   about 85s in production and 84s in sync. Investigate repeated dbt startup and
   fixture preparation before changing coverage. Preserve real dbt integration,
   data contracts, determinism, and all publication/ancestry safety checks.
2. **Cache sync dependency downloads.** Production has pip/npm caches; sync does
   not. Key caches to the prepared candidate's dependency files, runtime, and OS,
   not accidentally to release's requirements. Retain installation steps and a
   working cold-cache path. Expected gain is modest (seconds, not several minutes).
   Never cache DuckDB state or use cached outputs as validation evidence.
3. **Parallelize independent candidate validation carefully.** Pipeline/script
   tests and frontend checks currently run sequentially. After dependency setup,
   run them concurrently only after checking shared-file writes, temporary-path
   isolation, and resource contention. Require both exit statuses before push;
   recheck the clean candidate and refs as today. Rough potential saving is the
   shorter validation group, about 30s in this sample—not a guarantee. Additional
   runner jobs trade wall-clock savings for setup/runner cost.
4. **Evaluate production parallelism only after profiling.** Offline regression
   tests and live refresh may be independent with proper isolation, but don't race
   dbt targets/logs or snapshot writes. Preserve recent → history/overlap → trade
   order and require every test/build gate before publication. Main sync must still
   wait for verified production.
5. **Retain public verification and release/main isolation.** Shortening the
   verifier does not accelerate Cloudflare and could create false failures. Do not
   remove checks merely to turn the workflow green earlier. Main can contain
   different code, so passing production tests cannot replace candidate validation.

Acceptance for any later optimization: compare several cold/warm runs, maintain
the 10-minute production budget including the 240-second verifier and separate
10-minute sync cap, and keep failure/race/no-change regression tests passing.
No new server, persistent database, independent data deployment, or broad source
refresh is needed for the first optimizations.
