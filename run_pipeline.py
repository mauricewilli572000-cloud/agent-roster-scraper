"""Agent roster pipeline.

  python run_pipeline.py                  # build the roster, score listings, verify licenses, export
  python run_pipeline.py --file lic.xlsx  # use a license file you downloaded yourself
  python run_pipeline.py --inspect        # show how the license file's columns were mapped

Where agents come from:
  * data/imported_agents.csv      your own list (optional)
  * RentCast listings             agents whose listings are distressed / under market / 120+ days
Where the license file fits: it has no city, county, phone or email, so it only VERIFIES agents
(active license? which brokerage?). It is never the source of the roster.
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
import import_agents
import license_check
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
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {r["id"]: r for r in rows}


def carry_over(records: list[dict], previous: dict, today: str):
    """Keep first_seen and any phone/email found on an earlier run (e.g. from a brokerage page)."""
    for r in records:
        old = previous.get(r["id"], {})
        r["first_seen"] = old.get("first_seen") or r.get("first_seen") or today
        r["last_seen"] = today
        for k in ("phone", "phone_source", "email", "enriched_at", "notes"):
            if old.get(k) and not r.get(k):
                r[k] = old[k]


def print_profile(d: dict):
    print("DIAGNOSTICS (no personal data):")
    print("  rows:", d["rows"])
    print("  mapped:", d["mapping"])
    for h, c in d["columns"].items():
        print(f"  [{h}] filled={c['filled_pct']}% shapes={c['shapes']}")
        if c["values"] is not None:
            print(f"      values={c['values']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="Use this local xlsx/csv instead of downloading")
    ap.add_argument("--inspect", action="store_true", help="Print column mapping and exit")
    ap.add_argument("--skip-download", action="store_true", help="Reuse data/raw/license_db.xlsx")
    ap.add_argument("--skip-enrich", action="store_true")
    ap.add_argument("--skip-listings", action="store_true",
                    help="Do not fetch RentCast listings (stored history is still scored)")
    args = ap.parse_args(argv)

    src = Path(args.file) if args.file else config.RAW_FILE
    if not args.file and not args.skip_download and not download(src) and not src.exists():
        print("WARNING: no license file available; licenses will not be verified this run.")
    have_file = src.exists()

    if args.inspect:
        if not have_file:
            print(f"File not found: {src}")
            return 2
        print_profile(parse_licenses.diagnose(src))
        return 0

    today = dt.date.today().isoformat()

    # 1. agents: your own list, then the RentCast criteria
    records = import_agents.load()
    print(f"Imported agents: {len(records)}")
    if args.skip_listings:
        os.environ.pop("RENTCAST_API_KEY", None)
    records, listing_stats = rentcast_listings.run(records, today)
    if listing_stats:
        print("Listings stats:", dict(listing_stats))
        if listing_stats.get("skipped_no_api_key") and not listing_stats.get("listings_in_history"):
            print("  (listings not fetched: no API key / switch off)")
    carry_over(records, load_records(), today)

    # 2. verify against the license file
    check_stats: Counter = Counter()
    index_stats: Counter = Counter()
    if have_file:
        try:
            index, index_stats = license_check.build_index(src)
            print("License index:", dict(index_stats))
            if not index:
                print("WARNING: the license file produced no usable agents; showing its profile.")
                print_profile(parse_licenses.diagnose(src))
            check_stats = license_check.verify_all(records, index)
            print("License check:", dict(check_stats))
        except ValueError as e:
            print(f"WARNING: could not read the license file ({e}); licenses not verified.")

    # 3. phones/emails from brokerage pages, where configured
    enrich_stats: Counter = Counter()
    if not args.skip_enrich:
        enrich_stats = enrich_brokerages.run(records)
        if enrich_stats:
            print("Enrichment stats:", dict(enrich_stats))

    records.sort(key=lambda r: (-(r.get("distress_score") or 0), r.get("last_name", ""), r.get("first_name", "")))
    config.DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.RECORDS_FILE.write_text(json.dumps(records, indent=1), encoding="utf-8")
    by_county = Counter(r.get("county") or "(no county)" for r in records)
    meta = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "license_file_sha256": hashlib.sha256(src.read_bytes()).hexdigest()[:16] if have_file else "",
        "total": len(records),
        "with_phone": sum(1 for r in records if r.get("phone")),
        "with_email": sum(1 for r in records if r.get("email")),
        "license_verified": sum(1 for r in records if r.get("license_check") == "verified"),
        "distressed_agents": sum(1 for r in records if r.get("distressed_listings")),
        "imported_agents": sum(1 for r in records if r.get("source") == "import"),
        "listing_agents": sum(1 for r in records if r.get("source") == "listing"),
        "listing_stats": dict(listing_stats),
        "license_index": dict(index_stats),
        "license_check": dict(check_stats),
        "by_county": dict(by_county),
        "enrich_stats": dict(enrich_stats),
    }
    config.META_FILE.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    export.write_exports(records)
    if not records:
        print("No agents yet: add data/imported_agents.csv and/or run with listings enabled "
              "(RENTCAST_API_KEY).")
    print(f"Done: {meta['total']} agents ({meta['with_phone']} with phone, "
          f"{meta['license_verified']} license-verified). {dict(by_county)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
