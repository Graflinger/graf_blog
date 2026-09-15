/** @jest-environment node */
const fs = require('fs');
const path = require('path');
const os = require('os');
const { createHash, webcrypto } = require('crypto');
const history = require('../src/js/dashboards/electricity-history');
const build = require('../src/data_ingestion/builders/electricityHistory');
const { nextDate, endExclusive } = require('./fixtures/electricity-history');
const directory = path.resolve(__dirname, '../src/data-history/german-electricity');
const manifestRaw = fs.readFileSync(path.join(directory, 'manifest.json'));
const manifest = history.validateManifest(history.parseRaw(manifestRaw, 20000));
const clone = (value) => JSON.parse(JSON.stringify(value));
const entry = (year) => manifest.years.find((item) => item.year === year);
const rawYear = (year) => fs.readFileSync(path.join(directory, path.basename(entry(year).url)));
const partition = (year) => JSON.parse(rawYear(year));
const response = (raw) => ({ ok: true, arrayBuffer: async () => raw });

test('the entire real manifest passes Node and WebCrypto raw-byte checks, including actual nulls', async () => {
  expect(build.readHistory().manifest).toEqual(manifest);
  let gaps = 0;
  for (const item of manifest.years) {
    const raw = rawYear(item.year);
    const verified = await history.verifyPartition(raw, item, webcrypto.subtle);
    expect(build.verifyPartition(raw, item)).toEqual(verified);
    // Frozen v1 years keep the five documented gaps; v2 years may add new ones.
    if (verified.schema_version === 1) gaps += verified.rows.filter((row) => !history.complete(row)).length;
  }
  expect(gaps).toBe(5);
});

test.each([
  (m) => { m.extra = 1; },
  (m) => { m.schema_version = 2; },
  (m) => { m.source.license = 'CC0'; },
  (m) => { m.source.extra = 'x'; },
  (m) => { m.revision_policy = 'all numeric'; },
  (m) => { m.years = []; },
  (m) => { m.years.splice(1, 1); },
  (m) => { m.years.reverse(); },
  (m) => { m.years[0].url = 'https://example.com/2015.json'; },
  (m) => { m.years[0].sha256 = 'A'.repeat(64); },
  (m) => { m.years[0].frozen = 1; },
  (m) => { m.years[0].days = 364; },
  (m) => { m.years[0].last_date = '2015-02-30'; },
  (m) => { m.years[0].first_date = '2015-01-02'; },
  (m) => { m.last_date = nextDate(m.last_date); },
])('rejects malformed manifest #%#', (mutate) => {
  const candidate = clone(manifest);
  mutate(candidate);
  expect(() => history.validateManifest(candidate)).toThrow();
});

test.each([
  (p) => { p.extra = 1; },
  (p) => { p.source.name = 'Other source'; },
  (p) => { p.year = 2019; },
  (p) => { p.rows.pop(); },
  (p) => { p.rows[1].date = p.rows[0].date; },
  (p) => { p.rows[0].hours = '24'; },
  (p) => { p.rows.find((row) => row.hours === 23).hours = 24; },
  (p) => { p.rows[0].energy_gwh.extra = 1; },
  (p) => { p.rows[0].energy_gwh.nuclear = null; },
  (p) => { p.rows[0].energy_gwh.load = null; },
  (p) => { p.rows[0].energy_gwh.solar = '5'; },
  (p) => { p.rows[0].energy_gwh.solar = -1; },
  (p) => { p.rows[0].energy_gwh.solar = Infinity; },
  (p) => { p.rows[0].energy_gwh.solar = 5001; },
  (p) => { p.rows[0].price_eur_mwh = null; },
  (p) => { p.rows[0].price_eur_mwh = 10001; },
  (p) => { p.rows[0].price_zone = 'DE-LU'; },
  (p) => { p.rows.find((row) => row.date === '2018-10-01').price_zone = 'DE-AT-LU'; },
  (p) => { p.rows[0].nuclear_derived_zero = true; },
  (p) => { p.rows[0].nuclear_derived_zero = 0; },
])('rejects malformed partition #%#', (mutate) => {
  const candidate = partition(2018);
  mutate(candidate);
  expect(() => history.validatePartition(candidate, entry(2018))).toThrow();
});

test('only the five specific energy gaps may be null; a reconciled numeric value is valid', () => {
  for (const year of [2016, 2018]) {
    const candidate = partition(year);
    const gaps = candidate.rows.filter((row) => !history.complete(row));
    for (const row of gaps) {
      const key = Object.keys(row.energy_gwh).find((key) => row.energy_gwh[key] === null);
      row.energy_gwh[key] = 1;
    }
    expect(history.validatePartition(candidate, entry(year))).toBe(candidate);
    candidate.rows[0].energy_gwh.pumped_storage = null;
    expect(() => history.validatePartition(candidate, entry(year))).toThrow();
  }
});

test('nuclear is numeric, zero after shutdown, and derived zeros are explicitly flagged', () => {
  const p = partition(2023);
  expect(p.rows.find((row) => row.date === '2023-04-15').energy_gwh.nuclear).toBeGreaterThan(0);
  const post = p.rows.find((row) => row.date === '2023-04-16');
  expect(post.energy_gwh.nuclear).toBe(0);
  post.nuclear_derived_zero = true;
  expect(history.validatePartition(p, entry(2023))).toBe(p);
  post.energy_gwh.nuclear = 1;
  expect(() => history.validatePartition(p, entry(2023))).toThrow();
});

test.each([[2016, 365, 366], [2018, 361, 365]])('%i generation uses compatible days; load has independent coverage', (year, days, total) => {
  const p = partition(year);
  const summary = history.summarize(p);
  expect(summary.completeDays).toBe(days);
  expect(summary.days).toBe(total);
  const known = p.rows.filter(history.complete);
  const hours = known.reduce((sum, row) => sum + row.hours, 0);
  expect(summary.hours).toBe(hours);
  const loads = p.rows.filter((row) => row.energy_gwh.load !== null);
  const load = loads.reduce((sum, row) => sum + row.energy_gwh.load, 0);
  expect(summary.loadEnergy).toBe(load);
  expect(summary.loadAverage).toBe(load / loads.reduce((sum, row) => sum + row.hours, 0));
  for (const source of summary.mix) {
    const energy = known.reduce((sum, row) => sum + row.energy_gwh[source.key], 0);
    expect(source.energy).toBe(energy);
    expect(source.average).toBe(energy / hours);
  }
  const renewable = summary.mix.filter((source) => source.renewable).reduce((sum, source) => sum + source.energy, 0);
  expect(summary.renewableShare).toBe(renewable / summary.generationEnergy * 100);
  expect(summary.priceDays).toBe(total);
  expect(history.presentation(summary).coverage).toContain(`${days}/${total} vollständige Tage`);
  expect(history.presentation(summary).coverage).toContain('keine Jahressummen');
});

test('2015 excludes four unknown price days/96 hours, while energy remains complete', () => {
  const p = partition(2015);
  const summary = history.summarize(p);
  expect(summary.completeDays).toBe(365);
  expect(summary.priceDays).toBe(361);
  expect(summary.totalHours - summary.priceHours).toBe(96);
  expect(summary.priceAverage).toBe(p.rows.slice(4).reduce((sum, row) => sum + row.price_eur_mwh * row.hours, 0) / summary.priceHours);
  expect(summary).not.toHaveProperty('negativeHours');
  expect(summary).not.toHaveProperty('priceMin');
  p.rows[0].price_eur_mwh = 0;
  expect(() => history.validatePartition(p, entry(2015))).toThrow();
});

test('daily prices and power use actual 23/25-hour day lengths, not an average of averages', () => {
  const p = partition(2024);
  expect(history.hours('2024-03-31')).toBe(23);
  expect(history.hours('2024-10-27')).toBe(25);
  const rows = [p.rows.find((row) => row.hours === 23), p.rows.find((row) => row.hours === 25)];
  rows[0].price_eur_mwh = -100;
  rows[1].price_eur_mwh = 100;
  const summary = history.summarize({ ...p, rows });
  expect(summary.priceAverage).toBe(200 / 48);
  expect(summary.negativeDays).toBe(1);
  expect(summary.generationAverage).toBe(summary.generationEnergy / 48);
});

test('raw encoding rejects duplicate keys, whitespace, BOM, invalid UTF8 and oversize', () => {
  for (const raw of ['{"a":1,"a":2}', '{"b":1,"a":2}', '{"a": 1}', '{"a":1}\n', '\ufeff{}']) {
    expect(() => history.parseRaw(Buffer.from(raw), 20000)).toThrow();
  }
  expect(() => history.parseRaw(Buffer.from([255]), 20000)).toThrow();
  expect(() => history.parseRaw(manifestRaw, 1)).toThrow();
  expect(history.parseRaw(Buffer.from('{"a":0.0,"b":-0.0,"c":1e-07}'), 100)).toEqual({ a: 0, b: -0, c: 1e-7 });
});

test('SHA-256 hashes raw bytes, not a reserialized equivalent, in both consumers', async () => {
  const raw = rawYear(2015);
  const rewritten = Buffer.from(JSON.stringify(JSON.parse(raw)));
  expect(rewritten.equals(raw)).toBe(false);
  await expect(history.verifyPartition(rewritten, entry(2015), webcrypto.subtle)).rejects.toThrow('SHA-256');
  expect(() => build.verifyPartition(rewritten, entry(2015))).toThrow('SHA-256');
  await expect(history.verifyPartition(raw, entry(2015), null)).rejects.toThrow('nicht verfügbar');
});

test('build fails for a missing, corrupt or correctly rehashed but invalid unselected year', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'electricity-history-'));
  try {
    fs.cpSync(directory, temp, { recursive: true });
    const file = path.join(temp, path.basename(entry(2015).url));
    fs.unlinkSync(file);
    expect(() => build.readHistory(temp)).toThrow();
    fs.writeFileSync(file, rawYear(2016));
    expect(() => build.readHistory(temp)).toThrow('SHA-256');
    const p = partition(2015);
    p.source.license = 'invalid';
    const raw = Buffer.from(JSON.stringify(p));
    const m = clone(manifest);
    m.years[0].sha256 = createHash('sha256').update(raw).digest('hex');
    m.years[0].url = `${history.PREFIX}2015.${m.years[0].sha256}.json`;
    fs.writeFileSync(path.join(temp, path.basename(m.years[0].url)), raw);
    fs.writeFileSync(path.join(temp, 'manifest.json'), JSON.stringify(m));
    expect(() => build.readHistory(temp)).toThrow('Quelle');
  } finally { fs.rmSync(temp, { recursive: true, force: true }); }
});

test('publication check requires identical raw files and the exact pinned HTML manifest', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'electricity-published-'));
  try {
    const published = path.join(temp, 'data/history/german-electricity');
    fs.mkdirSync(published, { recursive: true });
    fs.cpSync(directory, published, { recursive: true });
    fs.copyFileSync(path.join(__dirname, '../src/_data/germanElectricity.json'), path.join(temp, 'data/german-electricity.json'));
    const page = path.join(temp, 'dashboards/strom/index.html');
    fs.mkdirSync(path.dirname(page), { recursive: true });
    const recent = JSON.parse(fs.readFileSync(path.join(temp, 'data/german-electricity.json')));
    const status = recent.schema_version === 2 ? `<script type="application/json" id="electricity-component-data">${history.safeJSON({ components: recent.components, refresh_status: recent.refresh_status })}</script>` : '';
    fs.writeFileSync(page, `${status}<script type="application/json" id="electricity-history-manifest">${history.safeJSON(manifest)}</script>`);
    expect(() => build.verifyPublished(temp)).not.toThrow();
    const recentFile = path.join(temp, 'data/german-electricity.json');
    const original = fs.readFileSync(recentFile);
    const changed = JSON.parse(original);
    changed.rows.at(-1)[1] += 0.001;
    fs.writeFileSync(recentFile, JSON.stringify(changed));
    expect(() => build.verifyPublished(temp)).toThrow('Published recent snapshot differs');
    fs.writeFileSync(recentFile, original);
    fs.writeFileSync(page, `${status}<p>Different deployment</p>`);
    expect(() => build.verifyPublished(temp)).toThrow('HTML/history manifest mismatch');
    fs.writeFileSync(path.join(published, path.basename(entry(2018).url)), Buffer.concat([rawYear(2018), Buffer.from('\n')]));
    expect(() => build.verifyPublished(temp)).toThrow('SHA-256');
  } finally { fs.rmSync(temp, { recursive: true, force: true }); }
});

test('lazy loader requests only chosen years, deduplicates in-flight work and caches verified years', async () => {
  const fetcher = jest.fn(async (url) => response(rawYear(Number(path.basename(url).slice(0, 4)))));
  const load = history.createLoader(manifest, fetcher, webcrypto.subtle);
  expect(fetcher).not.toHaveBeenCalled();
  const first = load(2018);
  expect(load(2018)).toBe(first);
  await first;
  await load(2018);
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher).toHaveBeenCalledWith(entry(2018).url, expect.objectContaining({ mode: 'same-origin' }));
  await load(2015);
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(() => load(2014)).toThrow();
});

test('failed verification is evicted from cache for retry', async () => {
  const fetcher = jest.fn().mockResolvedValueOnce(response(rawYear(2016))).mockResolvedValue(response(rawYear(2015)));
  const load = history.createLoader(manifest, fetcher, webcrypto.subtle);
  await expect(load(2015)).rejects.toThrow('SHA-256');
  await expect(load(2015)).resolves.toHaveProperty('year', 2015);
  expect(fetcher).toHaveBeenCalledTimes(2);
});

test('404 reloads the manifest, retries only an identical manifest and rejects mixed deployments', async () => {
  const fetcher = jest.fn().mockResolvedValueOnce({ ok: false, status: 404 })
    .mockResolvedValueOnce(response(manifestRaw)).mockResolvedValue(response(rawYear(2015)));
  await expect(history.createLoader(manifest, fetcher, webcrypto.subtle)(2015)).resolves.toHaveProperty('year', 2015);
  expect(fetcher.mock.calls.map(([url]) => url)).toEqual([entry(2015).url, `${history.PREFIX}manifest.json`, entry(2015).url]);
  const changed = clone(manifest);
  changed.years[0].frozen = false;
  const mixed = jest.fn().mockResolvedValueOnce({ ok: false, status: 404 }).mockResolvedValueOnce(response(Buffer.from(JSON.stringify(changed))));
  await expect(history.createLoader(manifest, mixed, webcrypto.subtle)(2015)).rejects.toThrow('Seite neu laden');
  expect(mixed).toHaveBeenCalledTimes(2);
});

test('request timeout aborts without caching the failure', async () => {
  jest.useFakeTimers();
  try {
    const fetcher = jest.fn((url, { signal }) => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new Error('aborted')))));
    const load = history.createLoader(manifest, fetcher, webcrypto.subtle);
    const rejected = expect(load(2015)).rejects.toThrow('aborted');
    await jest.advanceTimersByTimeAsync(15001);
    await rejected;
    fetcher.mockResolvedValue(response(rawYear(2015)));
    await expect(load(2015)).resolves.toHaveProperty('year', 2015);
  } finally { jest.useRealTimers(); }
});

test('history freshness uses the latest manifest boundary and safe JSON cannot close a script', () => {
  const boundary = endExclusive(manifest);
  expect(history.freshness(manifest, boundary + 96 * 3600000).stale).toBe(false);
  expect(history.freshness(manifest, boundary + 96 * 3600000 + 1).stale).toBe(true);
  expect(() => history.freshness(manifest, boundary - 1)).toThrow('Zukunft');
  const value = { text: '</script><script>alert(1)</script>&\u2028\u2029' };
  const encoded = history.safeJSON(value);
  expect(encoded).not.toMatch(/[<>&\u2028\u2029]/);
  expect(JSON.parse(encoded)).toEqual(value);
});

test.each([
  ['2024-03-30', '2024-03-30T23:00:00Z'],
  ['2024-03-31', '2024-03-31T22:00:00Z'],
  ['2024-10-26', '2024-10-26T22:00:00Z'],
  ['2024-10-27', '2024-10-27T23:00:00Z'],
  ['2024-12-31', '2024-12-31T23:00:00Z'],
  ['2025-01-01', '2025-01-01T23:00:00Z'],
])('freshness boundary for fixed Berlin calendar day %s', (last_date, exclusive) => {
  const metadata = { last_date };
  const boundary = Date.parse(exclusive);
  expect(endExclusive(metadata)).toBe(boundary);
  expect(history.freshness(metadata, boundary + 96 * 3600000).stale).toBe(false);
  expect(history.freshness(metadata, boundary + 96 * 3600000 + 1).stale).toBe(true);
  expect(() => history.freshness(metadata, boundary - 1)).toThrow('Zukunft');
});
