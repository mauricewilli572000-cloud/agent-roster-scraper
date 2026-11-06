"""CSV exports: everything, and a GoHighLevel-ready file of agents with a phone."""
from __future__ import annotations

import csv

import config

GHL_FIELDS = ["First Name", "Last Name", "Phone", "Email", "Company Name",
              "City", "State", "Postal Code", "Tags", "Source", "Distress Score"]


def write_exports(records: list[dict]):
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    all_fields = ["id", "name", "license_type", "brokerage", "county", "county_source",
                  "city", "state", "zip", "phone", "phone_source", "email",
                  "expiration", "first_seen", "last_seen", "license_verified", "distress_score",
                  "listings_12mo", "distressed_listings", "price_cut_listings", "stale_listings",
                  "lead_match_listings", "last_listing"]
    with open(config.EXPORT_DIR / "agents_all.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)

    _write_ghl(config.EXPORT_DIR / "ghl_agents.csv", records, distressed_only=False)
    _write_ghl(config.EXPORT_DIR / "ghl_distressed_agents.csv", records, distressed_only=True)


def _write_ghl(path, records, distressed_only: bool):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=GHL_FIELDS)
        w.writeheader()
        rows = [r for r in records if r.get("phone")]
        if distressed_only:
            rows = [r for r in rows if (r.get("distress_score") or 0) >= config.GHL_MIN_SCORE]
            rows.sort(key=lambda r: -(r.get("distress_score") or 0))
        for r in rows:
            tags = ["real-estate-agent", f'{r["county"].lower()}-county']
            if (r.get("distress_score") or 0) >= config.GHL_MIN_SCORE:
                tags.append("distressed-seller")
            if r.get("license_verified") is False:
                tags.append("license-unverified")
            src = "RentCast listings" if r.get("source") == "listing" else "OH Division of Real Estate license file"
            w.writerow({
                "First Name": r["first_name"], "Last Name": r["last_name"],
                "Phone": r["phone"], "Email": r.get("email", ""),
                "Company Name": r.get("brokerage", ""), "City": r.get("city", ""),
                "State": r.get("state", ""), "Postal Code": r.get("zip", ""),
                "Tags": ",".join(tags), "Source": src,
                "Distress Score": r.get("distress_score") or 0,
            })
