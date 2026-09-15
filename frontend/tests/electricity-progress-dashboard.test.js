const fs = require('fs');
const path = require('path');
const { TextDecoder } = require('util');
global.TextDecoder = TextDecoder;
const nunjucks = require('nunjucks');
const data = require('../src/js/dashboards/electricity-data');
const { verifyProgress, view } = require('../src/data_ingestion/builders/electricityProgress');
const { fixture, rollover, encode } = require('./fixtures/electricity-progress');
const { mount } = require('../src/js/dashboards/electricity-progress');
const recent = require('../src/_data/germanElectricity.json');
const template = fs.readFileSync(path.join(__dirname, '../src/dashboards/strom.njk'), 'utf8').replace(/^---[\s\S]*?---\n/, '');
const env = new nunjucks.Environment(new nunjucks.FileSystemLoader(path.join(__dirname, '../src/_includes')), { autoescape: true });
require('../src/data_ingestion/builders/electricityFilters').register(env);

describe.each([['baseline', fixture(), '2026-09-10'], ['rollover', rollover(), '2027-01-01']])('%s', (_, snapshot, day) => {
  const bytes = encode(snapshot);
  const progress = view(verifyProgress(bytes, Date.parse(`${day}T12:00:00Z`)), bytes.toString());
  let intersect, charts, theme, resize;
  const nodes = () => [...document.querySelectorAll('[data-progress-chart]')];
  const buttons = () => [...document.querySelectorAll('[data-progress-metric]')];
  const show = (node) => intersect([{ target: node, isIntersecting: true }]);
  beforeEach(() => {
    document.body.innerHTML = env.renderString(template, { germanElectricity: recent, germanElectricityProgressView: progress });
    charts = [];
    global.IntersectionObserver = jest.fn((callback) => { intersect = callback; return { observe: jest.fn(), unobserve: jest.fn() }; });
    global.ResizeObserver = jest.fn((callback) => { resize = callback; return { observe: jest.fn() }; });
    window.matchMedia = jest.fn(() => ({ addEventListener: (_, callback) => { theme = callback; } }));
    window.echarts = { init: jest.fn((node) => { const chart = { node, setOption: jest.fn(), resize: jest.fn(), dispose: jest.fn() }; charts.push(chart); return chart; }) };
    window.fetch = jest.fn();
  });
  afterEach(() => { delete global.IntersectionObserver; delete global.ResizeObserver; jest.restoreAllMocks(); });

  test('static latest values, separate goals, explicit ages, licenses and download without tables', () => {
    const root = document.querySelector('#electricity-progress');
    expect(root.querySelectorAll('table')).toHaveLength(0);
    expect(nodes()).toHaveLength(5);
    expect(nodes().every((node) => node.hidden)).toBe(true);
    expect(buttons().every((button) => button.disabled)).toBe(true);
    expect(document.querySelectorAll('.electricity-kpis > div')).toHaveLength(4);
    expect(root.compareDocumentPosition(document.querySelector('.electricity-method')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(root.textContent).toContain(progress.monthlyDate);
    expect(root.textContent).toContain(progress.capacityDate);
    expect(root.textContent).toContain(data.number(progress.capacityLatest.solar_gw, 3));
    expect(root.textContent).toContain(data.number(progress.annualLatest.cost_million_eur, 0));
    expect(root.textContent).toContain(data.number(progress.monthlyLatest.redispatch_cost_million_eur, 2));
    const targets = root.querySelector('.electricity-progress-targets');
    expect(targets.querySelectorAll('.electricity-progress-cards > div')).toHaveLength(3);
    expect(targets.textContent).toContain('DC-Modulleistung');
    expect(targets.textContent).toContain('mindestens 30 GW');
    expect([...targets.querySelectorAll('.electricity-progress-cards p > span')].every((span) => Number(span.textContent) >= 2030)).toBe(true);
    expect(root.querySelector('details').open).toBe(false);
    expect(root.querySelector(`details a[href="${snapshot.license_evidence}"]`)).not.toBeNull();
    expect(root.querySelector(`details a[href="${snapshot.source.license_url}"]`)).not.toBeNull();
    expect(root.querySelectorAll('.electricity-progress-key[aria-hidden="true"]')).toHaveLength(7);
    expect(root.querySelector('a[download]').getAttribute('href')).toBe('/data/german-electricity-progress.json');
    expect(JSON.parse(document.querySelector('#electricity-progress-data').textContent)).toEqual(progress.snapshot);
  });

  test('lazy visible-only init; metric changes only monthly chart, one axis; themes/resizes retain state', () => {
    mount();
    expect(window.echarts.init).not.toHaveBeenCalled();
    expect(global.IntersectionObserver.mock.calls[0][1]).toEqual({ rootMargin: '0px' });
    intersect([{ target: nodes()[0], isIntersecting: false }]);
    expect(window.echarts.init).not.toHaveBeenCalled();
    show(nodes()[0]); show(nodes()[0]);
    expect(window.echarts.init).toHaveBeenCalledTimes(1);
    expect(buttons().every((button) => button.disabled)).toBe(true);
    nodes().slice(1).forEach(show);
    buttons()[1].click();
    expect(charts.at(-1).setOption.mock.calls.at(-1)[0].yAxis.name).toBe('Mio. €');
    expect(buttons().map((button) => button.getAttribute('aria-pressed'))).toEqual(['false', 'true']);
    expect(nodes().at(-1).getAttribute('aria-label')).toContain('Kosten in Mio. €');
    charts.slice(0, -1).forEach((chart) => expect(chart.setOption).toHaveBeenCalledTimes(1));
    theme(); resize();
    expect(charts.at(-1).setOption.mock.calls.at(-1)[0].yAxis.name).toBe('Mio. €');
    charts.forEach((chart) => expect(chart.resize).toHaveBeenCalled());
    expect(window.echarts.init).toHaveBeenCalledTimes(5);
    expect(window.fetch).not.toHaveBeenCalled();
  });

  test('actual recent-period controller does not change progress or its local toggle', async () => {
    window.ElectricityData = data;
    window.fetch.mockResolvedValue({ ok: true, json: async () => recent });
    mount(); nodes().forEach(show); buttons()[1].click();
    const before = document.querySelector('#electricity-progress').innerHTML;
    const progressCharts = [...charts];
    window.eval(fs.readFileSync(path.join(__dirname, '../src/js/dashboards/electricity-dashboard.js'), 'utf8'));
    for (let i = 0; i < 12; i++) await Promise.resolve();
    for (const days of [1, 7, 30]) document.querySelector(`[data-days="${days}"]`).click();
    document.querySelector('.electricity-progress-method summary').click();
    // Opening methodology is the only HTML change.
    document.querySelector('.electricity-progress-method').removeAttribute('open');
    expect(document.querySelector('#electricity-progress').innerHTML).toBe(before);
    progressCharts.slice(0, -1).forEach((chart) => expect(chart.setOption).toHaveBeenCalledTimes(1));
    expect(progressCharts.at(-1).setOption).toHaveBeenCalledTimes(2);
    expect(window.fetch).toHaveBeenCalledTimes(1);
  });

  test('missing ECharts keeps static values, hides charts and disables controls', () => {
    delete window.echarts;
    mount();
    expect(nodes().every((node) => node.hidden)).toBe(true);
    expect(buttons().every((button) => button.disabled)).toBe(true);
    expect(document.querySelector('#electricity-progress-status').textContent).toContain('JSON-Download');
    expect(document.querySelector('#electricity-progress').textContent).toContain(progress.monthlyDate);
  });

  test('chart failures dispose only failed instance; malformed embed fails safely', () => {
    mount();
    const original = window.echarts.init;
    window.echarts.init = (node) => { const chart = original(node); chart.setOption.mockImplementation(() => { throw new Error('failed'); }); return chart; };
    show(nodes().at(-1));
    expect(charts[0].dispose).toHaveBeenCalled();
    expect(nodes().at(-1).hidden).toBe(true);
    expect(buttons().every((button) => button.disabled)).toBe(true);
    window.echarts.init = original;
    show(nodes()[0]);
    expect(charts[1].setOption).toHaveBeenCalledTimes(1);
    document.querySelector('#electricity-progress-data').textContent = '{';
    expect(() => mount()).not.toThrow();
  });

  test('without IntersectionObserver still initializes only visible containers', () => {
    delete global.IntersectionObserver;
    nodes().forEach((node, i) => { node.getBoundingClientRect = () => ({ top: i ? 5000 : 10, bottom: i ? 5300 : 310, height: 300, width: 400 }); });
    mount();
    expect(window.echarts.init).toHaveBeenCalledTimes(1);
  });
});
