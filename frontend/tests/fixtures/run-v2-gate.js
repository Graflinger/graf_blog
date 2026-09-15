/* Full offline gate in a disposable copy. Never replace real dashboard snapshots. */
const fs = require('fs');
const os = require('os');
const path = require('path');
const assert = require('assert');
const { createHash } = require('crypto');
const { spawnSync } = require('child_process');
const { canonical } = require('./electricity-trends');
const data = require('../../src/js/dashboards/electricity-data');
const history = require('../../src/js/dashboards/electricity-history');
const { midnight } = require('./electricity-history');

const frontend = path.resolve(__dirname, '../..');
const root = path.dirname(frontend);
const hash = (bytes) => createHash('sha256').update(bytes).digest('hex');
const iso = (time) => new Date(time).toISOString().replace('.000Z', 'Z');
function encode(snapshot, recent = false) {
  const semantic = { ...snapshot };
  delete semantic.content_hash;
  if (recent) delete semantic.snapshot_created_at;
  snapshot.content_hash = hash(canonical(semantic));
  return canonical(snapshot) + (recent ? '\n' : '');
}
function inject(directory) {
  const source = path.join(directory, 'src');
  const recentPath = path.join(source, '_data/germanElectricity.json');
  const snapshot = JSON.parse(fs.readFileSync(recentPath));
  const finalDay = data.dayKey(Date.parse(snapshot.window_end) - 1);
  const previousEnd = midnight(finalDay);
  snapshot.schema_version = 2;
  for (const row of snapshot.rows) {
    if (row[0] >= previousEnd) { row[13] = null; row[9] = null; }
  }
  snapshot.rows.at(-1)[12] = null;
  snapshot.rows.at(-2)[5] = null;
  snapshot.components = Object.fromEntries(data.COLUMNS.slice(1).map((key, index) => {
    const known = snapshot.rows.filter((row) => row[index + 1] !== null);
    return [key, { status: key === 'gas' ? 'stale' : known.length === snapshot.rows.length ? 'complete' : 'partial',
      known_hours: known.length, expected_hours: snapshot.rows.length,
      last_successful_window_end: key === 'gas' ? iso(previousEnd) : snapshot.window_end,
      source_observed_through: known.length ? iso(known.at(-1)[0] + data.HOUR) : null }];
  }));
  const historyDirectory = path.join(source, 'data-history/german-electricity');
  const manifestPath = path.join(historyDirectory, 'manifest.json');
  const manifest = JSON.parse(fs.readFileSync(manifestPath));
  // Exercise a nullable current year and an annual daily_sum gap beyond the frozen
  // 2016/2018 supplements. All changes are confined to the disposable copy.
  for (const entry of manifest.years.filter((entry) => entry.year >= manifest.years.at(-1).year - 1)) {
    const partition = JSON.parse(fs.readFileSync(path.join(historyDirectory, path.basename(entry.url))));
    partition.schema_version = 2;
    const row = partition.rows.at(-1);
    row.energy_gwh.gas = null;
    row.energy_gwh.load = null;
    row.price_eur_mwh = null;
    const bytes = canonical(partition);
    entry.sha256 = hash(bytes);
    entry.url = `${history.PREFIX}${entry.year}.${entry.sha256}.json`;
    fs.writeFileSync(path.join(historyDirectory, path.basename(entry.url)), bytes);
  }
  fs.writeFileSync(manifestPath, canonical(manifest));
  const tradePath = path.join(source, '_data/germanElectricityTrade.json');
  const trade = JSON.parse(fs.readFileSync(tradePath));
  trade.schema_version = 2;
  Object.assign(trade.rows.at(-1), { missing_series: [4486], imports_gwh: null, exports_gwh: null, net_exports_gwh: null });
  fs.writeFileSync(tradePath, encode(trade));
  snapshot.refresh_status = { history: { status: 'stale', data_through: manifest.last_date },
    trade: { status: 'partial', data_through: trade.last_month } };
  fs.writeFileSync(recentPath, encode(snapshot, true));
  data.validateSnapshot(snapshot);
  assert(snapshot.components.price.known_hours < snapshot.rows.length);
  assert.strictEqual(data.summarize(snapshot).priceAverage, null);
}
function fingerprints(directory) {
  const results = {};
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    if (['node_modules', '_site', '.git'].includes(entry.name)) continue;
    const file = path.join(directory, entry.name);
    if (entry.isDirectory()) Object.assign(results, fingerprints(file));
    else if (entry.isFile()) results[file] = hash(fs.readFileSync(file));
  }
  return results;
}
function run(command, args, cwd) {
  const result = spawnSync(command, args, { cwd, stdio: 'inherit', env: process.env });
  if (result.error) throw result.error;
  assert.strictEqual(result.status, 0, `${command} ${args.join(' ')} failed`);
}
const before = fingerprints(frontend);
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'electricity-full-v2-'));
try {
  const copy = path.join(temporary, 'frontend');
  fs.cpSync(frontend, copy, { recursive: true, filter: (file) => !['node_modules', '_site', '.git'].includes(path.basename(file)) });
  fs.symlinkSync(path.join(frontend, 'node_modules'), path.join(copy, 'node_modules'), 'dir');
  fs.cpSync(path.join(root, 'scripts'), path.join(temporary, 'scripts'), { recursive: true });
  inject(copy);
  console.log('Full v2 gate: populated recent/history/trade with nulls, partial coverage and retained failures.');
  run('npm', ['test', '--', '--runInBand', '--no-cache'], copy);
  run('npm', ['run', 'lint'], copy);
  run('npm', ['run', 'build'], copy);
  run(process.env.PYTHON || 'python3', ['-B', '-c',
    'from pathlib import Path; from scripts.verify_dashboard_deployment import load_expected, verify_once; root=Path.cwd(); output=root/"frontend/_site"; verify_once(load_expected(root), lambda url: (output/url.lstrip("/")/("index.html" if url.endswith("/") else "")).read_bytes()); print("Built v2 public HTML/data verifier: PASS")'], temporary);
  console.log('Full v2 frontend suite + lint + build + built-output public verification: PASS');
} finally {
  fs.rmSync(temporary, { recursive: true, force: true });
  assert.deepStrictEqual(fingerprints(frontend), before, 'Original frontend files changed during isolated gate');
}
