"""Check agents against the Ohio Division of Real Estate daily license file.

The file has license number, name, status, license type, expiration and the brokerage an agent is
affiliated with (Primary Licensee). It has NO city, state, county, phone or email, so it cannot be a
roster of local agents. It answers one question: does this person hold an active license, and with whom?
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import parse_licenses as pl
import rentcast_listings as rl


def build_index(path) -> tuple[dict[tuple, list[dict]], Counter]:
    """Active salespeople and brokers from the license file, keyed by (first name, last name)."""
    mapping, rows = pl.read_table(path)
    stats: Counter = Counter()
    index: dict[tuple, list[dict]] = {}

    def g(raw, field):
        h = mapping.get(field)
        return pl.clean(raw.get(h)) if h else ""

    for raw in rows:
        stats["rows"] += 1
        if pl.classify(g(raw, "license_type")) != "agent":
            stats["skipped_type"] += 1
            continue
        status = g(raw, "status")
        if status and not pl.is_active(status):
            stats["skipped_status"] += 1
            continue
        first, last = g(raw, "first_name"), g(raw, "last_name")
        if not (first and last):
            # newer licenses often have no legacy name; the account name is the person's name
            first, last = pl.split_name(g(raw, "claimed"))
        if not (first and last):
            stats["skipped_no_name"] += 1
            continue
        index.setdefault(rl.name_key(first, last), []).append({
            "license_number": g(raw, "license_number"),
            "license_type": g(raw, "license_type"),
            "brokerage": g(raw, "brokerage"),
            "expiration": g(raw, "expiration"),
        })
        stats["indexed"] += 1
    return index, stats


def verify(rec: dict, index: dict) -> str:
    """Set license_* fields on a record. Returns 'verified', 'ambiguous' or 'not found'."""
    cands = index.get(rl.name_key(rec.get("first_name", ""), rec.get("last_name", "")), [])
    pick = None
    if len(cands) == 1:
        pick = cands[0]
    elif len(cands) > 1:
        same_office = [c for c in cands if rl.brokerage_similar(c["brokerage"], rec.get("brokerage", ""))]
        if len(same_office) == 1:
            pick = same_office[0]
    if pick:
        check = "verified"
    else:
        check = "ambiguous" if cands else "not found"
    rec["license_check"] = check
    rec["license_verified"] = check == "verified"
    rec["license_number"] = pick["license_number"] if pick else ""
    rec["license_expires"] = pick["expiration"] if pick else ""
    rec["license_brokerage"] = pick["brokerage"] if pick else ""
    if pick:
        rec["license_type"] = pick["license_type"]
        if not rec.get("brokerage"):
            rec["brokerage"] = pick["brokerage"]
    return check


def verify_all(records: list[dict], index: dict) -> Counter:
    stats: Counter = Counter()
    for r in records:
        stats[verify(r, index)] += 1
    return stats
