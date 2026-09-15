const fs = require('fs');
const path = require('path');
const { createHash } = require('crypto');
const { isDeepStrictEqual } = require('util');
const history = require('../../js/dashboards/electricity-history');
const electricity = require('../../js/dashboards/electricity-data');
const { verifySnapshot } = require('./electricitySnapshot');

const DIRECTORY = path.resolve(__dirname, '../../data-history/german-electricity');
const RECENT_PATH = path.resolve(__dirname, '../../_data/germanElectricity.json');
function readBounded(file, limit) {
  if (fs.lstatSync(file).isSymbolicLink() || fs.statSync(file).size > limit) throw new Error(`Invalid history file: ${file}`);
  return fs.readFileSync(file);
}
function verifyPartition(raw, entry) {
  if (createHash('sha256').update(raw).digest('hex') !== entry.sha256) throw new Error(`History SHA-256 mismatch: ${entry.year}`);
  return history.validatePartition(history.parseRaw(raw, 250000), entry);
}
// Both inputs have already passed their full schema/raw-hash validators.
function compareOverlap(manifest, partitions, recent) {
  const lastDate = electricity.dayKey(Date.parse(recent.data_through) - 1);
  if (recent.schema_version === 1 && manifest.last_date !== lastDate) throw new Error(`History/recent cutoff mismatch: ${manifest.last_date} / ${lastDate}; refresh recent then history together`);
  if (recent.refresh_status?.history && recent.refresh_status.history.data_through !== manifest.last_date) throw new Error('History/status cutoff mismatch');
  const daily = new Map(partitions.flatMap((partition) => partition.rows.map((row) => [row.date, row])));
  const hourly = new Map();
  for (const row of recent.rows) {
    const date = electricity.dayKey(row[0]);
    if (!hourly.has(date)) hourly.set(date, []);
    hourly.get(date).push(row);
  }
  let days = 0;
  for (const [date, points] of hourly) {
    // A valid subset history can start later than the recent window (e.g. January).
    // Within the declared history range, every overlapping day is mandatory.
    if (date < manifest.first_date || date > manifest.last_date) continue;
    const row = daily.get(date);
    if (!row || row.hours !== points.length) throw new Error(`History/recent missing day or hour mismatch: ${date}`);
    days++;
    for (const [index, column] of electricity.COLUMNS.entries()) {
      if (column === 'timestamp') continue;
      const sum = points.reduce((total, point) => total + point[index], 0);
      const price = column === 'price';
      const observed = price ? row.price_eur_mwh : row.energy_gwh[column];
      if (recent.schema_version === 2 && (observed === null || points.some((point) => point[index] === null))) continue;
      const expected = price ? sum / points.length : sum;
      const tolerance = price ? 0.011 : (points.length + 1) * 0.005 / 1000 + 1e-8;
      // Never let null become zero through subtraction. Recent v1 is complete
      // and post-2023, so none of history's allowlisted 2015–2018 gaps can overlap.
      const delta = observed === null ? Infinity : Math.abs(observed - expected);
      if (!Number.isFinite(delta) || delta > tolerance) {
        throw new Error(`History/recent mismatch ${date}/${column}: delta=${delta}, tolerance=${tolerance}; refresh recent then history together`);
      }
    }
  }
  if (!days && recent.schema_version === 1) throw new Error('History/recent have no overlapping days');
  return { days, last_date: lastDate };
}
function readHistory(directory = DIRECTORY, recentRaw = readBounded(RECENT_PATH, 1000000).toString('utf8')) {
  // Optional raw producer snapshot supports isolated tests; verification cannot
  // be bypassed by passing an independently parsed/claimed-valid object.
  const recent = verifySnapshot(recentRaw);
  // A concurrent producer can supersede a manifest. Retry the entire set, never mix.
  for (let attempt = 0; attempt < 2; attempt++) {
    const raw = readBounded(path.join(directory, 'manifest.json'), 20000);
    const manifest = history.validateManifest(history.parseRaw(raw, 20000));
    try {
      const partitions = manifest.years.map((entry) => verifyPartition(readBounded(path.join(directory, path.basename(entry.url)), 250000), entry));
      if (!raw.equals(readBounded(path.join(directory, 'manifest.json'), 20000))) continue;
      const overlap = compareOverlap(manifest, partitions, recent);
      const summary = history.summarize(partitions[partitions.length - 1]);
      return { manifest, manifestJSON: history.safeJSON(manifest), partitions,
        manifestHash: createHash('sha256').update(raw).digest('hex'),
        overlap, summary: { ...summary, text: history.presentation(summary) } };
    } catch (error) {
      if (error.code !== 'ENOENT' || attempt) throw error;
    }
  }
  throw new Error('History changed during build; retry the complete build');
}
function verifyPublished(outputDirectory) {
  const published = path.join(outputDirectory, 'data/history/german-electricity');
  const recentRaw = readBounded(RECENT_PATH, 1000000).toString('utf8');
  const recent = verifySnapshot(recentRaw);
  const { manifest, manifestJSON } = readHistory(published, recentRaw);
  // Recent JSON is rendered (not raw passthrough); compare its parsed contents
  // against the exact validated producer snapshot used for overlap verification.
  const publishedRecent = JSON.parse(readBounded(path.join(outputDirectory, 'data/german-electricity.json'), 1000000).toString('utf8'));
  if (!isDeepStrictEqual(publishedRecent, recent)) throw new Error('Published recent snapshot differs from history overlap input');
  for (const name of ['manifest.json', ...manifest.years.map((entry) => path.basename(entry.url))]) {
    if (!fs.readFileSync(path.join(DIRECTORY, name)).equals(fs.readFileSync(path.join(published, name)))) throw new Error(`Published history bytes differ: ${name}`);
  }
  const html = fs.readFileSync(path.join(outputDirectory, 'dashboards/strom/index.html'), 'utf8');
  if (recent.schema_version === 2 && !html.includes(`<script type="application/json" id="electricity-component-data">${history.safeJSON({ components: recent.components, refresh_status: recent.refresh_status })}</script>`)) throw new Error('Published HTML/component status mismatch');
  if (!html.includes(`<script type="application/json" id="electricity-history-manifest">${manifestJSON}</script>`)) throw new Error('Published HTML/history manifest mismatch');
}
module.exports = { readHistory, verifyPartition, verifyPublished };
