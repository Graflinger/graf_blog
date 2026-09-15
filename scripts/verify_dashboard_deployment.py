#!/usr/bin/env python3
"""Read-only public deployment check after the daily atomic Git push.

Run from any directory: python3 -B scripts/verify_dashboard_deployment.py
Uses only public HTTPS GETs and repository snapshots; no Cloudflare credentials,
Git operations, uploads, generated files, or build dependencies are required.
"""

import argparse
import hashlib
from http.client import HTTPException
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "https://blog.databearer.de"
MAX_BYTES = 3_000_000
HTTP_TIMEOUT = 10
RECENT = "/data/german-electricity.json"
MANIFEST = "/data/history/german-electricity/manifest.json"
TRENDS = "/data/german-electricity-trends.json"
PROGRESS = "/data/german-electricity-progress.json"
PAGE = "/dashboards/strom/"
HISTORY_DIRECTORY = "frontend/src/data-history/german-electricity"
SCRIPTS = {
    "/js/lib/echarts.min.js",
    "/js/dashboards/electricity-data.js",
    "/js/dashboards/electricity-history.js",
    "/js/dashboards/electricity-dashboard.js",
    "/js/dashboards/electricity-trends.js",
    "/js/dashboards/electricity-progress.js",
}
INLINE_IDS = {
    "electricity-history-manifest", "electricity-trends-data", "electricity-progress-data",
    "electricity-component-data",
}
COMPONENT_LABELS = {
    "biomass": "Biomasse", "hydro": "Wasserkraft", "wind_offshore": "Wind auf See",
    "wind_onshore": "Wind an Land", "solar": "Solar", "other_renewables": "Sonstige Erneuerbare",
    "lignite": "Braunkohle", "hard_coal": "Steinkohle", "gas": "Erdgas",
    "other_conventional": "Sonstige Konventionelle", "pumped_storage": "Pumpspeicher",
    "load": "Netzlast", "price": "Day-Ahead-Preis",
}
COMPONENT_STATUS = {"complete": "vollständig", "partial": "teilweise",
                    "stale": "beibehalten / veraltet", "unavailable": "nicht verfügbar"}
REFRESH_STATUS = {"ok": "erfolgreich aktualisiert", "partial": "aktualisiert mit Quellenlücken",
                  "stale": "beibehalten nach fehlgeschlagener Aktualisierung"}


class VerificationError(RuntimeError):
    """Public content is missing, invalid, or different from the expected release."""


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result

    def number(token):
        value = float(token)
        require(math.isfinite(value), "Nonfinite JSON number")
        return value

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_float=number,
                           parse_constant=number)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise VerificationError("Invalid JSON response/snapshot") from error
    require(isinstance(value, dict), "JSON must be an object")
    return value


def same_json(left, right):
    """Ignore serialization and int/float spelling, but never equate true with 1."""
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_json(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same_json(a, b) for a, b in zip(left, right))
    return left == right


def digest(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value),
            "Missing or invalid expected SHA-256")
    return value


def read_bytes(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES, "Local snapshot exceeds 3 MB")
    return raw


def load_expected(root=ROOT):
    """Capture the local release once; polling must not move the expected target."""
    data = root / "frontend/src/_data"
    recent = strict_json(read_bytes(data / "germanElectricity.json"))
    trade = strict_json(read_bytes(data / "germanElectricityTrade.json"))
    progress = strict_json(read_bytes(data / "germanElectricityProgress.json"))
    manifest_raw = read_bytes(root / HISTORY_DIRECTORY / "manifest.json")
    manifest = strict_json(manifest_raw)
    for snapshot in (recent, trade, progress):
        digest(snapshot.get("content_hash"))
    require(isinstance(recent.get("data_through"), str) and recent["data_through"],
            "Missing expected recent data_through")
    years = manifest.get("years")
    require(isinstance(years, list) and years, "Missing expected history years")
    previous = 0
    for entry in years:
        require(isinstance(entry, dict) and type(entry.get("year")) is int
                and previous < entry["year"] <= 9999, "Invalid history year order")
        previous = entry["year"]
        sha = digest(entry.get("sha256"))
        require(entry.get("url") == f"/data/history/german-electricity/{previous}.{sha}.json",
                "History URL must match its local year and SHA-256")
    latest = years[-1]
    partition = read_bytes(root / HISTORY_DIRECTORY / latest["url"].rsplit("/", 1)[1])
    require(hashlib.sha256(partition).hexdigest() == latest["sha256"],
            "Local latest history partition SHA-256 mismatch")
    annual_path = data / "germanElectricityAnnual.json"
    annual_hash = (digest(strict_json(read_bytes(annual_path)).get("content_hash"))
                   if annual_path.exists() else None)
    # electricityTrends.js aggregate(): these are input identities, not a hash
    # recomputed from JSON.stringify (which changes Python numeric spellings).
    inputs = {
        "history_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "history_partitions": [{"year": e["year"], "sha256": e["sha256"]} for e in years],
        "trade_content_hash": trade["content_hash"],
        "annual_content_hash": annual_hash,
    }
    coverage = {
        "energy_first_date": manifest["first_date"], "energy_last_date": manifest["last_date"],
        "trade_first_month": trade["first_month"], "trade_last_month": trade["last_month"],
    }
    require(all(isinstance(value, str) and value for value in coverage.values()),
            "Missing expected trends coverage")
    return {"recent": recent, "manifest": manifest, "progress": progress,
            "latest": latest, "inputs": inputs, "coverage": coverage}


def base_url(value):
    parsed = urlsplit(value)
    require(not any(c.isspace() or ord(c) < 32 for c in value)
            and parsed.scheme == "https" and parsed.hostname
            and parsed.username is None and parsed.password is None
            and parsed.port in (None, 443) and parsed.path in ("", "/")
            and not parsed.query and not parsed.fragment,
            "--base-url must be a credential-free HTTPS origin (port 443)")
    return f"https://{parsed.netloc}"


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Reject even same-host redirects: only the requested public paths count.
        raise VerificationError("HTTP redirect refused; expected the public HTTPS path")


def fetch_bytes(url, timeout):
    """Bounded GET; disable environment proxies and never attach cookies/auth."""
    timeout = min(HTTP_TIMEOUT, timeout)
    deadline = time.monotonic() + timeout
    request = Request(url, headers={
        "Cache-Control": "no-cache", "Pragma": "no-cache", "Accept-Encoding": "identity",
        "Accept": "application/json, text/html", "User-Agent": "Databearer-deployment-check/1",
    })
    opener = build_opener(ProxyHandler({}), NoRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            require(response.status == 200, "Expected HTTP 200")
            require(response.geturl() == url, "Unexpected response URL")
            require(response.headers.get("Content-Encoding", "identity") == "identity",
                    "Unexpected compressed response")
            length = response.headers.get("Content-Length")
            if length is not None:
                require(length.isdecimal() and int(length) <= MAX_BYTES,
                        "Response exceeds 3 MB or has invalid Content-Length")
            raw = bytearray()
            while True:
                require(time.monotonic() < deadline, "HTTP request deadline exceeded")
                chunk = response.read1(min(65536, MAX_BYTES + 1 - len(raw)))
                require(time.monotonic() < deadline, "HTTP request deadline exceeded")
                if not chunk:
                    return bytes(raw)
                raw.extend(chunk)
                require(len(raw) <= MAX_BYTES, "Response exceeds 3 MB")
    except HTTPError as error:
        error.close()
        raise VerificationError(f"HTTP {error.code}") from error
    except (URLError, OSError, HTTPException) as error:
        raise VerificationError("HTTPS request failed or timed out") from error


class DashboardHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.articles = []
        self.scripts = []
        self.inline = {}
        self.active_script = None
        self.nav = []
        self.dashboard_nav = False
        self.inert = []

    def handle_starttag(self, tag, attrs):
        if tag in ("template", "noscript"):
            self.inert.append(tag)
        if self.inert:
            return
        attributes = dict(attrs)
        require(len(attributes) == len(attrs), "Duplicate HTML attribute")
        require(tag != "base", "Unexpected HTML base URL")
        if tag == "nav":
            self.nav.append("topics-nav" in (attributes.get("class") or "").split())
        if tag == "a" and any(self.nav) and attributes.get("href") == "/dashboards/":
            self.dashboard_nav |= attributes.get("aria-current") == "page"
        if attributes.get("id") == "electricity-dashboard":
            require(tag == "article", "Dashboard marker is not on the article")
            self.articles.append(attributes)
        if tag == "script":
            identity = attributes.get("id")
            if identity in INLINE_IDS:
                require(identity not in self.inline and attributes.get("type") == "application/json"
                        and "src" not in attributes, "Invalid or duplicate inline dashboard JSON")
                self.inline[identity] = ""
                self.active_script = identity
            elif attributes.get("src") in SCRIPTS:
                require(attributes.get("type", "text/javascript") in
                        ("text/javascript", "application/javascript") and "nomodule" not in attributes,
                        "Dashboard script is not executable JavaScript")
                self.scripts.append(attributes["src"])

    def handle_data(self, data):
        if self.active_script:
            self.inline[self.active_script] += data

    def handle_endtag(self, tag):
        if self.inert:
            if tag == self.inert[-1]:
                self.inert.pop()
            return
        if tag == "script":
            self.active_script = None
        if tag == "nav" and self.nav:
            self.nav.pop()


def compare(actual, expected, label):
    require(same_json(actual, expected), f"{label} mismatch")


class VisibleReport(HTMLParser):
    """Inspect reader-facing report text, not data attributes or hidden JSON.

    Reject hidden/inert ancestors and inline hiding. Closed details are allowed for
    a healthy report; a partial/retained report must be expanded in the static HTML.
    This checks markup, not computed CSS or general browser rendering.
    """
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.nodes = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        style = re.sub(r"\s+", "", attrs.get("style", "").lower())
        hidden = (tag in {"script", "style", "template", "noscript"}
                  or "hidden" in attrs or "inert" in attrs
                  or attrs.get("aria-hidden", "").lower() == "true"
                  or any(token in style for token in ("display:none", "visibility:hidden"))
                  or re.search(r"(?:^|;)opacity:0(?:\.0+)?(?:!important)?(?:;|$)", style) is not None
                  or any(node["hidden"] for node in self.stack))
        node = {"tag": tag, "attrs": attrs, "hidden": hidden,
                "ancestors": self.stack[:], "text": ""}
        self.nodes.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break

    def handle_data(self, text):
        if not any(node["hidden"] for node in self.stack):
            for node in self.stack:
                node["text"] += text

    def select(self, attribute, value=None, parent=None):
        return [node for node in self.nodes if attribute in node["attrs"]
                and (value is None or node["attrs"][attribute] == value)
                and (parent is None or any(ancestor is parent for ancestor in node["ancestors"]))]

    def one(self, attribute, value, parent=None):
        nodes = self.select(attribute, value, parent)
        require(len(nodes) == 1 and not nodes[0]["hidden"], f"Missing, hidden or duplicate visible report: {value}")
        return nodes[0]

    def fields(self, row, expected):
        nodes = self.select("data-component-field", parent=row)
        require(len(nodes) == len(expected), "Visible report field count mismatch")
        for key, text in expected.items():
            node = self.one("data-component-field", key, row)
            require(node["tag"] in {"td", "th", "span"}, "Invalid visible report field markup")
            compare(" ".join(node["text"].split()), text, f"Visible report {key}")


def verify_visible_report(html, recent):
    page = VisibleReport()
    page.feed(html)
    page.close()
    article = page.one("id", "electricity-dashboard")
    report = page.one("id", "electricity-component-report", article)
    require(report["tag"] == "details", "Invalid component report markup")
    components = recent["components"]
    refresh = recent["refresh_status"]
    partial = any(meta["status"] != "complete" for meta in components.values()) or any(meta["status"] != "ok" for meta in refresh.values())
    if partial:
        require("open" in report["attrs"], "Partial component report must be expanded")
        warning = page.one("id", "electricity-partial-warning", article)
        require("Teilaktualisierung" in warning["text"] and "Fehlende Werte sind keine Nullen" in warning["text"],
                "Missing visible partial/retained warning")
    require(len(page.select("data-component", parent=report)) == len(components), "Visible component row count mismatch")
    for key, meta in components.items():
        row = page.one("data-component", key, report)
        require(row["tag"] == "tr", "Component must be a visible table row")
        page.fields(row, {"label": COMPONENT_LABELS[key], "status": COMPONENT_STATUS[meta["status"]],
                         "coverage": f"{meta['known_hours']}/{meta['expected_hours']}",
                         "observed": meta["source_observed_through"] or "–",
                         "success": meta["last_successful_window_end"] or "–"})
    require(len(page.select("data-refresh", parent=report)) == len(refresh), "Visible refresh row count mismatch")
    for key, meta in refresh.items():
        row = page.one("data-refresh", key, report)
        require(row["tag"] == "p", "Refresh must be a visible report paragraph")
        page.fields(row, {"label": "Tageshistorie" if key == "history" else "Monatlicher Handel",
                         "status": REFRESH_STATUS[meta["status"]], "through": meta["data_through"]})


def verify_once(expected, get):
    recent = strict_json(get(RECENT))
    for field in ("content_hash", "data_through"):
        compare(recent.get(field), expected["recent"][field], f"{RECENT} {field}")
    # Comparing decoded content also catches altered rows with an unchanged claimed hash.
    compare(recent, expected["recent"], RECENT)
    compare(strict_json(get(MANIFEST)), expected["manifest"], MANIFEST)
    latest = expected["latest"]
    compare(hashlib.sha256(get(latest["url"])).hexdigest(), latest["sha256"],
            "Latest history partition raw SHA-256")
    trends = strict_json(get(TRENDS))
    compare(trends.get("kind"), "german-electricity-trends", "Trends kind")
    compare(trends.get("schema_version"), 1, "Trends schema")
    compare(trends.get("inputs"), expected["inputs"], "Trends input identity")
    compare(trends.get("coverage"), expected["coverage"], "Trends coverage")
    progress = strict_json(get(PROGRESS))
    compare(progress.get("content_hash"), expected["progress"]["content_hash"], "Progress content_hash")
    compare(progress, expected["progress"], PROGRESS)
    page = DashboardHTML()
    html = get(PAGE).decode("utf-8")
    page.feed(html)
    page.close()
    if recent.get("schema_version") == 2:
        require("electricity-component-data" in page.inline, "Missing public component status")
        compare(strict_json(page.inline["electricity-component-data"]),
                {key: recent[key] for key in ("components", "refresh_status")}, "HTML component coverage/status")
        verify_visible_report(html, recent)
    require(len(page.articles) == 1, "Missing or duplicate dashboard article")
    for marker, field in (("data-hash", "content_hash"), ("data-through", "data_through")):
        compare(page.articles[0].get(marker), expected["recent"][field], f"HTML {marker}")
    require(page.dashboard_nav, "Missing active Dashboards navigation link")
    require(set(page.scripts) == SCRIPTS and len(page.scripts) == len(SCRIPTS),
            "Missing or duplicate expected /js dashboard script links")
    for identity, value in (("electricity-history-manifest", expected["manifest"]),
                            ("electricity-trends-data", trends),
                            ("electricity-progress-data", progress)):
        require(identity in page.inline, f"Missing HTML {identity}")
        compare(strict_json(page.inline[identity]), value, f"HTML {identity}")


def verify_deployment(expected, *, base=DEFAULT_BASE_URL, timeout=240, interval=10,
                      fetch=fetch_bytes, clock=time.monotonic, sleep=time.sleep, report=print):
    base = base_url(base)
    require(math.isfinite(timeout) and timeout > 0 and math.isfinite(interval) and interval > 0,
            "Timeout and interval must be positive finite seconds")
    deadline = clock() + timeout
    attempt = 0
    last_error = "No completed verification attempt"
    while clock() < deadline:
        attempt += 1

        def get(path):
            remaining = deadline - clock()
            require(remaining > 0, "Deployment verification deadline exceeded")
            query = urlencode({"verify": expected["recent"]["content_hash"], "attempt": attempt})
            raw = fetch(f"{base}{path}?{query}", min(HTTP_TIMEOUT, remaining))
            require(clock() < deadline, "Deployment verification deadline exceeded")
            require(len(raw) <= MAX_BYTES, "Response exceeds 3 MB")
            return raw

        try:
            verify_once(expected, get)
            require(clock() < deadline, "Deployment verification deadline exceeded")
        except (VerificationError, OSError, ValueError, RecursionError) as error:
            last_error = str(error)
            report(f"Attempt {attempt}: not deployed yet: {last_error}")
        else:
            report(f"Deployment verified at {base}{PAGE}: recent "
                   f"{expected['recent']['content_hash']} through {expected['recent']['data_through']}; "
                   f"history through {expected['manifest']['last_date']} (latest raw SHA-256 checked); "
                   "trade/annual trend identities, monthly progress, HTML and script links match.")
            return True
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(interval, remaining))
    report(f"Deployment verification timed out after {timeout:g}s ({attempt} attempts). "
           f"Last failure: {last_error}. The pushed commit exists in Git, but its public "
           "deployment has not been verified. Inspect Cloudflare and retry the deployment "
           "for the intended commit, then rerun this check. Do not create a timestamp-only commit.")
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=240, help="Total polling seconds (default: 240)")
    parser.add_argument("--interval", type=float, default=10, help="Seconds between attempts (default: 10)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Public HTTPS origin")
    args = parser.parse_args(argv)
    try:
        return 0 if verify_deployment(load_expected(), base=args.base_url,
                                      timeout=args.timeout, interval=args.interval) else 1
    except (VerificationError, OSError, ValueError, KeyError) as error:
        print(f"Deployment verification failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
