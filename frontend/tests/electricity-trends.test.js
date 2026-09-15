/** @jest-environment node */
const fs = require('fs');
const path = require('path');
const history = require('../src/js/dashboards/electricity-history');
const data = require('../src/js/dashboards/electricity-data');
const trade = require('../src/js/dashboards/electricity-trade');
const { aggregate, verifyTrade, verifyAnnual, readTrends } = require('../src/data_ingestion/builders/electricityTrends');
const { options } = require('../src/js/dashboards/electricity-trends');
const { readHistory } = require('../src/data_ingestion/builders/electricityHistory');
const { canonical, encode, monthCount, clockAfter, annualLabel, scenarios } = require('./fixtures/electricity-trends');
const raw = fs.readFileSync(path.join(__dirname, '../src/_data/germanElectricityTrade.json'));
const annualRaw = fs.readFileSync(path.join(__dirname, '../src/_data/germanElectricityAnnual.json'));
const annual = verifyAnnual(annualRaw);
const historyData = readHistory();
const producer = JSON.parse(raw);
const recent = require('../src/_data/germanElectricity.json');
const now = clockAfter(historyData, producer, recent);
const cases = scenarios(historyData, producer, recent, annual);
beforeEach(() => { jest.useFakeTimers(); jest.setSystemTime(now); });
afterEach(() => jest.useRealTimers());
function fixtureTrade() {
  return { schema_version: 1, kind: 'german-electricity-trade', source: data.SOURCE, region: 'DE-LU', timezone: data.TIMEZONE,
    first_month: '2019-01', last_month: '2019-12', revision_policy: trade.POLICY, content_hash: '0'.repeat(64),
    rows: Array.from({ length: 12 }, (_, index) => ({ month: `2019-${String(index + 1).padStart(2, '0')}`, imports_gwh: 1000 + index, exports_gwh: 2000 + 2 * index, net_exports_gwh: 1000 + index, missing_series: [], structural_zero_series: [] })) };
}
function fixtureHistory() {
  const rows = Array.from({ length: 365 }, (_, index) => ({ date: new Date(Date.UTC(2019, 0, index + 1)).toISOString().slice(0, 10), hours: 24,
    energy_gwh: Object.fromEntries(history.ENERGY.map((key) => [key, 0])), price_eur_mwh: 10, nuclear_derived_zero: false }));
  for (const [index, solar] of [[0, 1], [1, 9]]) Object.assign(rows[index].energy_gwh, { solar, lignite: 1, hard_coal: 1, gas: 1, pumped_storage: 1, nuclear: 1 });
  return { manifest: { first_date: '2019-01-01', last_date: '2020-01-01', source: data.SOURCE, years: [{ year: 2019, sha256: 'a'.repeat(64) }, { year: 2020, sha256: 'b'.repeat(64) }] }, manifestHash: 'c'.repeat(64),
    partitions: [{ year: 2019, rows }, { year: 2020, rows: [{ ...rows[0], date: '2020-01-01' }] }] };
}

test('real producer snapshot validates without rewriting it; summary is compact and deterministic', () => {
  const snapshot = verifyTrade(raw, now);
  expect(snapshot.rows).toHaveLength(monthCount(snapshot.first_month, snapshot.last_month));
  const a = readTrends(), b = readTrends();
  expect(a.json).toBe(b.json);
  // Bound overhead per observation, rather than imposing a date-dependent cap.
  expect(Buffer.byteLength(a.json)).toBeLessThan(4000 + snapshot.rows.length * 250 + historyData.manifest.years.length * 1000);
  expect(a.summary.inputs.trade_content_hash).toBe(snapshot.content_hash);
  expect(a.summary.inputs.annual_content_hash).toBe(annual.content_hash);
  expect(a.summary.inputs.history_partitions).toEqual(historyData.manifest.years.map(({ year, sha256 }) => ({ year, sha256 })));
  expect(a.summary.inputs.history_manifest_sha256).toMatch(/^[a-f0-9]{64}$/);
  expect(a.summary.energy.map((row) => row.year)).toEqual(historyData.manifest.years.filter((entry) => entry.last_date.endsWith('-12-31')).map((entry) => entry.year));
  const months = Number(snapshot.last_month.slice(5));
  expect(a.summary.trade.years.at(-1)).toMatchObject({ label: annualLabel(snapshot.last_month), months, partial_year: months < 12 });
});

test.each(['0.0', '-0.0', '0e-07'])('hash preserves Python numeric token %s', (spelling) => {
  const fixture = fixtureTrade();
  Object.assign(fixture.rows[0], { exports_gwh: 0, net_exports_gwh: -1000 });
  const bytes = encode(fixture, spelling);
  expect(verifyTrade(bytes, now).rows[0].exports_gwh).toBeCloseTo(0);
  const reserialized = Buffer.from(canonical(JSON.parse(bytes)));
  expect(() => verifyTrade(reserialized, now)).toThrow(/content_hash/);
});

test('hash corruption and noncanonical encodings fail', () => {
  const bytes = encode(fixtureTrade());
  expect(() => verifyTrade(Buffer.from(bytes.toString().replace('"exports_gwh":2000', '"exports_gwh":2001').replace('"net_exports_gwh":1000', '"net_exports_gwh":1001')), now)).toThrow(/content_hash/);
  for (const body of [`${bytes}\n`, bytes.toString().replace('{', '{ '), bytes.toString().replace('"region":"DE-LU"', '"region":"DE-LU","region":"DE-LU"')]) {
    expect(() => verifyTrade(Buffer.from(body), now)).toThrow();
  }
});

test.each([
  ['extra fields', (s) => { s.extra = true; }],
  ['bad source', (s) => { s.source = { ...s.source, url: 'https://example.org' }; }],
  ['unknown policy', (s) => { s.revision_policy += ' changed'; }],
  ['wrong zone', (s) => { s.region = 'DE'; }],
  ['duplicate month', (s) => { s.rows[1].month = s.rows[0].month; }],
  ['missing month', (s) => { s.rows.splice(1, 1); }],
  ['negative gross', (s) => { s.rows[0].imports_gwh = -1; }],
  ['bad net', (s) => { s.rows[0].net_exports_gwh += 1; }],
  ['boolean number', (s) => { s.rows[0].exports_gwh = true; }],
  ['unflagged null', (s) => { s.rows[0].imports_gwh = null; }],
  ['unknown gap', (s) => { s.rows[0].missing_series = [4486]; }],
  ['bad zero', (s) => { s.rows[0].structural_zero_series = [4486]; }],
  ['unsorted IDs', (s) => { s.rows[0].structural_zero_series = [4708, 4706]; }],
])('strict trade schema rejects %s even with recomputed hash', (_, mutate) => {
  const fixture = fixtureTrade(); mutate(fixture);
  expect(() => verifyTrade(encode(fixture), now)).toThrow();
});

test('trade cutoff rejects current month and a month not fully covered by history', () => {
  const fixture = fixtureTrade();
  expect(() => verifyTrade(encode(fixture), Date.parse('2019-12-31T12:00:00Z'))).toThrow(/Unvollständiger/);
  const input = fixtureHistory();
  input.manifest.last_date = '2019-12-30';
  expect(() => aggregate(input, fixture)).toThrow(/Historienmonat/);
  input.manifest.last_date = '2019-12-31';
  expect(() => aggregate(input, fixture)).not.toThrow();
});

test('energy shares are weighted by summed generation, combine both coal sources and include pumped storage/nuclear', () => {
  const summary = aggregate(fixtureHistory(), fixtureTrade());
  expect(summary.energy).toHaveLength(1); // January 2020 is never a full-year bar.
  expect(summary.energy[0]).toMatchObject({ year: 2019, renewable_share: 50, coal_share: 20, gas_share: 10, generation_twh: 0.02,
    mix_twh: { solar: 0.01, lignite: 0.002, hard_coal: 0.002, nuclear: 0.002, pumped_storage: 0.002 } });
  expect(aggregate(fixtureHistory(), fixtureTrade())).toEqual(summary);
});

test('null energy coverage leaves every annual series null, without treating missing days as zero', () => {
  const summary = aggregate(historyData, verifyTrade(raw, now));
  for (const [year, days, complete_days] of [[2016, 366, 365], [2018, 365, 361]]) {
    const row = summary.energy.find((item) => item.year === year);
    expect(row).toMatchObject({ days, complete_days, renewable_share: null, coal_share: null, gas_share: null, generation_twh: null });
    expect(Object.values(row.mix_twh).every((value) => value === null)).toBe(true);
  }
  expect(summary.energy.find((row) => row.year === 2015).generation_twh).toBeGreaterThan(0); // Price gaps do not affect generation.
});

test('official annual rows replace all twelve categories and the entire denominator without filling any daily gaps', () => {
  const before = JSON.stringify(historyData);
  const summary = aggregate(historyData, verifyTrade(raw, now), annual);
  const dailyOnly = aggregate(historyData, verifyTrade(raw, now));
  expect(annual.years[0].energy_gwh.other_renewables).toBe(1835.92608);
  expect(annual.years[1].energy_gwh.pumped_storage).toBe(8804.80638);
  for (const source of annual.years) {
    const row = summary.energy.find((item) => item.year === source.year);
    const total = Object.values(source.energy_gwh).reduce((sum, value) => sum + value, 0);
    const renewable = history.SOURCES.filter((item) => item.renewable).reduce((sum, item) => sum + source.energy_gwh[item.key], 0);
    expect(row).toMatchObject({ method: 'source_annual_aggregate', days: source.year === 2016 ? 366 : 365, complete_days: source.year === 2016 ? 365 : 361 });
    expect(row.generation_twh).toBe(Number((total / 1000).toFixed(8)));
    expect(row.renewable_share).toBe(Number((renewable / total * 100).toFixed(8)));
    expect(row.coal_share).toBe(Number(((source.energy_gwh.lignite + source.energy_gwh.hard_coal) / total * 100).toFixed(8)));
    expect(row.gas_share).toBe(Number((source.energy_gwh.gas / total * 100).toFixed(8)));
    expect(row.mix_twh).toEqual(Object.fromEntries(Object.entries(source.energy_gwh).map(([key, value]) => [key, Number((value / 1000).toFixed(8))])));
  }
  expect(summary.energy.every((row) => Number.isFinite(row.generation_twh) || row.complete_days < row.days)).toBe(true);
  expect(summary.energy.filter((row) => row.method === 'daily_sum')).toEqual(dailyOnly.energy.filter((row) => ![2016, 2018].includes(row.year)));
  expect(summary.trade).toEqual(dailyOnly.trade);
  expect(JSON.stringify(historyData)).toBe(before);
  expect(historyData.partitions.filter((partition) => partition.schema_version === 1).flatMap((partition) => partition.rows).flatMap((row) => Object.values(row.energy_gwh)).filter((value) => value === null)).toHaveLength(5);
  expect(fs.readFileSync(path.join(__dirname, '../src/_data/germanElectricityAnnual.json'))).toEqual(annualRaw);
});

test('subset supplement leaves the other annual gap and never supplies a partial-year bar', () => {
  const subset = verifyAnnual(encode({ ...annual, years: [annual.years[1]] }));
  const input = JSON.parse(JSON.stringify(historyData));
  const summary = aggregate(input, producer, subset);
  expect(summary.energy.find((row) => row.year === 2016).generation_twh).toBeNull();
  expect(summary.energy.find((row) => row.year === 2018).generation_twh).toBeGreaterThan(0);
  input.partitions.find((partition) => partition.year === 2018).rows.pop();
  expect(aggregate(input, producer, subset).energy.some((row) => row.year === 2018)).toBe(false);
});

test('absent supplement file retains daily-only gaps; an unreadable file fails', () => {
  const lstat = fs.lstatSync.bind(fs);
  const spy = jest.spyOn(fs, 'lstatSync').mockImplementation((file, ...args) => {
    if (file.endsWith('germanElectricityAnnual.json')) throw Object.assign(new Error('missing'), { code: 'ENOENT' });
    return lstat(file, ...args);
  });
  try {
    const { summary } = readTrends();
    expect(summary.inputs.annual_content_hash).toBeNull();
    const expectedGaps = historyData.partitions.filter((partition) => partition.rows.at(-1).date.endsWith('-12-31') && partition.rows.some((row) => !history.complete(row))).map((partition) => partition.year);
    expect(summary.energy.filter((row) => row.generation_twh === null).map((row) => row.year)).toEqual(expectedGaps);
    spy.mockImplementation((file, ...args) => {
      if (file.endsWith('germanElectricityAnnual.json')) throw Object.assign(new Error('unreadable'), { code: 'EACCES' });
      return lstat(file, ...args);
    });
    expect(() => readTrends()).toThrow('unreadable');
  } finally { spy.mockRestore(); }
});

test.each(['0.0', '-0.0', '0e-07'])('annual hash preserves numeric token %s', (spelling) => {
  const fixture = JSON.parse(annualRaw);
  fixture.years[0].energy_gwh.solar = 0;
  const bytes = encode(fixture, spelling, 'solar');
  expect(verifyAnnual(bytes).years[0].energy_gwh.solar).toBeCloseTo(0);
  expect(() => verifyAnnual(Buffer.from(canonical(JSON.parse(bytes))))).toThrow(/content_hash/);
});

test('annual checksum, canonical encoding and size are mandatory', () => {
  expect(() => verifyAnnual(Buffer.from(annualRaw.toString().replace('1835.92608', '1836.92608')))).toThrow(/content_hash/);
  for (const body of [`${annualRaw}\n`, annualRaw.toString().replace('{', '{ '), annualRaw.toString().replace('"region":"DE"', '"region":"DE","region":"DE"'), ' '.repeat(10001)]) {
    expect(() => verifyAnnual(Buffer.from(body))).toThrow();
  }
  const fixture = JSON.parse(annualRaw);
  fixture.years[0].energy_gwh.solar = 0;
  expect(() => verifyAnnual(encode(fixture, '1e999', 'solar'))).toThrow(/Jahresmenge/);
});

test('annual tooltips identify the source supplement while daily-only gaps still report a missing year', () => {
  const colors = { ink: '#111', line: '#ddd', accent: '#c4ad61', muted: '#555', font: 'sans-serif' };
  for (const supplement of [annual, null]) {
    const summary = aggregate(historyData, producer, supplement);
    const charts = options(summary, colors);
    for (const year of [2016, 2018]) {
      const index = summary.energy.findIndex((row) => row.year === year);
      for (const name of ['shares', 'mix']) {
        const item = { name: String(year), axisValue: String(year), dataIndex: index, seriesName: 'Erzeugung', value: summary.energy[index].generation_twh };
        const text = charts[name].tooltip.formatter(name === 'mix' ? item : [item]);
        if (supplement) {
          expect(text).toContain('SMARD-Jahreswerte');
          expect(text).not.toContain('Jahreswert fehlt');
        } else expect(text).toContain('Jahreswert fehlt');
      }
    }
  }
});

test.each([
  ['extra root field', (s) => { s.extra = true; }],
  ['missing root field', (s) => { delete s.resolution; }],
  ['wrong source', (s) => { s.source.url = 'https://example.org'; }],
  ['extra source field', (s) => { s.source.extra = true; }],
  ['wrong region', (s) => { s.region = 'DE-LU'; }],
  ['wrong timezone', (s) => { s.timezone = 'UTC'; }],
  ['wrong schema', (s) => { s.schema_version = 2; }],
  ['wrong kind', (s) => { s.kind = 'german-electricity-trade'; }],
  ['wrong resolution', (s) => { s.resolution = 'day'; }],
  ['empty years', (s) => { s.years = []; }],
  ['duplicate years', (s) => { s.years[1] = s.years[0]; }],
  ['unsorted years', (s) => { s.years.reverse(); }],
  ['unapproved year', (s) => { s.years[0].year = 2015; }],
  ['string year', (s) => { s.years[0].year = '2016'; }],
  ['wrong method', (s) => { s.years[0].method = 'daily_sum'; }],
  ['extra row field', (s) => { s.years[0].complete_days = 366; }],
  ['missing category', (s) => { delete s.years[0].energy_gwh.nuclear; }],
  ['extra category', (s) => { s.years[0].energy_gwh.load = 1; }],
  ['null category', (s) => { s.years[0].energy_gwh.solar = null; }],
  ['boolean category', (s) => { s.years[0].energy_gwh.solar = true; }],
  ['string category', (s) => { s.years[0].energy_gwh.solar = '1'; }],
  ['negative category', (s) => { s.years[0].energy_gwh.solar = -1; }],
  ['category upper bound', (s) => { s.years[0].energy_gwh.solar = 200 * 8784 + 1; }],
  ['annual lower bound', (s) => { Object.keys(s.years[0].energy_gwh).forEach((key) => { s.years[0].energy_gwh[key] = 1; }); }],
  ['annual upper bound', (s) => { s.years[1].energy_gwh.solar = 200 * 8760; }],
])('strict annual schema rejects %s with a valid checksum', (_, mutate) => {
  const fixture = JSON.parse(annualRaw); mutate(fixture);
  expect(() => verifyAnnual(encode(fixture))).toThrow();
});

test('annual trade sums monthly energy once, with positive gross and signed net', () => {
  const summary = aggregate(fixtureHistory(), fixtureTrade());
  expect(summary.trade.years[0]).toMatchObject({ months: 12, complete_months: 12, partial_year: false,
    imports_twh: 12.066, exports_twh: 24.132, net_exports_twh: 12.066 });
  expect(summary.trade.months[0]).toMatchObject({ imports_twh: 1, exports_twh: 2, net_exports_twh: 1 });
});

test('allowlisted missing startup month invalidates all annual totals, but preserves coverage and monthly gaps', () => {
  const snapshot = JSON.parse(raw);
  const row = snapshot.rows.find((item) => item.month === '2020-11');
  Object.assign(row, { missing_series: [4706, 4708], imports_gwh: null, exports_gwh: null, net_exports_gwh: null });
  const validated = verifyTrade(encode(snapshot), now);
  const input = fixtureHistory(); input.manifest.last_date = historyData.manifest.last_date;
  const summary = aggregate(input, validated);
  expect(summary.trade.years.find((item) => item.year === 2020)).toMatchObject({ complete_months: 11, months: 12, imports_twh: null, exports_twh: null, net_exports_twh: null });
  expect(summary.trade.months.find((item) => item.month === '2020-11').imports_twh).toBeNull();
});

test.each(cases)('$name: coverage, chart labels and partial-year styling survive refresh', ({ historyData: input, snapshot, annual, now: clock, trends }) => {
  jest.setSystemTime(clock);
  const summary = aggregate(input, verifyTrade(encode(snapshot)), annual);
  expect(summary).toEqual(trends.summary);
  expect(summary.energy.map((row) => row.year)).toEqual(input.manifest.years.filter((entry) => entry.last_date.endsWith('-12-31')).map((entry) => entry.year));
  expect(summary.trade.months).toHaveLength(monthCount(snapshot.first_month, snapshot.last_month));
  const months = Number(snapshot.last_month.slice(5));
  expect(summary.trade.years.at(-1)).toMatchObject({ label: annualLabel(snapshot.last_month), months, partial_year: months < 12 });
  const latestRows = snapshot.rows.filter((row) => row.month.slice(0, 4) === snapshot.last_month.slice(0, 4));
  if (latestRows.some((row) => row.missing_series.length)) expect(summary.trade.years.at(-1).imports_twh).toBeNull();
  else expect(summary.trade.years.at(-1).imports_twh).toBeCloseTo(latestRows.reduce((sum, row) => sum + row.imports_gwh, 0) / 1000, 8);
  const charts = options(summary, { ink: '#111', line: '#ddd', accent: '#c4ad61', muted: '#555', font: 'sans-serif' });
  expect(charts.shares.series).toHaveLength(3);
  expect(charts.shares.series.map((series) => series.lineStyle)).toEqual(Array(3).fill({ type: 'solid', width: 3 }));
  expect(charts.shares.series.map((series) => series.itemStyle.color)).toEqual(['#c4ad61', '#555', history.SOURCES.find((source) => source.key === 'gas').color]);
  expect(charts.shares.series.every((series) => series.connectNulls === false)).toBe(true);
  expect(charts.shares.series[1].name).toContain('Braun- + Steinkohle');
  expect(charts.mix.series).toHaveLength(12);
  expect(charts.mix.legend.selectedMode).toBe('multiple');
  expect(charts.mix.legend.data).toEqual([...charts.mix.series].reverse().map((series) => series.name));
  expect(charts.mix.tooltip.trigger).toBe('item');
  expect(charts.mix.series.every((series) => series.emphasis.focus === 'series' && series.blur.itemStyle.opacity < 1)).toBe(true);
  expect(charts.mix.series.find((series) => series.name === 'Kernkraft').itemStyle.color).toBe(history.SOURCES.at(-1).color);
  expect(charts.mix.yAxis.name).toBe('TWh');
  expect(charts['trade-months'].series[2].name).toContain('Nettoimport (−)');
  expect(charts['trade-months'].xAxis.data).toEqual(snapshot.rows.map((row) => row.month));
  expect(charts['trade-years'].xAxis.data.at(-1)).toBe(annualLabel(snapshot.last_month).replace(' (', '\n('));
  expect(charts['trade-years'].xAxis.axisLabel).toMatchObject({ interval: 0, hideOverlap: false });
  if (months < 12) {
    expect(charts['trade-years'].series[0].data.at(-1).itemStyle.borderType).toBe('dashed');
    expect(charts['trade-years'].series[2].data.at(-1)).toBeNull();
    expect(charts['trade-years'].series[3].data.at(-1)).toBe(summary.trade.years.at(-1).net_exports_twh);
  } else {
    expect(charts['trade-years'].series[0].data.at(-1)).toBe(summary.trade.years.at(-1).imports_twh);
    expect(charts['trade-years'].series[2].data.at(-1)).toBe(summary.trade.years.at(-1).net_exports_twh);
    expect(charts['trade-years'].series[3].data.every((value) => value === null)).toBe(true);
  }
});
