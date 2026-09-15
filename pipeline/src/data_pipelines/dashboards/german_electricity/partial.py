"""Recent v2: bounded independent series acquisition and honest last-good retention.

Success means a structurally complete source window, including explicit JSON nulls.
An omitted timestamp, malformed payload, or transport failure is not a null observation.
No operational check clock enters semantic metadata; success is a source-window cutoff.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import time

from . import pipeline as p


def fetch_series(client, as_of, column):
    start_day = as_of - timedelta(days=p.FETCH_DAYS)
    if start_day < p.MIN_DATE:
        raise p.ValidationError("The entire fetch window must be post-2023")
    weeks = p.required_weeks(start_day, as_of)
    series_id = p.SERIES[column]
    base = f"https://www.smard.de/app/chart_data/{series_id}/DE"
    payload = client.get(f"{base}/index_hour.json")
    stamps = payload.get("timestamps") if isinstance(payload, dict) else None
    if (not isinstance(stamps, list) or not stamps or any(type(t) is not int for t in stamps)
            or len(set(stamps)) != len(stamps) or not set(weeks).issubset(stamps)):
        raise p.ValidationError(f"{column}: invalid index or unavailable week")
    result = {}
    for week in weeks:
        p.parse_week(client.get(f"{base}/{series_id}_DE_hour_{week}.json"), week,
                     p.midnight_ms(start_day), p.midnight_ms(as_of), column, result)
    return result


def source_window(points, as_of, column):
    for lag in range(p.MAX_LAG_DAYS):
        end_day = as_of - timedelta(days=lag)
        end = p.midnight_ms(end_day)
        last_start = p.midnight_ms(end_day - timedelta(days=1))
        if all(t in points for t in range(last_start, end, p.HOUR_MS)):
            start = p.midnight_ms(end_day - timedelta(days=p.DISPLAY_DAYS))
            for stamp in range(start, end, p.HOUR_MS):
                if stamp not in points:
                    raise p.ValidationError(f"{column}: omitted internal hour")
                if points[stamp] is not None:
                    p.number(points[stamp], column, power_scale=1000)
            return start, end
    raise p.ValidationError(f"{column}: no structurally complete source day within four days")


def acquire(client, as_of):
    def one(column):
        try:
            points = fetch_series(client, as_of, column)
            start, end = source_window(points, as_of, column)
            return column, (points, start, end), None
        except p.ValidationError as exc:
            return column, None, str(exc)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(one, p.SERIES))
    return ({column: value for column, value, error in results if value is not None},
            {column: error for column, value, error in results if error is not None})


def metadata(previous, column):
    if previous is None:
        return {"last_successful_window_end": None, "source_observed_through": None}
    if previous["schema_version"] == 2:
        return {key: previous["components"][column][key]
                for key in ("last_successful_window_end", "source_observed_through")}
    return {"last_successful_window_end": previous["window_end"],
            "source_observed_through": previous["window_end"]}


def align(successes, previous):
    ends = [value[2] for value in successes.values()]
    if previous:
        ends.append(p.utc_ms(previous["window_end"]))
    if not ends:
        raise p.ValidationError("All recent sources unavailable and no validated previous snapshot")
    end = max(ends)
    end_day = datetime.fromtimestamp(end / 1000, p.BERLIN).date()
    start = p.midnight_ms(end_day - timedelta(days=p.DISPLAY_DAYS))
    stamps = list(range(start, end, p.HOUR_MS))
    aligned, components = {}, {}
    for index, column in enumerate(p.SERIES, 1):
        meta = metadata(previous, column)
        if column in successes:
            points, source_start, source_end = successes[column]
            # A truncated source must not overwrite a newer validated component.
            if meta["last_successful_window_end"] and source_end < p.utc_ms(meta["last_successful_window_end"]):
                raise p.ValidationError(f"{column}: source coverage regressed")
            values = {stamp: points.get(stamp) if source_start <= stamp < source_end else None for stamp in stamps}
            numeric = [stamp for stamp, value in points.items() if value is not None and source_start <= stamp < source_end]
            meta = {"last_successful_window_end": p.iso_utc(source_end),
                    "source_observed_through": p.iso_utc(max(numeric) + p.HOUR_MS) if numeric else None}
            status = "complete" if all(v is not None for v in values.values()) else "partial"
        else:
            # Previous values are GW; staging accepts source MWh for energy.
            old = {row[0]: row[index] for row in previous["rows"]} if previous else {}
            scale = 1 if column == "price" else 1000
            values = {stamp: old[stamp] * scale if old.get(stamp) is not None else None for stamp in stamps}
            status = "stale" if meta["last_successful_window_end"] else "unavailable"
        aligned[column] = values
        components[column] = {**meta, "status": status, "known_hours": sum(v is not None for v in values.values()),
                              "expected_hours": len(stamps)}
    return aligned, components, start, end


def validate_components(snapshot):
    components = snapshot["components"]
    if not isinstance(components, dict) or set(components) != set(p.SERIES):
        raise p.ValidationError("Invalid component series")
    end = p.utc_ms(snapshot["window_end"])
    start = p.utc_ms(snapshot["window_start"])
    for index, column in enumerate(p.SERIES, 1):
        meta = components[column]
        if not isinstance(meta, dict) or set(meta) != {"status", "last_successful_window_end", "source_observed_through", "known_hours", "expected_hours"}:
            raise p.ValidationError("Invalid component metadata")
        known = sum(row[index] is not None for row in snapshot["rows"])
        if (type(meta["known_hours"]) is not int or meta["known_hours"] != known
                or type(meta["expected_hours"]) is not int or meta["expected_hours"] != len(snapshot["rows"])):
            raise p.ValidationError("Component coverage mismatch")
        status, success, observed = meta["status"], meta["last_successful_window_end"], meta["source_observed_through"]
        if status not in ("complete", "partial", "stale", "unavailable"):
            raise p.ValidationError("Invalid component status")
        if (status == "complete" and known != len(snapshot["rows"])) or (status == "partial" and known == len(snapshot["rows"])):
            raise p.ValidationError("Component completeness mismatch")
        if success is None:
            if status != "unavailable" or observed is not None or known:
                raise p.ValidationError("Unavailable component claims observations")
        else:
            boundary = p.utc_ms(success)
            local = datetime.fromtimestamp(boundary / 1000, p.BERLIN)
            if boundary > end or boundary % p.HOUR_MS or local.hour or local.minute or status == "unavailable":
                raise p.ValidationError("Invalid component success cutoff")
            if observed is not None and (p.utc_ms(observed) > boundary or p.utc_ms(observed) % p.HOUR_MS):
                raise p.ValidationError("Invalid component observation cutoff")
            numeric = [row[0] + p.HOUR_MS for row in snapshot["rows"] if row[index] is not None]
            observed_ms = p.utc_ms(observed) if observed is not None else None
            if numeric:
                if observed_ms != numeric[-1]:
                    raise p.ValidationError("Component observation cutoff must equal last numeric interval")
            elif observed_ms is not None and observed_ms > start:
                # An exclusive cutoff at/before start describes a retained value
                # that rolled out of the grid. A cutoff inside it requires a value.
                raise p.ValidationError("Component observation cutoff claims absent displayed value")
    statuses = snapshot["refresh_status"]
    if not isinstance(statuses, dict) or not set(statuses).issubset({"history", "trade"}):
        raise p.ValidationError("Invalid refresh status components")
    for key, value in statuses.items():
        if (not isinstance(value, dict) or set(value) != {"status", "data_through"}
                or value["status"] not in ("ok", "partial", "stale")):
            raise p.ValidationError("Invalid refresh status")
        text = value["data_through"]
        from datetime import date
        try:
            parsed = date.fromisoformat(text if key == "history" else text + "-01")
        except (ValueError, TypeError) as exc:
            raise p.ValidationError("Invalid refresh cutoff") from exc
        if (parsed.isoformat() if key == "history" else parsed.isoformat()[:7]) != text:
            raise p.ValidationError("Noncanonical refresh cutoff")


def refresh_recent(as_of, output, client=None):
    from .history import strict_json, read_bounded
    if as_of > datetime.now(p.BERLIN).date() or as_of - timedelta(days=p.FETCH_DAYS) < p.MIN_DATE:
        raise p.ValidationError("Invalid as-of date")
    client = client or p.SmardClient()
    started = time.monotonic()
    original = read_bounded(output, p.MAX_EXPORT_BYTES) if output.exists() or output.is_symlink() else None
    previous = strict_json(original) if original is not None else None
    if previous:
        p.validate_snapshot(previous)
    # Use strict duplicate-key parsing for this client too, without a module import cycle.
    client.JSON_DECODER = strict_json
    successes, failures = acquire(client, as_of)
    # Coverage regressions are isolated like malformed source payloads.
    for column, (_, _, end) in list(successes.items()):
        old_end = metadata(previous, column)["last_successful_window_end"]
        if old_end and end < p.utc_ms(old_end):
            del successes[column]
            failures[column] = "source coverage regressed"
    aligned, components, start, end = align(successes, previous)
    with tempfile.TemporaryDirectory(prefix="databearer-electricity-") as temporary:
        rows = p.build_curated(aligned, start, end, Path(temporary))
    if previous:
        prior_rows = {row[0]: row for row in previous["rows"]}
        for row in rows:
            for index, column in enumerate(p.SERIES, 1):
                if column not in successes:
                    row[index] = prior_rows[row[0]][index] if row[0] in prior_rows else None
    snapshot = p.make_snapshot(rows, start, end, components=components,
                               refresh_status=previous.get("refresh_status", {}) if previous else {})
    from .history import writer_lock
    with writer_lock(output.parent):
        current = read_bounded(output, p.MAX_EXPORT_BYTES) if output.exists() or output.is_symlink() else None
        if current != original:
            raise p.ValidationError("Recent snapshot changed while refreshing; retry")
        changed, size = p.publish_snapshot(snapshot, output)
    metrics = {"status": "changed" if changed else "unchanged", "failures": failures,
               "components": components, "export_bytes": size, "rows": len(rows),
               "requests": client.requests, "bytes_downloaded": client.bytes_downloaded,
               "total_seconds": round(time.monotonic() - started, 3)}
    print(json.dumps(metrics, sort_keys=True), flush=True)
    return metrics
