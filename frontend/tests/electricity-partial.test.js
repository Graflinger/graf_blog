/** @jest-environment node */
const { createHash } = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const data = require('../src/js/dashboards/electricity-data');
const history = require('../src/js/dashboards/electricity-history');
const trade = require('../src/js/dashboards/electricity-trade');
const { verifySnapshot } = require('../src/data_ingestion/builders/electricitySnapshot');
const { readHistory } = require('../src/data_ingestion/builders/electricityHistory');
const { aggregate } = require('../src/data_ingestion/builders/electricityTrends');
const { canonical } = require('./fixtures/electricity-trends');
const { midnight, nextDate } = require('./fixtures/electricity-history');

const iso = (time) => new Date(time).toISOString().replace('.000Z', 'Z');
const hash = (text) => createHash('sha256').update(text).digest('hex');
function recent(endDay = '2026-09-15') {
  const end = midnight(endDay);
  const startDay = new Date(Date.parse(`${endDay}T00:00:00Z`) - 30 * 86400000).toISOString().slice(0, 10);
  const start = midnight(startDay);
  const value = { schema_version: 2, source: data.SOURCE, timezone: data.TIMEZONE, window_start: iso(start), window_end: iso(end), data_through: iso(end),
    snapshot_created_at: iso(end), columns: data.COLUMNS, rows: [], units: { power: 'GW', price: 'EUR/MWh' }, expected_update: 'daily', stale_after_hours: 96,
    components: {}, refresh_status: {} };
  for (let time = start; time < end; time += data.HOUR) value.rows.push([time, ...Array(12).fill(1), -10]);
  return value;
}
function encode(value) {
  value.components = Object.fromEntries(data.COLUMNS.slice(1).map((key, index) => {
    const known = value.rows.filter((row) => row[index + 1] !== null);
    return [key, { status: known.length === value.rows.length ? 'complete' : 'partial', known_hours: known.length, expected_hours: value.rows.length,
      last_successful_window_end: value.window_end, source_observed_through: known.length ? iso(known.at(-1)[0] + data.HOUR) : null }];
  }));
  const { content_hash, snapshot_created_at, ...semantic } = value;
  void content_hash; void snapshot_created_at;
  value.content_hash = hash(canonical(semantic));
  return `${canonical(value)}\n`;
}
function partition(end = '2026-09-14', year = 2026) {
  const rows = [];
  for (let day = `${year}-01-01`; day <= end; day = nextDate(day)) {
    rows.push({ date: day, hours: history.hours(day), energy_gwh: { ...Object.fromEntries(data.SOURCES.map(({ key }) => [key, history.hours(day)])), load: history.hours(day), nuclear: 0 },
      price_eur_mwh: -10, price_zone: 'DE-LU', nuclear_derived_zero: true });
  }
  return { schema_version: 2, source: data.SOURCE, timezone: data.TIMEZONE, year, rows };
}
function historyFixture(part) {
  const raw = canonical(part);
  const entry = { year: part.year, days: part.rows.length, first_date: part.rows[0].date, last_date: part.rows.at(-1).date, frozen: false,
    sha256: hash(raw), url: `${history.PREFIX}${part.year}.${hash(raw)}.json` };
  history.validatePartition(part, entry);
  const manifest = { schema_version: 1, kind: 'german-electricity-history', timezone: data.TIMEZONE, source: data.SOURCE, years: [entry],
    first_date: entry.first_date, last_date: entry.last_date, revision_policy: history.POLICY };
  return { manifest, manifestHash: hash(canonical(manifest)), partitions: [part] };
}

test('September 13 null price day: honest 1/7/30-day means, coverage, negative hours and zero', () => {
  const input = recent('2026-09-14');
  for (const row of input.rows) if (data.dayKey(row[0]) === '2026-09-13') row[13] = null;
  input.rows[0][13] = 0;
  const snap = verifySnapshot(encode(input));
  expect(data.summarize(snap, 1).priceAverage).toBeNull();
  expect(data.summarize(snap, 1).negativeHours).toBeNull();
  expect(data.summarize(snap, 1).priceMin).toBeNull();
  expect(data.summarize(snap, 1).generationAverage).toBe(11);
  const week = data.summarize(snap, 7);
  expect(week.priceAverage).toBe(-10);
  expect(week.negativeHours).toBe(144);
  expect(data.presentation(week).priceText).toContain('144/168');
  const month = data.summarize(snap, 30);
  expect(month.priceAverage).toBe(-6950 / 696);
  expect(month.priceMax).toBe(0);
});

test.each(['gas', 'solar', 'load'])('%s missing: healthy independent means and incomplete generation suppression', (key) => {
  const input = recent();
  input.rows.at(-1)[data.COLUMNS.indexOf(key)] = null;
  const snap = verifySnapshot(encode(input));
  const result = data.summarize(snap);
  expect(result.priceAverage).toBe(-10);
  if (key !== 'load') {
    expect(result.generationEnergy).toBeNull();
    expect(result.renewableShare).toBeNull();
    expect(result.loadAverage).toBe(1);
    expect(result.mix.every((source) => source.share === null)).toBe(true);
  } else {
    expect(result.loadAverage).toBe(1);
    expect(result.loadEnergy).toBe(23);
    expect(result.generationAverage).toBe(11);
  }
});

test.each(['2026-03-30', '2026-10-26'])('all-null %s keeps DST coverage and produces no invented zero/NaN/infinity', (end) => {
  const input = recent(end);
  input.rows.forEach((row) => row.fill(null, 1));
  const snap = verifySnapshot(encode(input), Date.parse(input.window_end));
  const result = data.summarize(snap);
  expect(result.hours).toBe(end.includes('03') ? 23 : 25);
  for (const key of ['generationEnergy', 'generationAverage', 'renewableShare', 'loadAverage', 'loadEnergy', 'priceAverage', 'priceMin', 'priceMax', 'negativeHours']) expect(result[key]).toBeNull();
  expect(result.mix.every((source) => source.energy === null && source.average === null)).toBe(true);
  expect(JSON.stringify(data.presentation(result))).not.toMatch(/NaN|Infinity/);
});

test('coverage and freshness metadata cannot contradict values', () => {
  const input = recent();
  encode(input);
  input.components.gas.known_hours--;
  expect(() => data.validateSnapshot(input)).toThrow('abdeckung');
  encode(input);
  input.components.gas.source_observed_through = input.window_start;
  expect(() => data.validateSnapshot(input)).toThrow('Beobachtungshorizont');
  encode(input);
  input.components.gas.status = 'stale';
  expect(data.componentReport(input).find((row) => row.key === 'gas').stale).toBe(true);
});

test('latest numeric horizon is exact, including zero; aged-out retained observations remain valid', () => {
  const input = recent();
  input.rows.slice(-120).forEach((row) => { row[13] = null; });
  input.rows.at(-121)[13] = 0;
  encode(input);
  expect(() => data.validateSnapshot(input)).not.toThrow();
  expect(data.componentReport(input, Date.parse(input.window_end)).find((row) => row.key === 'price').stale).toBe(true);
  input.components.price.source_observed_through = input.window_end;
  expect(() => data.validateSnapshot(input)).toThrow('Beobachtungshorizont');
  input.rows.forEach((row) => { row[13] = null; });
  encode(input);
  expect(() => data.validateSnapshot(input)).not.toThrow();
  input.components.price.source_observed_through = input.window_end;
  expect(() => data.validateSnapshot(input)).toThrow('Beobachtungshorizont');
  Object.assign(input.components.price, { status: 'stale', source_observed_through: input.window_start, last_successful_window_end: input.window_start });
  expect(() => data.validateSnapshot(input)).not.toThrow();
});

test('daily nulls use compatible generation days, independent load and price hours; all-null stays unknown', () => {
  const part = partition();
  part.rows[0].energy_gwh.load = null;
  part.rows[1].energy_gwh.gas = null;
  part.rows[2].price_eur_mwh = null;
  historyFixture(part);
  const summary = history.summarize(part);
  expect(summary.completeDays).toBe(part.rows.length - 1);
  expect(summary.loadDays).toBe(part.rows.length - 1);
  expect(summary.priceDays).toBe(part.rows.length - 1);
  expect(summary.loadAverage).toBe(1);
  expect(summary.priceAverage).toBe(-10);
  part.rows.forEach((row) => { Object.keys(row.energy_gwh).forEach((key) => { if (key !== 'nuclear') row.energy_gwh[key] = null; }); row.price_eur_mwh = null; });
  historyFixture(part);
  const empty = history.summarize(part);
  expect(empty.generationEnergy).toBeNull();
  expect(empty.loadEnergy).toBeNull();
  expect(empty.priceAverage).toBeNull();
  expect(empty.negativeDays).toBeNull();
  expect(JSON.stringify(history.presentation(empty))).not.toMatch(/NaN|Infinity/);
});

test('nullable month makes annual trade null; load gaps do not erase complete generation trends', () => {
  const part = partition('2025-12-31', 2025);
  part.rows[0].energy_gwh.load = null;
  const input = historyFixture(part);
  const snapshot = JSON.parse(fs.readFileSync(path.join(__dirname, '../src/_data/germanElectricityTrade.json'), 'utf8'));
  snapshot.rows = snapshot.rows.filter((row) => row.month <= '2025-12');
  snapshot.last_month = '2025-12';
  snapshot.schema_version = 2;
  const month = snapshot.rows.at(-1);
  month.missing_series = [4486];
  month.imports_gwh = month.exports_gwh = month.net_exports_gwh = null;
  trade.validateSnapshot(snapshot);
  const result = aggregate(input, snapshot);
  expect(result.energy[0].generation_twh).toBeGreaterThan(0);
  expect(result.trade.years.at(-1).complete_months).toBe(11);
  expect(result.trade.years.at(-1).net_exports_twh).toBeNull();
  part.rows[0].energy_gwh.gas = null;
  expect(aggregate(historyFixture(part), snapshot).energy[0].generation_twh).toBeNull();
});

test('independent cutoffs/null inputs pass; available overlap contradiction still fails', () => {
  const input = recent();
  input.rows.forEach((row) => { if (data.dayKey(row[0]) === '2026-09-13') row[13] = null; });
  const part = partition('2026-09-13');
  part.rows.at(-1).price_eur_mwh = 100;
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'partial-overlap-'));
  function write() {
    const { manifest } = historyFixture(part);
    fs.writeFileSync(path.join(directory, path.basename(manifest.years[0].url)), canonical(part));
    fs.writeFileSync(path.join(directory, 'manifest.json'), canonical(manifest));
  }
  try {
    write();
    expect(readHistory(directory, encode(input)).manifest.last_date).toBe('2026-09-13');
    part.rows.at(-1).energy_gwh.gas += 1;
    write();
    expect(() => readHistory(directory, encode(input))).toThrow('2026-09-13/gas');
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});
