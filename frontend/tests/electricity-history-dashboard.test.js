const fs = require('fs');
const path = require('path');
const { TextDecoder } = require('util');
global.TextDecoder = TextDecoder;
const nunjucks = require('nunjucks');
const data = require('../src/js/dashboards/electricity-data');
const history = require('../src/js/dashboards/electricity-history');
const realHistoryData = require('../src/_data/germanElectricityHistory')();
const { nextDate, endExclusive, advanceHistory } = require('./fixtures/electricity-history');
const snapshot = require('../src/_data/germanElectricity.json');
const template = fs.readFileSync(path.join(__dirname, '../src/dashboards/strom.njk'), 'utf8').replace(/^---[\s\S]*?---\n/, '');
const controller = fs.readFileSync(path.join(__dirname, '../src/js/dashboards/electricity-dashboard.js'), 'utf8');
const env = new nunjucks.Environment(null, { autoescape: true });
require('../src/data_ingestion/builders/electricityFilters').register(env);
const realPartition = (year) => {
  const entry = realHistoryData.manifest.years.find((item) => item.year === year);
  return JSON.parse(fs.readFileSync(path.join(__dirname, '../src/data-history/german-electricity', path.basename(entry.url))));
};
const scenarios = [
  { name: 'current manifest', data: realHistoryData, partition: realPartition },
  { name: 'next completed day', ...advanceHistory(realHistoryData, realPartition, nextDate(realHistoryData.manifest.last_date)) },
  { name: 'next year January', ...advanceHistory(realHistoryData, realPartition, `${realHistoryData.manifest.years.at(-1).year + 1}-01-01`) },
];

describe.each(scenarios)('$name', ({ data: historyData, partition }) => {
const latestEntry = historyData.manifest.years.at(-1);
const previousEntries = historyData.manifest.years.slice(0, -1);
const entry = (year) => historyData.manifest.years.find((item) => item.year === year);
let charts;
let themeChange;
let loadYear;
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const select = (year) => {
  const node = document.getElementById('electricity-year');
  node.value = String(year);
  node.dispatchEvent(new Event('change'));
};
const status = () => document.getElementById('electricity-status').textContent;
const chartOption = (index) => charts[index].setOption.mock.calls.at(-1)[0];
const values = () => [...document.querySelectorAll('[data-value], [data-mix]')].map((node) => node.textContent);
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
beforeEach(() => {
  jest.useFakeTimers();
  jest.setSystemTime(Math.max(Date.parse(snapshot.snapshot_created_at), endExclusive(historyData.manifest)) + data.HOUR);
  document.body.innerHTML = env.renderString(template, { germanElectricity: snapshot, germanElectricityHistory: historyData });
  loadYear = jest.fn(async (year) => partition(year));
  window.ElectricityData = data;
  window.ElectricityHistory = { ...history, createLoader: jest.fn(() => loadYear) };
  window.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => snapshot });
  window.matchMedia = jest.fn(() => ({ addEventListener: (name, listener) => { themeChange = listener; } }));
  charts = [];
  window.echarts = { init: jest.fn(() => {
    const chart = { setOption: jest.fn(), resize: jest.fn(), dispose: jest.fn() };
    charts.push(chart);
    return chart;
  }) };
});
afterEach(() => { jest.clearAllTimers(); jest.useRealTimers(); });
async function start() { window.eval(controller); await flush(); }

test('static YTD is complete and readable without JavaScript, and embeds only the validated manifest', () => {
  expect(document.querySelector('[data-value="label"]').textContent).toBe(historyData.summary.label);
  expect(document.querySelectorAll('.electricity-mix tbody th[scope="row"]')).toHaveLength(12);
  expect(document.querySelectorAll('#electricity-year option')).toHaveLength(previousEntries.length + 1);
  expect([...document.querySelectorAll('#electricity-year option')].slice(1).map((node) => node.value)).toEqual(previousEntries.map((item) => String(item.year)));
  expect(JSON.parse(document.getElementById('electricity-history-manifest').textContent)).toEqual(historyData.manifest);
  expect(document.getElementById('electricity-price-kpi-label').textContent).toBe('Day-Ahead · Ø Preis (zeitgewichtet)');
  expect(document.getElementById('electricity-price-grain').textContent).toContain('Tagesmittel');
  expect(document.getElementById('electricity-negative-label').textContent).toBe('Tage mit negativem Tagesmittel');
  expect(document.getElementById('electricity-history-download').getAttribute('href')).toBe(latestEntry.url);
});

test('methodology distinguishes hourly data, daily history and missing values from zero', () => {
  const text = document.getElementById('electricity-dashboard').textContent;
  expect(text).toContain('Der aktuelle Stundendatensatz umfasst ein Raster von 30 abgeschlossenen Kalendertagen');
  expect(text).toContain('Die Jahresansichten nutzen die täglichen Energiesummen von SMARD');
  expect(text).toContain('Quellenlücken bleiben unbekannt und werden nicht durch 0 ersetzt');
  expect(text).not.toContain('Diese Werte bleiben null');
  expect(text).toContain('DE–AT–LU');
  expect(text).toContain('23 oder 25 Stunden');
  expect(text).toContain('nicht einzelne negative Viertelstunden');
});

test('latest-year label remains accurate after a year rollover without refreshed data', async () => {
  jest.setSystemTime(Date.parse(`${latestEntry.year + 1}-01-02T12:00:00Z`));
  const button = document.getElementById('electricity-ytd');
  expect(button.textContent).toBe(`Jahr ${latestEntry.year}`);
  expect(button.dataset.year).toBe(String(latestEntry.year));
  expect(document.getElementById('electricity-history-selection').textContent).toContain(`Jahr ${latestEntry.year}`);
  expect(document.getElementById('electricity-dashboard').textContent).not.toContain('YTD');
  await start();
  expect(loadYear).toHaveBeenCalledWith(latestEntry.year);
  expect(button.getAttribute('aria-pressed')).toBe('true');
  expect(document.getElementById('electricity-history-selection').textContent).toContain(`Jahr ${latestEntry.year}`);
  expect(document.getElementById('electricity-history-freshness').hidden).toBe(false);
});

test('default YTD then all years keep KPI, table, legend, graph, units and price labels synchronized', async () => {
  await start();
  expect(loadYear.mock.calls).toEqual([[latestEntry.year]]);
  expect(window.fetch).toHaveBeenCalledTimes(1);
  for (const { year } of previousEntries) {
    select(year);
    await flush();
    const summary = history.summarize(partition(year));
    const text = history.presentation(summary);
    document.querySelectorAll('[data-value]').forEach((node) => expect(node.textContent).toBe(text[node.dataset.value]));
    expect(document.getElementById('electricity-year').value).toBe(String(year));
    expect(document.querySelectorAll('[aria-pressed="true"]')).toHaveLength(0);
    expect(document.querySelectorAll('.electricity-legend li')).toHaveLength(12);
    expect(document.querySelector('[data-source="nuclear"]')).not.toBeNull();
    expect(chartOption(0).series).toHaveLength(12);
    expect(chartOption(0).yAxis.name).toBe('GW');
    expect(chartOption(1).yAxis.name).toBe('GW');
    expect(chartOption(0).xAxis.data).toEqual(summary.rows.map((row) => row.date));
    expect(chartOption(0).series[0].data).toEqual(summary.rows.map((row) => history.complete(row) ? row.energy_gwh.biomass / row.hours : null));
    expect(chartOption(1).series[0].data).toEqual(summary.rows.map((row) => row.energy_gwh.load === null ? null : row.energy_gwh.load / row.hours));
    expect(chartOption(2).series[0].data.map((point) => point.value)).toEqual(summary.rows.map((row) => row.price_eur_mwh));
    expect(document.getElementById('electricity-history-download').getAttribute('href')).toBe(entry(year).url);
  }
  document.querySelector('[data-days="7"]').click();
  expect(document.querySelectorAll('.electricity-legend li')).toHaveLength(11);
  expect(document.querySelector('[data-source="nuclear"]')).toBeNull();
  expect(chartOption(0).series).toHaveLength(11);
  expect(document.getElementById('electricity-negative-label').textContent).toBe('Stunden mit negativem Stundenmittel');
  expect(document.getElementById('electricity-generation-heading').textContent).toBe('Erzeugung im Zeitraum');
  expect(document.getElementById('electricity-history-coverage').hidden).toBe(false);
  expect(document.getElementById('electricity-history-coverage').textContent).toContain('Abdeckung');
  expect(document.getElementById('electricity-year').value).toBe('');
  document.getElementById('electricity-ytd').click();
  await flush();
  expect(document.getElementById('electricity-ytd').getAttribute('aria-pressed')).toBe('true');
  expect(document.getElementById('electricity-negative-label').textContent).toBe('Tage mit negativem Tagesmittel');
  expect(document.getElementById('electricity-generation-heading').textContent).toBe('Erzeugung im Jahresverlauf');
});

test('2018 retains four generation gaps across every stacked source, complete load, independent prices and blended zone', async () => {
  await start();
  select(2018);
  await flush();
  expect(document.getElementById('electricity-history-coverage').textContent).toContain('361/365 vollständige Tage');
  expect(document.getElementById('electricity-history-coverage').textContent).toContain('keine Jahressummen');
  for (const source of chartOption(0).series) {
    expect(source.data.filter((value) => value === null)).toHaveLength(4);
    expect(source.connectNulls).toBe(false);
  }
  expect(chartOption(1).series[0].data.every(Number.isFinite)).toBe(true);
  expect(chartOption(2).series[0].data.every((point) => Number.isFinite(point.value))).toBe(true);
  expect(document.getElementById('electricity-price-grain').textContent).toContain('gemischte Marktgebiete 2018');
  select(2015);
  await flush();
  expect(chartOption(2).series[0].data.slice(0, 4).map((point) => point.value)).toEqual([null, null, null, null]);
  expect(document.getElementById('electricity-price-text').textContent).toContain('361/365 Tage');
});

test('slow older request cannot replace a newer selection or its loading state', async () => {
  await start();
  const old = deferred();
  const latest = deferred();
  loadYear.mockImplementation((year) => year === 2015 ? old.promise : latest.promise);
  select(2015);
  select(2018);
  expect(document.getElementById('electricity-year').value).toBe('');
  expect(document.getElementById('electricity-ytd').getAttribute('aria-pressed')).toBe('true');
  old.resolve(partition(2015));
  await flush();
  expect(status()).toContain('2018 wird geladen');
  expect(document.getElementById('electricity-dashboard').getAttribute('aria-busy')).toBe('true');
  latest.resolve(partition(2018));
  await flush();
  expect(document.getElementById('electricity-year').value).toBe('2018');
  expect(document.getElementById('electricity-dashboard').hasAttribute('aria-busy')).toBe(false);
});

test.each(['history', 'recent'])('failed request preserves previous %s selection and status through theme changes', async (previous) => {
  await start();
  if (previous === 'recent') document.querySelector('[data-days="30"]').click();
  else { select(2018); await flush(); }
  const before = values();
  const failure = deferred();
  loadYear.mockReturnValue(failure.promise);
  select(2015);
  const loading = status();
  themeChange();
  expect(status()).toBe(loading);
  expect(values()).toEqual(before);
  failure.reject(new Error('SHA-256 mismatch'));
  await flush();
  const error = status();
  expect(error).toContain('bisherige Auswahl bleibt erhalten');
  themeChange();
  expect(values()).toEqual(before);
  expect(status()).toBe(error);
  expect(document.getElementById('electricity-year').value).toBe(previous === 'recent' ? '' : '2018');
});

test('switching to recent invalidates pending history; late failure cannot overwrite successful state', async () => {
  await start();
  const pending = deferred();
  loadYear.mockReturnValue(pending.promise);
  select(2015);
  document.querySelector('[data-days="7"]').click();
  const before = values();
  const message = status();
  pending.reject(new Error('offline'));
  await flush();
  expect(values()).toEqual(before);
  expect(status()).toBe(message);
  expect(document.querySelector('[data-days="7"]').getAttribute('aria-pressed')).toBe('true');
});

test('late successful history response cannot overwrite a newer successful history selection', async () => {
  await start();
  const slow = deferred();
  loadYear.mockImplementation((year) => year === 2015 ? slow.promise : Promise.resolve(partition(year)));
  select(2015);
  select(2018);
  await flush();
  const before = values();
  const message = status();
  slow.resolve(partition(2015));
  await flush();
  expect(values()).toEqual(before);
  expect(status()).toBe(message);
  expect(document.getElementById('electricity-year').value).toBe('2018');
});

test('initial history failure preserves static YTD across theme changes; retry remains available', async () => {
  loadYear.mockRejectedValueOnce(new Error('offline'));
  const before = values();
  await start();
  const message = status();
  expect(message).toContain('nicht geladen oder validiert');
  themeChange();
  expect(values()).toEqual(before);
  expect(status()).toBe(message);
  expect(window.echarts.init).not.toHaveBeenCalled();
  document.getElementById('electricity-ytd').click();
  await flush();
  expect(charts).toHaveLength(3);
});

test('history remains usable when hourly request or charts fail', async () => {
  window.fetch.mockRejectedValue(new Error('offline'));
  delete window.echarts;
  await start();
  select(2015);
  await flush();
  expect(status()).toContain('Diagramme konnten nicht dargestellt');
  expect(document.querySelector('[data-value="label"]').textContent).toBe(history.summarize(partition(2015)).label);
  expect(document.querySelectorAll('[data-days]:disabled')).toHaveLength(3);
  expect(document.querySelectorAll('.electricity-mix tbody th')).toHaveLength(12);
  await jest.advanceTimersByTimeAsync(60000);
  expect(document.getElementById('electricity-recent-error').hidden).toBe(false);
  expect(document.getElementById('electricity-recent-error').textContent).toContain('nicht geladen oder validiert');
});

test('closed-year selection is not stale; warning advances based on newest history date', async () => {
  const boundary = endExclusive(historyData.manifest);
  jest.setSystemTime(boundary + 96 * data.HOUR);
  await start();
  select(2015);
  await flush();
  expect(document.getElementById('electricity-history-freshness').hidden).toBe(true);
  await jest.advanceTimersByTimeAsync(60000);
  expect(document.getElementById('electricity-history-freshness').hidden).toBe(false);
  expect(document.getElementById('electricity-history-freshness').textContent).toContain(historyData.manifest.last_date);
  expect(document.getElementById('electricity-year').value).toBe('2015');
});
});
