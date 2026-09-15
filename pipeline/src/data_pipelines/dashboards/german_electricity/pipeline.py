"""Fetch → temporary staging → selected dbt build/tests → atomic JSON snapshot.

Run from pipeline/ with PYTHONPATH=.; all raw values stay in temporary storage.
Validated previous exports provide timestamp-aligned per-series failure retention.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as day_time, timedelta, timezone
import hashlib
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc
HOUR_MS = 3_600_000
DISPLAY_DAYS = 30
FETCH_DAYS = 35
MAX_LAG_DAYS = 4
MIN_DATE = date(2024, 1, 1)
MAX_EXPORT_BYTES = 1_000_000
SERIES = {
    "biomass": 4066,
    "hydro": 1226,
    "wind_offshore": 1225,
    "wind_onshore": 4067,
    "solar": 4068,
    "other_renewables": 1228,
    "lignite": 1223,
    "hard_coal": 4069,
    "gas": 4071,
    "other_conventional": 1227,
    "pumped_storage": 4070,
    "load": 410,
    "price": 4169,
}
COLUMNS = ["timestamp", *SERIES]
SOURCE = {
    "name": "Bundesnetzagentur | SMARD.de",
    "url": "https://www.smard.de/home/marktdaten",
    "license": "CC BY 4.0",
    "license_url": "https://creativecommons.org/licenses/by/4.0/",
    "terms_url": "https://www.smard.de/home/datennutzung",
}
HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1] / "databearer_dbt"
DEFAULT_OUTPUT = HERE.parents[4] / "frontend/src/_data/germanElectricity.json"


class ValidationError(ValueError):
    """Upstream data or an export fails the dashboard contract."""


class ConsistencyError(ValidationError):
    """Available independently sourced values contradict; never degrade silently."""


class ComponentUnavailable(ValidationError):
    """An upstream component cannot supply a valid refresh; retain its prior export."""


def midnight_ms(day):
    return int(datetime.combine(day, day_time(), BERLIN).timestamp() * 1000)


def iso_utc(timestamp_ms):
    return datetime.fromtimestamp(timestamp_ms / 1000, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_ms(value):
    if not isinstance(value, str):
        raise ValidationError("Expected ISO UTC timestamp")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        timestamp = int(instant.timestamp() * 1000)
    except (ValueError, OverflowError, OSError) as exc:
        raise ValidationError("Invalid ISO UTC timestamp") from exc
    if not value.endswith("Z") or iso_utc(timestamp) != value:
        raise ValidationError("Expected canonical ISO UTC seconds with Z")
    return timestamp


def number(value, column, power_scale=1):
    # bool is an int subclass, but is never an observation.
    # Python integers are finite but can overflow conversion inside math.isfinite.
    # Compare them directly against the bounds below, without float conversion.
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        raise ValidationError(f"{column}: expected a finite JSON number")
    if column == "price":
        if not -10_000 <= value <= 10_000:
            raise ValidationError("price outside broad EUR/MWh sanity bounds")
    elif not 0 <= value <= 200 * power_scale:
        raise ValidationError(f"{column} outside broad 0–200 GW sanity bounds")


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def semantic_content(snapshot):
    return {key: value for key, value in snapshot.items() if key not in ("content_hash", "snapshot_created_at")}


class SmardClient:
    """Three concurrent requests maximum, with explicit total and per-request budgets."""

    MAX_RESPONSE_BYTES = 256_000
    MAX_TOTAL_BYTES = 24_000_000
    MAX_ATTEMPTS = 273  # 13 indices + at most 6*13 weeks, each tried at most 3 times
    FETCH_TIMEOUT = 240
    JSON_DECODER = staticmethod(json.loads)

    def __init__(self):
        self.requests = 0
        self.bytes_downloaded = 0
        self.started = time.monotonic()
        self.lock = threading.Lock()

    def get(self, url):
        for attempt in range(3):
            remaining = self.FETCH_TIMEOUT - (time.monotonic() - self.started)
            with self.lock:
                if remaining <= 0 or self.requests >= self.MAX_ATTEMPTS:
                    raise ValidationError("SMARD fetch time/request budget exhausted")
                self.requests += 1
            try:
                request = Request(url, headers={"User-Agent": "Databearer-GermanElectricity/1.0", "Accept": "application/json", "Accept-Encoding": "identity"})
                with urlopen(request, timeout=min(15, remaining)) as response:
                    chunks = []
                    response_bytes = 0
                    while True:
                        # read1 returns after one underlying read, allowing the
                        # whole-fetch deadline to stop a slowly streaming response.
                        chunk = response.read1(16_384)
                        if not chunk:
                            break
                        response_bytes += len(chunk)
                        with self.lock:
                            self.bytes_downloaded += len(chunk)
                            too_large = self.bytes_downloaded > self.MAX_TOTAL_BYTES
                        if response_bytes > self.MAX_RESPONSE_BYTES or too_large:
                            raise ValidationError("SMARD response byte budget exhausted")
                        if time.monotonic() - self.started > self.FETCH_TIMEOUT:
                            raise ValidationError("SMARD fetch time budget exhausted")
                        chunks.append(chunk)
                try:
                    return self.JSON_DECODER(b"".join(chunks))
                except (ValueError, UnicodeError) as exc:
                    raise ValidationError(f"Malformed SMARD JSON: {url}") from exc
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise ValidationError(f"SMARD HTTP {exc.code}: {url}") from exc
                exc.close()
            except (URLError, TimeoutError, socket.timeout, ConnectionError, HTTPException) as exc:
                if attempt == 2:
                    raise ValidationError(f"SMARD request failed: {url}") from exc
            time.sleep(2 ** attempt)
        raise AssertionError("unreachable")


def required_weeks(start_day, end_day):
    monday = start_day - timedelta(days=start_day.weekday())
    weeks = []
    while monday < end_day:
        weeks.append(midnight_ms(monday))
        monday += timedelta(days=7)
    if len(weeks) > 6:
        raise ValidationError("Fetch window exceeds six weekly chunks")
    return weeks


def parse_week(payload, week, start, end, column, observations):
    if not isinstance(payload, dict) or not isinstance(payload.get("series"), list) or not payload["series"]:
        raise ValidationError(f"{column}: empty/malformed weekly series")
    week_day = datetime.fromtimestamp(week / 1000, BERLIN).date()
    week_end = midnight_ms(week_day + timedelta(days=7))
    seen = set()
    for point in payload["series"]:
        if not isinstance(point, list) or len(point) != 2:
            raise ValidationError(f"{column}: malformed observation")
        timestamp, value = point
        if type(timestamp) is not int or timestamp % HOUR_MS or not week <= timestamp < week_end:
            raise ValidationError(f"{column}: invalid hourly timestamp")
        if timestamp in seen or timestamp in observations:
            raise ValidationError(f"{column}: duplicate timestamp {timestamp}")
        seen.add(timestamp)
        if value is not None:
            number(value, column, power_scale=1000)
        if start <= timestamp < end:
            observations[timestamp] = value


def fetch_observations(client, as_of):
    start_day = as_of - timedelta(days=FETCH_DAYS)
    if start_day < MIN_DATE:
        raise ValidationError("The entire 35-day fetch window must be post-2023")
    start, end = midnight_ms(start_day), midnight_ms(as_of)
    weeks = required_weeks(start_day, as_of)
    observations = {column: {} for column in SERIES}
    jobs = []
    # Every index is fetched once (apart from transient HTTP retries).
    for column, series_id in SERIES.items():
        base = f"https://www.smard.de/app/chart_data/{series_id}/DE"
        payload = client.get(f"{base}/index_hour.json")
        timestamps = payload.get("timestamps") if isinstance(payload, dict) else None
        if not isinstance(timestamps, list) or not timestamps or any(type(t) is not int for t in timestamps):
            raise ValidationError(f"{column}: malformed index")
        if len(set(timestamps)) != len(timestamps) or not set(weeks).issubset(timestamps):
            raise ValidationError(f"{column}: duplicate index or required week unavailable")
        for week in weeks:
            jobs.append((column, week, f"{base}/{series_id}_DE_hour_{week}.json"))
    with ThreadPoolExecutor(max_workers=3) as pool:
        # map retains deterministic job order; each worker performs only HTTP I/O.
        payloads = pool.map(client.get, [job[2] for job in jobs])
        for (column, week, _), payload in zip(jobs, payloads):
            parse_week(payload, week, start, end, column, observations[column])
    return observations


def select_window(observations, as_of):
    if set(observations) != set(SERIES):
        raise ValidationError("Missing/unexpected series")
    for lag in range(1, MAX_LAG_DAYS + 1):
        last_day = as_of - timedelta(days=lag)
        day_start = midnight_ms(last_day)
        end = midnight_ms(last_day + timedelta(days=1))
        if all(observations[column].get(t) is not None for column in SERIES for t in range(day_start, end, HOUR_MS)):
            start = midnight_ms(last_day - timedelta(days=DISPLAY_DAYS - 1))
            if datetime.fromtimestamp(start / 1000, BERLIN).date() < MIN_DATE:
                raise ValidationError("Dashboard coverage must be post-2023")
            # Once the latest complete day is chosen, holes inside its window fail.
            # Never shift the window backwards to conceal an internal missing hour.
            for column in SERIES:
                for timestamp in range(start, end, HOUR_MS):
                    value = observations[column].get(timestamp)
                    if value is None:
                        raise ValidationError(f"{column}: missing value at {iso_utc(timestamp)}")
                    number(value, column, power_scale=1000)
            return start, end
    raise ValidationError("No common complete day within the four-day publication-lag limit")


def build_curated(observations, start, end, directory):
    import duckdb

    database = directory / "german_electricity.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("create schema staging")
        connection.execute("create table staging.smard_hourly (series varchar, timestamp_ms bigint, value double)")
        connection.executemany(
            "insert into staging.smard_hourly values (?, ?, ?)",
            [(column, timestamp, value) for column in SERIES for timestamp, value in sorted(observations[column].items())],
        )
        connection.execute("create table staging.smard_window (start_ms bigint, end_ms bigint)")
        connection.execute("insert into staging.smard_window values (?, ?)", [start, end])
    environment = {
        **os.environ,
        "GERMAN_ELECTRICITY_DB": str(database),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    command = [
        str(Path(sys.executable).parent / "dbt"), "build",
        "--project-dir", str(PROJECT), "--profiles-dir", str(HERE),
        "--target", "dashboard", "--select", "+german_electricity_hourly",
        "--target-path", str(directory / "target"), "--log-path", str(directory / "logs"),
        "--no-partial-parse", "--fail-fast",
    ]
    subprocess.run(command, env=environment, check=True, timeout=120)
    with duckdb.connect(str(database), read_only=True) as connection:
        return [list(row) for row in connection.execute(
            'select ' + ', '.join(f'"{column}"' for column in COLUMNS)
            + ' from dashboard_curated.german_electricity_hourly order by "timestamp"'
        ).fetchall()]


def make_snapshot(rows, start, end, created_at=None, *, now_ms=None, components=None, refresh_status=None):
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    snapshot = {
        "schema_version": 1,
        "source": dict(SOURCE),
        "timezone": "Europe/Berlin",
        "window_start": iso_utc(start),
        "window_end": iso_utc(end),
        "data_through": iso_utc(end),
        "columns": list(COLUMNS),
        "rows": rows,
        "units": {"power": "GW", "price": "EUR/MWh"},
        "expected_update": "daily",
        "stale_after_hours": 96,
    }
    if components is not None:
        snapshot.update(schema_version=2, components=components, refresh_status=refresh_status or {})
    snapshot["content_hash"] = hashlib.sha256(canonical_bytes(snapshot)).hexdigest()
    snapshot["snapshot_created_at"] = created_at or iso_utc(now_ms)
    validate_snapshot(snapshot, now_ms=now_ms)
    return snapshot


def validate_snapshot(snapshot, *, now_ms=None):
    """Validate semantic content and creation-time bounds against the current clock."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    keys = {"schema_version", "source", "timezone", "window_start", "window_end", "data_through", "snapshot_created_at", "content_hash", "columns", "rows", "units", "expected_update", "stale_after_hours"}
    if isinstance(snapshot, dict) and snapshot.get("schema_version") == 2:
        keys |= {"components", "refresh_status"}
    if not isinstance(snapshot, dict) or set(snapshot) != keys:
        raise ValidationError("Invalid snapshot fields")
    if (type(snapshot["schema_version"]) is not int or snapshot["schema_version"] not in (1, 2)
            or snapshot["source"] != SOURCE or snapshot["timezone"] != "Europe/Berlin"
            or snapshot["columns"] != COLUMNS or snapshot["units"] != {"power": "GW", "price": "EUR/MWh"}
            or snapshot["expected_update"] != "daily" or type(snapshot["stale_after_hours"]) is not int
            or snapshot["stale_after_hours"] != 96):
        raise ValidationError("Invalid snapshot metadata")
    start, end = utc_ms(snapshot["window_start"]), utc_ms(snapshot["window_end"])
    created = utc_ms(snapshot["snapshot_created_at"])
    start_local = datetime.fromtimestamp(start / 1000, BERLIN)
    end_local = datetime.fromtimestamp(end / 1000, BERLIN)
    if (start_local.time() != day_time() or end_local.time() != day_time()
            or (end_local.date() - start_local.date()).days != DISPLAY_DAYS
            or start_local.date() < MIN_DATE or snapshot["data_through"] != snapshot["window_end"]):
        raise ValidationError("Invalid 30-calendar-day window")
    if not end <= created <= now_ms:
        raise ValidationError("snapshot_created_at must be between data_through and now")
    rows = snapshot["rows"]
    if not isinstance(rows, list) or len(rows) != (end - start) // HOUR_MS:
        raise ValidationError("Incorrect hourly row count")
    for expected, row in zip(range(start, end, HOUR_MS), rows):
        if not isinstance(row, list) or len(row) != len(COLUMNS) or type(row[0]) is not int or row[0] != expected:
            raise ValidationError("Missing, duplicate, unordered, or malformed hourly row")
        for column, value in zip(COLUMNS[1:], row[1:]):
            if value is not None or snapshot["schema_version"] == 1:
                number(value, column)
    if snapshot["schema_version"] == 2:
        from .partial import validate_components
        validate_components(snapshot)
    expected_hash = hashlib.sha256(canonical_bytes(semantic_content(snapshot))).hexdigest()
    if snapshot["content_hash"] != expected_hash:
        raise ValidationError("Snapshot content hash mismatch")
    if len(canonical_bytes(snapshot)) + 1 > MAX_EXPORT_BYTES:
        raise ValidationError("Snapshot exceeds 1 MB")


def publish_snapshot(snapshot, output, *, now_ms=None):
    """Validate both snapshots; a corrupt existing file is an explicit failure."""
    validate_snapshot(snapshot, now_ms=now_ms)
    output = Path(output)
    if output.exists():
        if output.stat().st_size > MAX_EXPORT_BYTES:
            raise ValidationError("Existing snapshot exceeds 1 MB; refusing replacement")
        try:
            previous = json.loads(output.read_bytes())
        except (ValueError, UnicodeError) as exc:
            raise ValidationError("Existing snapshot is corrupt; refusing replacement") from exc
        validate_snapshot(previous, now_ms=now_ms)
        if canonical_bytes(semantic_content(previous)) == canonical_bytes(semantic_content(snapshot)):
            return False, output.stat().st_size  # Preserve its bytes, mtime, and creation timestamp.
    data = canonical_bytes(snapshot) + b"\n"
    # Same directory makes os.replace atomic. Never touch the destination on failure.
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=f".{output.name}.", delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        validate_snapshot(json.loads(temp_path.read_bytes()), now_ms=now_ms)
        os.replace(temp_path, output)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return True, len(data)


def run(as_of, output, client=None):
    from .partial import refresh_recent
    return refresh_recent(as_of, Path(output), client=client)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat, default=datetime.now(BERLIN).date(),
                        help="Berlin run date; only preceding completed days are eligible")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.as_of > datetime.now(BERLIN).date():
        parser.error("--as-of must not be in the future")
    if args.as_of - timedelta(days=FETCH_DAYS) < MIN_DATE:
        parser.error("the 35-day fetch window must start on or after 2024-01-01")
    if not args.output.parent.is_dir():
        parser.error("--output parent directory must already exist")
    try:
        run(args.as_of, args.output)
    except (ValidationError, OSError, subprocess.SubprocessError) as exc:
        print(f"German electricity refresh failed: {exc}", file=sys.stderr)
        sys.exit(1)
