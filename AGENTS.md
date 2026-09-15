# Databearer agent guidance

This repository contains the Python/DuckDB/dbt data pipeline in `pipeline/`, the
Eleventy/ECharts frontend in `frontend/`, and image helpers in `image-generation/`.
Follow directory-specific guidance, including [frontend/AGENTS.md](frontend/AGENTS.md),
when working in those areas. Load the relevant project skills under `.opencode/skills/`.

Repository-level documentation lives in [`docs/`](docs/). Keep `README.md` and this
`AGENTS.md` at the repository root; place new cross-project documentation in `docs/`.

## Dashboard architecture

Before implementing or changing dashboard ingestion, scheduling, exports, or
publication, read:

- [Dashboard architecture](docs/dashboard_architecture.md): the default design and
  acceptance checklist for one daily, stateless dashboard using the existing stack.
- [Dashboard partial refresh](docs/dashboard_partial_refresh.md): approved durable
  recent per-component retention, v2 null/status contracts, coordinated `.refresh`,
  independent history cutoff, hard shared failures and recovery. Feature live
  acceptance/manual promotion to both branches remain pending; implementation/PR
  authorization does not authorize production rollout or frozen-export rewrites.
- [Plan B](docs/plan_b.md): optional paths for independent publishing, durable history,
  managed databases, scheduled compute, or a server. Do not introduce these without
  an explicit need and agreement on the trade-offs.
- [Dashboard publication](docs/dashboard_publication.md): authorized daily data-only
  publication, released-base guards, public verification, and recovery.
- [German electricity history](docs/german_electricity_history.md): approved exception
  storing validated daily history in yearly Git-tracked partitions. Refresh only the
  current year's correction window; closed years require explicit reconciliation.
- [Monthly electricity trade](docs/electricity_trade.md): DE–LU commercial imports
  and exports from 2019. Use source monthly aggregates and the bounded three-month
  correction window; do not derive trade from generation minus load.
- [Annual electricity supplements](docs/electricity_annual.md): frozen official
  SMARD annual aggregates for 2016/2018 complete the annual charts only. Never infer
  missing daily observations from them or fetch these years during routine refreshes.
- [Electricity progress](docs/electricity_progress.md): separately refreshed SMARD
  compact capacity and congestion data. Keep net capacity and statutory targets
  separate; no target-attainment ratios. Do not add this monthly/manual source to
  the daily refresh or treat congestion measures as outages or renewable losses.

Dashboard runs are stateless by default; approved electricity recent per-component
retention, history and trade use validated repository snapshots as durable state.
All runs must remain idempotent and select only necessary sources/models. Do not
persist the DuckDB database between runs. Keep dependencies, source requests, and
runtime bounded; publish only validated, compact exports. Preserve frozen blog-post
datasets and the last working dashboard
on shared failure. Use coordinated `.refresh`: stage recent, independent daily history
with available overlapping observations checked, then monthly trade. Isolate source
failures with explicit statuses; shared corruption/dbt/storage/consistency errors
abort the bundle. Build long-term generation trends from existing history
without additional source requests.
Daily production data publication is authorized in `dashboard-refresh.yml` at 06:00
UTC. The release-first implementation must be deliberately promoted to **both `main`
and `releases/cloudflare`** before the schedule relies on released scripts; new live
rollout verification is pending (the September 10 success used the old both-ref design).
Manual dispatch defaults to `publish=false`, refreshing/validating only the selected
ref. Publishing requires the `main` event ref, then explicitly checks out
`releases/cloudflare` for released scripts, runtime, and frontend. Require
`HEAD == origin/releases/cloudflare` before source fetching; main's position does
not gate production. Commit only validated recent/current-year-history/trade
allowlist changes and push **release only**, without force. Verify public data/HTML,
including no-change runs; Git push success is not a deployment guarantee.

Only verified `release_sha` output permits the separate `sync-main` job. Using the
released sync script, check the exact release SHA, prepare a separate worktree from
current main, and merge real release ancestry. Run offline electricity/script tests
and frontend tests/lint/build on the candidate before a non-force **main-only** push;
fetch no live source data during sync. Bot `GITHUB_TOKEN` pushes cannot rely on push
CI. Fail on conflicts or branch races without silently overwriting main or changing
schemas. Sync failure makes the workflow red but leaves verified production intact
and future production refreshes possible; no-change publishing runs retry outstanding
sync. There is no two-ref atomic update promise. Preserve the ten-minute production
job budget including the 240-second verifier; sync has its own ten-minute cap and
additional validation/build cost.

Normal blog/code and monthly/manual progress changes retain manual promotion:
incorporate latest release ancestry into reviewed main via sync or explicit real-merge
reconciliation, preserving newer snapshots, then fast-forward release without force.
Main and release need not routinely equal. Preserve explicit history/trade
reconciliation at rollover; never auto-correct closed years. Follow the publication
runbook's separate recovery paths for public verification failures and sync failures.

## Working conventions

- Run pipeline commands from `pipeline/`, with that directory on `PYTHONPATH`.
- Run frontend commands from `frontend/`; use `npm test` and `npm run build` for
  frontend changes. Follow pipeline skill guidance for focused dbt selection/tests.
- Keep `.data/`, raw downloads, and credentials out of Git. Preserve unrelated work.
- Do not commit, push, or deploy unless requested; documenting a future automated
  publication workflow does not authorize publishing during the current task.
