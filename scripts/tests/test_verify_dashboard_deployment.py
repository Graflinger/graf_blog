"""Offline deployment checks with fake HTTPS responses and a deterministic clock.

Run: python3 -B -m unittest discover -s scripts/tests -p 'test_verify_dashboard_deployment.py' -v
"""

import copy
import hashlib
from html import escape
from http.client import IncompleteRead
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request


SCRIPT = Path(__file__).resolve().parents[1] / "verify_dashboard_deployment.py"
SPEC = importlib.util.spec_from_file_location("verify_dashboard_deployment", SCRIPT)
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class FakeClock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recent = {"content_hash": "a" * 64, "data_through": "2026-09-09T22:00:00Z",
                       "rows": [[1, 0.0]], "source": "Bundesnetzagentur <SMARD>"}
        self.trade = {"content_hash": "b" * 64, "first_month": "2019-01", "last_month": "2026-08"}
        self.progress = {"content_hash": "c" * 64, "capacity": {"data_through": "2025-12-31"}}
        self.annual = {"content_hash": "d" * 64}
        self.partition = b'{"year":2026,"rows":[{"date":"2026-09-09"}]}'
        sha = hashlib.sha256(self.partition).hexdigest()
        self.latest = {"year": 2026, "url": f"/data/history/german-electricity/2026.{sha}.json",
                       "sha256": sha, "frozen": False, "first_date": "2026-01-01",
                       "last_date": "2026-09-09", "days": 252}
        self.closed = {**self.latest, "year": 2025, "sha256": "e" * 64,
                       "url": f"/data/history/german-electricity/2025.{'e' * 64}.json",
                       "frozen": True, "first_date": "2025-01-01", "last_date": "2025-12-31"}
        self.manifest = {"schema_version": 1, "kind": "german-electricity-history",
                         "timezone": "Europe/Berlin", "source": {"name": "Fixture"},
                         "first_date": "2025-01-01", "last_date": "2026-09-09",
                         "revision_policy": "Frozen closed years", "years": [self.closed, self.latest]}
        for name, data in (("germanElectricity", self.recent), ("germanElectricityTrade", self.trade),
                           ("germanElectricityProgress", self.progress), ("germanElectricityAnnual", self.annual)):
            self.write(f"frontend/src/_data/{name}.json", encoded(data))
        self.write(f"{verify.HISTORY_DIRECTORY}/manifest.json", encoded(self.manifest))
        self.write(f"{verify.HISTORY_DIRECTORY}/{self.latest['url'].rsplit('/', 1)[1]}", self.partition)
        self.expected = verify.load_expected(self.root)
        self.trends = {"kind": "german-electricity-trends", "schema_version": 1,
                       "inputs": self.expected["inputs"], "coverage": self.expected["coverage"],
                       "energy": [], "trade": {"years": [], "months": []}}
        self.public = {verify.RECENT: encoded(self.recent), verify.MANIFEST: encoded(self.manifest),
                       self.latest["url"]: self.partition, verify.TRENDS: encoded(self.trends),
                       verify.PROGRESS: encoded(self.progress), verify.PAGE: self.html()}
        self.clock = FakeClock()
        self.requests = []
        self.messages = []

    def write(self, path, raw):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)

    def html(self):
        report = ""
        if self.recent.get("schema_version") == 2:
            status = {key: self.recent[key] for key in ("components", "refresh_status")}
            def fields(values):
                return "".join(f'<span data-component-field="{key}">{escape(str(value))}</span>' for key, value in values.items())
            rows = ""
            for key, meta in status["components"].items():
                cells = {"label": verify.COMPONENT_LABELS[key], "status": verify.COMPONENT_STATUS[meta["status"]],
                         "coverage": f"{meta['known_hours']}/{meta['expected_hours']}",
                         "observed": meta["source_observed_through"] or "–",
                         "success": meta["last_successful_window_end"] or "–"}
                rows += f'<tr data-component="{key}">' + fields(cells).replace("<span ", "<td ").replace("</span>", "</td>") + '</tr>'
            retained = ""
            for key, meta in status["refresh_status"].items():
                retained += f'<p data-refresh="{key}">' + fields({
                    "label": "Tageshistorie" if key == "history" else "Monatlicher Handel",
                    "status": verify.REFRESH_STATUS[meta["status"]], "through": meta["data_through"]}) + '</p>'
            report = ('<p id="electricity-partial-warning">Teilaktualisierung: Fehlende Werte sind keine Nullen.</p>'
                      '<details id="electricity-component-report" open><summary>Quellenstände</summary>'
                      f'<table><tbody>{rows}</tbody></table>{retained}</details>'
                      '<script type="application/json" id="electricity-component-data">'
                      + encoded(status).decode() + '</script>')
        inline = "".join(f'<script type="application/json" id="{identity}">{encoded(value).decode()}</script>'
                         for identity, value in (("electricity-history-manifest", self.manifest),
                                                 ("electricity-trends-data", self.trends),
                                                 ("electricity-progress-data", self.progress)))
        scripts = "".join(f'<script src="{src}" defer></script>' for src in sorted(verify.SCRIPTS))
        return (f'<html><nav class="topics-nav"><a href="/dashboards/" aria-current="page">Dashboards</a></nav>'
                f'<article data-through="{self.recent["data_through"]}" id="electricity-dashboard" '
                f'data-hash="{self.recent["content_hash"]}">{report}</article>{inline}{scripts}</html>').encode()

    def fetch(self, url, timeout):
        self.requests.append((url, timeout))
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 10)
        return self.public[urlsplit(url).path]

    def poll(self, **kwargs):
        options = dict(timeout=25, interval=10, fetch=self.fetch, clock=self.clock,
                       sleep=self.clock.sleep, report=self.messages.append)
        options.update(kwargs)
        return verify.verify_deployment(self.expected, **options)

    def test_success_decoded_json_and_exactly_one_latest_partition(self):
        # Eleventy changes indentation, escaping and 0.0 to 0. These are equivalent.
        public = copy.deepcopy(self.recent)
        public["rows"][0][1] = 0
        self.public[verify.RECENT] = json.dumps(public, indent=2).replace("<", "\\u003c").encode()
        self.public[verify.MANIFEST] = json.dumps(self.manifest, indent=2).encode()
        self.assertTrue(self.poll())
        paths = [urlsplit(url).path for url, _ in self.requests]
        self.assertEqual(paths, [verify.RECENT, verify.MANIFEST, self.latest["url"],
                                 verify.TRENDS, verify.PROGRESS, verify.PAGE])
        self.assertNotIn(self.closed["url"], paths)
        self.assertEqual(self.clock.sleeps, [])
        self.assertIn("Deployment verified", self.messages[-1])

    def test_v2_requires_matching_public_component_status(self):
        self.recent.update(schema_version=2, components={"price": {"status": "partial", "known_hours": 696,
                           "expected_hours": 720, "source_observed_through": "2026-09-08T22:00:00Z",
                           "last_successful_window_end": "2026-09-09T22:00:00Z"}},
                           refresh_status={"history": {"status": "stale", "data_through": "2026-09-09"}})
        self.expected['recent'] = self.recent
        self.public[verify.RECENT] = encoded(self.recent)
        with self.assertRaisesRegex(verify.VerificationError, 'component status'):
            verify.verify_once(self.expected, self.public.__getitem__)
        self.public[verify.PAGE] = self.html()
        verify.verify_once(self.expected, self.public.__getitem__)
        self.public[verify.PAGE] = self.public[verify.PAGE].replace(b'"known_hours":696', b'"known_hours":720')
        with self.assertRaisesRegex(verify.VerificationError, 'coverage/status'):
            verify.verify_once(self.expected, self.public.__getitem__)

    def test_v2_visible_report_rejects_mismatched_missing_hidden_and_retained_disclosures(self):
        self.recent.update(schema_version=2, components={
            "price": {"status": "partial", "known_hours": 600, "expected_hours": 720,
                      "source_observed_through": "2026-09-04T22:00:00Z",
                      "last_successful_window_end": "2026-09-09T22:00:00Z"},
            "gas": {"status": "stale", "known_hours": 0, "expected_hours": 720,
                    "source_observed_through": "2026-07-01T22:00:00Z", "last_successful_window_end": "2026-07-01T22:00:00Z"},
            "solar": {"status": "unavailable", "known_hours": 0, "expected_hours": 720,
                      "source_observed_through": None, "last_successful_window_end": None}},
            refresh_status={"history": {"status": "stale", "data_through": "2026-09-09"},
                            "trade": {"status": "partial", "data_through": "2026-08"}})
        self.expected["recent"] = self.recent
        self.public[verify.RECENT] = encoded(self.recent)
        html = self.html()
        self.public[verify.PAGE] = html
        verify.verify_once(self.expected, self.public.__getitem__)
        changes = [
            (b'>600/720<', b'>720/720<'),
            (b'>teilweise<', '>vollständig<'.encode()),
            (b'>beibehalten / veraltet<', '>vollständig<'.encode()),
            (b'>beibehalten nach fehlgeschlagener Aktualisierung<', b'>erfolgreich aktualisiert<'),
            (b'>2026-09-04T22:00:00Z<', b'>2026-09-09T22:00:00Z<'),
            (b'data-component="price"', b'data-component="other"'),
            (b'data-refresh="history"', b'data-refresh="other"'),
            (b'data-refresh="trade"', b'data-refresh="other"'),
            (b'id="electricity-component-report"', b'id="missing-report"'),
            (b'id="electricity-component-report" open', b'id="electricity-component-report"'),
            (b'id="electricity-partial-warning"', b'id="electricity-partial-warning" hidden'),
            (b'<table>', b'<table hidden>'),
            (b'<table>', b'<table style="display: none">'),
            (b'<table>', b'<table aria-hidden="true">'),
            (b'<table>', b'<table style="opacity:0">'),
            (b'data-component-field="coverage"', b'hidden data-component-field="coverage"'),
            (b'<details ', b'<noscript><details '),
            (b'data-component="gas"', b'data-component="price"'),
        ]
        for old, new in changes:
            with self.subTest(change=new):
                self.public[verify.PAGE] = html.replace(old, new)
                with self.assertRaises(verify.VerificationError):
                    verify.verify_once(self.expected, self.public.__getitem__)

    def test_healthy_v2_report_may_be_collapsed_without_a_partial_warning(self):
        self.recent.update(schema_version=2, components={"price": {
            "status": "complete", "known_hours": 720, "expected_hours": 720,
            "source_observed_through": "2026-09-09T22:00:00Z",
            "last_successful_window_end": "2026-09-09T22:00:00Z"}},
            refresh_status={"history": {"status": "ok", "data_through": "2026-09-09"}})
        self.expected["recent"] = self.recent
        self.public[verify.RECENT] = encoded(self.recent)
        self.public[verify.PAGE] = self.html().replace(b' open>', b'>').replace(
            b'id="electricity-partial-warning"', b'id="electricity-partial-warning" hidden')
        verify.verify_once(self.expected, self.public.__getitem__)

    def test_loading_and_polling_preserve_local_bytes_and_mtimes(self):
        def snapshot():
            return {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in self.root.rglob("*") if path.is_file()}

        before = snapshot()
        verify.load_expected(self.root)
        self.assertTrue(self.poll())
        self.assertEqual(snapshot(), before)

    def test_optional_annual_identity_is_explicit_null_when_absent(self):
        (self.root / "frontend/src/_data/germanElectricityAnnual.json").unlink()
        expected = verify.load_expected(self.root)
        self.assertIsNone(expected["inputs"]["annual_content_hash"])
        self.assertEqual(expected["inputs"]["trade_content_hash"], self.trade["content_hash"])

    def test_old_site_404_then_success_and_cache_busting(self):
        def fetch(url, timeout):
            raw = self.fetch(url, timeout)
            if parse_qs(urlsplit(url).query)["attempt"] == ["1"]:
                raise HTTPError(url, 404, "Old site", {}, io.BytesIO())
            return raw

        self.assertTrue(self.poll(fetch=fetch))
        self.assertEqual(self.clock.sleeps, [10])
        queries = [parse_qs(urlsplit(url).query) for url, _ in self.requests]
        self.assertEqual(queries[0], {"verify": ["a" * 64], "attempt": ["1"]})
        self.assertEqual(queries[1], {"verify": ["a" * 64], "attempt": ["2"]})

    def test_permanent_mismatch_has_bounded_attempts_and_recovery_message(self):
        self.public[verify.RECENT] = encoded({**self.recent, "content_hash": "f" * 64})
        self.assertFalse(self.poll())
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.clock.sleeps, [10, 10, 5])
        self.assertEqual(self.clock(), 25)
        self.assertIn("commit exists in Git", self.messages[-1])
        self.assertIn("retry the deployment", self.messages[-1])
        self.assertIn("timestamp-only commit", self.messages[-1])
        self.assertFalse(any("Deployment verified" in message for message in self.messages))

    def test_each_public_identity_mismatch_fails(self):
        cases = [
            (verify.RECENT, {**self.recent, "data_through": "old"}),
            (verify.RECENT, {**self.recent, "rows": [[1, 900]]}),
            (verify.MANIFEST, {**self.manifest, "revision_policy": "changed"}),
            (verify.MANIFEST, {**self.manifest, "years": [self.latest]}),
            (verify.MANIFEST, {**self.manifest, "schema_version": True}),
            (verify.PROGRESS, {**self.progress, "content_hash": "f" * 64}),
            (verify.PROGRESS, {**self.progress, "capacity": {"data_through": "old"}}),
        ]
        for field, value in self.expected["inputs"].items():
            cases.append((verify.TRENDS, {**self.trends, "inputs": {**self.expected["inputs"], field: None}}))
        cases.append((verify.TRENDS, {**self.trends, "coverage": {}}))
        for path, value in cases:
            with self.subTest(path=path, value=value):
                responses = {**self.public, path: encoded(value)}
                self.assertFalse(self.poll(timeout=1, fetch=lambda url, timeout: responses[urlsplit(url).path]))

    def test_raw_partition_hash_rejects_even_equivalent_json(self):
        self.public[self.latest["url"]] += b"\n"
        self.assertFalse(self.poll(timeout=1))
        self.assertIn("raw SHA-256 mismatch", self.messages[-1])

    def test_html_missing_stale_or_nonexecuting_markers_fail(self):
        html = self.public[verify.PAGE].decode()
        changes = [
            html.replace('data-hash="' + "a" * 64, 'data-hash="' + "f" * 64),
            html.replace('data-through="2026', 'data-through="2025'),
            html.replace('class="topics-nav"', 'class="footer-nav"'),
            html.replace('href="/dashboards/"', 'href="/other/"'),
            html.replace('aria-current="page"', ''),
            html.replace('/js/dashboards/electricity-history.js', '/js/old.js'),
            html.replace('id="electricity-history-manifest"', 'id="old-manifest"'),
            html.replace('id="electricity-trends-data"', 'id="old-trends"'),
            html.replace('id="electricity-progress-data"', 'id="old-progress"'),
            html.replace('"trade_content_hash":"' + "b" * 64, '"trade_content_hash":"' + "f" * 64),
            html.replace('"frozen":false', '"frozen":true'),
            html.replace('<script src=', '<script type="application/json" src='),
            '<template>' + html + '</template>',
            '<noscript>' + html + '</noscript>',
            '<!--' + html + '-->',
            '<base href="https://elsewhere.invalid/">' + html,
            html + '<article id="electricity-dashboard"></article>',
            html + '<script type="application/json" id="electricity-trends-data">{}</script>',
            html.replace('id="electricity-dashboard"', 'id="electricity-dashboard" data-hash="old"'),
        ]
        for changed in changes:
            with self.subTest(html=changed[:150]):
                self.public[verify.PAGE] = changed.encode()
                self.assertFalse(self.poll(timeout=1))

    def test_no_accumulating_successes_across_attempts(self):
        def fetch(url, timeout):
            raw = self.fetch(url, timeout)
            parts = urlsplit(url)
            attempt = int(parse_qs(parts.query)["attempt"][0])
            if (attempt == 1 and parts.path == verify.PAGE) or (attempt > 1 and parts.path == verify.RECENT):
                return b"{}"
            return raw

        self.assertFalse(self.poll(fetch=fetch))
        self.assertEqual(sum(urlsplit(url).path == verify.PAGE for url, _ in self.requests), 1)

    def test_deadline_clips_requests_sleep_and_rejects_late_success(self):
        def fetch(url, timeout):
            raw = self.fetch(url, timeout)
            self.clock.now += 1
            return raw

        self.assertFalse(self.poll(timeout=5, fetch=fetch))
        self.assertEqual([timeout for _, timeout in self.requests], [5, 4, 3, 2, 1])
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.clock(), 5)

    def test_malformed_oversized_and_nonfinite_responses_never_pass(self):
        for raw in (b"not JSON", b'[]', b'{"x":1,"x":1}', b'{"x":NaN}',
                    b'{"x":1e999}', b"\xff", b"x" * (verify.MAX_BYTES + 1)):
            with self.subTest(raw=raw[:50]):
                self.public[verify.RECENT] = raw
                self.assertFalse(self.poll(timeout=1))

    def test_local_invalid_partition_or_url_stops_before_polling(self):
        self.write(f"{verify.HISTORY_DIRECTORY}/{self.latest['url'].rsplit('/', 1)[1]}", b"wrong")
        with self.assertRaisesRegex(verify.VerificationError, "Local latest"):
            verify.load_expected(self.root)
        for url in ("https://elsewhere.invalid/file", "/data/history/german-electricity/../private.json"):
            manifest = copy.deepcopy(self.manifest)
            manifest["years"][-1]["url"] = url
            self.write(f"{verify.HISTORY_DIRECTORY}/manifest.json", encoded(manifest))
            with self.assertRaisesRegex(verify.VerificationError, "History URL"):
                verify.load_expected(self.root)

    def test_base_url_and_polling_arguments(self):
        for base in ("http://blog.databearer.de", "https://user:secret@blog.databearer.de",
                     "https://example.invalid/path", "https://example.invalid?token=x",
                     "https://example.invalid#fragment", "https://example.invalid:8443", "https://bad\nhost"):
            with self.subTest(base=base), self.assertRaises((verify.VerificationError, ValueError)):
                self.poll(base=base)
        for value in (0, -1, float("inf"), float("nan")):
            for key in ("timeout", "interval"):
                with self.subTest(key=key, value=value), self.assertRaises(verify.VerificationError):
                    self.poll(**{key: value})
        self.assertEqual(self.requests, [])
        self.assertTrue(self.poll(base="https://preview.example.invalid/"))
        self.assertTrue(all(url.startswith("https://preview.example.invalid/") for url, _ in self.requests))

    def test_cli_defaults_overrides_and_nonzero_timeout(self):
        with patch.object(verify, "load_expected", return_value=self.expected), \
                patch.object(verify, "verify_deployment", return_value=True) as poll:
            self.assertEqual(verify.main([]), 0)
            poll.assert_called_once_with(self.expected, base=verify.DEFAULT_BASE_URL, timeout=240, interval=10)
            poll.return_value = False
            self.assertEqual(verify.main(["--timeout", "12", "--interval", "2",
                                          "--base-url", "https://preview.example.invalid"]), 1)
            self.assertEqual(poll.call_args.kwargs, {"base": "https://preview.example.invalid",
                                                    "timeout": 12, "interval": 2})


class TransportTests(unittest.TestCase):
    def response(self, raw=b"{}", headers=None):
        response = io.BytesIO(raw)
        response.status = 200
        response.headers = headers or {}
        response.geturl = lambda: "https://blog.databearer.de/data/test.json?verify=abc&attempt=1"
        return response

    def test_get_headers_no_credentials_and_socket_timeout(self):
        response = self.response()
        with patch.object(verify, "build_opener") as factory:
            factory.return_value.open.return_value = response
            self.assertEqual(verify.fetch_bytes(response.geturl(), 50), b"{}")
            request = factory.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            self.assertEqual(request.get_header("Cache-control"), "no-cache")
            self.assertEqual(request.get_header("Accept-encoding"), "identity")
            self.assertIsNone(request.get_header("Authorization"))
            self.assertIsNone(request.get_header("Cookie"))
            self.assertEqual(factory.return_value.open.call_args.kwargs["timeout"], 10)
            self.assertEqual(factory.call_args.args[0].proxies, {})

    def test_redirects_refused_before_following_cross_host_or_http(self):
        handler = verify.NoRedirects()
        request = Request("https://blog.databearer.de/data/test.json")
        for url in ("https://evil.invalid/", "http://blog.databearer.de/", "https://blog.databearer.de/other"):
            with self.subTest(url=url), self.assertRaisesRegex(verify.VerificationError, "redirect refused"):
                handler.redirect_request(request, None, 302, "Found", {}, url)

    def test_transport_response_bounds_and_errors(self):
        for raw, headers in ((b"{}", {"Content-Length": str(verify.MAX_BYTES + 1)}),
                             (b"x" * (verify.MAX_BYTES + 1), {}),
                             (b"{}", {"Content-Encoding": "gzip"})):
            response = self.response(raw, headers)
            with self.subTest(headers=headers), patch.object(verify, "build_opener") as factory:
                factory.return_value.open.return_value = response
                with self.assertRaises(verify.VerificationError):
                    verify.fetch_bytes(response.geturl(), 10)
        for error in (HTTPError("https://blog.databearer.de/", 404, "Not found", {}, io.BytesIO()),
                      TimeoutError("timed out"), IncompleteRead(b"partial")):
            with patch.object(verify, "build_opener") as factory:
                factory.return_value.open.side_effect = error
                with self.assertRaises(verify.VerificationError):
                    verify.fetch_bytes("https://blog.databearer.de/", 10)

    def test_slow_body_is_rejected_at_request_deadline(self):
        response = self.response()
        with patch.object(verify, "build_opener") as factory, \
                patch.object(verify.time, "monotonic", side_effect=[0, 0, 11]):
            factory.return_value.open.return_value = response
            with self.assertRaisesRegex(verify.VerificationError, "deadline exceeded"):
                verify.fetch_bytes(response.geturl(), 10)


if __name__ == "__main__":
    unittest.main()
