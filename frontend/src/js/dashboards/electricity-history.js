/* Daily history is a separate contract from the recent hourly snapshot. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./electricity-data'));
  else root.ElectricityHistory = factory(root.ElectricityData);
})(typeof globalThis !== 'undefined' ? globalThis : this, function (recent) {
  'use strict';
  const PREFIX = '/data/history/german-electricity/';
  const DAY = 86400000;
  const SOURCES = [...recent.SOURCES, { key: 'nuclear', label: 'Kernkraft', color: '#b49a55', renewable: false }];
  const ENERGY = [...SOURCES.map((source) => source.key), 'load'];
  const GAPS = {
    '2016-11-08': 'other_renewables', '2018-01-21': 'pumped_storage',
    '2018-08-02': 'pumped_storage', '2018-08-03': 'pumped_storage', '2018-08-23': 'pumped_storage',
  };
  const POLICY = 'Daily SMARD sums (GWh) and mean prices (EUR/MWh); current-year corrections in the latest 35 complete days; older dates retained, closed years frozen even in January. Backfill required for gaps; existing years replaced only by explicit --reconcile. 2015-01-01 through 2015-01-04 prices unknown; missing nuclear after 2023-04-15 derived as zero and flagged. Historical daily sums may include upstream partial data/interpolation; only recent overlap is checked against complete hourly data. Known source gaps retained as null: other_renewables 2016-11-08; pumped_storage 2018-01-21, 2018-08-02, 2018-08-03, 2018-08-23. Never treat these as zero.';
  function assert(condition, message) {
    if (!condition) throw new Error(`Stromhistorie: ${message}`);
  }
  function fields(value, keys) {
    assert(value && typeof value === 'object' && !Array.isArray(value) &&
      Object.keys(value).sort().join() === [...keys].sort().join(), 'Unbekannte oder fehlende Felder');
  }
  function date(value) {
    assert(typeof value === 'string' && /^\d{4}-\d\d-\d\d$/.test(value), 'Ungültiges Datum');
    const time = Date.parse(`${value}T00:00:00Z`);
    assert(Number.isFinite(time) && new Date(time).toISOString().slice(0, 10) === value && value >= '2015-01-01', 'Ungültiges Datum');
    return time;
  }
  function nextDate(value) { return new Date(date(value) + DAY).toISOString().slice(0, 10); }
  function midnight(value) {
    const utc = date(value);
    // Berlin midnight is UTC+1/+2. At 22:00 UTC the local date determines which.
    return recent.dayKey(utc - 2 * recent.HOUR) === value ? utc - 2 * recent.HOUR : utc - recent.HOUR;
  }
  function hours(value) { return (midnight(nextDate(value)) - midnight(value)) / recent.HOUR; }
  function metadata(value, nullable = false) {
    assert((value.schema_version === 1 || (nullable && value.schema_version === 2)) && value.timezone === recent.TIMEZONE, 'Unbekanntes Datenformat');
    fields(value.source, Object.keys(recent.SOURCE));
    assert(Object.entries(recent.SOURCE).every(([key, expected]) => value.source[key] === expected), 'Ungültige Quelle');
  }
  function validateEntry(entry) {
    fields(entry, ['year', 'url', 'sha256', 'first_date', 'last_date', 'days', 'frozen']);
    assert(Number.isInteger(entry.year) && entry.year >= 2015 && entry.year <= 9998 && typeof entry.frozen === 'boolean', 'Ungültiges Jahr');
    assert(typeof entry.sha256 === 'string' && /^[a-f0-9]{64}$/.test(entry.sha256) &&
      entry.url === `${PREFIX}${entry.year}.${entry.sha256}.json`, 'Ungültige URL oder SHA-256');
    assert(entry.first_date === `${entry.year}-01-01` && typeof entry.last_date === 'string' && entry.last_date.startsWith(`${entry.year}-`), 'Ungültige Jahresabdeckung');
    const count = (date(entry.last_date) - date(entry.first_date)) / DAY + 1;
    assert(Number.isInteger(entry.days) && entry.days === count && count >= 1 && count <= 366, 'Ungültige Tagesanzahl');
    return entry;
  }
  function validateManifest(manifest) {
    fields(manifest, ['schema_version', 'kind', 'timezone', 'source', 'first_date', 'last_date', 'years', 'revision_policy']);
    metadata(manifest);
    assert(manifest.kind === 'german-electricity-history' && manifest.revision_policy === POLICY, 'Unbekannte Historienregeln');
    assert(Array.isArray(manifest.years) && manifest.years.length >= 1 && manifest.years.length <= 100, 'Ungültige Jahresliste');
    manifest.years.forEach((entry, index) => {
      validateEntry(entry);
      if (index) assert(nextDate(manifest.years[index - 1].last_date) === entry.first_date, 'Lücke oder unsortierte Jahre');
    });
    assert(manifest.first_date === manifest.years[0].first_date && manifest.last_date === manifest.years[manifest.years.length - 1].last_date, 'Manifest-Abdeckung stimmt nicht');
    return manifest;
  }
  function validatePartition(partition, entry) {
    validateEntry(entry);
    fields(partition, ['schema_version', 'year', 'timezone', 'source', 'rows']);
    metadata(partition, true);
    assert(partition.year === entry.year && Array.isArray(partition.rows) && partition.rows.length === entry.days, 'Jahr/Tagesanzahl stimmt nicht');
    let expected = entry.first_date;
    partition.rows.forEach((row) => {
      fields(row, ['date', 'hours', 'energy_gwh', 'price_eur_mwh', 'price_zone', 'nuclear_derived_zero']);
      assert(row.date === expected && row.hours === hours(expected), 'Lücke, Reihenfolge oder DST-Stunden ungültig');
      fields(row.energy_gwh, ENERGY);
      ENERGY.forEach((key) => {
        const value = row.energy_gwh[key];
        assert((value === null && (partition.schema_version === 2 || GAPS[row.date] === key)) ||
          (typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 200 * row.hours), 'Ungültige Energie oder unerlaubte Lücke');
      });
      const price = row.price_eur_mwh;
      assert(row.date < '2015-01-05' ? price === null : (partition.schema_version === 2 && price === null) || typeof price === 'number' && Number.isFinite(price) && Math.abs(price) <= 10000, 'Ungültiger Tagespreis');
      assert(row.price_zone === (row.date < '2018-10-01' ? 'DE-AT-LU' : 'DE-LU'), 'Ungültige Preiszone');
      assert(typeof row.nuclear_derived_zero === 'boolean' && (!row.nuclear_derived_zero || (row.date > '2023-04-15' && row.energy_gwh.nuclear === 0)), 'Ungültige abgeleitete Kernkraft-Null');
      assert(row.date <= '2023-04-15' || row.energy_gwh.nuclear === 0, 'Kernkraft nach Abschaltung');
      expected = nextDate(expected);
    });
    assert(partition.rows[partition.rows.length - 1].date === entry.last_date, 'Jahresende stimmt nicht');
    return partition;
  }
  // Preserve Python numeric tokens; reject duplicate keys, whitespace, unsorted keys
  // and trailing newlines without reserializing floating-point observations.
  function parseCanonical(bytes, limit, omitRoot = []) {
    assert(ArrayBuffer.isView(bytes) && bytes.BYTES_PER_ELEMENT === 1 && bytes.byteLength > 0 && bytes.byteLength <= limit, 'Ungültige Dateigröße');
    const raw = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes);
    const parsed = JSON.parse(raw);
    const tokens = raw.match(/"(?:[^"\\]|\\.)*"|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null|[{}[\],:]/g);
    let index = 0;
    let semantic;
    function consume(expected) { assert(tokens[index++] === expected, 'Ungültige JSON-Struktur'); }
    function value(root = false) {
      const token = tokens[index++];
      if (token === '{') {
        const entries = new Map();
        while (tokens[index] !== '}') {
          const key = JSON.parse(tokens[index++]);
          assert(!entries.has(key), 'Doppelter JSON-Schlüssel');
          consume(':');
          entries.set(key, value());
          if (tokens[index] !== ',') break;
          consume(',');
        }
        consume('}');
        const keys = [...entries.keys()].sort();
        const encode = (selected) => `{${selected.map((key) => `${JSON.stringify(key)}:${entries.get(key)}`).join(',')}}`;
        if (root) semantic = encode(keys.filter((key) => !omitRoot.includes(key)));
        return encode(keys);
      }
      if (token === '[') {
        const items = [];
        while (tokens[index] !== ']') {
          items.push(value());
          if (tokens[index] !== ',') break;
          consume(',');
        }
        consume(']');
        return `[${items.join(',')}]`;
      }
      return token.startsWith('"') ? JSON.stringify(JSON.parse(token)) : token;
    }
    assert(value(true) === raw && index === tokens.length, 'JSON muss kompakt, sortiert und ohne Zeilenumbruch sein');
    return { parsed, semantic };
  }
  function parseRaw(bytes, limit) { return parseCanonical(bytes, limit).parsed; }
  async function verifyPartition(bytes, entry, subtle = globalThis.crypto && globalThis.crypto.subtle) {
    validateEntry(entry);
    assert(bytes.byteLength <= 250000 && subtle, 'SHA-256-Prüfung nicht verfügbar oder Datei zu groß');
    const hash = [...new Uint8Array(await subtle.digest('SHA-256', bytes))].map((n) => n.toString(16).padStart(2, '0')).join('');
    assert(hash === entry.sha256, 'SHA-256 stimmt nicht mit Manifest überein');
    return validatePartition(parseRaw(bytes, 250000), entry);
  }
  function createLoader(manifest, fetcher = globalThis.fetch.bind(globalThis), subtle) {
    validateManifest(manifest);
    // Pin the entire HTML manifest; refreshing a stale URL must never swap identities.
    const identity = JSON.stringify(manifest);
    const cache = new Map();
    async function request(url, limit) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 15000);
      try {
        const response = await fetcher(url, { mode: 'same-origin', signal: controller.signal,
          cache: url.endsWith('/manifest.json') ? 'no-cache' : 'default' });
        if (!response.ok) { const error = new Error('Historie nicht verfügbar'); error.status = response.status; throw error; }
        const bytes = new Uint8Array(await response.arrayBuffer());
        assert(bytes.byteLength <= limit, 'Datei zu groß');
        return bytes;
      } finally { clearTimeout(timeout); }
    }
    return function loadYear(year) {
      const entry = manifest.years.find((item) => item.year === year);
      assert(entry, 'Jahr nicht im Manifest');
      if (!cache.has(year)) {
        const pending = (async () => {
          let bytes;
          try { bytes = await request(entry.url, 250000); }
          catch (error) {
            if (error.status !== 404) throw error;
            const current = validateManifest(parseRaw(await request(`${PREFIX}manifest.json`, 20000), 20000));
            assert(JSON.stringify(current) === identity, 'Veröffentlichung geändert. Bitte die Seite neu laden');
            bytes = await request(entry.url, 250000);
          }
          return verifyPartition(bytes, entry, subtle);
        })();
        cache.set(year, pending);
        pending.catch(() => cache.delete(year));
      }
      return cache.get(year);
    };
  }
  function complete(row) { return SOURCES.every((source) => row.energy_gwh[source.key] !== null); }
  function summarize(partition) {
    const rows = partition.rows;
    const known = rows.filter(complete);
    const knownHours = known.reduce((sum, row) => sum + row.hours, 0);
    const totalHours = rows.reduce((sum, row) => sum + row.hours, 0);
    const energy = Object.fromEntries(SOURCES.map(({ key }) => [key, known.reduce((sum, row) => sum + row.energy_gwh[key], 0)]));
    const generationEnergy = SOURCES.reduce((sum, source) => sum + energy[source.key], 0);
    const renewableEnergy = SOURCES.filter((source) => source.renewable).reduce((sum, source) => sum + energy[source.key], 0);
    const prices = rows.filter((row) => row.price_eur_mwh !== null);
    const loads = rows.filter((row) => row.energy_gwh.load !== null);
    const loadHours = loads.reduce((sum, row) => sum + row.hours, 0);
    const loadEnergy = loadHours ? loads.reduce((sum, row) => sum + row.energy_gwh.load, 0) : null;
    const priceHours = prices.reduce((sum, row) => sum + row.hours, 0);
    const mix = SOURCES.map((source) => ({ ...source, energy: knownHours ? energy[source.key] : null,
      average: knownHours ? energy[source.key] / knownHours : null,
      share: generationEnergy ? energy[source.key] / generationEnergy * 100 : null }));
    return { kind: 'history', year: partition.year, rows, mix, hours: knownHours, totalHours,
      completeDays: known.length, days: rows.length, priceDays: prices.length, priceHours,
      generationEnergy: knownHours ? generationEnergy : null,
      generationAverage: knownHours ? generationEnergy / knownHours : null,
      loadDays: loads.length, loadHours, loadEnergy, loadAverage: loadHours ? loadEnergy / loadHours : null,
      renewableShare: generationEnergy ? renewableEnergy / generationEnergy * 100 : null,
      priceAverage: priceHours ? prices.reduce((sum, row) => sum + row.price_eur_mwh * row.hours, 0) / priceHours : null,
      negativeDays: prices.length ? prices.filter((row) => row.price_eur_mwh < 0).length : null,
      derivedZeroDays: rows.filter((row) => row.nuclear_derived_zero).length,
      zone: partition.year === 2018 ? 'DE–AT–LU / DE–LU (gemischte Marktgebiete 2018)' : partition.year < 2018 ? 'DE–AT–LU' : 'DE–LU',
      label: `${recent.dateLabel(date(rows[0].date))} – ${recent.dateLabel(date(rows[rows.length - 1].date))}` };
  }
  function presentation(summary) {
    const n = recent.number;
    const coverage = `${summary.completeDays}/${summary.days} vollständige Tage`;
    return { label: summary.label, generation: n(summary.generationAverage), energy: n(summary.generationEnergy),
      renewable: n(summary.renewableShare), load: n(summary.loadAverage), price: n(summary.priceAverage, 2),
      hours: n(summary.hours, 0), negative: n(summary.negativeDays, 0),
      coverage: `${coverage} für Erzeugung und Mix. Netzlast: ${summary.loadDays}/${summary.days} Tage. ${summary.completeDays < summary.days ? 'Teilsummen, keine Jahressummen: Erzeugung und Anteile nur über dieselben vollständigen Erzeugungstage.' : 'Vollständig bedeutet: alle täglichen Quellwerte vorhanden; keine Bestätigung lückenloser zugrunde liegender Stunden.'}`,
      generationText: `${coverage}: ${n(summary.generationEnergy)} GWh öffentliche Erzeugung, ${n(summary.generationAverage)} GW im Mittel; ${n(summary.renewableShare)} % erneuerbar. Tagesenergie / tatsächliche Tagesstunden (23/24/25) ergibt die dargestellte Leistung. Unvollständige Erzeugungstage bleiben als Lücken sichtbar.`,
      loadText: `Netzlast: ${summary.loadDays}/${summary.days} Tage (${n(summary.loadHours, 0)}/${n(summary.totalHours, 0)} Stunden): ${n(summary.loadEnergy)} GWh aus bekannten Werten, bei Lücken Teilsumme; im Mittel ${n(summary.loadAverage)} GW. Unabhängige Abdeckung von der Erzeugung.`,
      priceText: `${summary.zone}: ${n(summary.priceAverage, 2)} €/MWh, Tagesmittel nach tatsächlichen Tagesstunden gewichtet. Preisabdeckung: ${summary.priceDays}/${summary.days} Tage (${n(summary.priceHours, 0)}/${n(summary.totalHours, 0)} Stunden). ${n(summary.negativeDays, 0)} ${summary.negativeDays === 1 ? 'Tag' : 'Tage'} mit negativem Tagesmittel unter bekannten Tagen. Negative Preisstunden und stündliche Minima/Maxima sind daraus nicht ableitbar.`,
    };
  }
  function freshness(manifest, now = Date.now()) {
    const end = midnight(nextDate(manifest.last_date));
    assert(Number.isFinite(now) && end <= now, 'Historienende liegt in der Zukunft; Geräteuhr prüfen');
    return { stale: now - end > 96 * recent.HOUR };
  }
  function safeJSON(value) {
    return JSON.stringify(value).replace(/</g, '\\u003c').replace(/>/g, '\\u003e').replace(/&/g, '\\u0026').replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029');
  }
  return { PREFIX, POLICY, SOURCES, ENERGY, date, hours, complete, parseRaw, parseCanonical, validateManifest, validatePartition,
    verifyPartition, createLoader, summarize, presentation, freshness, safeJSON };
});
