"""Offline failure/null fixtures including the September 13 price outage scenario."""
import copy
from datetime import date, timedelta
from http.client import IncompleteRead, BadStatusLine
import io
from pathlib import Path
import tempfile
import threading
import subprocess
import unittest
from unittest.mock import Mock, patch

from src.data_pipelines.dashboards.german_electricity import pipeline as p, partial, history as h, trade as t, refresh
from test_german_electricity import fixture
from test_german_electricity_history import source as daily_source, snapshot as recent_fixture, partition as daily_partition
from test_german_electricity_trade import source as trade_source, snapshot as trade_fixture

AS_OF = date(2026, 9, 15)


def curated(values, start, end, directory):
    return [[stamp, *[None if values[key][stamp] is None else values[key][stamp] / (1 if key == 'price' else 1000)
                       for key in p.SERIES]] for stamp in range(start, end, p.HOUR_MS)]


def successes(as_of=AS_OF):
    return {key: (values, *partial.source_window(values, as_of, key)) for key, values in fixture(as_of).items()}


class RecentPartialTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock(requests=0, bytes_downloaded=0)

    def test_null_price_day_does_not_freeze_energy_and_zero_is_distinct(self):
        values = successes()
        day = p.midnight_ms(date(2026, 9, 13))
        for stamp in range(day, day + 24 * p.HOUR_MS, p.HOUR_MS):
            values['price'][0][stamp] = None
        values['solar'][0][day] = 0
        with tempfile.TemporaryDirectory() as tmp, patch.object(partial, 'acquire', return_value=(values, {})), patch.object(p, 'build_curated', side_effect=curated):
            path = Path(tmp) / 'recent.json'
            p.run(AS_OF, path, self.client)
            snap = h.strict_json(path.read_bytes())
            self.assertEqual(snap['schema_version'], 2)
            self.assertEqual(snap['window_end'], p.iso_utc(p.midnight_ms(AS_OF)))
            self.assertEqual(snap['components']['price']['known_hours'], 696)
            self.assertEqual(snap['components']['solar']['status'], 'complete')
            row = next(row for row in snap['rows'] if row[0] == day)
            self.assertIsNone(row[-1])
            self.assertEqual(row[5], 0)
            raw, mtime = path.read_bytes(), path.stat().st_mtime_ns
            p.run(AS_OF, path, self.client)
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (raw, mtime))

    def test_retention_alignment_all_unavailable_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, 'build_curated', side_effect=curated):
            path = Path(tmp) / 'recent.json'
            with patch.object(partial, 'acquire', return_value=(successes(AS_OF - timedelta(days=1)), {})):
                p.run(AS_OF - timedelta(days=1), path, self.client)
            previous = h.strict_json(path.read_bytes())
            values = successes()
            del values['gas']
            del values['load']
            with patch.object(partial, 'acquire', return_value=(values, {'gas': 'HTTP 503', 'load': 'empty response'})):
                p.run(AS_OF, path, self.client)
            snap = h.strict_json(path.read_bytes())
            self.assertEqual(snap['components']['gas']['status'], 'stale')
            self.assertEqual(snap['components']['gas']['last_successful_window_end'], previous['window_end'])
            self.assertEqual(snap['rows'][0][9], previous['rows'][24][9])
            self.assertTrue(all(row[9] is None and row[12] is None for row in snap['rows'][-24:]))
            with patch.object(partial, 'acquire', return_value=({}, dict.fromkeys(p.SERIES, 'HTTP error'))):
                p.run(AS_OF, path, self.client)
                first = path.read_bytes()
                p.run(AS_OF, path, self.client)
                self.assertEqual(path.read_bytes(), first)
            with patch.object(partial, 'acquire', return_value=(successes(), {})):
                p.run(AS_OF, path, self.client)
            self.assertTrue(all(meta['status'] == 'complete' for meta in h.strict_json(path.read_bytes())['components'].values()))

    def test_empty_malformed_duplicate_failure_isolated_max_three_requests(self):
        active, maximum = 0, 0
        lock = threading.Lock()
        def get(url):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                series = int(url.split('/')[5])
                if series == p.SERIES['gas']:
                    raise p.ValidationError('HTTP 503')
                if url.endswith('index_hour.json'):
                    return {'timestamps': p.required_weeks(AS_OF - timedelta(days=35), AS_OF)}
                week = int(url.rsplit('_', 1)[1].split('.')[0])
                if series == p.SERIES['load']:
                    return {'series': []}
                if series == p.SERIES['solar']:
                    return {'series': [[week, 1], [week, 1]]}
                end = p.midnight_ms(p.datetime.fromtimestamp(week / 1000, p.BERLIN).date() + timedelta(days=7))
                return {'series': [[stamp, None if series == 4169 else 1000] for stamp in range(week, end, p.HOUR_MS)]}
            finally:
                with lock:
                    active -= 1
        self.client.get.side_effect = get
        ok, failures = partial.acquire(self.client, AS_OF)
        self.assertEqual(set(failures), {'gas', 'load', 'solar'})
        self.assertIn('price', ok)
        self.assertLessEqual(maximum, 3)
        self.assertLessEqual(self.client.get.call_count, 91)
        aligned, components, start, end = partial.align(ok, None)
        snap = p.make_snapshot(curated(aligned, start, end, None), start, end, components=components)
        self.assertEqual(snap['components']['price']['known_hours'], 0)
        self.assertIsNone(snap['components']['price']['source_observed_through'])
        self.assertEqual(snap['components']['gas']['status'], 'unavailable')

    def test_omitted_internal_timestamp_is_failure_not_null(self):
        values = fixture(AS_OF)['price']
        del values[p.midnight_ms(date(2026, 9, 13))]
        with self.assertRaisesRegex(p.ValidationError, 'omitted internal'):
            partial.source_window(values, AS_OF, 'price')
        with self.assertRaises(p.ValidationError):
            partial.align({}, None)

    def test_transport_protocol_and_huge_integer_failures_remain_per_series(self):
        class BrokenBody(io.BytesIO):
            def read1(self, size):
                raise IncompleteRead(b'{"series":', 100)

        calls = []
        weeks = p.required_weeks(AS_OF - timedelta(days=35), AS_OF)
        def response(request, **kwargs):
            url = request.full_url
            calls.append(url)
            series = int(url.split('/')[5])
            if series == p.SERIES['gas']:
                return BrokenBody()
            if series == p.SERIES['load']:
                raise BadStatusLine('invalid status line')
            if url.endswith('index_hour.json'):
                payload = {'timestamps': weeks}
            else:
                week = int(url.rsplit('_', 1)[1].split('.')[0])
                end = p.midnight_ms(p.datetime.fromtimestamp(week / 1000, p.BERLIN).date() + timedelta(days=7))
                # JSON accepts this integer; float conversion would raise OverflowError.
                value = 10 ** 400 if series == p.SERIES['solar'] else 1000
                payload = {'series': [[stamp, value] for stamp in range(week, end, p.HOUR_MS)]}
            return io.BytesIO(p.canonical_bytes(payload))

        with patch.object(p, 'urlopen', side_effect=response), patch.object(p.time, 'sleep'):
            ok, failures = partial.acquire(p.SmardClient(), AS_OF)
        self.assertEqual(set(failures), {'gas', 'load', 'solar'})
        self.assertEqual(len(ok), 10)
        self.assertEqual(sum('/4071/' in url for url in calls), 3)
        self.assertEqual(sum('/410/' in url for url in calls), 3)
        self.assertIn('sanity bounds', failures['solar'])
        for huge in (10 ** 400, -(10 ** 400)):
            with self.subTest(huge_sign=huge > 0), self.assertRaises(p.ValidationError):
                p.number(huge, 'price')
            with self.subTest(trade_sign=huge > 0), self.assertRaises(p.ValidationError):
                t.source_number(huge, 4486, date(2026, 8, 1))
        # Normalization belongs to network/numeric boundaries, not an indiscriminate
        # catch in acquire that could conceal programmer or filesystem failures.
        for error in (RuntimeError('shared bug'), OSError('storage failure')):
            with patch.object(partial, 'fetch_series', side_effect=error):
                with self.assertRaises(type(error)):
                    partial.acquire(self.client, AS_OF)

    def test_observation_cutoff_exactness_and_rolled_out_retention(self):
        values = successes()
        _, _, end = values['gas']
        values['gas'][0][end - p.HOUR_MS] = None
        aligned, components, start, end = partial.align(values, None)
        snap = p.make_snapshot(curated(aligned, start, end, None), start, end, components=components)
        for claimed in (end, end - 2 * p.HOUR_MS, start, None):
            invalid = copy.deepcopy(snap)
            invalid['components']['gas']['source_observed_through'] = p.iso_utc(claimed) if claimed else None
            with self.subTest(claimed=claimed), self.assertRaisesRegex(p.ValidationError, 'last numeric interval'):
                partial.validate_components(invalid)
        empty = copy.deepcopy(snap)
        for row in empty['rows']:
            row[9] = None
        meta = empty['components']['gas']
        meta.update(status='stale', known_hours=0)
        for boundary in (start, start - p.HOUR_MS, None):
            meta['source_observed_through'] = p.iso_utc(boundary) if boundary else None
            partial.validate_components(empty)
        meta['source_observed_through'] = p.iso_utc(start + p.HOUR_MS)
        with self.assertRaisesRegex(p.ValidationError, 'absent displayed value'):
            partial.validate_components(empty)

        # Exercise actual alignment when the whole last-good component ages out.
        old_values = successes(AS_OF - timedelta(days=31))
        old_aligned, old_meta, old_start, old_end = partial.align(old_values, None)
        previous = p.make_snapshot(curated(old_aligned, old_start, old_end, None), old_start, old_end, components=old_meta)
        del values['gas']
        aligned, components, start, end = partial.align(values, previous)
        result = p.make_snapshot(curated(aligned, start, end, None), start, end, components=components)
        self.assertEqual(result['components']['gas']['known_hours'], 0)
        self.assertEqual(result['components']['gas']['source_observed_through'], previous['window_end'])

    def test_nullable_dbt_dst_and_schema_integrity(self):
        for day, count in ((date(2026, 3, 30), 719), (date(2026, 10, 26), 721)):
            values = successes(day)
            for points, _, _ in values.values():
                for stamp in points:
                    points[stamp] = None
            aligned, components, start, end = partial.align(values, None)
            with tempfile.TemporaryDirectory() as tmp:
                rows = p.build_curated(aligned, start, end, Path(tmp))
            snap = p.make_snapshot(rows, start, end, now_ms=end, components=components)
            self.assertEqual(len(rows), count)
            self.assertTrue(all(v is None for row in rows for v in row[1:]))
            broken = copy.deepcopy(snap)
            broken['components']['gas']['known_hours'] = 1
            with self.assertRaises(p.ValidationError):
                p.validate_snapshot(broken, now_ms=end)


class BundleTests(unittest.TestCase):
    def setUp(self):
        quiet = patch('sys.stdout', new_callable=io.StringIO)
        quiet.start()
        self.addCleanup(quiet.stop)

    def setup_bundle(self, root):
        recent, directory, trade = root / 'recent.json', root / 'history', root / 'trade.json'
        directory.mkdir()
        p.publish_snapshot(recent_fixture(date(2026, 9, 12)), recent)
        partitions = {2026: daily_partition(2026, date(2026, 9, 12))}
        h.publish_history(directory, h.make_manifest(partitions, AS_OF), partitions, None)
        trade.write_bytes(p.canonical_bytes(trade_fixture()))
        return recent, directory, trade

    def test_history_trade_failure_retained_healthy_recent_published_and_repeat_stable(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, 'build_curated', side_effect=curated), patch.object(p, 'urlopen', side_effect=AssertionError('No live calls')):
            paths = self.setup_bundle(Path(tmp))
            old_history = (paths[1] / 'manifest.json').read_bytes()
            old_trade = paths[2].read_bytes()
            values = successes()
            for key, (points, _, _) in values.items():
                for stamp in points:
                    points[stamp] = -10 if key == 'price' else 1000
            with patch.object(partial, 'acquire', return_value=(values, {})), patch.object(h, 'fetch_daily', side_effect=p.ValidationError('daily HTTP 503')), patch.object(t, 'fetch_monthly', side_effect=p.ValidationError('trade HTTP 503')) as trade_fetch:
                refresh.run(AS_OF, *paths)
                self.assertTrue(trade_fetch.called)
                raw = paths[0].read_bytes()
                snap = h.strict_json(raw)
                self.assertEqual(snap['refresh_status']['history']['status'], 'stale')
                self.assertEqual(snap['refresh_status']['trade']['status'], 'stale')
                self.assertEqual(snap['window_end'], p.iso_utc(p.midnight_ms(AS_OF)))
                self.assertEqual((paths[1] / 'manifest.json').read_bytes(), old_history)
                self.assertEqual(paths[2].read_bytes(), old_trade)
                refresh.run(AS_OF, *paths)
                self.assertEqual(paths[0].read_bytes(), raw)

    def test_contradiction_blocks_bundle_but_independent_trade_attempted(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, 'build_curated', side_effect=curated):
            paths = self.setup_bundle(Path(tmp))
            old = paths[0].read_bytes()
            with patch.object(partial, 'acquire', return_value=(successes(), {})), patch.object(h, 'fetch_daily', return_value=daily_source([2026])), patch.object(t, 'fetch_monthly', side_effect=p.ValidationError('trade down')) as trade_fetch:
                with self.assertRaises(p.ValidationError):
                    refresh.run(AS_OF, *paths)
                self.assertTrue(trade_fetch.called)
            self.assertEqual(paths[0].read_bytes(), old)

    def test_daily_null_and_trade_null_acceptance_but_absent_slots_fail(self):
        values = daily_source([2026])
        day = date(2026, 9, 13)
        values[2026]['price'][day] = None
        values[2026]['load'][day] = None
        rows = h.make_rows(values, date(2026, 1, 1), day, nullable=True)
        h.partition(2026, rows, nullable=True)
        self.assertIsNone(rows[-1]['energy_gwh']['load'])
        del values[2026]['load'][day]
        with self.assertRaises(p.ValidationError):
            h.make_rows(values, day, day, nullable=True)
        obs = trade_source(range(2019, 2027))
        month = date(2026, 8, 1)
        obs[2026][4486][month] = None
        rows = t.make_rows(obs, t.FIRST, month, {}, nullable=True)
        t.snapshot(rows, nullable=True)
        self.assertEqual(rows[-1]['missing_series'], [4486])
        self.assertIsNone(rows[-1]['net_exports_gwh'])
        del obs[2026][4486][month]
        with self.assertRaises(p.ValidationError):
            t.make_rows(obs, month, month, {}, nullable=True)

    def test_all_recent_sources_fail_daily_and_trade_still_refresh_then_cross_language_validate(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, 'urlopen', side_effect=AssertionError('No live calls')):
            paths = self.setup_bundle(Path(tmp))
            previous = h.strict_json(paths[0].read_bytes())
            daily = daily_source([2026])
            daily[2026]['price'][date(2026, 9, 13)] = None
            daily[2026]['gas'][date(2026, 9, 14)] = None
            monthly = trade_source([2026])
            monthly[2026][4486][date(2026, 8, 1)] = None
            with patch.object(partial, 'acquire', return_value=({}, dict.fromkeys(p.SERIES, 'HTTP 503'))), patch.object(h, 'fetch_daily', return_value=daily), patch.object(t, 'fetch_monthly', return_value=monthly):
                refresh.run(AS_OF, *paths)
            snapshot, manifest, partitions, trade_snapshot = refresh.bundle(*paths)
            self.assertEqual(snapshot['rows'], previous['rows'])
            self.assertEqual(snapshot['window_end'], previous['window_end'])
            self.assertEqual(manifest['last_date'], '2026-09-14')
            self.assertEqual(snapshot['refresh_status']['history']['status'], 'partial')
            self.assertEqual(snapshot['refresh_status']['trade']['status'], 'partial')
            self.assertEqual(trade_snapshot['schema_version'], 2)
            root = p.DEFAULT_OUTPUT.parents[3]
            code = """
const fs = require('fs');
const [root, recentPath, directory, tradePath] = process.argv.slice(1);
const data = require(root + '/frontend/src/js/dashboards/electricity-data');
const history = require(root + '/frontend/src/data_ingestion/builders/electricityHistory');
const trends = require(root + '/frontend/src/data_ingestion/builders/electricityTrends');
const snapshot = require(root + '/frontend/src/data_ingestion/builders/electricitySnapshot').verifySnapshot(fs.readFileSync(recentPath, 'utf8'));
const daily = history.readHistory(directory, fs.readFileSync(recentPath, 'utf8'));
const monthly = trends.verifyTrade(fs.readFileSync(tradePath));
const result = trends.aggregate(daily, monthly);
if (snapshot.components.gas.status !== 'stale' || daily.manifest.last_date !== '2026-09-14' || result.trade.years.at(-1).net_exports_twh !== null) throw Error('integration mismatch');
if (/NaN|Infinity/.test(JSON.stringify(data.presentation(data.summarize(snapshot))))) throw Error('nonfinite summary');
"""
            subprocess.run(['node', '-e', code, str(root), *(str(path) for path in paths)], check=True, timeout=30)

    def test_handled_final_write_failure_restores_entire_bundle(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, 'build_curated', side_effect=curated):
            paths = self.setup_bundle(Path(tmp))
            before = {path: path.read_bytes() for path in Path(tmp).rglob('*.json')}
            values = successes()
            for key, (points, _, _) in values.items():
                for stamp in points:
                    points[stamp] = -10 if key == 'price' else 1000
            original = p.publish_snapshot
            def fail_final(snapshot, output, **kwargs):
                if output == paths[0]:
                    raise OSError('fixture final write failed')
                return original(snapshot, output, **kwargs)
            with patch.object(partial, 'acquire', return_value=(values, {})), patch.object(h, 'fetch_daily', return_value=daily_source([2026])), patch.object(t, 'fetch_monthly', return_value=trade_source([2026])), patch.object(p, 'publish_snapshot', side_effect=fail_final):
                with self.assertRaisesRegex(OSError, 'final write'):
                    refresh.run(AS_OF, *paths)
            self.assertEqual({path: path.read_bytes() for path in Path(tmp).rglob('*.json')}, before)

    def test_shared_validation_failure_does_not_degrade_to_stale(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, 'build_curated', side_effect=curated):
            paths = self.setup_bundle(Path(tmp))
            old = paths[0].read_bytes()
            with patch.object(partial, 'acquire', return_value=({}, dict.fromkeys(p.SERIES, 'HTTP 503'))), patch.object(h, 'run', side_effect=p.ValidationError('frozen year guard')), patch.object(t, 'run') as trade_run:
                with self.assertRaisesRegex(p.ValidationError, 'Shared refresh failure'):
                    refresh.run(AS_OF, *paths)
                self.assertTrue(trade_run.called)
            self.assertEqual(paths[0].read_bytes(), old)

    def test_standalone_history_trade_repair_then_coordinated_refresh(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, 'build_curated', side_effect=curated), patch.object(p, 'urlopen', side_effect=AssertionError('No live calls')):
            paths = self.setup_bundle(Path(tmp))
            paths[2].write_bytes(p.canonical_bytes(trade_fixture(date(2026, 7, 1))))
            outages = ({}, dict.fromkeys(p.SERIES, 'HTTP 503'))
            with patch.object(partial, 'acquire', return_value=outages), patch.object(h, 'fetch_daily', side_effect=p.ValidationError('daily unavailable')), patch.object(t, 'fetch_monthly', side_effect=p.ValidationError('trade unavailable')):
                refresh.run(AS_OF, *paths)
            prior = paths[0].read_bytes()
            with patch.object(h, 'fetch_daily', return_value=daily_source([2026])):
                h.run('refresh', AS_OF, paths[1], snapshot_path=paths[0])
            with patch.object(t, 'fetch_monthly', return_value=trade_source([2026])):
                t.run('refresh', AS_OF, paths[2], history_manifest=paths[1] / 'manifest.json')
            self.assertEqual(paths[0].read_bytes(), prior)
            with self.assertRaisesRegex(p.ValidationError, 'status cutoff mismatch'):
                refresh.bundle(*paths)
            with patch.object(partial, 'acquire', return_value=outages), patch.object(h, 'fetch_daily', return_value=daily_source([2026])), patch.object(t, 'fetch_monthly', return_value=trade_source([2026])):
                refresh.run(AS_OF, *paths)
            snap, manifest, _, monthly = refresh.bundle(*paths)
            self.assertEqual(snap['refresh_status']['history'], {'status': 'ok', 'data_through': '2026-09-14'})
            self.assertEqual(snap['refresh_status']['trade'], {'status': 'ok', 'data_through': '2026-08'})
            self.assertEqual(manifest['last_date'], '2026-09-14')
            self.assertEqual(monthly['last_month'], '2026-08')

            # Forward-only relaxation never authorizes a regressed export or corrupt
            # bytes, and all of this is checked before even attempting source reads.
            paths[2].write_bytes(p.canonical_bytes(trade_fixture(date(2026, 7, 1))))
            with patch.object(partial, 'acquire') as acquire:
                with self.assertRaisesRegex(p.ValidationError, 'trade: component status cutoff mismatch'):
                    refresh.run(AS_OF, *paths)
                acquire.assert_not_called()
            paths[2].write_bytes(b'broken')
            with patch.object(partial, 'acquire') as acquire:
                with self.assertRaises(p.ValidationError):
                    refresh.run(AS_OF, *paths)
                acquire.assert_not_called()

    def test_regressed_daily_feed_retained_while_recent_and_trade_advance(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, 'build_curated', side_effect=curated):
            paths = self.setup_bundle(Path(tmp))
            original = (paths[1] / 'manifest.json').read_bytes()
            paths[2].write_bytes(p.canonical_bytes(trade_fixture(date(2026, 7, 1))))
            daily = daily_source([2026])
            for key in daily[2026]:
                daily[2026][key] = {day: value for day, value in daily[2026][key].items() if day <= date(2026, 9, 11)}
            values = successes()
            for key, (points, _, _) in values.items():
                for stamp in points:
                    points[stamp] = -10 if key == 'price' else 1000
            with patch.object(partial, 'acquire', return_value=(values, {})), patch.object(h, 'fetch_daily', return_value=daily), patch.object(t, 'fetch_monthly', return_value=trade_source([2026])) as monthly_fetch:
                refresh.run(AS_OF, *paths)
                monthly_fetch.assert_called_once()
            snap, _, _, monthly = refresh.bundle(*paths)
            self.assertEqual((paths[1] / 'manifest.json').read_bytes(), original)
            self.assertEqual(snap['refresh_status']['history']['status'], 'stale')
            self.assertEqual(snap['window_end'], p.iso_utc(p.midnight_ms(AS_OF)))
            self.assertEqual(monthly['last_month'], '2026-08')

    def test_closed_v2_reconcile_completes_december_without_recent_and_preserves_v1(self):
        as_of = date(2026, 1, 5)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = daily_source([2025])
            source[2025]['price'][date(2025, 9, 13)] = None
            source[2025]['load'][date(2025, 12, 30)] = None
            partitions = {2024: daily_partition(2024), 2025: h.partition(2025, h.make_rows(source, date(2025, 1, 1), date(2025, 12, 30), nullable=True), nullable=True)}
            manifest = h.make_manifest(partitions, as_of)
            h.publish_history(directory, manifest, partitions, None)
            frozen_path = directory / Path(manifest['years'][0]['url']).name
            frozen = frozen_path.read_bytes()
            with patch.object(h, 'fetch_daily', return_value=source) as fetch:
                h.run('backfill', as_of, directory, start_year=2025, end_year=2025, reconcile=True,
                      snapshot_path=directory / 'does-not-exist.json')
                fetch.assert_called_once()
                self.assertEqual(fetch.call_args.args[1], [2025])
            manifest, parts, _ = h.load_history(directory, required=True)
            self.assertEqual(manifest['last_date'], '2025-12-31')
            self.assertEqual(parts[2025]['schema_version'], 2)
            self.assertIsNone(next(row for row in parts[2025]['rows'] if row['date'] == '2025-09-13')['price_eur_mwh'])
            self.assertIsNone(parts[2025]['rows'][-2]['energy_gwh']['load'])
            self.assertEqual(frozen_path.read_bytes(), frozen)
            self.assertEqual(parts[2024]['schema_version'], 1)
            # Repeating backfill without explicit reconcile does not fetch/rewrite.
            with patch.object(h, 'fetch_daily') as fetch:
                h.run('backfill', as_of, directory, start_year=2024, end_year=2025,
                      snapshot_path=directory / 'does-not-exist.json')
                fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
