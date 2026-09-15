"""Bounded monthly DE-LU commercial trade; see docs/electricity_trade.md.

Run from pipeline/: python -m src.data_pipelines.dashboards.german_electricity.trade
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from urllib.error import HTTPError

from .history import (
    DEFAULT_DIRECTORY, MANIFEST_LIMIT, atomic_write, load_history, parse_date,
    read_bounded, strict_json, writer_lock,
)
from .pipeline import (
    BERLIN, DEFAULT_OUTPUT, HOUR_MS, SOURCE, SmardClient, ValidationError,
    canonical_bytes, midnight_ms,
)


PAIRS = {
    "DK1": (4486, 4504), "DK2": (4487, 4505), "FR": (4488, 4506),
    "NL": (4489, 4507), "PL": (4490, 4508), "SE4": (4491, 4509),
    "CH": (4492, 4510), "CZ": (4493, 4511), "AT": (4494, 4512),
    "NO2": (4718, 4720), "BE": (4706, 4708),
}
EXPORTS = {pair[0] for pair in PAIRS.values()}
IMPORTS = {pair[1] for pair in PAIRS.values()}
GROSS = EXPORTS | IMPORTS
NET = 4629
IDS = sorted(GROSS | {NET})
STARTS = {4706: date(2020, 11, 1), 4708: date(2020, 11, 1),
          4718: date(2020, 12, 1), 4720: date(2020, 12, 1)}
TRADING_STARTS = {4706: date(2020, 11, 18), 4708: date(2020, 11, 18),
                  4718: date(2020, 12, 9), 4720: date(2020, 12, 9)}
KNOWN_GAPS = {(date(2020, 11, 1), 4706), (date(2020, 11, 1), 4708),
              (date(2020, 12, 1), 4718), (date(2020, 12, 1), 4720)}
FIRST = date(2019, 1, 1)
OUTPUT = DEFAULT_OUTPUT.with_name("germanElectricityTrade.json")
HISTORY_MANIFEST = DEFAULT_DIRECTORY / "manifest.json"
EXPORT_LIMIT = 250_000
NET_TOLERANCE_MWH = 0.12  # 22 rounded gross terms + rounded net: 23 * 0.005 = 0.115
# Audited monthly gross sum minus monthly 4629, 10 September 2026. These are
# provider inconsistencies, not rounding allowances. New/changed disparities fail.
KNOWN_NET_DISCREPANCIES = {date(2021, 12, 1): -1065.0, date(2022, 1, 1): 1438.75,
                           date(2022, 10, 1): -38.25, date(2022, 12, 1): -2153.5}
POLICY = (
    "Monthly SMARD DE-LU scheduled commercial exchanges, MWh converted to GWh; "
    "imports and exports are positive magnitudes, net_exports = exports - imports. "
    "Cutoff is the latest full month supported by validated daily history and as-of. "
    "Correct only the latest three completed months within the current year; retain "
    "older months and freeze closed years. Gaps and closed-year corrections require "
    "explicit backfill --reconcile. Missing BE before 2020-11 and NO2 before 2020-12 "
    "are flagged structural zeros (commercial trading had not begun). Known missing "
    "BE 2020-11 and NO2 2020-12 use a bounded daily fallback from commercial start; "
    "if unrecoverable, keep all three totals null with missing series IDs. New gaps fail. "
    "Official net discrepancies in 2021-12, 2022-01, 2022-10 and 2022-12 are documented; "
    "derived net always uses gross inputs. Source sums do not certify hourly completeness."
)


class TradeClient(SmardClient):
    JSON_DECODER = staticmethod(strict_json)
    MAX_ATTEMPTS = 260
    MAX_TOTAL_BYTES = 2_000_000
    MAX_RESPONSE_BYTES = 64_000
    FETCH_TIMEOUT = 120

    def __init__(self, *, refresh=False):
        super().__init__()
        if refresh:
            self.MAX_ATTEMPTS = 50
            self.FETCH_TIMEOUT = 15


def shift(month, count):
    year, index = divmod(month.year * 12 + month.month - 1 + count, 12)
    return date(year, index + 1, 1)


def months(start, end):
    while start <= end:
        yield start
        start = shift(start, 1)


def month_key(month):
    return month.strftime("%Y-%m")


def parse_month(value):
    if not isinstance(value, str) or len(value) != 7:
        raise ValidationError("Invalid trade month")
    result = parse_date(value + "-01")
    if result < FIRST or month_key(result) != value:
        raise ValidationError("Invalid trade month")
    return result


def source_number(value, series_id, month):
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        raise ValidationError(f"{month_key(month)}/{series_id}: expected finite number")
    hours = (midnight_ms(shift(month, 1)) - midnight_ms(month)) / HOUR_MS
    if abs(value) > 200_000 * hours:
        raise ValidationError(f"{month_key(month)}/{series_id}: exceeds 200 GW sanity bound")
    if (series_id in EXPORTS and value < 0) or (series_id in IMPORTS and value > 0):
        raise ValidationError(f"{month_key(month)}/{series_id}: unexpected source sign")
    if month < STARTS.get(series_id, FIRST) and value != 0:
        raise ValidationError(f"{month_key(month)}/{series_id}: nonzero before commercial trading")


def parse_chunk(payload, year, series_id):
    points = payload.get("series") if isinstance(payload, dict) else None
    if not isinstance(points, list) or not 1 <= len(points) <= 12:
        raise ValidationError(f"{series_id}/{year}: malformed monthly chunk")
    result = {}
    for point in points:
        if not isinstance(point, list) or len(point) != 2 or type(point[0]) is not int:
            raise ValidationError(f"{series_id}/{year}: malformed observation")
        timestamp, value = point
        if not midnight_ms(date(year, 1, 1)) <= timestamp < midnight_ms(date(year + 1, 1, 1)):
            raise ValidationError(f"{series_id}/{year}: timestamp outside year")
        month = datetime.fromtimestamp(timestamp / 1000, BERLIN).date()
        if month.day != 1 or timestamp != midnight_ms(month) or month in result:
            raise ValidationError(f"{series_id}/{year}: duplicate/non-month-start timestamp")
        if value is not None:
            source_number(value, series_id, month)
        result[month] = value
    return result


def parse_index(payload, series_id):
    stamps = payload.get("timestamps") if isinstance(payload, dict) else None
    if (not isinstance(stamps, list) or not 1 <= len(stamps) <= 100
            or any(type(t) is not int for t in stamps) or len(set(stamps)) != len(stamps)):
        raise ValidationError(f"{series_id}: malformed monthly index")
    for stamp in stamps:
        try:
            local = datetime.fromtimestamp(stamp / 1000, BERLIN)
            valid = stamp == midnight_ms(date(local.year, 1, 1))
        except (ValueError, OverflowError, OSError):
            valid = False
        if not valid:
            raise ValidationError(f"{series_id}: index must use Berlin January 1")
    return set(stamps)


def absent_year_allowed(series_id, year):
    # Only NO2's documented startup-year monthly 404 is an in-service exception.
    return series_id in STARTS and (year < STARTS[series_id].year or
                                   (series_id in PAIRS["NO2"] and year == 2020))


def fetch_monthly(client, years):
    """23 indices + at most 23 annual chunks/year; all I/O has max 3 workers."""
    result = {year: {series_id: {} for series_id in IDS} for year in years}
    if not years:
        return result
    base = "https://www.smard.de/app/chart_data"
    with ThreadPoolExecutor(max_workers=3) as pool:
        indices = pool.map(client.get, [f"{base}/{i}/DE-LU/index_month.json" for i in IDS])
        jobs = []
        for series_id, payload in zip(IDS, indices):
            stamps = parse_index(payload, series_id)
            for year in years:
                stamp = midnight_ms(date(year, 1, 1))
                if stamp not in stamps:
                    if series_id == NET or absent_year_allowed(series_id, year):
                        continue
                    raise ValidationError(f"{series_id}/{year}: monthly chunk absent from index")
                jobs.append((series_id, year, f"{base}/{series_id}/DE-LU/{series_id}_DE-LU_month_{stamp}.json"))

        def get(job):
            series_id, year, url = job
            try:
                return client.get(url)
            except ValidationError as exc:
                if (absent_year_allowed(series_id, year) and isinstance(exc.__cause__, HTTPError)
                        and exc.__cause__.code == 404):
                    return None
                raise

        for (series_id, year, _), payload in zip(jobs, pool.map(get, jobs)):
            if payload is not None:
                result[year][series_id] = parse_chunk(payload, year, series_id)
    return result


def recover_startup_months(client, observations, metrics):
    """At most four daily requests, only for the four known monthly source holes."""
    jobs = [(month, i) for month, i in sorted(KNOWN_GAPS)
            if month.year in observations and observations[month.year][i].get(month) is None]

    def get(job):
        month, i = job
        stamp = midnight_ms(date(month.year, 1, 1))
        url = f"https://www.smard.de/app/chart_data/{i}/DE-LU/{i}_DE-LU_day_{stamp}.json"
        try:
            return client.get(url)
        except ValidationError as exc:
            if isinstance(exc.__cause__, HTTPError) and exc.__cause__.code == 404:
                return None
            raise

    with ThreadPoolExecutor(max_workers=3) as pool:
        for (month, i), payload in zip(jobs, pool.map(get, jobs)):
            if payload is None:
                continue
            points = payload.get("series") if isinstance(payload, dict) else None
            if not isinstance(points, list) or not 1 <= len(points) <= 366:
                raise ValidationError(f"{i}: malformed startup daily fallback")
            daily = {}
            for point in points:
                if not isinstance(point, list) or len(point) != 2 or type(point[0]) is not int:
                    raise ValidationError(f"{i}: malformed fallback observation")
                stamp, value = point
                if not midnight_ms(date(2020, 1, 1)) <= stamp < midnight_ms(date(2021, 1, 1)):
                    raise ValidationError(f"{i}: fallback timestamp outside 2020")
                day = datetime.fromtimestamp(stamp / 1000, BERLIN).date()
                if stamp != midnight_ms(day) or day in daily:
                    raise ValidationError(f"{i}: duplicate/non-midnight fallback timestamp")
                if value is not None:
                    source_number(value, i, day.replace(day=1))
                    hours = (midnight_ms(day + timedelta(days=1)) - stamp) / HOUR_MS
                    if abs(value) > hours * 200_000 or (day < TRADING_STARTS[i] and value != 0):
                        raise ValidationError(f"{i}: invalid pre-trading/daily fallback value")
                daily[day] = value
            active = []
            day = TRADING_STARTS[i]
            while day < shift(month, 1):
                active.append(daily.get(day))
                day += timedelta(days=1)
            if all(value is not None for value in active):
                recovered = math.fsum(active)
                observations[2020][i][month] = recovered
                metrics.setdefault("daily_fallback_mwh", {})[f"{month_key(month)}/{i}"] = recovered
            else:
                metrics.setdefault("unrecoverable_startup_series", []).append(f"{month_key(month)}/{i}")


def make_rows(observations, start, end, metrics, *, nullable=False):
    rows, unknown = [], []
    for month in months(start, end):
        values, missing, structural = {}, [], []
        for series_id in sorted(GROSS):
            value = observations[month.year][series_id].get(month)
            if value is None and month < STARTS.get(series_id, FIRST):
                structural.append(series_id)
                value = 0
            elif value is None:
                missing.append(series_id)
                if (month, series_id) not in KNOWN_GAPS and not (nullable and month in observations[month.year][series_id]):
                    unknown.append(f"{month_key(month)}/{series_id}")
            if value is not None:
                source_number(value, series_id, month)
            values[series_id] = value
        row = {"month": month_key(month), "imports_gwh": None, "exports_gwh": None,
               "net_exports_gwh": None, "missing_series": missing, "structural_zero_series": structural}
        if not missing:
            imports = -math.fsum(values[i] for i in IMPORTS)
            exports = math.fsum(values[i] for i in EXPORTS)
            row.update(imports_gwh=round(imports / 1000, 8), exports_gwh=round(exports / 1000, 8),
                       net_exports_gwh=round((exports - imports) / 1000, 8))
            net = observations[month.year][NET].get(month)
            if net is not None:
                signed_delta = exports - imports - net
                delta = abs(signed_delta)
                metrics["net_months_checked"] = metrics.get("net_months_checked", 0) + 1
                metrics["max_net_delta_mwh"] = round(max(metrics.get("max_net_delta_mwh", 0), delta), 8)
                if delta > NET_TOLERANCE_MWH:
                    known = KNOWN_NET_DISCREPANCIES.get(month)
                    if known is None or abs(signed_delta - known) > NET_TOLERANCE_MWH:
                        from .pipeline import ConsistencyError
                        raise ConsistencyError(f"{month_key(month)}: official net mismatch {signed_delta:.8f} MWh; investigate")
                    metrics.setdefault("known_net_discrepancies_mwh", {})[month_key(month)] = round(signed_delta, 8)
        rows.append(row)
    if unknown:
        raise ValidationError("New missing trade observations: " + ", ".join(unknown) +
                              "; investigate source and explicitly backfill; no arbitrary fill")
    return rows


def digest(value):
    return hashlib.sha256(canonical_bytes({k: v for k, v in value.items() if k != "content_hash"})).hexdigest()


def snapshot(rows, *, nullable=False):
    if not rows:
        raise ValidationError("No completed trade months")
    value = {"schema_version": 2 if nullable else 1, "kind": "german-electricity-trade", "source": SOURCE,
             "region": "DE-LU", "timezone": "Europe/Berlin", "first_month": "2019-01",
             "last_month": rows[-1]["month"], "revision_policy": POLICY, "rows": rows}
    value["content_hash"] = digest(value)
    validate_snapshot(value)
    return value


def validate_snapshot(value):
    keys = {"schema_version", "kind", "source", "region", "timezone", "first_month",
            "last_month", "revision_policy", "rows", "content_hash"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValidationError("Invalid trade snapshot fields")
    if (type(value["schema_version"]) is not int or value["schema_version"] not in (1, 2)
            or value["kind"] != "german-electricity-trade" or value["source"] != SOURCE
            or value["region"] != "DE-LU" or value["timezone"] != "Europe/Berlin"
            or value["first_month"] != "2019-01" or value["revision_policy"] != POLICY):
        raise ValidationError("Invalid trade snapshot metadata")
    last = parse_month(value["last_month"])
    if last >= datetime.now(BERLIN).date().replace(day=1):
        raise ValidationError("Trade snapshot includes an incomplete/future month")
    expected = list(months(FIRST, last))
    rows = value["rows"]
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise ValidationError("Trade coverage gap; explicitly backfill --reconcile")
    for month, row in zip(expected, rows):
        if not isinstance(row, dict) or set(row) != {"month", "imports_gwh", "exports_gwh", "net_exports_gwh",
                                                   "missing_series", "structural_zero_series"}:
            raise ValidationError("Invalid trade row fields")
        if row["month"] != month_key(month):
            raise ValidationError("Trade months missing/duplicate/unordered; explicitly backfill --reconcile")
        for field in ("missing_series", "structural_zero_series"):
            ids = row[field]
            if (not isinstance(ids, list) or any(type(i) is not int or i not in GROSS for i in ids)
                    or ids != sorted(set(ids))):
                raise ValidationError(f"Invalid {field}")
        if value["schema_version"] == 1 and any((month, i) not in KNOWN_GAPS for i in row["missing_series"]):
            raise ValidationError("Unknown trade missing series")
        if any(month >= STARTS.get(i, FIRST) for i in row["structural_zero_series"]):
            raise ValidationError("Invalid structural zero")
        totals = [row[k] for k in ("imports_gwh", "exports_gwh", "net_exports_gwh")]
        if row["missing_series"]:
            if totals != [None, None, None]:
                raise ValidationError("Missing trade input requires all three totals null")
        else:
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in totals):
                raise ValidationError("Trade totals must be finite numbers")
            imports, exports, net = totals
            hours = (midnight_ms(shift(month, 1)) - midnight_ms(month)) / HOUR_MS
            if not (0 <= imports <= 200 * hours and 0 <= exports <= 200 * hours):
                raise ValidationError("Trade magnitude outside sanity bound")
            if abs(net - (exports - imports)) > 2e-8:
                raise ValidationError("Trade net identity mismatch")
    if value["content_hash"] != digest(value):
        raise ValidationError("Trade content hash mismatch")
    if len(canonical_bytes(value)) > EXPORT_LIMIT:
        raise ValidationError("Trade export exceeds size limit")


def load_snapshot(path, *, required=False):
    if not path.exists() and not path.is_symlink():
        if required:
            raise ValidationError("Missing trade snapshot; run backfill --start-year 2019")
        return None, None
    raw = read_bounded(path, EXPORT_LIMIT)
    value = strict_json(raw)
    validate_snapshot(value)
    if canonical_bytes(value) != raw:
        raise ValidationError("Noncanonical trade snapshot; refusing to rewrite frozen rows")
    return value, raw


def history_cutoff(path, as_of):
    # Validate every referenced partition and checksum, not just an untrusted date.
    # load_history uses manifest.json; support an explicitly named alternate manifest.
    raw = read_bounded(path, MANIFEST_LIMIT)
    if path.name != "manifest.json":
        from .history import PARTITION_LIMIT, PUBLIC_PREFIX, VERSION_FILE, validate_history
        manifest = strict_json(raw)
        if not isinstance(manifest, dict) or not isinstance(manifest.get("years"), list):
            raise ValidationError("Invalid history manifest")
        blobs = {}
        for entry in manifest["years"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
                raise ValidationError("Invalid history partition URL")
            name = entry["url"].removeprefix(PUBLIC_PREFIX)
            if entry["url"] != PUBLIC_PREFIX + name or not VERSION_FILE.fullmatch(name) or type(entry.get("year")) is not int:
                raise ValidationError("Invalid history partition URL/year")
            blobs[entry["year"]] = read_bounded(path.parent / name, PARTITION_LIMIT)
        validate_history(manifest, blobs)
    else:
        manifest, _, validated_raw = load_history(path.parent, required=True)
        if raw != validated_raw:
            raise ValidationError("History manifest changed during validation; retry")
    last_day = parse_date(manifest["last_date"])
    if last_day >= as_of:
        raise ValidationError("History is ahead of --as-of")
    boundary = min(last_day + timedelta(days=1), as_of.replace(day=1))
    cutoff = shift(boundary.replace(day=1), -1)
    if read_bounded(path, MANIFEST_LIMIT) != raw:
        raise ValidationError("History manifest changed during validation; retry")
    return cutoff, raw


def publish(value, output, expected_raw):
    """Caller holds the directory lock; compare validated prior bytes before replace."""
    validate_snapshot(value)
    _, actual = load_snapshot(output)
    if actual != expected_raw:
        raise ValidationError("Stale trade snapshot; retry from fresh output")
    data = canonical_bytes(value)
    if data == actual:
        return False
    atomic_write(output, data)
    return True


def run(mode, as_of, output=OUTPUT, *, start_year=2019, end_year=None, reconcile=False,
        history_manifest=HISTORY_MANIFEST, client=None, nullable=True):
    started = time.monotonic()
    client = client or TradeClient(refresh=mode == "refresh")
    output, history_manifest = Path(output), Path(history_manifest)
    end_year = as_of.year if end_year is None else end_year
    metrics = {"mode": mode, "as_of": as_of.isoformat(), "status": "failed"}
    try:
        if mode not in ("backfill", "refresh") or not FIRST < as_of <= datetime.now(BERLIN).date():
            raise ValidationError("Invalid mode/as-of")
        if not 2019 <= start_year <= end_year <= as_of.year:
            raise ValidationError("Invalid backfill year range")
        if mode == "refresh" and reconcile:
            raise ValidationError("--reconcile belongs to backfill")
        if not output.parent.is_dir():
            raise ValidationError("Output parent must exist")
        with writer_lock(output.parent):
            previous, previous_raw = load_snapshot(output, required=mode == "refresh")
            cutoff, history_raw = history_cutoff(history_manifest, as_of)
            last = parse_month(previous["last_month"]) if previous else None
            if last and (last > cutoff or last.year > as_of.year):
                raise ValidationError("Refusing to move trade coverage backwards")
            retained = previous["rows"][:] if previous else []
            if mode == "refresh":
                correction_start = max(date(as_of.year, 1, 1), shift(cutoff, -2))
                if last < shift(correction_start, -1) or (
                    last.year < as_of.year and last != date(as_of.year - 1, 12, 1)
                ):
                    raise ValidationError("Trade gap/unfinished closed year; run backfill --start-year YEAR "
                                          "--end-year YEAR --reconcile before refresh")
                years = [as_of.year] if cutoff.year == as_of.year else []
            else:
                existing = {parse_month(row["month"]).year for row in retained}
                years = [year for year in range(start_year, min(end_year, cutoff.year) + 1)
                         if reconcile or year not in existing]
                if not previous and start_year != 2019:
                    raise ValidationError("Initial trade backfill must start in 2019")
            from .pipeline import ComponentUnavailable, ConsistencyError
            try:
                observations = fetch_monthly(client, years)
                recover_startup_months(client, observations, metrics)
            except ValidationError as exc:
                raise ComponentUnavailable(str(exc)) from exc
            metrics["fetch_seconds"] = round(time.monotonic() - started, 3)
            fresh = []
            for year in years:
                start = correction_start if mode == "refresh" else date(year, 1, 1)
                end = min(date(year, 12, 1), cutoff)
                try:
                    fresh.extend(make_rows(observations, start, end, metrics, nullable=nullable))
                except ConsistencyError:
                    raise
                except ValidationError as exc:
                    raise ComponentUnavailable(str(exc)) from exc
                retained = [row for row in retained if not start <= parse_month(row["month"]) <= end]
            # Emit v2 only when required by new gaps or retaining an existing v2.
            rows = sorted(retained + fresh, key=lambda row: row["month"])
            use_v2 = (previous and previous["schema_version"] == 2) or any(
                (parse_month(row["month"]), i) not in KNOWN_GAPS for row in rows for i in row["missing_series"])
            value = snapshot(rows, nullable=bool(use_v2))
            if last and parse_month(value["last_month"]) < last:
                raise ValidationError("Refusing to truncate trade coverage")
            if read_bounded(history_manifest, MANIFEST_LIMIT) != history_raw:
                raise ValidationError("History manifest changed before trade publication; retry")
            changed = publish(value, output, previous_raw)
            metrics.update(status="changed" if changed else "unchanged", years_fetched=years,
                           first_month=value["first_month"], last_month=value["last_month"], rows=len(value["rows"]),
                           null_months=[r["month"] for r in value["rows"] if r["missing_series"]],
                           export_bytes=len(canonical_bytes(value)), content_hash=value["content_hash"])
        return metrics
    finally:
        metrics.update(requests=client.requests, bytes_downloaded=client.bytes_downloaded,
                       total_seconds=round(time.monotonic() - started, 3))
        print(json.dumps(metrics, sort_keys=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    for mode in ("backfill", "refresh"):
        command = commands.add_parser(mode)
        command.add_argument("--as-of", type=date.fromisoformat, default=datetime.now(BERLIN).date())
        command.add_argument("--history-manifest", type=Path, default=HISTORY_MANIFEST)
        command.add_argument("--output", type=Path, default=OUTPUT)
        if mode == "backfill":
            command.add_argument("--start-year", type=int, default=2019)
            command.add_argument("--end-year", type=int)
            command.add_argument("--reconcile", action="store_true")
    try:
        run(**vars(parser.parse_args()))
    except (ValidationError, OSError) as exc:
        print(f"German electricity trade failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
