const fs = require('fs');
const path = require('path');
const nunjucks = require('nunjucks');
const data = require('../src/js/dashboards/electricity-data');
const snapshot = require('../src/_data/germanElectricity.json');
const template = fs.readFileSync(path.join(__dirname, '../src/dashboards/strom.njk'), 'utf8').replace(/^---[\s\S]*?---\n/, '');
const controller = fs.readFileSync(path.join(__dirname, '../src/js/dashboards/electricity-dashboard.js'), 'utf8');
const env = new nunjucks.Environment(null, { autoescape: true });
require('../src/data_ingestion/builders/electricityFilters').register(env);

let instances;
let themeChange;
async function start() {
  window.eval(controller);
  // Drain the fetch -> json -> render promise chain, without advancing interval timers.
  for (let i = 0; i < 6; i++) await Promise.resolve();
}
beforeEach(() => {
  jest.useFakeTimers();
  jest.setSystemTime(Date.parse(snapshot.snapshot_created_at) + data.HOUR);
  document.body.innerHTML = env.renderString(template, { germanElectricity: snapshot });
  window.ElectricityData = data;
  window.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => snapshot });
  window.matchMedia = jest.fn(() => ({ addEventListener: (name, listener) => { themeChange = listener; } }));
  instances = [];
  window.echarts = { init: jest.fn(() => {
    const chart = { setOption: jest.fn(), resize: jest.fn(), dispose: jest.fn() };
    instances.push(chart);
    return chart;
  }) };
});
afterEach(() => { jest.clearAllTimers(); jest.useRealTimers(); });

test('no-JS HTML has a daily summary, full accessible mix and chart text', () => {
  expect(document.querySelectorAll('h1')).toHaveLength(1);
  expect(document.querySelectorAll('.electricity-mix tbody th[scope="row"]')).toHaveLength(11);
  expect(document.querySelector('[data-value="label"]').textContent).toBe(data.summarize(snapshot).label);
  expect(document.getElementById('electricity-price-text').textContent).toContain('negatives Stundenmittel');
  expect(document.getElementById('electricity-negative-label').textContent).toBe('Stunden mit negativem Stundenmittel');
  expect(document.getElementById('electricity-price-grain').textContent).toContain('Stundenmittel');
  expect(document.querySelectorAll('[data-days]:disabled')).toHaveLength(3);
  expect(document.querySelector('a[download]').href).toContain('/data/german-electricity.json');
});

test('copy uses plain calendar days and preserves source attribution and timezone methodology', () => {
  const text = document.getElementById('electricity-dashboard').textContent;
  expect(text).not.toMatch(/inspiriert|Kate Morley|Berliner Kalendertage/i);
  expect(document.querySelector('a[href="https://grid.iamkate.com/"]')).toBeNull();
  expect(text).toContain('30 abgeschlossenen Kalendertagen');
  expect(text).toContain('deutsche Ortszeit (MEZ/MESZ, Zeitzone Europe/Berlin)');
  expect(text).toContain('Kalendertage reichen von Mitternacht bis Mitternacht');
  expect(document.querySelector('.electricity-meta').textContent).not.toContain('Europe/Berlin');
  expect(text).toContain('Bundesnetzagentur | SMARD.de');
  expect(text).toContain('CC BY 4.0');
});

test('switches all KPIs, mix and hourly charts together using one same-origin request', async () => {
  await start();
  expect(window.fetch).toHaveBeenCalledTimes(1);
  expect(window.fetch).toHaveBeenCalledWith('/data/german-electricity.json', expect.objectContaining({ mode: 'same-origin' }));
  for (const days of [7, 30, 1]) {
    document.querySelector(`[data-days="${days}"]`).click();
    const summary = data.summarize(snapshot, days);
    const text = data.presentation(summary);
    document.querySelectorAll('[data-value]').forEach((node) => expect(node.textContent).toBe(text[node.dataset.value]));
    expect(document.querySelectorAll('[aria-pressed="true"]')).toHaveLength(1);
    expect(document.querySelector('[aria-pressed="true"]').dataset.days).toBe(String(days));
    expect(document.getElementById('electricity-generation-heading').textContent).toBe(days === 1 ? 'Erzeugung im Tagesverlauf' : 'Erzeugung im Zeitraum');
    expect(document.getElementById('electricity-price-grain').textContent).toContain('Stundenmittel');
    expect(document.querySelector('[data-source="pumped_storage"] [data-mix="share"]').textContent).toBe(data.number(summary.mix[10].share));
    instances.forEach((chart) => {
      const option = chart.setOption.mock.calls.at(-1)[0];
      expect(option.xAxis.data).toHaveLength(summary.rows.length);
      expect(option.series[0].data).toHaveLength(summary.rows.length);
    });
    const price = instances[2].setOption.mock.calls.at(-1)[0];
    expect(price.series[0].data.map((point) => point.value)).toEqual(summary.rows.map((row) => row[13]));
    expect(price.yAxis.min).toBeUndefined();
  }
  themeChange();
  expect(instances).toHaveLength(3);
  expect(window.fetch).toHaveBeenCalledTimes(1);
});

test.each(['network', 'http', 'schema', 'mixed-build', 'timeout'])('%s failure retains static summary and disables switching', async (failure) => {
  if (failure === 'network') window.fetch.mockRejectedValue(new Error('offline'));
  if (failure === 'http') window.fetch.mockResolvedValue({ ok: false });
  if (failure === 'schema') window.fetch.mockResolvedValue({ ok: true, json: async () => ({}) });
  if (failure === 'mixed-build') window.fetch.mockResolvedValue({ ok: true, json: async () => ({ ...snapshot, content_hash: 'b'.repeat(64) }) });
  if (failure === 'timeout') window.fetch.mockImplementation((url, { signal }) => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(new Error('aborted')));
  }));
  await start();
  if (failure === 'timeout') await jest.advanceTimersByTimeAsync(15001);
  expect(document.getElementById('electricity-status').textContent).toContain('nicht geladen oder validiert');
  expect(document.querySelectorAll('[data-days]:disabled')).toHaveLength(3);
  expect(document.querySelector('[data-value="generation"]').textContent).toBe(data.presentation(data.summarize(snapshot)).generation);
  expect(document.getElementById('electricity-dashboard').hasAttribute('aria-busy')).toBe(false);
});

test('missing chart library preserves interactive summaries and source mix', async () => {
  delete window.echarts;
  await start();
  document.querySelector('[data-days="30"]').click();
  expect(document.getElementById('electricity-status').textContent).toContain('Diagramme konnten nicht dargestellt');
  expect(document.querySelector('[data-value="label"]').textContent).toBe(data.summarize(snapshot, 30).label);
  expect(document.querySelectorAll('.electricity-chart[hidden]')).toHaveLength(3);
});

test('theme changes cannot render a rejected HTML/JSON candidate', async () => {
  const candidate = JSON.parse(JSON.stringify(snapshot));
  candidate.content_hash = 'b'.repeat(64);
  candidate.rows.forEach((row) => { row[1] = 100; });
  window.fetch.mockResolvedValue({ ok: true, json: async () => candidate });
  const before = [...document.querySelectorAll('[data-value], [data-mix]')].map((node) => node.textContent);
  await start();
  const error = document.getElementById('electricity-status').textContent;
  expect(error).toContain('nicht geladen oder validiert');
  themeChange();
  themeChange();
  expect([...document.querySelectorAll('[data-value], [data-mix]')].map((node) => node.textContent)).toEqual(before);
  expect(document.getElementById('electricity-status').textContent).toBe(error);
  expect(document.querySelectorAll('[data-days]:disabled')).toHaveLength(3);
  expect(window.echarts.init).not.toHaveBeenCalled();
  expect(document.querySelectorAll('.electricity-chart[hidden]')).toHaveLength(3);
});

test('rendering failure disposes partially initialized charts', async () => {
  window.echarts.init.mockImplementationOnce(() => { throw new Error('canvas failed'); });
  await start();
  expect(document.getElementById('electricity-status').textContent).toContain('Diagramme konnten nicht dargestellt');
  expect(document.querySelectorAll('.electricity-chart[hidden]')).toHaveLength(3);
});

test('freshness warning advances in an open tab independently of fetch', async () => {
  jest.setSystemTime(Date.parse(snapshot.data_through) + 96 * data.HOUR);
  await start();
  const partial = data.componentReport(snapshot).some((component) => component.stale || component.status !== 'complete') || Object.values(snapshot.refresh_status || {}).some((meta) => meta.status !== 'ok');
  expect(document.getElementById('electricity-freshness').hidden).toBe(!partial);
  await jest.advanceTimersByTimeAsync(60000);
  expect(document.getElementById('electricity-freshness').hidden).toBe(false);
  expect(window.fetch).toHaveBeenCalledTimes(1);
});

test('future HTML snapshot warns even when JSON cannot load', async () => {
  jest.setSystemTime(Date.parse(snapshot.data_through) - data.HOUR);
  await start();
  expect(document.getElementById('electricity-freshness').hidden).toBe(false);
  expect(document.getElementById('electricity-freshness').textContent).toContain('Zukunft');
  expect(window.fetch).not.toHaveBeenCalled();
  expect(document.querySelectorAll('[data-days]:disabled')).toHaveLength(3);
});

test('v2 source gaps and retained failures are visible without JS; charts never interpolate partial generation', async () => {
  const input = JSON.parse(JSON.stringify(snapshot));
  input.schema_version = 2;
  input.rows.at(-1)[9] = null;
  input.rows.at(-1)[13] = null;
  input.components = Object.fromEntries(data.COLUMNS.slice(1).map((key, index) => {
    const known = input.rows.filter((row) => row[index + 1] !== null);
    return [key, { status: key === 'gas' ? 'stale' : known.length === input.rows.length ? 'complete' : 'partial',
      known_hours: known.length, expected_hours: input.rows.length, last_successful_window_end: input.window_end,
      source_observed_through: new Date(known.at(-1)[0] + data.HOUR).toISOString().replace('.000Z', 'Z') }];
  }));
  input.refresh_status = { history: { status: 'stale', data_through: data.dayKey(Date.parse(input.window_end) - 1) } };
  data.validateSnapshot(input);
  document.body.innerHTML = env.renderString(template, { germanElectricity: input });
  expect(document.getElementById('electricity-history-coverage').hidden).toBe(false);
  expect(document.getElementById('electricity-history-coverage').textContent).toContain('Teilsummen');
  expect(document.querySelector('noscript').textContent).not.toContain('letzten vollständigen Tag');
  expect(document.getElementById('electricity-partial-warning').hidden).toBe(false);
  expect(document.getElementById('electricity-component-report').textContent).toContain('beibehalten');
  expect(document.querySelector('[data-value="generation"]').textContent).toBe('–');
  window.fetch.mockResolvedValue({ ok: true, json: async () => input });
  await start();
  expect(document.getElementById('electricity-freshness').hidden).toBe(false);
  const generation = instances[0].setOption.mock.calls.at(-1)[0];
  expect(generation.series.every((series) => series.connectNulls === false && series.data.at(-1) === null)).toBe(true);
  const load = instances[1].setOption.mock.calls.at(-1)[0];
  expect(load.series[0].data.at(-1)).toBe(input.rows.at(-1)[12]);
  const price = instances[2].setOption.mock.calls.at(-1)[0];
  expect(price.series[0].data.at(-1).value).toBeNull();
  expect(document.getElementById('electricity-history-coverage').textContent).toContain('Abdeckung');
});
