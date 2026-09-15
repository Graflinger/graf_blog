"""Coordinated daily refresh: isolate upstream failures, validate the candidate bundle.

All work is staged outside tracked exports. Shared validation/dbt/storage failures
and available overlap contradictions remain hard publication failures. Frontend
tests/lint/build and the release publisher are additional mandatory workflow gates.
"""

import argparse
from contextlib import ExitStack
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from . import pipeline as p, history as h, trade


def promote(snapshot, manifest, partitions, candidate_trade, recent, directory, trade_path,
            original_recent, original_manifest, original_trade, year):
    """Handled write failures roll back the local bundle; Git/build gates protect kills.

    Never remove pre-existing unreferenced versions while a multi-file transaction is
    pending. The recent snapshot, which embeds component status, is promoted last.
    """
    with ExitStack() as locks:
        for parent in sorted({recent.parent.resolve(), trade_path.parent.resolve(), directory.resolve()}):
            locks.enter_context(h.writer_lock(parent))
        if (recent.read_bytes() != original_recent or trade_path.read_bytes() != original_trade
                or (directory / "manifest.json").read_bytes() != original_manifest):
            raise p.ValidationError("Bundle changed while refreshing; retry")
        existing = {path.name for path in directory.iterdir()}
        history_changed = p.canonical_bytes(manifest) != original_manifest
        try:
            h.publish_history(directory, manifest, partitions, original_manifest, cleanup_years=set())
            if candidate_trade != original_trade:
                h.atomic_write(trade_path, candidate_trade)
            p.publish_snapshot(snapshot, recent)
        except BaseException:
            # Atomic writers leave old bytes on individual replace failures. Restore
            # only files actually advanced before the failure, keeping all old blobs.
            for path, raw in ((recent, original_recent), (trade_path, original_trade),
                              (directory / "manifest.json", original_manifest)):
                if path.read_bytes() != raw:
                    h.atomic_write(path, raw)
            for path in directory.iterdir():
                if path.name not in existing and h.VERSION_FILE.fullmatch(path.name):
                    path.unlink()
            raise
        # Same retention policy as history: new and immediately preceding references.
        if not history_changed:
            return
        keep = {Path(entry["url"]).name for entry in manifest["years"]}
        keep.update(Path(entry["url"]).name for entry in h.strict_json(original_manifest)["years"])
        for path in directory.iterdir():
            if h.VERSION_FILE.fullmatch(path.name) and path.name.startswith(f"{year}.") and path.name not in keep:
                try:
                    path.unlink()
                except OSError:
                    print(f"Bundle published; retention cleanup deferred: {path.name}")


def bundle(recent, directory, trade_path, *, check_status=True, allow_advanced_status=False):
    snapshot = h.strict_json(h.read_bounded(recent, p.MAX_EXPORT_BYTES))
    p.validate_snapshot(snapshot)
    manifest, partitions, _ = h.load_history(directory, required=True)
    h.compare_hourly([row for part in partitions.values() for row in part["rows"]], snapshot)
    trade_snapshot, _ = trade.load_snapshot(trade_path, required=True)
    # Trade's own cutoff reader verifies every history partition too.
    cutoff, _ = trade.history_cutoff(directory / "manifest.json", datetime.now(p.BERLIN).date())
    if trade.parse_month(trade_snapshot["last_month"]) > cutoff:
        raise p.ValidationError("Trade extends beyond complete history months")
    if check_status:
        for key, actual in (("history", manifest["last_date"]), ("trade", trade_snapshot["last_month"])):
            meta = snapshot.get("refresh_status", {}).get(key)
            if meta and meta["data_through"] != actual:
                # Standalone validated repairs can advance an export without editing
                # recent metadata. Permit only that direction on coordinator entry;
                # all hashes, schemas, overlap and trade/history checks still apply.
                if not allow_advanced_status or meta["data_through"] > actual:
                    raise p.ValidationError(f"{key}: component status cutoff mismatch")
    return snapshot, manifest, partitions, trade_snapshot


def run(as_of, recent=p.DEFAULT_OUTPUT, directory=h.DEFAULT_DIRECTORY, trade_path=trade.OUTPUT):
    recent, directory, trade_path = Path(recent), Path(directory), Path(trade_path)
    if as_of > datetime.now(p.BERLIN).date() or as_of - p.timedelta(days=p.FETCH_DAYS) < p.MIN_DATE:
        raise p.ValidationError("Invalid as-of date")
    # Existing corruption is a shared hard error, never an upstream fallback.
    bundle(recent, directory, trade_path, allow_advanced_status=True)
    original_recent = recent.read_bytes()
    original_trade = trade_path.read_bytes()
    original_manifest = (directory / "manifest.json").read_bytes()
    failures, hard = {}, []
    with tempfile.TemporaryDirectory(prefix="electricity-bundle-") as temporary:
        root = Path(temporary)
        candidate_recent, candidate_trade = root / "recent.json", root / "trade.json"
        candidate_history = root / "history"
        shutil.copyfile(recent, candidate_recent)
        shutil.copyfile(trade_path, candidate_trade)
        shutil.copytree(directory, candidate_history)
        try:
            p.run(as_of, candidate_recent)
        except (p.ValidationError, OSError, subprocess.SubprocessError) as exc:
            # Source errors are handled per series by recent; an exception here is shared.
            hard.append(exc)
        for name, operation in (
            ("history", lambda: h.run("refresh", as_of, candidate_history, snapshot_path=candidate_recent, independent=True)),
            ("trade", lambda: trade.run("refresh", as_of, output=candidate_trade, history_manifest=candidate_history / "manifest.json")),
        ):
            try:
                operation()
            except p.ComponentUnavailable as exc:
                failures[name] = str(exc)
            except (p.ValidationError, OSError, subprocess.SubprocessError) as exc:
                hard.append(exc)
        if hard:
            raise p.ValidationError(f"Shared refresh failure: {hard}")
        snapshot, manifest, partitions, trade_snapshot = bundle(candidate_recent, candidate_history, candidate_trade, check_status=False)
        if snapshot["schema_version"] != 2:
            raise p.ValidationError("Coordinated refresh requires a v2 recent candidate")
        history_partial = any(row['price_eur_mwh'] is None or any(value is None for value in row['energy_gwh'].values())
                              for row in partitions[max(partitions)]['rows'])
        trade_partial = any(row['missing_series'] for row in trade_snapshot['rows'] if row['month'].startswith(str(as_of.year)))
        snapshot["refresh_status"] = {
            "history": {"status": "stale" if "history" in failures else "partial" if history_partial else "ok", "data_through": manifest["last_date"]},
            "trade": {"status": "stale" if "trade" in failures else "partial" if trade_partial else "ok", "data_through": trade_snapshot["last_month"]},
        }
        snapshot["content_hash"] = hashlib.sha256(p.canonical_bytes(p.semantic_content(snapshot))).hexdigest()
        snapshot["snapshot_created_at"] = p.iso_utc(int(p.time.time() * 1000))
        p.validate_snapshot(snapshot)
        p.publish_snapshot(snapshot, candidate_recent)
        bundle(candidate_recent, candidate_history, candidate_trade)
        # Directory locks and byte guards prevent stale local writers. Git publication
        # remains a separate complete-build gate and serializes cross-runner updates.
        promote(snapshot, manifest, partitions, candidate_trade.read_bytes(), recent, directory, trade_path,
                original_recent, original_manifest, original_trade, as_of.year)
        report = {"components": snapshot["components"], "refresh_status": snapshot["refresh_status"], "failures": failures}
        print(json.dumps(report, sort_keys=True), flush=True)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
                handle.write("## Electricity component coverage\n\n| Component | Status | Coverage / cutoff |\n|---|---|---|\n")
                for key, meta in snapshot["components"].items():
                    handle.write(f"| {key} | {meta['status']} | {meta['known_hours']}/{meta['expected_hours']} hours; {meta['source_observed_through']} |\n")
                for key, meta in snapshot["refresh_status"].items():
                    handle.write(f"| {key} | {meta['status']} | {meta['data_through']} |\n")
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat, default=datetime.now(p.BERLIN).date())
    parser.add_argument("--output", type=Path, default=p.DEFAULT_OUTPUT)
    parser.add_argument("--history-directory", type=Path, default=h.DEFAULT_DIRECTORY)
    parser.add_argument("--trade-output", type=Path, default=trade.OUTPUT)
    args = parser.parse_args()
    run(args.as_of, args.output, args.history_directory, args.trade_output)


if __name__ == "__main__":
    main()
