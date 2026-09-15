/** @jest-environment node */
const fs = require('fs');
const path = require('path');
const { createHash } = require('crypto');
const { verifySnapshot, validateBuildSnapshot } = require('../src/data_ingestion/builders/electricitySnapshot');

const raw = fs.readFileSync(path.join(__dirname, '../src/_data/germanElectricity.json'), 'utf8');
const snapshot = JSON.parse(raw);
const now = Date.parse(snapshot.snapshot_created_at) + 3600000;

function replaceObservation(input, token) {
  return input.replace(/("rows":\[\[\d+,)[^,]+/, `$1${token}`);
}
// Independent extraction for this fixed schema's two string-valued root fields.
function rehash(input) {
  const semantic = input.trimEnd()
    .replace(/"content_hash":"[a-f0-9]{64}",/, '')
    .replace(/"snapshot_created_at":"[^"]+",/, '');
  const hash = createHash('sha256').update(semantic).digest('hex');
  return input.replace(/"content_hash":"[a-f0-9]{64}"/, `"content_hash":"${hash}"`);
}

afterEach(() => jest.restoreAllMocks());

test('verifies the real producer artifact and its existing Python-generated hash', () => {
  expect(verifySnapshot(raw, now)).toEqual(snapshot);
});

test('modified observation retaining its hash fails verification', () => {
  const changed = replaceObservation(raw, '123.456');
  expect(JSON.parse(changed).rows[0][1]).toBe(123.456);
  expect(() => verifySnapshot(changed, now)).toThrow('content_hash');
});

test.each(['0.0', '-0.0', '1e-07', '1.0000000000000002'])('preserves Python numeric spelling %s in the hash preimage', (token) => {
  const input = rehash(replaceObservation(raw, token));
  expect(Object.is(verifySnapshot(input, now).rows[0][1], Number(token))).toBe(true);
  // Rewriting an equivalent numeric value with JS spelling changes the hash bytes.
  if (JSON.stringify(Number(token)) !== token) {
    expect(() => verifySnapshot(replaceObservation(input, JSON.stringify(Number(token))), now)).toThrow('content_hash');
  }
});

test('an operational creation timestamp change leaves the semantic hash valid', () => {
  const created = new Date(now - 1000).toISOString().replace('.000Z', 'Z');
  const changed = raw.replace(snapshot.snapshot_created_at, created);
  expect(verifySnapshot(changed, now).content_hash).toBe(snapshot.content_hash);
  expect(verifySnapshot(changed, now).snapshot_created_at).toBe(created);
});

test.each([
  ['duplicate root key', (input) => input.replace('{', '{"schema_version":1,')],
  ['duplicate nested key', (input) => input.replace('"source":{', '"source":{"license":"CC BY 4.0",')],
  ['whitespace', (input) => input.replace('{', '{ ')],
  ['missing newline', (input) => input.trimEnd()],
  ['extra newline', (input) => `${input}\n`],
  ['key order', (input) => input.replace(`"schema_version":${snapshot.schema_version},`, '').replace('{', `{"schema_version":${snapshot.schema_version},`)],
  ['trailing garbage', (input) => `${input}oops`],
])('rejects noncanonical or ambiguous artifact: %s', (name, change) => {
  expect(() => verifySnapshot(change(raw), now)).toThrow();
});

test('build validation rereads the raw file and rejects stale Eleventy data', () => {
  jest.spyOn(Date, 'now').mockReturnValue(now);
  const read = jest.spyOn(fs, 'readFileSync').mockReturnValue(raw);
  expect(validateBuildSnapshot(snapshot)).toEqual(snapshot);
  const changed = rehash(replaceObservation(raw, '123.456'));
  read.mockReturnValue(changed);
  expect(() => validateBuildSnapshot(snapshot)).toThrow('Eleventy-Daten');
  expect(validateBuildSnapshot(JSON.parse(changed)).rows[0][1]).toBe(123.456);
});

test('both Eleventy rendering filters fail on changed observations with the old hash', () => {
  jest.spyOn(Date, 'now').mockReturnValue(now);
  jest.resetModules();
  jest.doMock('@11ty/eleventy', () => ({ HtmlBasePlugin: {} }));
  const filters = {};
  const config = { on: jest.fn(), ignores: { add: jest.fn() }, watchIgnores: { add: jest.fn() },
    addFilter: (name, fn) => { filters[name] = fn; }, addPassthroughCopy: jest.fn(),
    addPlugin: jest.fn(), addGlobalData: jest.fn(), addCollection: jest.fn() };
  require('../.eleventy.js')(config);
  const changed = replaceObservation(raw, '123.456');
  const read = jest.spyOn(fs, 'readFileSync').mockReturnValue(changed);
  for (const name of ['electricitySummary', 'electricityJSON']) {
    expect(() => filters[name](JSON.parse(changed))).toThrow('content_hash');
  }
  read.mockReturnValue(raw);
  expect(filters.electricitySummary(snapshot).text.label).toBeTruthy();
  expect(JSON.parse(filters.electricityJSON(snapshot))).toEqual(snapshot);
});
