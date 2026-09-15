const data = require('../../js/dashboards/electricity-data');
const { safeJSON } = require('../../js/dashboards/electricity-history');

// Shared rendering contract for production and isolated template tests.
function summary(snapshot) {
  const value = data.summarize(snapshot);
  const components = data.componentReport(snapshot);
  return { ...value, text: data.presentation(value), components,
    partial: components.some((component) => component.status !== 'complete') || Object.values(snapshot.refresh_status || {}).some((meta) => meta.status !== 'ok'),
    createdLabel: data.timestampLabel(Date.parse(snapshot.snapshot_created_at)),
    stale: data.freshness(snapshot).stale };
}
function statusJSON(snapshot) {
  return safeJSON({ components: snapshot.components, refresh_status: snapshot.refresh_status });
}
function register(env) {
  env.addFilter('electricitySummary', summary);
  env.addFilter('electricityNumber', data.number);
  env.addFilter('electricityStatusJSON', statusJSON);
}
module.exports = { summary, statusJSON, register };
