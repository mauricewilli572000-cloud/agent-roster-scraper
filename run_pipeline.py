"""Agent roster pipeline.

  python run_pipeline.py                 # download, parse, enrich, export
  python run_pipeline.py --file lic.xlsx # use a file you downloaded yourself
  python run_pipeline.py --inspect       # show how the file's columns were mapped
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

import config
import enrich_brokerages
import export
import parse_licenses
import rentcast_listings


def download(dest: Path) -> bool:
    import requests

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = requests.get(config.LICENSE_DB_URL, timeout=180,
                            headers={"User-Agent": config.USER_AGENT})
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"Download failed: {e}")
        return False
    dest.write_bytes(resp.content)
    print(f"Downloaded {len(resp.content):,} bytes -> {dest}")
    return True


def load_records() -> dict:
    try:
        rows = json.loads(config.RECORDS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return {r["id"]: r for r in rows}


def merge(existing: dict, fresh: list[dict], today: str) -> list[dict]:
    """Accumulate across runs: keep first_seen and any enriched contact info."""
    existing = {k: v for k, v in existing.items() if v.get("source") != "listing"}
    fresh_ids = set()
    for rec in fresh:
        fresh_ids.add(rec["id"])
        old = existing.get(rec["id"])
        if old:
            rec["first_seen"] = old.get("first_seen", today)
            for k in ("phone", "phone_source", "email", "enriched_at", "notes"):
                if old.get(k) and not rec.get(k):
                    rec[k] = old[k]
        else:
            rec["first_seen"] = today
        rec["last_seen"] = today
        rec["in_latest_file"] = True
        existing[rec["id"]] = rec
    for rid, rec in existing.items():
        if rid not in fresh_ids:
            rec["in_latest_file"] = False
    return sorted(existing.values(), key=lambda r: (r["county"], r["last_name"], r["first_name"]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="Use this local xlsx/csv instead of downloading")
    ap.add_argument("--inspect", action="store_true", help="Print column mapping and exit")
    ap.add_argument("--skip-download", action="store_true", help="Reuse data/raw/license_db.xlsx")
    ap.add_argument("--skip-enrich", action="store_true")
    ap.add_argument("--skip-listings", action="store_true",
                    help="Do not fetch RentCast listings (stored history is still scored)")
    args = ap.parse_args(argv)

    if args.file:
        src = Path(args.file)
    else:
        src = config.RAW_FILE
        if not args.skip_download and not download(src) and not src.exists():
            print("No license file available. Download it from the Division's "
                  "'Notice Regarding License Lookup' page and rerun with --file <path>.")
            return 2
    if not src.exists():
        print(f"File not found: {src}")
        return 2

    mapping, rows = parse_licenses.read_table(src)
    if args.inspect:
        print("Column mapping (our field -> file header):")
        for k, v in mapping.items():
            print(f"  {k:15} -> {v}")
        missing = [k for k in config.COLUMN_ALIASES if k not in mapping]
        print("Not found in file:", ", ".join(missing) or "none")
        print("\nFirst 3 data rows:")
        for row in list(__import__("itertools").islice(rows, 3)):
            print("  ", {k: v for k, v in row.items() if v not in (None, "")})
        return 0

    agents, stats, mapping = parse_licenses.parse_file(src)
    print("Parse stats:", dict(stats))
    if not agents:
        print("No agents matched the county/type/status filters; leaving existing data untouched.")
        d = parse_licenses.diagnose(src)
        print("DIAGNOSTICS (no personal data):")
        print("  rows:", d["rows"])
        print("  mapped:", d["mapping"])
        for h, c in d["columns"].items():
            print(f"  [{h}] filled={c['filled_pct']}% shapes={c['shapes']}")
            if c["values"] is not None:
                print(f"      values={c['values']}")
        return 3

    today = dt.date.today().isoformat()
    records = merge(load_records(), agents, today)

    listing_stats: Counter = Counter()
    if args.skip_listings:
        os.environ.pop("RENTCAST_API_KEY", None)
    records, listing_stats = rentcast_listings.run(records, today)
    if listing_stats:
        print("Listings stats:", dict(listing_stats))
        if listing_stats.get("skipped_no_api_key") and not listing_stats.get("listings_in_history"):
            print("  (set RENTCAST_API_KEY to fetch distressed-listing data)")

    enrich_stats: Counter = Counter()
    if not args.skip_enrich:
        enrich_stats = enrich_brokerages.run(records)
        if enrich_stats:
            print("Enrichment stats:", dict(enrich_stats))

    config.DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.RECORDS_FILE.write_text(json.dumps(records, indent=1), encoding="utf-8")
    by_county = Counter(r["county"] for r in records if r.get("in_latest_file"))
    meta = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source_sha256": hashlib.sha256(src.read_bytes()).hexdigest()[:16],
        "total": len(records),
        "in_latest_file": sum(1 for r in records if r.get("in_latest_file")),
        "with_phone": sum(1 for r in records if r.get("phone")),
        "with_email": sum(1 for r in records if r.get("email")),
        "distressed_agents": sum(1 for r in records if r.get("distressed_listings")),
        "listing_only_agents": sum(1 for r in records if r.get("source") == "listing"),
        "listing_stats": dict(listing_stats),
        "by_county": dict(by_county),
        "parse_stats": dict(stats),
        "enrich_stats": dict(enrich_stats),
        "column_mapping": mapping,
    }
    config.META_FILE.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    export.write_exports(records)
    print(f"Done: {meta['total']} agents ({meta['with_phone']} with phone). {dict(by_county)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
