(function () {
  'use strict';
  const root = document.getElementById('electricity-dashboard');
  if (!root) return;
  const data = window.ElectricityData;
  const history = window.ElectricityHistory;
  const embedded = document.getElementById('electricity-history-manifest');
  const ytd = document.getElementById('electricity-ytd');
  const yearSelect = document.getElementById('electricity-year');
  const status = document.getElementById('electricity-status');
  const warning = document.getElementById('electricity-freshness');
  const buttons = [...root.querySelectorAll('[data-days]')];
  const chartNodes = ['generation', 'load', 'price'].map((name) => document.getElementById(`electricity-${name}`));
  const theme = window.matchMedia('(prefers-color-scheme: dark)');
  let snapshot;
  let manifest;
  let loadYear;
  let view = null; // Only committed, validated selections may be rendered.
  let selection = embedded ? { kind: 'history', year: Number(ytd.dataset.year) } : { kind: 'recent', days: 1 };
  let requestId = 0;
  let pending = false;
  let charts = [];

  function checkFreshness() {
    try {
      const state = data.freshness({ data_through: root.dataset.through,
        snapshot_created_at: root.dataset.created, stale_after_hours: 96 });
      const statusData = snapshot || (document.getElementById('electricity-component-data') ? JSON.parse(document.getElementById('electricity-component-data').textContent) : null);
      const components = statusData?.components ? data.componentReport({ ...statusData, schema_version: 2 }) : [];
      const partial = components.some((component) => component.stale || component.status !== 'complete') || Object.values(statusData?.refresh_status || {}).some((meta) => meta.status !== 'ok');
      warning.hidden = !state.stale && !partial;
      warning.textContent = partial ? 'Stundendaten teilweise unvollständig oder veraltet. Separate Quellenstände und Abdeckung stehen im Komponentenbericht. Fehlende Werte sind keine Nullen.' : 'Die aktuellen Stundendaten sind älter als 96 Stunden. Es liegen hier noch keine neueren vollständigen Daten vor.';
      return true;
    } catch (error) {
      warning.hidden = false;
      warning.textContent = 'Die Zeitangaben dieses Snapshots sind ungültig oder liegen in der Zukunft. Bitte auch die Uhrzeit Ihres Geräts prüfen. Die vorgerenderte Übersicht ist nicht als aktuell bestätigt.';
      return false;
    }
  }
  function checkHistoryFreshness() {
    if (!manifest) return;
    const notice = document.getElementById('electricity-history-freshness');
    try {
      notice.hidden = !history.freshness(manifest).stale;
      notice.textContent = `Die Tageshistorie reicht bis ${manifest.last_date}; ihr neuester Datenstand ist älter als 96 Stunden. Diese Warnung betrifft nicht das Alter eines ausgewählten früheren Jahres.`;
    } catch (error) { notice.hidden = false; notice.textContent = error.message; }
  }
  function hideCharts() {
    charts.forEach((chart) => chart.dispose());
    charts = [];
    chartNodes.forEach((node) => { node.hidden = true; });
  }
  function options(summary) {
    const historical = summary.kind === 'history';
    const style = getComputedStyle(root);
    const ink = style.getPropertyValue('--electricity-ink').trim();
    const line = style.getPropertyValue('--electricity-line').trim();
    const accent = style.getPropertyValue('--electricity-accent').trim();
    const rows = summary.rows;
    const labels = rows.map((row) => historical ? row.date : String(row[0]));
    const shortTime = new Intl.DateTimeFormat('de-DE', {
      timeZone: data.TIMEZONE, hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
      ...(selection.days > 1 ? { day: '2-digit', month: '2-digit' } : {}),
    });
    function base(unit, description) {
      return {
        animation: false,
        backgroundColor: 'transparent',
        textStyle: { color: ink, fontFamily: style.fontFamily },
        aria: { enabled: true, label: { description } },
        grid: { left: 8, right: 18, top: 35, bottom: 12, containLabel: true },
        tooltip: {
          trigger: 'axis', renderMode: 'richText', confine: true,
          formatter: (items) => {
            const label = historical ? `${data.dateLabel(history.date(items[0].axisValue))} · Tagesmittel` : `${data.timestampLabel(Number(items[0].axisValue))} · Stundenbeginn`;
            return `${label}\n${items.map((item) => {
              const value = item.value && typeof item.value === 'object' ? item.value.value : item.value;
              return `${item.seriesName}: ${data.number(typeof value === 'number' ? value : null, unit === '€/MWh' ? 2 : 1)} ${unit}`;
            }).join('\n')}`;
          },
        },
        xAxis: { type: 'category', data: labels, boundaryGap: true,
          axisLabel: { color: ink, hideOverlap: true, formatter: (value) => historical ? `${value.slice(8, 10)}.${value.slice(5, 7)}.` : shortTime.format(Number(value)) },
          axisLine: { lineStyle: { color: line } }, axisTick: { show: false } },
        yAxis: { type: 'value', name: unit, nameTextStyle: { color: ink },
          axisLabel: { color: ink }, splitLine: { lineStyle: { color: line } } },
      };
    }
    const text = historical ? history.presentation(summary) : data.presentation(summary);
    return [
      { ...base('GW', text.generationText), series: (historical ? history.SOURCES : data.SOURCES).map((source) => ({
        name: source.label, type: 'line', stack: 'generation', step: 'middle',
        showSymbol: false, lineStyle: { width: 0 }, areaStyle: { opacity: 0.9 },
        itemStyle: { color: source.color }, emphasis: { focus: 'series' },
        connectNulls: false,
        data: rows.map((row) => historical ? (history.complete(row) ? row.energy_gwh[source.key] / row.hours : null) : (data.SOURCES.every((item) => row[item.index] !== null) ? row[source.index] : null)),
      })) },
      { ...base('GW', text.loadText), series: [{ name: 'Netzlast', type: 'line',
        step: 'middle', showSymbol: false, lineStyle: { width: 2 },
        connectNulls: false,
        itemStyle: { color: accent }, areaStyle: { opacity: 0.12 }, data: rows.map((row) => historical ? (row.energy_gwh.load === null ? null : row.energy_gwh.load / row.hours) : row[12]) }] },
      { ...base('€/MWh', text.priceText), series: [{ name: historical ? 'Day-Ahead · Tagesmittel' : 'Day-Ahead · Stundenmittel', type: 'bar',
        barCategoryGap: '10%', data: rows.map((row) => {
          const value = historical ? row.price_eur_mwh : row[13];
          return { value, itemStyle: { color: value !== null && value < 0 ? '#cb6573' : accent } };
        }),
        markLine: { silent: true, symbol: 'none', label: { show: false },
          lineStyle: { color: ink, type: 'solid', width: 1 }, data: [{ yAxis: 0 }] } }] },
    ];
  }
  function syncSelection() {
    buttons.forEach((button) => { button.setAttribute('aria-pressed', String(selection.kind === 'recent' && Number(button.dataset.days) === selection.days)); });
    if (ytd) {
      const latest = manifest ? manifest.years[manifest.years.length - 1].year : Number(ytd.dataset.year);
      ytd.setAttribute('aria-pressed', String(selection.kind === 'history' && selection.year === latest));
      yearSelect.value = selection.kind === 'history' && selection.year !== latest ? String(selection.year) : '';
      yearSelect.dataset.active = String(yearSelect.value !== '');
    }
  }
  function renderMix(summary) {
    const tbody = root.querySelector('.electricity-mix tbody');
    const legend = root.querySelector('.electricity-legend');
    tbody.replaceChildren();
    legend.replaceChildren();
    summary.mix.forEach((source) => {
      const row = document.createElement('tr');
      row.dataset.source = source.key;
      row.style.setProperty('--source-color', source.color);
      const heading = document.createElement('th');
      heading.scope = 'row';
      const swatch = document.createElement('span');
      swatch.className = 'electricity-swatch';
      swatch.setAttribute('aria-hidden', 'true');
      heading.append(swatch, document.createTextNode(source.label));
      const bar = document.createElement('span');
      bar.className = 'electricity-mix-bar';
      bar.setAttribute('aria-hidden', 'true');
      const fill = document.createElement('span');
      fill.style.width = `${source.share || 0}%`;
      bar.append(fill);
      heading.append(bar);
      row.append(heading);
      ['average', 'energy', 'share'].forEach((key) => {
        const cell = document.createElement('td');
        cell.dataset.mix = key;
        cell.textContent = data.number(source[key]);
        row.append(cell);
      });
      tbody.append(row);
      const item = document.createElement('li');
      item.style.setProperty('--source-color', source.color);
      item.append(swatch.cloneNode(), document.createTextNode(source.label));
      legend.append(item);
    });
  }
  function render(announce = true) {
    if (!view) return;
    const summary = view;
    const historical = summary.kind === 'history';
    const text = historical ? history.presentation(summary) : data.presentation(summary);
    root.querySelectorAll('[data-value]').forEach((node) => { node.textContent = text[node.dataset.value]; });
    renderMix(summary);
    syncSelection();
    const coverage = document.getElementById('electricity-history-coverage');
    coverage.hidden = false;
    coverage.textContent = text.coverage;
    const provenance = document.getElementById('electricity-history-selection');
    provenance.hidden = !historical;
    if (historical) {
      const entry = manifest.years.find((item) => item.year === selection.year);
      provenance.textContent = `Jahr ${entry.year} · ${entry.frozen ? 'eingefrorener Datenstand' : 'Korrekturfenster: 35 Tage'} · ${entry.last_date.endsWith('-12-31') ? 'bis Jahresende' : `Teiljahr bis ${entry.last_date}`} · ${summary.zone}.`;
      document.getElementById('electricity-history-download').href = entry.url;
    }
    const download = document.getElementById('electricity-history-download');
    if (download) download.hidden = !historical;
    document.getElementById('electricity-negative-label').textContent = historical ? 'Tage mit negativem Tagesmittel' : 'Stunden mit negativem Stundenmittel';
    document.getElementById('electricity-price-kpi-label').textContent = 'Day-Ahead · Ø Preis (zeitgewichtet)';
    document.getElementById('electricity-generation-heading').textContent = historical ? 'Erzeugung im Jahresverlauf' : selection.days === 1 ? 'Erzeugung im Tagesverlauf' : 'Erzeugung im Zeitraum';
    document.getElementById('electricity-generation-grain').textContent = `${historical ? 'Tägliche' : 'Stündliche'} mittlere Leistung in GW · Flächen nach Energieträger gestapelt`;
    document.getElementById('electricity-load-grain').textContent = `Gesamt (Netzlast) · ${historical ? 'Tagesmittel' : 'Stundenmittel'} in GW`;
    document.getElementById('electricity-price-grain').textContent = `Day-Ahead, ${historical ? `${summary.zone} · Tagesmittel` : 'DE–LU · Stundenmittel'} · €/MWh`;
    try {
      if (!window.echarts) throw new Error('ECharts unavailable');
      if (!charts.length) {
        chartNodes.forEach((node) => {
          node.hidden = false;
          charts.push(window.echarts.init(node));
        });
      }
      options(summary).forEach((option, index) => charts[index].setOption(option, { notMerge: true }));
      if (announce) status.textContent = `${summary.label} · ${historical ? `${summary.completeDays}/${summary.days} vollständige Erzeugungstage (tägliche Quellwerte)` : text.coverage}. Zeitraum-Ansichten aktualisiert; Langfristvergleich unverändert.`;
    } catch (error) {
      hideCharts();
      if (announce) status.textContent = `${summary.label}: Diagramme konnten nicht dargestellt werden. Kennzahlen, Quellenmix und Textzusammenfassungen sind verfügbar. Bitte die Seite neu laden, um die Diagramme erneut zu versuchen.`;
    }
  }
  function chooseRecent(days) {
    if (!snapshot) return;
    ++requestId;
    pending = false;
    root.removeAttribute('aria-busy');
    view = data.summarize(snapshot, days);
    selection = { kind: 'recent', days };
    render();
  }
  async function chooseHistory(year) {
    const id = ++requestId;
    pending = true;
    syncSelection(); // The dropdown continues to describe the committed view while loading.
    root.setAttribute('aria-busy', 'true');
    status.textContent = `Tageshistorie ${year} wird geladen und geprüft … Die bisherige Auswahl bleibt sichtbar.`;
    try {
      const partition = await loadYear(year);
      if (id !== requestId) return;
      const candidate = history.summarize(partition);
      selection = { kind: 'history', year };
      view = candidate;
      render();
    } catch (error) {
      if (id !== requestId) return;
      status.textContent = `Historie ${year} konnte nicht geladen oder validiert werden. Die bisherige Auswahl bleibt erhalten. Bitte erneut wählen oder die Seite neu laden. ${error.message}`;
      syncSelection();
    } finally {
      if (id === requestId) { pending = false; root.removeAttribute('aria-busy'); }
    }
  }
  async function load() {
    if (!data || !checkFreshness()) {
      if (!embedded) status.textContent = 'Interaktive Daten konnten nicht validiert werden. Die vorgerenderte Tagesübersicht bleibt lesbar.';
      return;
    }
    if (!embedded) {
      status.textContent = 'Vorbereitete Stromdaten werden geladen …';
      root.setAttribute('aria-busy', 'true');
    }
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch('/data/german-electricity.json', { signal: controller.signal, mode: 'same-origin' });
      if (!response.ok) throw new Error('Snapshot request failed');
      const candidate = data.validateSnapshot(await response.json());
      if (candidate.content_hash !== root.dataset.hash || candidate.data_through !== root.dataset.through || candidate.snapshot_created_at !== root.dataset.created) {
        throw new Error('HTML and JSON snapshots differ');
      }
      snapshot = candidate;
      checkFreshness();
      if (!embedded) chooseRecent(1);
      buttons.forEach((button) => {
        button.disabled = false;
        button.addEventListener('click', () => chooseRecent(Number(button.dataset.days)));
      });
    } catch (error) {
      if (!embedded) status.textContent = 'Interaktive Daten konnten nicht geladen oder validiert werden. Die vorgerenderte Tagesübersicht bleibt lesbar. Bitte die Seite neu laden, um es erneut zu versuchen.';
      else {
        const errorNotice = document.getElementById('electricity-recent-error');
        errorNotice.hidden = false;
        errorNotice.textContent = 'Aktuelle Stundendaten konnten nicht geladen oder validiert werden; die 1/7/30-Tage-Auswahl ist nicht verfügbar. Bitte die Seite neu laden.';
      }
    } finally {
      clearTimeout(timeout);
      if (!pending) root.removeAttribute('aria-busy');
    }
  }
  theme.addEventListener('change', () => render(false));
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(() => charts.forEach((chart) => chart.resize())).observe(root);
  } else {
    window.addEventListener('resize', () => charts.forEach((chart) => chart.resize()));
  }
  // Freshness must advance even when a tab remains open and no new build succeeds.
  function checkAllFreshness() { checkFreshness(); checkHistoryFreshness(); }
  setInterval(checkAllFreshness, 60000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) checkAllFreshness(); });
  if (embedded) {
    try {
      manifest = history.validateManifest(JSON.parse(embedded.textContent));
      loadYear = history.createLoader(manifest);
      ytd.disabled = false;
      yearSelect.disabled = false;
      const latest = manifest.years[manifest.years.length - 1].year;
      ytd.addEventListener('click', () => chooseHistory(latest));
      yearSelect.addEventListener('change', () => { if (yearSelect.value) chooseHistory(Number(yearSelect.value)); else syncSelection(); });
      checkHistoryFreshness();
      chooseHistory(latest);
    } catch (error) {
      status.textContent = 'Die Historie konnte nicht validiert werden. Die vorgerenderte Übersicht bleibt lesbar. Bitte die Seite neu laden.';
    }
  }
  load();
})();
