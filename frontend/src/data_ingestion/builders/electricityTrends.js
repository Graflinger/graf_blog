const fs = require('fs');
const path = require('path');
const { createHash } = require('crypto');
const { isDeepStrictEqual } = require('util');
const history = require('../../js/dashboards/electricity-history');
const electricity = require('../../js/dashboards/electricity-data');
const trade = require('../../js/dashboards/electricity-trade');
const { readHistory } = require('./electricityHistory');
const { verifySnapshot } = require('./electricitySnapshot');

const TRADE_PATH = path.resolve(__dirname, '../../_data/germanElectricityTrade.json');
const ANNUAL_PATH = path.resolve(__dirname, '../../_data/germanElectricityAnnual.json');
const GENERATION_KEYS = history.SOURCES.map((source) => source.key);
const MONTHS = ['Jan', 'Feb', 'Mär', 'Apr', 'Mai', 'Jun', 'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez'];
const round = (value) => value === null ? null : Number(value.toFixed(8));
const twh = (value) => value === null ? null : round(value / 1000);
function verifyTrade(bytes, now) {
  const { snapshot, semantic } = trade.parseSnapshot(bytes, now);
  if (createHash('sha256').update(semantic, 'utf8').digest('hex') !== snapshot.content_hash) throw new Error('Stromhandel: content_hash stimmt nicht mit dem Dateiinhalt überein');
  return snapshot;
}
function verifyAnnual(bytes) {
  const { parsed: snapshot, semantic } = history.parseCanonical(bytes, 10000, ['content_hash']);
  const assert = (condition, message) => { if (!condition) throw new Error(`Stromjahreswerte: ${message}`); };
  const fields = (value, keys) => assert(value && typeof value === 'object' && !Array.isArray(value) && isDeepStrictEqual(Object.keys(value).sort(), [...keys].sort()), 'Unbekannte oder fehlende Felder');
  fields(snapshot, ['schema_version', 'kind', 'source', 'region', 'timezone', 'resolution', 'years', 'content_hash']);
  assert(isDeepStrictEqual(snapshot.source, electricity.SOURCE), 'Ungültige Quelle');
  assert(snapshot.schema_version === 1 && snapshot.kind === 'german-electricity-annual' && snapshot.region === 'DE' && snapshot.timezone === electricity.TIMEZONE && snapshot.resolution === 'year', 'Unbekannte Metadaten');
  assert(typeof snapshot.content_hash === 'string' && /^[a-f0-9]{64}$/.test(snapshot.content_hash), 'Ungültiger content_hash');
  assert(createHash('sha256').update(semantic, 'utf8').digest('hex') === snapshot.content_hash, 'content_hash stimmt nicht mit dem Dateiinhalt überein');
  assert(Array.isArray(snapshot.years) && snapshot.years.length > 0 && snapshot.years.length <= 2, 'Ungültige Jahresauswahl');
  snapshot.years.forEach((row, index) => {
    fields(row, ['year', 'energy_gwh', 'method']);
    assert([2016, 2018].includes(row.year) && (!index || row.year > snapshot.years[index - 1].year), 'Ungültige Jahresauswahl');
    assert(row.method === 'source_annual_aggregate', 'Unbekannte Jahresmethode');
    fields(row.energy_gwh, GENERATION_KEYS);
    const upper = 200 * 24 * (row.year === 2016 ? 366 : 365);
    const values = Object.values(row.energy_gwh);
    assert(values.every((value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= upper), 'Ungültige Jahresmenge');
    const total = values.reduce((sum, value) => sum + value, 0);
    assert(total >= 100000 && total <= upper, 'Unplausible Jahreserzeugung');
  });
  return snapshot;
}
// Inputs are validated by readHistory/verifyTrade/verifyAnnual. No clock-dependent aggregation.
function aggregate(historyData, snapshot, annual = null) {
  const { manifest, manifestHash, partitions } = historyData;
  const lastDay = manifest.last_date;
  const nextDay = new Date(history.date(lastDay) + 86400000).toISOString().slice(0, 10);
  const cutoff = nextDay.endsWith('-01') ? lastDay.slice(0, 7) : new Date(Date.UTC(Number(lastDay.slice(0, 4)), Number(lastDay.slice(5, 7)) - 1, 0)).toISOString().slice(0, 7);
  if (snapshot.last_month > cutoff) throw new Error('Stromhandel reicht über den letzten vollständigen Historienmonat hinaus');
  const energy = partitions.filter((partition) => partition.rows.at(-1).date === `${partition.year}-12-31`).map((partition) => {
    const summary = history.summarize(partition);
    const supplement = annual?.years.find((row) => row.year === partition.year);
    const complete = Boolean(supplement) || summary.completeDays === summary.days;
    // Replace the whole annual mix, never individual cells or missing daily rows.
    const byKey = supplement ? supplement.energy_gwh : Object.fromEntries(summary.mix.map((source) => [source.key, source.energy]));
    const generation = supplement ? GENERATION_KEYS.reduce((sum, key) => sum + byKey[key], 0) : summary.generationEnergy;
    const renewable = history.SOURCES.filter((source) => source.renewable).reduce((sum, source) => sum + byKey[source.key], 0);
    const share = (amount) => complete && generation > 0 ? round(amount / generation * 100) : null;
    return { year: partition.year, days: summary.days, complete_days: summary.completeDays,
      method: supplement ? supplement.method : 'daily_sum',
      renewable_share: share(renewable),
      coal_share: share(byKey.lignite + byKey.hard_coal), gas_share: share(byKey.gas),
      generation_twh: complete ? twh(generation) : null,
      mix_twh: Object.fromEntries(GENERATION_KEYS.map((key) => [key, complete ? twh(byKey[key]) : null])) };
  });
  const months = snapshot.rows.map((row) => ({ month: row.month, imports_twh: twh(row.imports_gwh), exports_twh: twh(row.exports_gwh), net_exports_twh: twh(row.net_exports_gwh), missing_series: row.missing_series, structural_zero_series: row.structural_zero_series }));
  const years = [...new Set(snapshot.rows.map((row) => Number(row.month.slice(0, 4))))].map((year) => {
    const rows = snapshot.rows.filter((row) => row.month.startsWith(`${year}-`));
    const known = rows.filter((row) => !row.missing_series.length);
    const lastMonth = rows.at(-1).month;
    const complete = known.length === rows.length;
    const sum = (key) => complete ? twh(rows.reduce((total, row) => total + row[key], 0)) : null;
    return { year, months: rows.length, complete_months: known.length, partial_year: rows.length < 12,
      label: rows.length === 12 ? String(year) : `${year} (${rows.length === 1 ? 'Jan' : `Jan–${MONTHS[Number(lastMonth.slice(5)) - 1]}`})`,
      imports_twh: sum('imports_gwh'), exports_twh: sum('exports_gwh'), net_exports_twh: sum('net_exports_gwh') };
  });
  const annualYears = energy.filter((row) => row.method === 'source_annual_aggregate').map((row) => row.year);
  return { schema_version: 1, kind: 'german-electricity-trends', source: manifest.source,
    inputs: { history_manifest_sha256: manifestHash, history_partitions: manifest.years.map(({ year, sha256 }) => ({ year, sha256 })), trade_content_hash: snapshot.content_hash, annual_content_hash: annual?.content_hash ?? null },
    coverage: { energy_first_date: manifest.first_date, energy_last_date: lastDay, trade_first_month: snapshot.first_month, trade_last_month: snapshot.last_month },
    notes: { energy: 'Nur abgeschlossene Kalenderjahre. Methode source_annual_aggregate: veröffentlichte SMARD-Jahresaggregate aller zwölf Energieträger; daily_sum: summierte Tageswerte, bei fehlenden Erzeugungstagen Jahreslücke statt Teilsumme. Tageslücken und complete_days bleiben unverändert. Anteile aus Energiesummen; Nenner inklusive Pumpspeicher und Kernkraft. Quellaggregate bestätigen keine lückenlosen zugrunde liegenden Stunden.',
      annual: annualYears.length ? `${annualYears.join(' und ')}: veröffentlichte SMARD-Jahresaggregate; übrige Jahre aus Tageswerten summiert. Lücken in Tageshistorie bleiben bestehen.` : null,
      trade: 'Geplanter kommerzieller Austausch DE–LU, keine physischen Flüsse. Import und Export als positive Mengen; Nettoexport = Export − Import. Teiljahre nur Januar bis letztem vollständigen Monat, keine Hochrechnung. Bei fehlenden Monaten keine Jahressumme. Quellsummen bestätigen keine Stunden-Vollständigkeit.' },
    energy, trade: { years, months } };
}
function readTrends() {
  const stat = fs.lstatSync(TRADE_PATH);
  if (!stat.isFile() || stat.size > 250000) throw new Error('Ungültige Handelsdatei');
  const snapshot = verifyTrade(fs.readFileSync(TRADE_PATH));
  const recent = verifySnapshot(fs.readFileSync(path.resolve(__dirname, '../../_data/germanElectricity.json'), 'utf8'));
  if (recent.refresh_status?.trade && recent.refresh_status.trade.data_through !== snapshot.last_month) throw new Error('Trade/status cutoff mismatch');
  let annual = null;
  let annualStat;
  try { annualStat = fs.lstatSync(ANNUAL_PATH); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  if (annualStat) {
    if (!annualStat.isFile() || annualStat.size > 10000) throw new Error('Ungültige Jahresdatei');
    annual = verifyAnnual(fs.readFileSync(ANNUAL_PATH));
  }
  const summary = aggregate(readHistory(), snapshot, annual);
  return { summary, json: history.safeJSON(summary), sources: history.SOURCES };
}
function verifyPublishedTrends(outputDirectory) {
  const { summary, json } = readTrends();
  const published = JSON.parse(fs.readFileSync(path.join(outputDirectory, 'data/german-electricity-trends.json'), 'utf8'));
  if (!isDeepStrictEqual(published, summary)) throw new Error('Published trends differ from validated inputs; retry build');
  const html = fs.readFileSync(path.join(outputDirectory, 'dashboards/strom/index.html'), 'utf8');
  if (!html.includes(`<script type="application/json" id="electricity-trends-data">${json}</script>`)) throw new Error('Published HTML/trends mismatch');
}
module.exports = { aggregate, verifyTrade, verifyAnnual, readTrends, verifyPublishedTrends };
