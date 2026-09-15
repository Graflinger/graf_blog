# AGENTS.md

This file provides guidance to coding agents when working with the frontend in this repository.

## Project Overview

Databearer is a German data journalism blog (databearer.de) built with Eleventy (11ty) v3. The site covers topics including energy (Energie), politics & society (Politik & Gesellschaft), and economics (Wirtschaft). Posts feature interactive Apache ECharts visualizations generated from CSV data.

## Commands

```bash
# Development server with hot reload
npm start

# Production build
npm run build

# Deployment: Cloudflare Pages builds this folder (root dir = frontend) and serves blog.databearer.de

# Generate charts only
npm run build:charts

# Generate specific chart config file
node "src/data_ingestion/generate-charts.js" industriepolitik.js
```

## Architecture

### Live Dashboards

Read [dashboard_architecture.md](../docs/dashboard_architecture.md) before adding dashboard
pages, chart data, freshness metadata, or refresh/deployment automation. It specifies
the default daily, stateless pipeline and approved durable-export exceptions using the existing stack. Keep live dashboard
datasets separate from frozen blog-post snapshots, and render prepared exports rather
than fetching raw upstream data in visitors' browsers.

Read [plan_b.md](../docs/plan_b.md) when evaluating independent data publishing, durable
history, databases, or servers. These are optional future alternatives, not prerequisites
for the first dashboard. Read [dashboard_publication.md](../docs/dashboard_publication.md)
for the implemented daily workflow, released-base guard, and rollout status.

The electricity dashboard now has approved [persistent daily history](../docs/german_electricity_history.md).
Read [partial refresh](../docs/dashboard_partial_refresh.md) for the approved durable
recent per-component exception, v1/v2 compatibility, nullable metrics and explicit
coverage/statuses, independent daily cutoffs and available overlap checks. Use
coordinated `.refresh`; source failures retain last-good data, shared errors abort
the bundle. Keep DuckDB temporary. Implementation/PR authorization does not authorize
production rollout; feature live acceptance and manual code promotion to both
branches remain pending. Do not rewrite frozen exports to migrate the code.
Preserve raw hashed partition bytes, source gaps, nuclear-era and price-zone metadata.
Load historical years lazily; never interpret missing observations as zero or daily
price averages as negative-hour counts. Daily data-only publication is authorized at
06:00 UTC. Deliberately promote the release-first implementation to **both `main`
and `releases/cloudflare`** before the schedule uses released scripts; new live
verification is pending. The September 10 success used the old both-ref design.
Manual `publish=false` defaults to refreshing/validating only the selected ref.
Publishing requires the `main` event ref, then explicitly checks out release for
released scripts/runtime/frontend. Guard `HEAD == origin/releases/cloudflare` before
source fetching; main's position does not gate production. Only validated
recent/current-year-history/trade exports may be committed and pushed **release
only, without force**, then publicly verified, including on no-change runs.

Only verified `release_sha` output enables separate `sync-main` using the released
script: check exact release SHA, create a worktree from current main, merge real
release ancestry, and run offline electricity/script tests plus frontend
tests/lint/build on Node 20 before a non-force **main-only** push. Recheck both refs;
fetch no live source data during sync. Bot main pushes cannot rely on push CI.
Conflicts, validation failures, and races must fail without silently overwriting
main or changing schemas. Sync failure makes the workflow red but leaves verified
production intact and future refreshes possible; no-change publishing runs retry
outstanding sync. There is no two-ref atomic promise. Production retains a ten-minute
budget including its 240-second verifier; sync has its own ten-minute cap and extra
validation/build cost. Follow the runbook's separate public-failure and sync recovery.

Normal code/blog changes require manual promotion: incorporate latest release
ancestry into reviewed main via sync or explicit real-merge reconciliation, preserve
newer snapshots, pass checks, then fast-forward release without force. Main and
release need not routinely equal. Monthly/manual progress and frozen annual
supplements are excluded from daily source refreshes and the write allowlist.
Public copy describes automatic daily updates with actual observation dates, while
progress retains its separate cadence. A Git push is not proof of deployment.


### Directory Structure
- `src/` - Source files (Eleventy input)
- `src/_includes/` - Nunjucks layouts (`base.njk` for site shell, `post.njk` for blog posts)
- `src/_data/` - Global data files (`site.js` contains locale, author, metadata)
- `src/posts/{year}/` - Markdown blog posts organized by year
- `src/themen/` - Topic landing pages (energie, wirtschaft, politik-und-gesellschaft)
- `src/data_ingestion/` - Chart generation system (excluded from Eleventy build)
- `_site/` - Build output

### Chart Generation Pipeline
Charts are auto-generated before each Eleventy build via `.eleventy.js` hook:

1. **Data files**: Place CSV in `src/data_ingestion/data/`
2. **Config files**: Create JS config in `src/data_ingestion/charts/` exporting an array of chart configs
3. **Output**: Generated JS files go to `src/js/charts/{config-name}/`

Chart config structure:
```javascript
{
  type: 'line' | 'bar',
  dataFile: 'data.csv',           // relative to data/
  outputFile: 'chart.js',         // output filename
  containerId: 'chart-id',        // DOM element ID
  xKey: 'column_name',
  yKey: 'value',                  // single series
  seriesKeys: ['col1', 'col2'],   // multi-series
  seriesNames: ['Label1', 'Label2']
}
```

### Post Frontmatter
```yaml
title: "Post Title"
date: 2025-01-01
excerpt: "Short description"
image: "/images/blog_card_images/2025/filename.png"
imageText: "Image caption"
topic: ["energie", "wirtschaft"]  # array, determines collections
fullWidthCard: false              # optional
lastUpdated: 2025-01-15           # optional
```

### Collections
Defined in `.eleventy.js`:
- `post` - All posts from `src/posts/**/*.md`
- `energiePosts`, `politikPosts`, `wirtschaftPosts` - Filtered by topic array

### Using Charts in Posts
```html
<script src="/js/lib/echarts.min.js"></script>
<div id="chart-id" style="width: 100%; height: 400px;"></div>
<script src="/js/charts/config-name/chart.js"></script>
```

Charts support dark mode detection and lazy loading via IntersectionObserver.

## Key Files
- `.eleventy.js` - Eleventy config, filters, collections, chart generation hook
- `src/_includes/base.njk` - Site layout with SEO meta, structured data, navigation
- `src/data_ingestion/generate-charts.js` - Chart generation entry point
- `src/data_ingestion/builders/` - Chart builder modules (lineChart.js, barChart.js)
