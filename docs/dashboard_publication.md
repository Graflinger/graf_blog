# Daily electricity dashboard publication

Repository paths are relative to the repository root.

## Rollout status

**Release-first publication is implemented; its new live rollout is pending.**
Deliberately promote this implementation to **both `main` and
`releases/cloudflare`** before the schedule uses the released scripts. Updating
`main` alone is insufficient: the workflow definition runs from `main`, but
publishing explicitly checks out the release branch for scripts, runtime, and frontend.

The uncommitted [partial-refresh feature](dashboard_partial_refresh.md) requires the
same deliberate manual code promotion to both branches. Its implementation and PR
are authorized, **not production rollout during this task**. No live refresh/deploy
acceptance of this feature has occurred, and frozen exports are not rewritten for
code promotion. The September 14 baseline recorded in
[runtime notes](dashboard_refresh_improvements.md) predates this feature.

Historical evidence, distinct from this rollout:

- **10 September 2026:** [manual run 34532806712](https://github.com/Graflinger/databearer/actions/runs/34532806712)
  succeeded with the **old both-ref publication design** and public verification,
  in approximately **5m47s**. This confirms that historical run, not the new sync path.
- **11 and 12 September 2026:** the old equality guards stopped scheduled runs
  because `main` was ahead of production.
- **New release-first rollout:** production publication/public verification and the
  separate validated `sync-main` job still require live verification and recording.

Production remains Cloudflare Pages' Git integration on `releases/cloudflare`, with
root directory `frontend`. Normal blog/code changes require manual promotion.
The approved exception publishes only validated daily electricity exports from an
already released commit, then separately synchronizes verified release ancestry
into current `main`. The branches need not routinely be equal; unpublished `main`
work does not gate production data refreshes.

## Trigger, branch gate, and publication scope

- Schedule: **06:00 UTC daily**, cron `0 6 * * *` (07:00 MEZ / 08:00 MESZ).
  Scheduling is best effort, not a promised publication time.
- Manual `workflow_dispatch`: boolean **`publish`, default `false`**. The default
  refreshes/validates the selected ref only and retains a seven-day review artifact;
  it neither publishes nor synchronizes branches. Set `publish=true` on `main` for
  an explicitly requested production refresh.
- Publishing requires the **event ref `github.ref == refs/heads/main`**. After that
  authorization, the workflow explicitly checks out **`releases/cloudflare`**.
  `scripts/dashboard_publish.py check` requires a clean release-branch checkout and
  **`HEAD == origin/releases/cloudflare`**, freshly fetched **before source fetching,
  dependency installation, or building**. The publisher neither reads nor pushes
  `main`; its position is irrelevant to this guard. Production refresh and frontend
  validation use released code and dependencies, even when `main` has newer changes.
- Runs share a serialized concurrency group, with no cancellation of an in-progress
  publisher. Recheck the released base before committing; a competing branch update
  must fail safely rather than be overwritten.

Only these changed paths may enter the automated commit:

| Path | Permitted daily change |
| --- | --- |
| `frontend/src/_data/germanElectricity.json` | Validated recent hourly snapshot |
| `frontend/src/_data/germanElectricityTrade.json` | Validated monthly trade; current-year, three-completed-month correction window |
| `frontend/src/data-history/german-electricity/manifest.json` | Validated current-year history references/coverage; prior year may be marked frozen at rollover |
| `frontend/src/data-history/german-electricity/YEAR.<sha256>.json` | Current **Europe/Berlin calendar year** only: new immutable referenced partition and bounded removal of superseded retention files |

Closed-year partition bytes and trade rows stay frozen. Annual supplements and
monthly/manual progress data are outside this write allowlist, as are generated
JavaScript/CSS, templates, posts, workflows, raw downloads, and databases. An
unexpected changed path or staged change fails publication; never use `git add .`.

`scripts/dashboard_publish.py` checks the base and changed paths. Following all
data and frontend checks, it commits only changed allowlisted exports and performs
one **non-force push to `releases/cloudflare` only**. Although that command uses
`--atomic`, it has a single destination ref: there is **no two-ref atomic publication
promise**. A rejected release push stops production publication without updating `main`.
No-change output creates no commit and triggers no new Git-based Cloudflare build.

**Git publication is not Cloudflare deployment.** A successful push
only establishes the repository commit; the external build and public content
still need verification.

## Refresh and validation order

1. Check the released base for a publishing run.
2. Install the narrow Python 3.11 dashboard runtime and run all offline electricity
   Python tests (`test_german_electricity*.py`). Tests for annual/progress use
   fixtures; they do not authorize live refreshes of those sources. Run the
   publication, synchronization, and verifier safeguards in `scripts/tests/` too.
3. Run `python -m src.data_pipelines.dashboards.german_electricity.refresh` from
   `pipeline/` with `PYTHONPATH=.`. It validates the existing bundle and stages recent
   per-component refresh in a fresh temporary DuckDB database with focused dbt tests.
4. Within that coordinator, attempt current-year history using its independent
   reported-day cutoff and available recent overlap, then monthly DE–LU trade
   against the validated history cutoff, even after an isolated source failure.
5. Validate and promote the complete local candidate bundle with embedded statuses.
   Source `ComponentUnavailable` retains original history/trade; recent failures
   retain per-series last-good values. Shared errors and available contradictions
   abort the bundle. See [failure boundary and CLI](dashboard_partial_refresh.md#coordinated-cli-failure-boundary-and-recovery).
6. Use **Node 20** for frontend tests, lint, and production build. The build validates
   recent/history overlap, hashes, trends, progress, and rendered exports/HTML.
   The current workflow also runs these checks on unchanged data.
7. Retain the validated review artifact. For publishing runs, apply the allowlist,
   commit changed exports, push only the release ref, and verify public deployment.
8. Only after successful public verification, emit the exact `release_sha` job
   output. The separate `sync-main` job consumes that output to synchronize `main`.

Long-term trends use existing history, trade, and the frozen 2016/2018 annual
supplements without extra source requests. **Never fetch annual supplements in the
daily run.** Capacity/congestion progress is refreshed separately by hand, at most
monthly, and promoted through the normal manual release process. Checking its
published hash each day does not refresh its source or advance its observation dates.

The production/validation job budget remains **10 minutes**, including installation,
tests, refresh, build, and up to 240 seconds of public verification. Individual source
deadlines are additional bounds, not additive entitlements beyond that job timeout.
Measure runner time and external Cloudflare builds on rollout. No-change runs still
cost validation time; changed data causes the pre-publication build plus Cloudflare's
external build. The separate sync job has **its own 10-minute cap** and additional
installation/test/build cost when a merge candidate changes `main`; the whole workflow
is no longer bounded to ten minutes. No-change publishing runs can still incur that
sync cost when integration is outstanding. Keep daily cadence and account for
ordinary blog/preview builds.

Partial refresh can take longer than the old early-abort path because healthy
components are still attempted, with additional staging/validation and regression
test cost. Existing request/worker/deadline bounds remain; measure feature runtime
at authorized rollout rather than treating old timings as acceptance.

See [refresh timing and deferred improvements](dashboard_refresh_improvements.md)
for the September 14 live timing baseline, SMARD publication-time findings, and
performance ideas retained for later. These notes do not change the active schedule
or authorize additional polling.

## Separate synchronization to main

`sync-main` needs the production job's **verified `release_sha` output**. A failed
refresh, push, or public verification does not authorize sync. Both sync commands
run using `scripts/dashboard_sync.py` from the released checkout.

1. Fetch current `main` and release refs. Require the released checkout and remote
   release tip to match the exact verified SHA; a later release is not silently
   substituted.
2. Create a separate worktree on current `origin/main`. Merge the exact release
   with **real ancestry**: a fast-forward when possible, otherwise a direct merge
   with current main and that release as parents. If main already contains the
   release, preparation is a no-op. Never squash, copy snapshots over main, or
   automatically resolve conflicts with an ours/theirs preference.
3. For a changed candidate, install its narrow runtime and run all offline
   `test_german_electricity*.py` tests plus `scripts/tests/test_*.py`, then frontend
   tests, lint, and build on Node 20 **in the candidate worktree**. There is no live
   source fetching or dashboard refresh in sync. These checks establish that main's
   code can consume the integrated snapshots without changing their schemas.
4. Recheck the candidate's clean worktree and ancestry, that main still equals its
   prepared base, and that release still equals the verified SHA. Push **only `main`,
   without force**. Ordinary non-fast-forward rejection protects concurrent main
    advances after the fetch. Already-integrated no-ops also undergo the final checks.
    Fetch both tips again after the push/no-op and fail on observed movement. A
    main-only push cannot lock release: a race detected afterward may mean main
    already contains the validated candidate. Never roll either ref back. These are
    point-in-time checks, not a cross-branch transaction or a lock on later writers.

A sync conflict, failed validation, or branch race makes the **workflow red**, while
the production job/output and summaries distinguish **production already verified**
from **main synchronization failed**. Sync never reverts production and a failed sync
does not prevent future production refreshes. Every successfully verified publishing
run, **including no-change runs**, attempts sync again against current main. This is
a fresh attempt; a stale prepared merge is never blindly retried.

## Public deployment verification

`scripts/verify_dashboard_deployment.py` polls the public HTTPS site for at most
**240 seconds**, at ten-second intervals. It compares the intended local snapshots
with:

- `/data/german-electricity.json`: content, hash, and observation cutoff;
- `/data/history/german-electricity/manifest.json` and the latest referenced yearly
  partition, including that partition's **raw-byte SHA-256**;
- `/data/german-electricity-trends.json`: history/trade/annual input identities and
  coverage;
- `/data/german-electricity-progress.json`: the separately maintained snapshot;
- `/dashboards/strom/`: snapshot markers, embedded manifest/trends/progress JSON,
  expected script links, active dashboard navigation, and v2 embedded recent
  components/history/trade refresh status matching the intended local snapshot.

Missing, old, or mismatched remote data fails the run after the polling budget.
**No-change publishing runs still verify deployment.** Verification is read-only:
it does not retry Cloudflare builds, deploy, or manufacture timestamp-only changes.
It proves the checked public responses match the intended snapshot, not a general
availability guarantee or the health of every browser/CDN location.

From the repository root of the intended released checkout:

```bash
python3 scripts/verify_dashboard_deployment.py --timeout 240 --interval 10
```

## Permissions and new release-first rollout

The publisher uses the repository **`GITHUB_TOKEN`**, with least-scope
**`contents: write`** for the release-only publication and separate main-only sync.
Do not bypass branch protections or add a broad personal token to trigger CI.
Token-authenticated pushes
do not trigger ordinary GitHub Actions `push` workflows. Production validation must
finish inside the production job, and the bot's main push **cannot rely on push CI**:
the merged candidate must pass checks inside `sync-main` before pushing. PR checks
should run all offline electricity Python tests, publisher/sync/verifier tests in
`scripts/tests/`, and frontend tests/lint/build on Node 20.

Cloudflare's Git integration is external to Actions. Its reaction to the new
release-first bot push must be checked during **new rollout**, including project branch/path
filters, build status, public snapshot, and manifest/partition cache headers. Do
not infer successful deployment from Actions push-recursion behavior. No Cloudflare
API token is stored or required for this Git-based publication/read-only verification.

Rollout procedure:

1. Complete clean PR checks, review branch protections and token permissions, and
   coordinate deliberate promotion of this implementation to **both branches**.
   The schedule on main must not begin relying on scripts absent from or still old
   on release. Merging main alone does not complete rollout.
2. **Reconcile current release ancestry before promotion.** Incorporate the latest
   release into reviewed main using a real merge where diverged; another squash does
   not preserve ancestry. Preserve newer production snapshots as a coherent set
   (recent, manifest/referenced partitions, trade) and review any conflict or schema
   incompatibility explicitly. Rerun offline pipeline/script and frontend checks.
3. Manually fast-forward `releases/cloudflare` to that reviewed main, without force,
   so both contain this implementation. Fetch/review again if either branch moved;
   do not overwrite a newer daily snapshot. This initial deliberate code release is
   separate from daily data publication; routine ref equality is not required.
4. Request a manual publishing run on `main` (or observe the first scheduled run).
   Confirm released runtime/frontend, the allowed release-only data commit,
   Cloudflare's external Git build, and public verification. Confirm the exact
   verified SHA feeds sync, its candidate checks pass, and only main is pushed by
   that job. Exercise no-change verification with an outstanding sync and confirm
   production success is distinguishable from sync failure before marking rollout verified.
5. Record run/deployment URLs, checked commit, public hashes/coverage, measured total
   runtime **for each job**, and both outcomes here. **These new live results are pending.**

Normal subsequent blog/code and monthly/manual progress promotion remains a reviewed
fast-forward of `releases/cloudflare` to the reviewed main commit. First ensure
successful sync or manually reconcile
the **latest** release ancestry into main, preserve newer snapshots, and pass checks.
Fetch both refs immediately before promotion; if release has advanced, integrate and
validate again before a non-force release push. Main may hold unpublished code while
daily production refresh continues on released code. Do not use ref equality as a
routine precondition or silently change data contracts to make a merge pass.

## Failure and recovery

- **Isolated source failure:** retain the affected validated component and expose
  stale/unavailable status while healthy components may advance. Explicit source
  nulls may advance as partial data. The next bounded coordinated run retries and
  recalculates statuses on recovery; it does not fill nulls or widen correction
  windows. See [partial-refresh recovery](dashboard_partial_refresh.md#coordinated-cli-failure-boundary-and-recovery).
- **Shared validation, consistency, storage, or build failure:** fail without pushing.
  The last published snapshot remains the serving target; local intermediate outputs
  are not publication. Investigate the failing contract and rerun from the current
  released base. Restore a coherent bundle after an interrupted local multi-file write.
- **Production branch movement or rejected release push:** fetch/review the release
  tip and start a fresh publishing run from it. Main need not equal release. Do not
  force-push or retry a stale prepared commit over concurrent work.
- **Push succeeded, public verification failed:** the new commit can exist on the
  release ref while Cloudflare still serves the old site. No verified output is
  emitted, so `sync-main` does not run. Inspect the external deployment
  for the intended commit; fix the cause and manually retry that deployment through
  Cloudflare's controls. Run the read-only verifier again from the intended checkout.
  Then request a fresh publishing run on main to obtain the verified output and
  attempt sync (or let the next schedule do so). A standalone verifier invocation
  does not authorize a sync job. A no-change refresh detects this condition but does
  not repair Cloudflare automatically; after deployment recovery it can verify and sync.
- **Production verified, sync conflict or candidate validation failure:** inspect the
  production and sync job summaries separately; do not roll back a good deployment
  to fix main. Fetch both current tips, make a reviewed integration branch from
  current main, and merge current release with real ancestry. Resolve conflicts
  explicitly, preserving newer snapshots and coherent manifest/partition references
  alongside intended main changes. Do not blindly choose ours/theirs or change schemas
  to suppress failures. Run offline electricity/script tests and frontend tests/lint/build
  without live refreshes, then integrate through the reviewed process preserving that
  ancestry. Never force-push. The next publishing run will check/retry sync, even if
  data is unchanged; outstanding conflicts require human reconciliation.
- **Sync branch race or rejected main push:** leave verified production alone. Discard
  the stale candidate/state and fetch/review current tips. Start a fresh publishing
  run, which re-verifies the current release and prepares a new sync against current
  main. If release advanced, never substitute it for the old job's verified SHA or
  reuse old verification/state. If main advanced, validate a newly prepared merge;
  do not replay the stale main push. Manual reconciliation uses the checks above.
- **Bad published snapshot:** restore the last known-good complete Cloudflare
  deployment, preserving manifest and referenced files together. Prepare a reviewed
  corrective Git commit and manual promotion so subsequent runs target the intended
  fixed version. Do not blindly retry a superseded deployment.
- **Missed correction window / first year rollover:** explicitly reconcile the
  affected history/trade year using the commands below, review/test the complete
  result, and manually promote it. January may require manual completion of
  31 December in history and December in trade after SMARD makes them available.
  Routine refresh never fetches/corrects a prior closed year. Do not expand the daily
  allowlist, loosen overlap tolerances, or fill absent observations to avoid this stop.
- **Schedule inactivity:** inspect Actions failures and disabled schedules. GitHub
  can disable public-repository schedules after 60 days without activity; successful
  no-change runs are not a keepalive guarantee. Re-enable deliberately after checking
  refs and data coverage, using reconciliation if the correction window was missed.

From `pipeline/`, with the affected year explicitly selected:

```bash
PYTHONPATH=. python -m src.data_pipelines.dashboards.german_electricity
PYTHONPATH=. python -m src.data_pipelines.dashboards.german_electricity.history backfill --start-year YEAR --end-year YEAR --reconcile
PYTHONPATH=. python -m src.data_pipelines.dashboards.german_electricity.history refresh
PYTHONPATH=. python -m src.data_pipelines.dashboards.german_electricity.trade backfill --start-year YEAR --end-year YEAR --reconcile
PYTHONPATH=. python -m src.data_pipelines.dashboards.german_electricity.trade refresh
PYTHONPATH=. python -m src.data_pipelines.dashboards.german_electricity.refresh
```

Choose only the affected reconciliation steps after inspecting coverage. See the
[history](german_electricity_history.md) and [trade](electricity_trade.md) contracts
for cutoff/continuity requirements. Recovery that changes closed years is a manual
reviewed release, not a daily publisher rerun.
The final coordinated run recomputes embedded statuses/cutoffs after standalone
reconciliation; standalone commands do not maintain the whole bundle's metadata.

References: [architecture](dashboard_architecture.md),
[frontend checks](../frontend/README-dashboard.md),
[GitHub workflow triggering](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow),
[scheduled events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
