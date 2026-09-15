# Dashboard Plan B: history, independent publishing, and servers

Repository paths in this document are relative to the repository root.

Status: alternatives for evaluation; this document does not itself implement them.
The default remains [the stateless daily architecture](dashboard_architecture.md).
The approved [German electricity daily history](german_electricity_history.md)
now implements a narrow durable-partition variant: yearly compact JSON in the
repository, rolling current-year corrections, frozen closed years, manifest-last
publication, and bounded version retention. It adds no object store/server or
persistent database. Daily data-only publication is separately authorized in
[the publication workflow](dashboard_publication.md). The release-first design runs
released scripts/runtime/frontend, guards only the current release tip, pushes data
to release without force, and verifies public content before a separate validated
ancestry sync to main. Main need not equal release; sync failures do not block future
production refreshes. This remains Git-integrated full-site publication, with no
two-ref atomic promise. Production retains a ten-minute budget including its
240-second verifier; sync adds a separate ten-minute cap and validation/build cost.
New rollout requires deliberate promotion to both branches and live verification;
the September 10 success covered the old both-ref design only. Independent data or
asset publishing options below remain alternatives, not part of this implementation.

The approved [partial-refresh contract](dashboard_partial_refresh.md) also retains
validated recent values per component on source failure, using the existing tracked
export as durable input. This adds no persistent database or infrastructure and does
not activate the optional migrations below. Its manual rollout/live acceptance is pending.

Add infrastructure only for a demonstrated need. **Keeping history does not require
a server, and renting a server does not require making the website dynamic.** Keep
Eleventy/ECharts and CDN-served exports unless a feature genuinely needs a live API.

## 1. Identify what must change

| Need or observed constraint | Smallest plausible next step |
| --- | --- |
| More frequent refreshes cause commit noise or too many site builds | Publish prebuilt assets or data independently |
| Provider deletes old observations or revises past releases | Archive source snapshots in durable object storage |
| Re-fetching the full window exceeds the runtime/API budget | Durable partitions and incremental processing |
| A database must persist but there is only one batch writer | Persistent DuckDB on a single host, with backups |
| Multiple writers or interactive queries require shared state | Managed PostgreSQL or another suitable managed database |
| Jobs outgrow hosted CI limits or need tighter scheduling control | Managed batch scheduler/container job or a small server |
| Users need personalized or arbitrary queries | Small API backed by a database; retain static pages where possible |

Before choosing, measure refresh duration, transferred bytes, source availability,
build frequency, storage growth, query patterns, and expected monthly cost. Set
retention and recovery requirements explicitly rather than storing everything forever.

## 2. Option A: publish without data commits

Keep GitHub Actions and stateless ingestion, but publish generated output directly.

### A1. Build the complete site in Actions and deploy it

- Produce the data, run checks, build Eleventy, and upload the complete `_site`
  directory to Cloudflare Pages using Wrangler.
- Keep code in Git without committing each operational snapshot. This changes
  deployment ownership, not the pipeline's need for durable database state.
- Define one production publication path for both blog changes and scheduled
  refreshes. Prevent a regular Git-triggered deployment from racing with or
  replacing fresh data with an older snapshot. A Pages deployment is a full-site
  publication, not a patch of a few data files.
- Explicitly decide how every code deployment obtains dashboard data: regenerate
  it or read a durable last-good export. Neither runner-local files nor an expiring
  Actions artifact are a permanent source of truth.

Cloudflare supports Wrangler deployments to a Git-integrated project with automatic
Git builds disabled via branch settings. Review current deployment limits and costs;
do not assume all kinds of deployment are unlimited because the build ran elsewhere.
See [external CI uploads](https://developers.cloudflare.com/pages/how-to/use-direct-upload-with-continuous-integration/)
and [Git integration](https://developers.cloudflare.com/pages/configuration/git-integration/).

### A2. Publish data independently of the site

- Keep normal blog deployments; upload small JSON/CSV exports to object storage
  behind a CDN, for example Cloudflare R2 or another S3-compatible service.
- The dashboard loads our prepared data, not upstream APIs. No database or API
  server is required to serve static snapshots.
- Use versioned dataset paths and a small manifest containing schema version,
  coverage, content hashes, and freshness. Upload and validate every file first,
  then atomically replace the manifest/pointer. Readers must see a complete version,
  not a mixture of old and new series.
- Give versioned files long cache lifetimes and the mutable manifest a short,
  explicit freshness policy. Retain old versions long enough for cached readers and
  rollback. Configure CORS narrowly if using a separate origin; never ship upload
  credentials to the browser.
- Version the frontend/data contract so a data refresh cannot break deployed code.

**Trade-off:** cleaner Git history and no full-site rebuild per data update, but
additional credentials, storage/cache configuration, and error handling. This is
the natural first migration if hourly refreshes become worthwhile.

## 3. Option B: durable history without an always-on server

Keep scheduled Actions (or another batch runner) and archive data in object storage.

Decide which kind of history is needed:

- **Observation history:** a time series of events/measurements, usually partitioned
  by observation date and useful for charts.
- **Source-vintage history:** what the provider reported at a particular retrieval
  or release date, needed to reproduce analyses despite later revisions.

Store immutable raw responses when licensing permits and/or typed Parquet partitions.
Record source URL, retrieval timestamp, source release, checksum, schema version,
and transformation code revision. Preserve both observation time and retrieval time
where revisions matter. Avoid duplicate blobs through content hashes, while keeping
a retrieval manifest when the fact that a check occurred matters.

Each job can still use a temporary DuckDB database to query selected archived files,
process new/revised periods, and produce the same frontend export contract.

Required operational rules:

- A defined primary key and upsert/deduplication policy.
- A lookback window for late arrivals/revisions; do not assume an append-only source.
- Watermarks advance only after durable data and manifests are successfully written.
- Retries and backfills are idempotent; serialize writers or use conditional updates
  to prevent lost manifest/watermark changes.
- Retention/lifecycle policies, storage/request/egress budgets, and restore tests.
- A documented recovery path from archives after losing all local/cache state.

Do not treat Actions caches or short-lived artifacts as the only historical archive.
Avoid passing a mutable DuckDB file among concurrent runners or treating object
storage as a shared writable database filesystem. Immutable partitions are generally
an easier fit for batch history than synchronizing one database file.

**Trade-off:** durable, reproducible history with little server administration, but
more work around manifests, revisions, retention, and incremental correctness.

## 4. Option C: managed persistent database and optional API

Use a managed database when concurrent access, relational updates, or interactive
queries justify it. PostgreSQL is one candidate; choose after measuring the workload,
not just because a free tier exists.

- Actions or managed scheduled jobs remain responsible for ingestion.
- Use migrations, stable keys, transactions, idempotent upserts, and a run ledger.
- Separate ingestion privileges from any read-only serving role. Store credentials
  in secrets; restrict network access and use encrypted connections.
- Export precomputed chart datasets to the CDN whenever practical. Most dashboard
  visits still do not need a live database query.
- If an API is required, add query bounds, caching, pagination, rate limits, timeouts,
  and authentication where needed. Never expose a database password to client JS.
- Budget for backups, retention, compute, storage, connections, and egress; verify
  free-tier sleep/inactivity behavior and recovery guarantees before depending on it.

**Trade-off:** shared durable state and less host administration, but recurring cost,
provider dependence, migrations, and a larger security/operational surface. A database
alone does not provide a reliable scheduler, tested backups, or a safe public API.

## 5. Option D: small server with persistent disk

Run the existing pipeline on a VPS or suitable existing machine using a container
or virtual environment, persistent storage, and a timer/cron schedule. The frontend
can remain entirely on Cloudflare Pages.

- Start with DuckDB for a single batch writer; explicitly lock/serialize refresh jobs
  and avoid unsupported concurrent multi-process writes. Use a database designed
  for concurrent clients if the workload changes.
- Keep generated frontend exports separate from the active database; publish only
  after validation. Do not publicly expose the database file or ingestion process.
- Use atomic updates/transactions, bounded retries, job timeouts, and missed-run
  recovery. A timer improves control but does not make uptime or punctuality automatic.
- Provide off-host backups, a documented restore procedure, and periodic restore
  tests. A persistent disk or provider snapshot alone is not a complete backup plan.
- Own OS/security updates, credentials, firewall rules, disk monitoring, log rotation,
  alerting, and recovery after reboot or host loss.
- If using a home machine, account for power/network interruptions and do not assume
  it is more reliable or cheaper overall than hosted batch execution.
- If adding a self-hosted Actions runner, isolate it from untrusted pull-request code;
  do not expose persistent production state or secrets to arbitrary public CI jobs.

**Trade-off:** maximum control and a straightforward persistent local database, but
ongoing maintenance and fixed hosting/resource costs. Choose this because the
workload needs it, not merely because the word “dashboard” suggests a backend.

## 6. Option E: managed scheduled jobs or serverless execution

A managed scheduler plus container/batch job can replace Actions while keeping the
pipeline recognizable. Pair it with object storage or a managed database for state.

Check runtime, memory, temporary disk, dependency packaging, cold-start behavior,
network access, scheduling precision, retries, overlapping execution, and billing.
Do not assume a lightweight edge function can run the existing Python/dbt/DuckDB
stack unchanged. A container job may fit it better than a short-lived function.

**Trade-off:** no host patching and potentially better batch scheduling controls, but
provider-specific configuration and quotas. Retain a local entry point and portable
exports so the provider is an execution choice, not the data model.

## 7. Migration principles and exit criteria

Across all options:

1. Preserve the dashboard's versioned data contract and the separation from frozen
   blog-post snapshots. Keep browser payloads compact and CDN-cacheable.
2. Keep transformations testable with fixed inputs/windows. Adding state must not
   remove idempotency, deterministic exports, or safe failure behavior.
3. Introduce one capability at a time: independent publishing, then history or a
   database only if needed. More visitors alone usually do not justify a backend.
4. Assign an authoritative data store and one production publisher. Document schema
   ownership, credentials, retention, backup, and the exact rollback path.
5. Backfill and run old/new paths in parallel without dual production writes. Compare
   coverage, units, keys, freshness, hashes/tolerances, runtime, and cost.
6. Test provider outages, interrupted writes, duplicate runs, late revisions, and
   restoration. Switch only after the new path reproduces the expected snapshot.
7. Set budget alerts and document what stops or degrades when a quota is exhausted.
   Define acceptable data loss (RPO) and recovery time (RTO) before promising service
   reliability that the chosen storage/scheduler cannot deliver.

Recommended escalation: **stateless daily MVP → independent static data publishing
if cadence demands it → object-storage history if sources demand it → persistent
database/server only for a demonstrated processing or query requirement.** These
steps are alternatives to combine selectively, not a mandatory roadmap.
