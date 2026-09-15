/* Shared browser/Node schema; raw canonical hashing is performed by the build. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./electricity-data'), require('./electricity-history'));
  else root.ElectricityTrade = factory(root.ElectricityData, root.ElectricityHistory);
})(typeof globalThis !== 'undefined' ? globalThis : this, function (recent, history) {
  'use strict';
  const POLICY = 'Monthly SMARD DE-LU scheduled commercial exchanges, MWh converted to GWh; imports and exports are positive magnitudes, net_exports = exports - imports. Cutoff is the latest full month supported by validated daily history and as-of. Correct only the latest three completed months within the current year; retain older months and freeze closed years. Gaps and closed-year corrections require explicit backfill --reconcile. Missing BE before 2020-11 and NO2 before 2020-12 are flagged structural zeros (commercial trading had not begun). Known missing BE 2020-11 and NO2 2020-12 use a bounded daily fallback from commercial start; if unrecoverable, keep all three totals null with missing series IDs. New gaps fail. Official net discrepancies in 2021-12, 2022-01, 2022-10 and 2022-12 are documented; derived net always uses gross inputs. Source sums do not certify hourly completeness.';
  const STARTS = { 4706: '2020-11', 4708: '2020-11', 4718: '2020-12', 4720: '2020-12' };
  const IDS = [4486, 4487, 4488, 4489, 4490, 4491, 4492, 4493, 4494, 4504, 4505, 4506, 4507, 4508, 4509, 4510, 4511, 4512, 4706, 4708, 4718, 4720];
  const TOTALS = ['imports_gwh', 'exports_gwh', 'net_exports_gwh'];
  function assert(condition, message) { if (!condition) throw new Error(`Stromhandel: ${message}`); }
  function fields(value, keys) {
    assert(value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).sort().join() === [...keys].sort().join(), 'Unbekannte oder fehlende Felder');
  }
  function monthIndex(month) {
    assert(typeof month === 'string' && /^\d{4}-(0[1-9]|1[0-2])$/.test(month) && month >= '2019-01' && month < '9999-01', 'Ungültiger Monat');
    return Number(month.slice(0, 4)) * 12 + Number(month.slice(5)) - 1;
  }
  function validateSnapshot(snapshot, now = Date.now()) {
    fields(snapshot, ['schema_version', 'kind', 'source', 'region', 'timezone', 'first_month', 'last_month', 'revision_policy', 'rows', 'content_hash']);
    fields(snapshot.source, Object.keys(recent.SOURCE));
    assert(Object.entries(recent.SOURCE).every(([key, value]) => snapshot.source[key] === value), 'Ungültige Quelle');
    assert([1, 2].includes(snapshot.schema_version) && snapshot.kind === 'german-electricity-trade' && snapshot.region === 'DE-LU' && snapshot.timezone === recent.TIMEZONE && snapshot.first_month === '2019-01' && snapshot.revision_policy === POLICY, 'Unbekannte Metadaten');
    assert(typeof snapshot.content_hash === 'string' && /^[a-f0-9]{64}$/.test(snapshot.content_hash), 'Ungültiger content_hash');
    const first = monthIndex(snapshot.first_month), last = monthIndex(snapshot.last_month);
    assert(Number.isFinite(now) && snapshot.last_month < recent.dayKey(now).slice(0, 7), 'Unvollständiger oder zukünftiger Monat');
    assert(Array.isArray(snapshot.rows) && snapshot.rows.length === last - first + 1 && snapshot.rows.length <= 1200, 'Monatsabdeckung stimmt nicht');
    snapshot.rows.forEach((row, index) => {
      fields(row, ['month', ...TOTALS, 'missing_series', 'structural_zero_series']);
      assert(monthIndex(row.month) === first + index, 'Lücke, doppelte oder unsortierte Monate');
      for (const field of ['missing_series', 'structural_zero_series']) {
        assert(Array.isArray(row[field]) && row[field].every((id, i, ids) => Number.isInteger(id) && IDS.includes(id) && (!i || id > ids[i - 1])), 'Ungültige Serienliste');
      }
      assert(snapshot.schema_version === 2 || row.missing_series.every((id) => STARTS[id] === row.month), 'Unbekannte Quellenlücke');
      assert(row.structural_zero_series.every((id) => STARTS[id] && row.month < STARTS[id]), 'Ungültige strukturelle Null');
      if (row.missing_series.length) assert(TOTALS.every((key) => row[key] === null), 'Fehlende Quelle erfordert drei Nullwerte');
      else {
        assert(TOTALS.every((key) => typeof row[key] === 'number' && Number.isFinite(row[key])), 'Ungültige Handelssumme');
        const start = `${row.month}-01`;
        const end = new Date(Date.UTC(Number(row.month.slice(0, 4)), Number(row.month.slice(5)), 1)).toISOString().slice(0, 10);
        // Sum actual Berlin days, including the March/October clock change.
        let hours = 0;
        for (let time = history.date(start); time < history.date(end); time += 86400000) hours += history.hours(new Date(time).toISOString().slice(0, 10));
        assert(row.imports_gwh >= 0 && row.exports_gwh >= 0 && row.imports_gwh <= 200 * hours && row.exports_gwh <= 200 * hours, 'Unplausible Handelsmenge');
        assert(Math.abs(row.net_exports_gwh - (row.exports_gwh - row.imports_gwh)) <= 2e-8, 'Nettoidentität stimmt nicht');
      }
    });
    return snapshot;
  }
  function parseSnapshot(bytes, now) {
    const { parsed, semantic } = history.parseCanonical(bytes, 250000, ['content_hash']);
    return { snapshot: validateSnapshot(parsed, now), semantic };
  }
  return { POLICY, TOTALS, validateSnapshot, parseSnapshot };
});
