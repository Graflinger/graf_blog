# databearer

Monorepo for the **databearer** data-journalism blog (https://blog.databearer.de).

Public and open-sourced for reproducibility/transparency of the data analysis. A separate
**private** `video-generator` project (not in this repo) consumes the published site to produce videos.

## Documentation

- [Dashboard architecture](docs/dashboard_architecture.md): the daily dashboard design and approved durable-export exceptions.
- [Dashboard publication](docs/dashboard_publication.md): authorized daily data-only publication, branch guards, rollout status, and recovery.
- [Dashboard partial refresh](docs/dashboard_partial_refresh.md): v2 nulls/coverage, durable per-component last-good retention, coordinated CLI, failure recovery and pending manual rollout.
- [Plan B](docs/plan_b.md): alternatives for persistent history, independent publishing, and servers.
- [German electricity dashboard](docs/german_electricity_data.md): SMARD licensing, methodology, refresh commands, and validation.
- [Electricity history](docs/german_electricity_history.md): yearly data from 2015, YTD, rolling corrections, and reconciliation.
- [Electricity trade](docs/electricity_trade.md): monthly commercial DE–LU imports and exports from 2019, definitions and runtime budget.
- [Annual supplements](docs/electricity_annual.md): official 2016/2018 annual totals without inventing missing daily observations.
- [Capacity and congestion](docs/electricity_progress.md): separately refreshed power-capacity trends, statutory milestones, storage power, and congestion management; source rights and limitations.
- [Agent guidance](AGENTS.md): repository-wide working conventions.

Paths and shell commands below are relative to the repository root.

## Structure

| Folder | What | Stack |
|--------|------|-------|
| [`pipeline/`](pipeline/) | Data pipeline — ingest, transform, export datasets/CSV | Python, DuckDB, dbt |
| [`frontend/`](frontend/) | The blog site (consumes the data, renders charts) | Eleventy (11ty) v3, Apache ECharts |
| [`image-generation/`](image-generation/) | Generates blog header/card images | Python, Azure FLUX |

Data flow: **pipeline** produces datasets → **frontend** renders them as posts/charts →
**image-generation** creates header images → finals land in `frontend/src/images/blog_card_images/`.

## Branching & deployment

Production is deployed by **Cloudflare Pages from the `releases/cloudflare` branch** — not from
`main`. Blog/code changes retain an explicit manual promotion gate. The authorized
daily dashboard publisher advances production with validated data-only changes using
released code, then separately synchronizes verified release ancestry into current
`main`. Unpublished main changes do not block production refreshes.

| Branch | Role |
|--------|------|
| `feature/*`, `bugfix/*` | Day-to-day work. Open a PR into `main`. |
| `main` | Integration / trunk. Always buildable; PRs merge here. Code changes require manual production promotion. |
| `releases/cloudflare` | **Production.** Cloudflare Pages builds & deploys from here (Root directory = `frontend`). |

**Manual blog/code publish flow:** first incorporate the latest release ancestry
into `main` through successful sync or reviewed manual reconciliation. Use a real
merge if diverged; a squash does not preserve release ancestry. Preserve newer
production snapshots and resolve conflicts explicitly, then run offline
electricity/script tests and frontend tests/lint/build. After review and checks:

```bash
# 1. develop -> reviewed main; integrate and validate latest release ancestry
# 2. fetch current tips; stop/reconcile again if release advanced since review
git fetch origin
# 3. from a clean checkout, promote the reviewed current origin/main:
git switch releases/cloudflare
git merge --ff-only origin/releases/cloudflare
git merge --ff-only origin/main # only after latest release is contained in reviewed main
git push origin releases/cloudflare   # Cloudflare Pages picks it up and deploys
```

Use non-force fast-forward promotion. If either tip moves, fetch, review, integrate
and validate again rather than overwrite newer snapshots. Daily data commits land
directly on release through the guarded publisher; `main` and release need not
routinely be equal. The publisher does not release main's code/blog changes.

**Daily data publication:** `dashboard-refresh.yml` schedules 06:00 UTC daily;
manual dispatch has `publish=false` by default and refreshes/validates only the
selected ref. Publishing requires the `main` **event ref**, then explicitly checks
out `releases/cloudflare` for scripts, runtime, and frontend. The pre-fetch guard
requires `HEAD == origin/releases/cloudflare` and ignores main. Only validated
recent data, current-year history, and monthly trade may be committed and pushed
**to release only, without force**. Monthly/manual capacity and congestion and
frozen annual supplements are outside the daily refresh and write allowlist.

The uncommitted [partial-refresh feature](docs/dashboard_partial_refresh.md) stages
recent/history/trade through `.refresh`, retaining validated last-good components
on source errors with explicit status and coverage. This is an approved durable
recent-export exception, with no persistent database. Implementation/PR authorization
does not authorize production rollout; feature live acceptance and manual code
promotion to both branches remain pending. Frozen exports are not rewritten.

Publishing runs verify public snapshots/HTML for up to 240 seconds, including
no-change runs. Only the verified release SHA permits a separate `sync-main` job:
merge exact release ancestry into a worktree on current main, run offline
electricity/script tests and frontend tests/lint/build without live source fetching,
recheck both tips, then push **main only, without force**. Bot main pushes cannot
rely on `GITHUB_TOKEN` triggering push CI. A failed sync makes the workflow red but
leaves verified production intact and does not block future production refreshes.
No-change publishing runs retry outstanding sync. There is no two-ref atomic promise.
Production retains its ten-minute job budget including verification; sync has a
separate ten-minute cap and extra installation/test/build cost.

**New rollout is pending:** deliberately promote this implementation to **both
branches** before the main schedule relies on released scripts. The
[September 10 manual run](https://github.com/Graflinger/databearer/actions/runs/34532806712)
succeeded under the **old both-ref design**, including public verification, in about
5m47s; September 11/12 old guards stopped because main was ahead. Those runs do not
verify the new release-first/sync design. A push is not proof of deployment. See the
[publication runbook](docs/dashboard_publication.md) for rollout and recovery.

## Important: running the pipeline

The pipeline code uses repo-relative paths (`src/config/...`, `.data/output/...`) and
`from src...` imports. After the monorepo move it must be run **from the `pipeline/` directory**
(so `pipeline/` is the working dir and on `PYTHONPATH`). The devcontainer is preconfigured for this
(`PYTHONPATH=${containerWorkspaceFolder}/pipeline`).

```bash
cd pipeline
pip install -r requirements.txt
# run pipeline scripts from here
```

## Frontend

```bash
cd frontend
npm install
npm start          # dev server + hot reload
npm run build      # production build -> _site
```

Hosting: **Cloudflare Pages**, built from the **`releases/cloudflare`** branch with
**Root directory = `frontend`**, served at `blog.databearer.de` (see
[Branching & deployment](#branching--deployment)).

## Image generation

```bash
cd image-generation
cp .env.example .env   # fill in AZURE_FLUX_API_KEY
# run image_generation_flux.ipynb
```

Generated images are written to `../generated-images/` (gitignored). Curate finals into
`frontend/src/images/blog_card_images/`.
