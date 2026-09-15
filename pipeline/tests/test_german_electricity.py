"""Offline fixtures; these synthetic observations are never published to frontend/."""

import copy
from datetime import date, timedelta
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from src.data_pipelines.dashboards.german_electricity import pipeline as ge


AS_OF = date(2026, 9, 10)
NOW_MS = ge.utc_ms("2026-09-10T08:00:00Z")


def fixture(as_of=AS_OF):
    start = ge.midnight_ms(as_of - timedelta(days=ge.FETCH_DAYS))
    end = ge.midnight_ms(as_of)
    return {
        column: {timestamp: (-12.34 if column == "price" else 1234.5)
                 for timestamp in range(start, end, ge.HOUR_MS)}
        for column in ge.SERIES
    }


def snapshot():
    start, end = ge.select_window(fixture(), AS_OF)
    rows = [[timestamp, *([1.2345] * 12), -12.34]
            for timestamp in range(start, end, ge.HOUR_MS)]
    return ge.make_snapshot(rows, start, end, "2026-09-10T06:00:00Z", now_ms=NOW_MS)


class CoverageTests(unittest.TestCase):
    def test_latest_common_completed_day_and_lag(self):
        values = fixture()
        values["solar"][ge.midnight_ms(date(2026, 9, 9)) + 19 * ge.HOUR_MS] = None
        start, end = ge.select_window(values, AS_OF)
        self.assertEqual(ge.iso_utc(start), "2026-08-09T22:00:00Z")
        self.assertEqual(ge.iso_utc(end), "2026-09-08T22:00:00Z")
        for lag in range(1, 5):
            values["load"][ge.midnight_ms(AS_OF - timedelta(days=lag))] = None
            if lag == 3:
                self.assertEqual(ge.select_window(values, AS_OF)[1], ge.midnight_ms(date(2026, 9, 7)))
        with self.assertRaisesRegex(ge.ValidationError, "four-day"):
            ge.select_window(values, AS_OF)

    def test_missing_internal_hour_fails_instead_of_shifting_window(self):
        for missing in (None, "absent"):
            with self.subTest(missing=missing):
                values = fixture()
                timestamp = ge.midnight_ms(date(2026, 9, 4))
                if missing is None:
                    values["gas"][timestamp] = None
                else:
                    del values["gas"][timestamp]
                with self.assertRaisesRegex(ge.ValidationError, "gas: missing"):
                    ge.select_window(values, AS_OF)

    def test_dst_calendar_windows(self):
        for as_of, hours in ((date(2026, 3, 30), 719), (date(2026, 10, 26), 721)):
            with self.subTest(as_of=as_of):
                start, end = ge.select_window(fixture(as_of), as_of)
                self.assertEqual((end - start) // ge.HOUR_MS, hours)
        self.assertEqual((ge.midnight_ms(date(2026, 3, 30)) - ge.midnight_ms(date(2026, 3, 29))) // ge.HOUR_MS, 23)
        self.assertEqual((ge.midnight_ms(date(2026, 10, 26)) - ge.midnight_ms(date(2026, 10, 25))) // ge.HOUR_MS, 25)

    def test_post_2023_only_and_series_allowlist(self):
        with self.assertRaises(ge.ValidationError):
            ge.select_window(fixture(date(2024, 1, 20)), date(2024, 1, 20))
        values = fixture()
        del values["biomass"]
        with self.assertRaises(ge.ValidationError):
            ge.select_window(values, AS_OF)
        self.assertNotIn(1224, ge.SERIES.values())

    def test_strict_numbers_and_negative_prices(self):
        for invalid in (True, False, "1", None, float("nan"), float("inf"), -1, 200001):
            with self.subTest(invalid=invalid), self.assertRaises(ge.ValidationError):
                ge.number(invalid, "gas", power_scale=1000)
        for value in (0, 1, 1.5, 200000):
            ge.number(value, "gas", power_scale=1000)
        ge.number(-123.45, "price")


class SourceTests(unittest.TestCase):
    def test_bad_or_incomplete_index_fails(self):
        for payload in ({}, {"timestamps": []}, {"timestamps": [True]}, {"timestamps": [1, 1]}, {"timestamps": [1]}):
            client = unittest.mock.Mock()
            client.get.return_value = payload
            with self.subTest(payload=payload), self.assertRaises(ge.ValidationError):
                ge.fetch_observations(client, AS_OF)

    def test_duplicate_malformed_and_wrong_granularity(self):
        week = ge.midnight_ms(date(2026, 9, 7))
        end = ge.midnight_ms(date(2026, 9, 14))
        for points in ([], [[week, 1], [week, 1]], [[week + 900000, 1]],
                       [[end, 1]], [[week, "1"]], [[week, True]], [[week]], [[week, float("nan")]]):
            with self.subTest(points=points), self.assertRaises(ge.ValidationError):
                ge.parse_week({"series": points}, week, week, end, "gas", {})
        values = {}
        ge.parse_week({"series": [[week, None], [week + ge.HOUR_MS, 0]]}, week, week, end, "gas", values)
        self.assertIsNone(values[week])
        self.assertEqual(values[week + ge.HOUR_MS], 0)

    def test_request_allowlist_bounds_and_dst_week_indices(self):
        for as_of in (AS_OF, date(2026, 3, 30), date(2026, 10, 26)):
            start = as_of - timedelta(days=35)
            weeks = ge.required_weeks(start, as_of)
            calls = []

            class Client:
                def get(self, url):
                    calls.append(url)
                    if url.endswith("index_hour.json"):
                        return {"timestamps": weeks}
                    week = int(url.rsplit("_", 1)[1].split(".")[0])
                    day = ge.datetime.fromtimestamp(week / 1000, ge.BERLIN).date()
                    end = ge.midnight_ms(day + timedelta(days=7))
                    return {"series": [[timestamp, 1000] for timestamp in range(week, end, ge.HOUR_MS)]}

            result = ge.fetch_observations(Client(), as_of)
            self.assertEqual(sum(url.endswith("index_hour.json") for url in calls), 13)
            self.assertEqual(len(calls), 13 * (1 + len(weeks)))
            self.assertLessEqual(len(calls), 91)
            self.assertEqual(min(result["gas"]), ge.midnight_ms(start))
            self.assertEqual(max(result["gas"]), ge.midnight_ms(as_of) - ge.HOUR_MS)
            self.assertTrue(all("/DE/" in url and "/1224/" not in url for url in calls))

    @patch.object(ge.time, "sleep")
    def test_http_retry_timeout_json_and_byte_limits(self, sleep):
        for error in (URLError("timeout"), TimeoutError(), HTTPError("https://example.test", 503, "unavailable", {}, None)):
            with self.subTest(error=error), patch.object(ge, "urlopen", side_effect=error) as request:
                client = ge.SmardClient()
                with self.assertRaises(ge.ValidationError):
                    client.get("https://example.test")
                self.assertEqual(request.call_count, 3)
        with patch.object(ge, "urlopen", side_effect=HTTPError("https://example.test", 404, "missing", {}, None)) as request:
            with self.assertRaises(ge.ValidationError):
                ge.SmardClient().get("https://example.test")
            self.assertEqual(request.call_count, 1)
        for raw in (b"not JSON", b"x" * 256001):
            with patch.object(ge, "urlopen", return_value=io.BytesIO(raw)):
                with self.assertRaises(ge.ValidationError):
                    ge.SmardClient().get("https://example.test")
        with patch.object(ge, "urlopen", return_value=io.BytesIO(b"{}")):
            client = ge.SmardClient()
            client.MAX_TOTAL_BYTES = 1
            with self.assertRaises(ge.ValidationError):
                client.get("https://example.test")


class PublicationTests(unittest.TestCase):
    def setUp(self):
        clock = patch.object(ge.time, "time", return_value=NOW_MS / 1000)
        clock.start()
        self.addCleanup(clock.stop)

    def test_creation_time_inclusive_bounds(self):
        valid = snapshot()
        for created_at in (valid["data_through"], ge.iso_utc(NOW_MS)):
            with self.subTest(created_at=created_at):
                valid["snapshot_created_at"] = created_at
                ge.validate_snapshot(valid, now_ms=NOW_MS)

    def test_before_coverage_and_future_creation_times_fail_every_publication_path(self):
        valid = snapshot()
        for created_at in (ge.iso_utc(ge.utc_ms(valid["data_through"]) - 1000), ge.iso_utc(NOW_MS + 1000)):
            with self.subTest(created_at=created_at), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "snapshot.json"
                invalid = copy.deepcopy(valid)
                invalid["snapshot_created_at"] = created_at
                # The timestamp is excluded from the hash, so this remains equal.
                self.assertEqual(invalid["content_hash"], valid["content_hash"])
                with self.assertRaisesRegex(ge.ValidationError, "between data_through and now"):
                    ge.make_snapshot(valid["rows"], ge.utc_ms(valid["window_start"]),
                                     ge.utc_ms(valid["window_end"]), created_at, now_ms=NOW_MS)
                with self.assertRaises(ge.ValidationError):
                    ge.publish_snapshot(invalid, output, now_ms=NOW_MS)
                self.assertFalse(output.exists())
                ge.publish_snapshot(valid, output, now_ms=NOW_MS)
                original = output.read_bytes()
                with self.assertRaises(ge.ValidationError):
                    ge.publish_snapshot(invalid, output, now_ms=NOW_MS)
                self.assertEqual(output.read_bytes(), original)
                # An otherwise identical existing snapshot must fail before no-change.
                corrupt = ge.canonical_bytes(invalid) + b"\n"
                output.write_bytes(corrupt)
                with self.assertRaises(ge.ValidationError):
                    ge.publish_snapshot(valid, output, now_ms=NOW_MS)
                self.assertEqual(output.read_bytes(), corrupt)
                # Recheck timestamps in the on-disk candidate before replacement.
                output.write_bytes(original)
                revised = copy.deepcopy(valid)
                revised["rows"][0][1] += 1
                revised = ge.make_snapshot(revised["rows"], ge.utc_ms(valid["window_start"]),
                                           ge.utc_ms(valid["window_end"]), now_ms=NOW_MS)
                real_read = Path.read_bytes

                def corrupt_candidate(path):
                    return corrupt if path != output else real_read(path)

                with patch.object(Path, "read_bytes", corrupt_candidate):
                    with self.assertRaisesRegex(ge.ValidationError, "between data_through and now"):
                        ge.publish_snapshot(revised, output, now_ms=NOW_MS)
                self.assertEqual(output.read_bytes(), original)
                self.assertEqual(list(Path(temporary).iterdir()), [output])

    def test_determinism_timestamp_preservation_and_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "snapshot.json"
            first = snapshot()
            ge.publish_snapshot(first, output)
            original = output.read_bytes()
            mtime = output.stat().st_mtime_ns
            later = copy.deepcopy(first)
            later["snapshot_created_at"] = "2026-09-10T07:00:00Z"
            self.assertEqual(ge.publish_snapshot(later, output)[0], False)
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(output.stat().st_mtime_ns, mtime)
            revised_rows = copy.deepcopy(first["rows"])
            revised_rows[0][1] += 0.001
            revised = ge.make_snapshot(revised_rows, ge.utc_ms(first["window_start"]), ge.utc_ms(first["window_end"]))
            self.assertNotEqual(first["content_hash"], revised["content_hash"])
            self.assertTrue(ge.publish_snapshot(revised, output)[0])

    def test_invalid_new_or_corrupt_existing_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "snapshot.json"
            valid = snapshot()
            ge.publish_snapshot(valid, output)
            original = output.read_bytes()
            for mutate in (lambda s: s["rows"].pop(),
                           lambda s: s["rows"].__setitem__(1, s["rows"][0]),
                           lambda s: s["rows"][0].__setitem__(1, None),
                           lambda s: s.__setitem__("content_hash", "bad"),
                           lambda s: s.__setitem__("data_through", "2026-09-01T00:00:00Z")):
                invalid = copy.deepcopy(valid)
                mutate(invalid)
                with self.assertRaises(ge.ValidationError):
                    ge.publish_snapshot(invalid, output)
                self.assertEqual(output.read_bytes(), original)
            for corrupt in (b"broken", b"{}", original.replace(b'"biomass"', b'"badmass"')):
                output.write_bytes(corrupt)
                with self.assertRaises(ge.ValidationError):
                    ge.publish_snapshot(valid, output)
                self.assertEqual(output.read_bytes(), corrupt)

    def test_atomic_replace_failure_preserves_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "snapshot.json"
            first = snapshot()
            ge.publish_snapshot(first, output)
            original = output.read_bytes()
            first["rows"][0][1] += 1
            revised = ge.make_snapshot(first["rows"], ge.utc_ms(first["window_start"]), ge.utc_ms(first["window_end"]))
            with patch.object(ge.os, "replace", side_effect=OSError("fixture disk failure")):
                with self.assertRaises(OSError):
                    ge.publish_snapshot(revised, output)
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(list(Path(temporary).iterdir()), [output])

    def test_pipeline_fetch_validation_and_dbt_failures_preserve_destination(self):
        from src.data_pipelines.dashboards.german_electricity import partial
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "snapshot.json"
            ge.publish_snapshot(snapshot(), output)
            original = output.read_bytes()
            with patch.object(partial, "acquire", side_effect=ge.ValidationError("fixture shared failure")):
                with self.assertRaises(ge.ValidationError):
                    ge.run(AS_OF, output)
            values = fixture()
            start, end = ge.select_window(values, AS_OF)
            successes = {key: (points, start, end) for key, points in values.items()}
            with patch.object(partial, "acquire", return_value=(successes, {})), patch.object(
                ge, "build_curated", side_effect=subprocess.CalledProcessError(1, "dbt")
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    ge.run(AS_OF, output)
            self.assertEqual(output.read_bytes(), original)


class DbtIntegrationTests(unittest.TestCase):
    def test_real_dbt_dst_conversion_and_determinism(self):
        previous = None
        for as_of in (date(2026, 3, 30), date(2026, 3, 30), date(2026, 10, 26)):
            values = fixture(as_of)
            # Insertion order is not part of the data contract.
            values = {column: dict(reversed(list(points.items()))) for column, points in values.items()}
            start, end = ge.select_window(values, as_of)
            with tempfile.TemporaryDirectory() as temporary:
                rows = ge.build_curated(values, start, end, Path(temporary))
            result = ge.make_snapshot(rows, start, end, "2026-10-26T06:00:00Z",
                                      now_ms=ge.utc_ms("2026-10-26T08:00:00Z"))
            self.assertAlmostEqual(rows[0][1], 1.2345)
            self.assertEqual(rows[0][-1], -12.34)
            encoded = ge.canonical_bytes(result)
            if as_of == date(2026, 3, 30):
                if previous is not None:
                    self.assertEqual(previous, encoded)
                previous = encoded
            self.assertEqual(len(rows), 719 if as_of.month == 3 else 721)

    def test_dbt_rejects_duplicate_before_pivot(self):
        import duckdb

        values = fixture()
        start, end = ge.select_window(values, AS_OF)
        original_run = subprocess.run

        def inject_duplicate(command, **kwargs):
            with duckdb.connect(kwargs["env"]["GERMAN_ELECTRICITY_DB"]) as connection:
                connection.execute("insert into staging.smard_hourly select * from staging.smard_hourly where timestamp_ms = ? and series = 'gas'", [start])
            return original_run(command, **kwargs)

        with tempfile.TemporaryDirectory() as temporary, patch.object(ge.subprocess, "run", side_effect=inject_duplicate):
            with self.assertRaises(subprocess.CalledProcessError):
                ge.build_curated(values, start, end, Path(temporary))


if __name__ == "__main__":
    unittest.main()
