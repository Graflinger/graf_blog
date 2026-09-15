"""SMARD daily history: explicit backfill/reconcile and bounded rolling refresh.

Run from pipeline/: python -m src.data_pipelines.dashboards.german_electricity.history
See docs/german_electricity_history.md for the storage and reader contract.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from .pipeline import (
    BERLIN, COLUMNS, DEFAULT_OUTPUT, HOUR_MS, MAX_EXPORT_BYTES, SERIES, SOURCE,
    SmardClient, ValidationError, ConsistencyError, ComponentUnavailable, canonical_bytes, midnight_ms, number,
    utc_ms, validate_snapshot,
)


FIRST_DAY = date(2015, 1, 1)
PRICE_START = date(2015, 1, 5)
PRICE_SPLIT = date(2018, 10, 1)
SHUTDOWN = date(2023, 4, 15)
CORRECTION_DAYS = 35
ENERGY = {key: value for key, value in SERIES.items() if key != "price"} | {"nuclear": 1224}
# Verified against the official daily AND hourly endpoints (24 null hours each).
# These five series-days remain null; every date and every other series is retained.
KNOWN_ENERGY_GAPS = {
    (date(2016, 11, 8), "other_renewables"),
    (date(2018, 1, 21), "pumped_storage"),
    (date(2018, 8, 2), "pumped_storage"),
    (date(2018, 8, 3), "pumped_storage"),
    (date(2018, 8, 23), "pumped_storage"),
}
DAILY_SERIES = ENERGY | {"price_old": 251, "price": 4169}
PUBLIC_PREFIX = "/data/history/german-electricity/"
DEFAULT_DIRECTORY = DEFAULT_OUTPUT.parents[1] / "data-history/german-electricity"
POLICY = (
    "Daily SMARD sums (GWh) and mean prices (EUR/MWh); current-year corrections in the "
    "latest 35 complete days; older dates retained, closed years frozen even in January. "
    "Backfill required for gaps; existing years replaced only by explicit --reconcile. "
    "2015-01-01 through 2015-01-04 prices unknown; missing nuclear after 2023-04-15 "
    "derived as zero and flagged. Historical daily sums may include upstream partial "
    "data/interpolation; only recent overlap is checked against complete hourly data. "
    "Known source gaps retained as null: other_renewables 2016-11-08; pumped_storage "
    "2018-01-21, 2018-08-02, 2018-08-03, 2018-08-23. Never treat these as zero."
)
PARTITION_LIMIT = 250_000
MANIFEST_LIMIT = 20_000
VERSION_FILE = re.compile(r"\d{4}\.[0-9a-f]{64}\.json")


class DailyClient(SmardClient):
    # 15 indices + 167 annual chunks for 2015–2026 = 182 successful requests.
    # Retries count toward the hard cap, rather than tripling the backfill budget.
    MAX_ATTEMPTS = 200
    MAX_TOTAL_BYTES = 8_000_000
    FETCH_TIMEOUT = 240

    def get(self, url):
        self.JSON_DECODER = strict_json
        return super().get(url)


def days(start, end):
    """Inclusive calendar dates, independent of DST."""
    while start <= end:
        yield start
        start += timedelta(days=1)


def hours(day):
    return (midnight_ms(day + timedelta(days=1)) - midnight_ms(day)) // HOUR_MS


def parse_date(value):
    try:
        result = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Invalid date: {value!r}") from exc
    if result.isoformat() != value or result < FIRST_DAY:
        raise ValidationError(f"Invalid history date: {value!r}")
    return result


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValidationError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(data, object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as exc:
        raise ValidationError(f"Corrupt JSON: {exc}") from exc


def read_bounded(path, limit):
    if path.is_symlink():
        raise ValidationError(f"Refusing symlink: {path}")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValidationError(f"Oversized history input: {path}")
    return data


def parse_chunk(payload, year, column):
    points = payload.get("series") if isinstance(payload, dict) else None
    if not isinstance(points, list) or not points or len(points) > 366:
        raise ValidationError(f"{column}/{year}: empty or malformed daily chunk")
    result = {}
    for point in points:
        if not isinstance(point, list) or len(point) != 2 or type(point[0]) is not int:
            raise ValidationError(f"{column}/{year}: malformed daily observation")
        timestamp, value = point
        if not midnight_ms(date(year, 1, 1)) <= timestamp < midnight_ms(date(year + 1, 1, 1)):
            raise ValidationError(f"{column}/{year}: timestamp outside year")
        day = datetime.fromtimestamp(timestamp / 1000, BERLIN).date()
        if timestamp != midnight_ms(day) or day in result:
            raise ValidationError(f"{column}/{year}: duplicate/non-midnight timestamp")
        if value is not None:
            number(value, "price" if column.startswith("price") else column,
                   power_scale=1000 * hours(day))
        if column == "nuclear" and day > SHUTDOWN and value not in (None, 0):
            raise ValidationError(f"Unexpected nonzero nuclear after shutdown: {day}")
        result[day] = value
    return result


def fetch_daily(client, years):
    """Read each necessary index once and only the selected annual day chunks."""
    observations = {year: {key: {} for key in DAILY_SERIES} for year in years}
    jobs = []
    for column, series_id in DAILY_SERIES.items():
        selected = [year for year in years if not (
            (column == "price_old" and year > 2018) or (column == "price" and year < 2018)
        )]
        if not selected:
            continue
        base = f"https://www.smard.de/app/chart_data/{series_id}/DE"
        index = client.get(f"{base}/index_day.json")
        stamps = index.get("timestamps") if isinstance(index, dict) else None
        if (not isinstance(stamps, list) or not stamps or len(stamps) > 200
                or any(type(t) is not int for t in stamps) or len(set(stamps)) != len(stamps)):
            raise ValidationError(f"{column}: invalid daily index")
        for stamp in stamps:
            try:
                local = datetime.fromtimestamp(stamp / 1000, BERLIN)
            except (ValueError, OverflowError, OSError) as exc:
                raise ValidationError(f"{column}: invalid index timestamp") from exc
            if stamp != midnight_ms(date(local.year, 1, 1)):
                raise ValidationError(f"{column}: index must contain Berlin January 1 timestamps")
        for year in selected:
            stamp = midnight_ms(date(year, 1, 1))
            if stamp not in stamps:
                if column == "nuclear" and year > SHUTDOWN.year:
                    continue  # Shutdown-specific absence; all other absent chunks fail.
                raise ValidationError(f"{column}/{year}: required daily chunk unavailable")
            jobs.append((year, column, f"{base}/{series_id}_DE_day_{stamp}.json"))
    with ThreadPoolExecutor(max_workers=3) as pool:
        for (year, column, _), payload in zip(jobs, pool.map(client.get, [job[2] for job in jobs])):
            observations[year][column] = parse_chunk(payload, year, column)
    return observations


def make_rows(observations, start, end, *, nullable=False):
    result, missing = [], []
    for day in days(start, end):
        values = observations[day.year]
        energy = {}
        derived = False
        for column in ENERGY:
            value = values[column].get(day)
            if column == "nuclear" and day > SHUTDOWN:
                if value not in (None, 0):
                    raise ValidationError(f"Unexpected nonzero nuclear after shutdown: {day}")
                derived = value is None
                value = 0
            if value is None:
                if (day, column) in KNOWN_ENERGY_GAPS or (nullable and day in values[column]):
                    energy[column] = None
                else:
                    missing.append(f"{day}/{column}")
            else:
                number(value, column, power_scale=1000 * hours(day))
                energy[column] = round(value / 1000, 8)
        price = values["price_old" if day < PRICE_SPLIT else "price"].get(day)
        if day < PRICE_START:
            if price is not None:
                raise ValidationError("Previously unknown 2015 price now available; review missing-price policy")
        elif price is None and not (nullable and day in values["price_old" if day < PRICE_SPLIT else "price"]):
            missing.append(f"{day}/price")
        elif price is not None:
            number(price, "price")
        result.append({"date": day.isoformat(), "hours": hours(day), "energy_gwh": energy,
                       "price_eur_mwh": price, "price_zone": "DE-AT-LU" if day < PRICE_SPLIT else "DE-LU",
                       "nuclear_derived_zero": derived})
    if missing:
        raise ValidationError(f"Missing daily observations ({len(missing)}): {', '.join(missing)}. "
                              "No dates dropped or generic zero fill; investigate source and explicitly backfill.")
    return result


def partition(year, rows, *, nullable=False):
    value = {"schema_version": 2 if nullable else 1, "year": year, "timezone": "Europe/Berlin", "source": SOURCE, "rows": rows}
    validate_partition(value)
    return value


def validate_partition(value):
    if not isinstance(value, dict) or set(value) != {"schema_version", "year", "timezone", "source", "rows"}:
        raise ValidationError("Invalid partition fields")
    year = value["year"]
    if (type(year) is not int or not 2015 <= year <= 9998 or type(value["schema_version"]) is not int
            or value["schema_version"] not in (1, 2) or value["timezone"] != "Europe/Berlin" or value["source"] != SOURCE):
        raise ValidationError("Invalid partition metadata")
    rows = value["rows"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 366:
        raise ValidationError("Invalid partition row count")
    expected = date(year, 1, 1)
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"date", "hours", "energy_gwh", "price_eur_mwh", "price_zone", "nuclear_derived_zero"}:
            raise ValidationError("Invalid daily row fields")
        day = parse_date(row["date"])
        if day != expected or day.year != year:
            raise ValidationError("Missing, duplicate, unordered, or wrong-year day; explicitly backfill gaps")
        if type(row["hours"]) is not int or row["hours"] != hours(day):
            raise ValidationError("Invalid daily DST hours")
        energy = row["energy_gwh"]
        if not isinstance(energy, dict) or set(energy) != set(ENERGY):
            raise ValidationError("Invalid energy series")
        for column, observation in energy.items():
            if observation is not None or (value["schema_version"] == 1 and (day, column) not in KNOWN_ENERGY_GAPS):
                number(observation, column, power_scale=hours(day))
        price = row["price_eur_mwh"]
        if day < PRICE_START:
            if price is not None:
                raise ValidationError("2015-01-01 through 2015-01-04 must have null prices")
        elif price is not None or value["schema_version"] == 1:
            number(price, "price")
        if row["price_zone"] != ("DE-AT-LU" if day < PRICE_SPLIT else "DE-LU"):
            raise ValidationError("Invalid historical price zone")
        derived = row["nuclear_derived_zero"]
        if type(derived) is not bool or (derived and (day <= SHUTDOWN or energy["nuclear"] != 0)):
            raise ValidationError("Invalid nuclear derived-zero flag")
        if day > SHUTDOWN and energy["nuclear"] != 0:
            raise ValidationError("Unexpected nonzero nuclear after shutdown")
        expected += timedelta(days=1)
    if len(canonical_bytes(value)) > PARTITION_LIMIT:
        raise ValidationError("Partition exceeds byte budget")


def year_entry(value, raw, frozen):
    digest = hashlib.sha256(raw).hexdigest()
    return {"year": value["year"], "url": f"{PUBLIC_PREFIX}{value['year']}.{digest}.json", "sha256": digest,
            "first_date": value["rows"][0]["date"], "last_date": value["rows"][-1]["date"],
            "days": len(value["rows"]), "frozen": frozen}


def make_manifest(partitions, as_of, previous=None):
    prior = {item["year"]: item for item in previous["years"]} if previous else {}
    entries = [year_entry(value, canonical_bytes(value), year < as_of.year or prior.get(year, {}).get("frozen", False))
               for year, value in sorted(partitions.items())]
    return {"schema_version": 1, "kind": "german-electricity-history", "timezone": "Europe/Berlin", "source": SOURCE,
            "first_date": entries[0]["first_date"], "last_date": entries[-1]["last_date"],
            "years": entries, "revision_policy": POLICY}


def validate_history(manifest, blobs):
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "kind", "timezone", "source", "first_date", "last_date", "years", "revision_policy"}:
        raise ValidationError("Invalid manifest fields")
    if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["kind"] != "german-electricity-history" or manifest["timezone"] != "Europe/Berlin"
            or manifest["source"] != SOURCE or manifest["revision_policy"] != POLICY):
        raise ValidationError("Invalid manifest metadata")
    entries = manifest["years"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise ValidationError("Invalid manifest years")
    result, previous_end = {}, None
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"year", "url", "sha256", "first_date", "last_date", "days", "frozen"}:
            raise ValidationError("Invalid year entry")
        if type(entry["year"]) is not int or type(entry["days"]) is not int or type(entry["frozen"]) is not bool:
            raise ValidationError("Invalid year entry types")
        raw = blobs.get(entry["year"])
        if not isinstance(raw, bytes) or len(raw) > PARTITION_LIMIT:
            raise ValidationError("Missing/oversized partition")
        value = strict_json(raw)
        validate_partition(value)
        if entry != year_entry(value, raw, entry["frozen"]):
            raise ValidationError("Partition hash/URL/coverage mismatch")
        start, end = parse_date(entry["first_date"]), parse_date(entry["last_date"])
        if previous_end is not None and start != previous_end + timedelta(days=1):
            raise ValidationError("History gap or unordered years; explicitly backfill")
        if entry["year"] in result:
            raise ValidationError("Duplicate history year")
        result[entry["year"]] = value
        previous_end = end
    if manifest["first_date"] != entries[0]["first_date"] or manifest["last_date"] != entries[-1]["last_date"]:
        raise ValidationError("Manifest coverage mismatch")
    if len(canonical_bytes(manifest)) > MANIFEST_LIMIT:
        raise ValidationError("Manifest exceeds byte budget")
    return result


def load_history(directory, *, required=False):
    path = directory / "manifest.json"
    if not path.exists():
        if required or any(directory.glob("*.json")):
            raise ValidationError("Missing history manifest; restore valid history or run backfill in an empty directory")
        return None, {}, None
    raw = read_bounded(path, MANIFEST_LIMIT)
    manifest = strict_json(raw)
    blobs = {}
    # Only load a strictly constrained local basename, never a manifest-supplied path.
    if not isinstance(manifest, dict) or not isinstance(manifest.get("years"), list):
        raise ValidationError("Invalid manifest")
    for entry in manifest["years"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
            raise ValidationError("Invalid partition URL")
        if type(entry.get("year")) is not int:
            raise ValidationError("Invalid partition year")
        name = entry["url"].removeprefix(PUBLIC_PREFIX)
        if entry["url"] != PUBLIC_PREFIX + name or not VERSION_FILE.fullmatch(name):
            raise ValidationError("Invalid partition URL")
        try:
            blobs[entry.get("year")] = read_bounded(directory / name, PARTITION_LIMIT)
        except OSError as exc:
            raise ValidationError(f"Missing/unreadable history partition: {name}") from exc
    partitions = validate_history(manifest, blobs)
    if any(blobs[year] != canonical_bytes(value) for year, value in partitions.items()):
        raise ValidationError("Noncanonical existing partition; refusing to rewrite frozen bytes")
    return manifest, partitions, raw


def recent_snapshot(path, as_of):
    value = strict_json(read_bounded(path, MAX_EXPORT_BYTES))
    validate_snapshot(value)
    end = datetime.fromtimestamp(utc_ms(value["data_through"]) / 1000, BERLIN).date()
    cutoff = end - timedelta(days=1)
    if value["schema_version"] == 2:
        if cutoff >= as_of:
            raise ValidationError("Recent hourly snapshot ahead of --as-of")
        return value, as_of - timedelta(days=1)
    if not 1 <= (as_of - cutoff).days <= 4:
        raise ValidationError("Recent hourly snapshot is stale/ahead of --as-of; refresh it before history")
    return value, cutoff


def compare_hourly(rows, snapshot):
    """Check daily energy sums/mean prices; tolerance only covers endpoint rounding."""
    hourly = {}
    for row in snapshot["rows"]:
        day = datetime.fromtimestamp(row[0] / 1000, BERLIN).date().isoformat()
        hourly.setdefault(day, []).append(row)
    energy_max, price_max, checked = 0, 0, 0
    for row in rows:
        points = hourly.get(row["date"])
        if points is None:
            continue
        checked += 1
        for column in SERIES:
            observed = row["price_eur_mwh"] if column == "price" else row["energy_gwh"][column]
            if observed is None or len(points) != row["hours"] or any(point[COLUMNS.index(column)] is None for point in points):
                continue
            total = sum(point[COLUMNS.index(column)] for point in points)
            delta = abs(row["price_eur_mwh"] - total / len(points)) if column == "price" else abs(row["energy_gwh"][column] - total)
            # Each rounded hourly MWh contributes at most 0.005 MWh, plus daily rounding.
            tolerance = 0.011 if column == "price" else (len(points) + 1) * 0.005 / 1000 + 1e-8
            if delta > tolerance:
                raise ConsistencyError(f"Daily/hourly mismatch {row['date']}/{column}: delta={delta:.8f}, "
                                      f"tolerance={tolerance:.8f}; refresh recent snapshot/investigate partial sums")
            if column == "price":
                price_max = max(price_max, delta)
            else:
                energy_max = max(energy_max, delta)
    return {"hourly_overlap_days": checked, "max_energy_delta_gwh": round(energy_max, 8),
            "max_price_delta_eur_mwh": round(price_max, 8)}


@contextmanager
def writer_lock(directory):
    # Lock the directory inode: no persistent lock file, no unlink/recreate race.
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValidationError("History writer already running; retry from fresh manifest") from exc
        yield
    finally:
        os.close(descriptor)


def atomic_write(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".history-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def publish_history(directory, manifest, partitions, expected_raw, *, cleanup_years=None):
    """Caller holds writer_lock. Immutable blobs first; manifest is the commit point.

    cleanup_years limits retention cleanup; None keeps the direct writer's all-year default.
    """
    blobs = {year: canonical_bytes(value) for year, value in partitions.items()}
    validate_history(manifest, blobs)
    previous, _, actual_raw = load_history(directory)
    if actual_raw != expected_raw:
        raise ValidationError("Stale history manifest; retry from fresh manifest")
    data = canonical_bytes(manifest)
    if data == actual_raw:
        return False
    created = []
    committed = False
    try:
        for entry in manifest["years"]:
            path = directory / entry["url"].removeprefix(PUBLIC_PREFIX)
            raw = blobs[entry["year"]]
            if path.exists():
                if read_bounded(path, PARTITION_LIMIT) != raw:
                    raise ValidationError("Existing content-addressed artifact is corrupt")
            else:
                atomic_write(path, raw)
                created.append(path)
        # Re-read all staged bytes before advancing the only public pointer.
        validate_history(manifest, {entry["year"]: read_bounded(
            directory / entry["url"].removeprefix(PUBLIC_PREFIX), PARTITION_LIMIT) for entry in manifest["years"]})
        pointer = directory / "manifest.json"
        current = read_bounded(pointer, MANIFEST_LIMIT) if pointer.exists() else None
        if current != expected_raw:
            raise ValidationError("Stale history manifest before publication")
        atomic_write(directory / "manifest.json", data)
        committed = True
    finally:
        if not committed:
            for path in created:
                path.unlink(missing_ok=True)
    # Keep this and the preceding manifest's blobs, at most two per cleanup-eligible year.
    # Old cached readers must retry the current manifest on a missing version.
    keep = {Path(entry["url"]).name for entry in manifest["years"]}
    if previous:
        keep.update(Path(entry["url"]).name for entry in previous["years"])
    for path in directory.iterdir():
        if (VERSION_FILE.fullmatch(path.name) and path.name not in keep
                and (cleanup_years is None or int(path.name[:4]) in cleanup_years)):
            try:
                path.unlink()
            except OSError as exc:
                print(f"History published; cleanup deferred for {path.name}: {exc}", file=sys.stderr)
    return True


def run(mode, as_of, directory=DEFAULT_DIRECTORY, *, start_year=2015, end_year=None,
        reconcile=False, snapshot_path=DEFAULT_OUTPUT, client=None, independent=False):
    started = time.monotonic()
    client = client or DailyClient()
    directory = Path(directory)
    end_year = as_of.year if end_year is None else end_year
    metrics = {"mode": mode, "as_of": as_of.isoformat(), "status": "failed"}
    try:
        if as_of > datetime.now(BERLIN).date() or as_of < FIRST_DAY or mode not in ("backfill", "refresh"):
            raise ValidationError("Invalid mode/as-of date")
        if not 2015 <= start_year <= end_year <= as_of.year or end_year - start_year >= 100:
            raise ValidationError("Invalid backfill year range")
        directory.mkdir(parents=True, exist_ok=True)
        with writer_lock(directory):
            previous, partitions, previous_raw = load_history(directory, required=mode == "refresh")
            snapshot = None
            if mode == "refresh" or end_year == as_of.year:
                if independent:
                    snapshot = strict_json(read_bounded(Path(snapshot_path), MAX_EXPORT_BYTES))
                    validate_snapshot(snapshot)
                    if utc_ms(snapshot["window_end"]) > midnight_ms(as_of):
                        raise ValidationError("Recent snapshot ahead of --as-of")
                    cutoff = as_of - timedelta(days=1)
                else:
                    snapshot, cutoff = recent_snapshot(Path(snapshot_path), as_of)
            else:
                cutoff = date(end_year, 12, 31)
            if previous and cutoff < parse_date(previous["last_date"]):
                # A historical reconcile may target earlier partitions, but cannot truncate one.
                if mode == "refresh" or end_year >= max(partitions):
                    raise ValidationError("Refusing to move history coverage backwards")
            if mode == "refresh":
                if reconcile:
                    raise ValidationError("--reconcile belongs to explicit backfill only")
                last = parse_date(previous["last_date"])
                correction_start = max(date(as_of.year, 1, 1), cutoff - timedelta(days=CORRECTION_DAYS - 1))
                if last < correction_start - timedelta(days=1) or (
                    last.year < as_of.year and last != date(as_of.year - 1, 12, 31)
                ):
                    raise ValidationError("History gap/unfinished closed year; run backfill --start-year YEAR "
                                          "--end-year YEAR --reconcile before refresh (closed years are frozen even in January)")
                years = [as_of.year] if cutoff.year == as_of.year else []
                if any(item["year"] in years and item["frozen"] for item in previous["years"]):
                    raise ValidationError("Refusing to refresh a frozen year; use explicit backfill --reconcile")
            else:
                years = [year for year in range(start_year, end_year + 1) if reconcile or year not in partitions]
            try:
                observations = fetch_daily(client, years) if years else {}
            except ValidationError as exc:
                raise ComponentUnavailable(str(exc)) from exc
            nullable = independent or (snapshot is not None and snapshot["schema_version"] == 2)
            if nullable and mode == "refresh" and years:
                # Daily availability is independent of recent source availability.
                # Explicit nulls count as reported slots; omitted dates do not.
                for lag in range(4):
                    candidate = as_of - timedelta(days=lag + 1)
                    if candidate.year != as_of.year:
                        continue
                    values = observations[as_of.year]
                    if all(candidate in values[key] for key in SERIES):
                        cutoff = candidate
                        break
                else:
                    raise ComponentUnavailable("No reported daily boundary within four days")
                if previous and cutoff < parse_date(previous["last_date"]):
                    raise ComponentUnavailable("Daily source coverage regressed; retaining previous history")
                correction_start = max(date(as_of.year, 1, 1), cutoff - timedelta(days=CORRECTION_DAYS - 1))
            fresh = []
            for year in years:
                # A v2 partition keeps its null contract during explicit closed-year
                # reconciliation, even without any recent snapshot at rollover.
                year_nullable = nullable or partitions.get(year, {}).get("schema_version") == 2
                start = correction_start if mode == "refresh" else date(year, 1, 1)
                end = min(date(year, 12, 31), cutoff)
                if end < start:
                    raise ValidationError("No completed days in requested year")
                try:
                    rows = make_rows(observations, start, end, nullable=year_nullable)
                except ValidationError as exc:
                    raise ComponentUnavailable(str(exc)) from exc
                fresh.extend(rows)
                retained = [row for row in partitions.get(year, {}).get("rows", []) if parse_date(row["date"]) < start]
                partitions[year] = partition(year, retained + rows, nullable=year_nullable)
            if snapshot:
                metrics.update(compare_hourly(fresh, snapshot))
            if not partitions:
                raise ValidationError("No history to publish")
            manifest = make_manifest(partitions, as_of, previous)
            # Routine refresh must preserve even unreferenced closed-year versions.
            # Explicit backfill/reconcile may clean only the years it fetched.
            cleanup_years = {as_of.year} if mode == "refresh" else set(years)
            changed = publish_history(directory, manifest, partitions, previous_raw,
                                      cleanup_years=cleanup_years)
            metrics.update(status="changed" if changed else "unchanged", years_fetched=years,
                           first_date=manifest["first_date"], last_date=manifest["last_date"],
                           days=sum(item["days"] for item in manifest["years"]),
                           export_bytes=len(canonical_bytes(manifest)) + sum(len(canonical_bytes(p)) for p in partitions.values()),
                           null_price_days=sum(row["price_eur_mwh"] is None for p in partitions.values() for row in p["rows"]),
                           null_energy_values=sum(value is None for p in partitions.values() for row in p["rows"] for value in row["energy_gwh"].values()),
                           nuclear_derived_days=sum(row["nuclear_derived_zero"] for p in partitions.values() for row in p["rows"]))
        return metrics
    finally:
        metrics.update(requests=client.requests, bytes_downloaded=client.bytes_downloaded,
                       total_seconds=round(time.monotonic() - started, 3))
        print(json.dumps(metrics, sort_keys=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("backfill", "refresh"):
        command = subparsers.add_parser(mode)
        command.add_argument("--as-of", type=date.fromisoformat, default=datetime.now(BERLIN).date())
        command.add_argument("--output-dir", type=Path, default=DEFAULT_DIRECTORY)
        command.add_argument("--recent-snapshot", type=Path, default=DEFAULT_OUTPUT)
        if mode == "backfill":
            command.add_argument("--start-year", type=int, default=2015)
            command.add_argument("--end-year", type=int)
            command.add_argument("--reconcile", action="store_true", help="Explicitly replace selected existing years")
    args = vars(parser.parse_args())
    args["directory"] = args.pop("output_dir")
    args["snapshot_path"] = args.pop("recent_snapshot")
    try:
        run(**args)
    except (ValidationError, OSError) as exc:
        print(f"German electricity history failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
