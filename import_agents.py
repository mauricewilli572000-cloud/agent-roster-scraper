"""Import your own agent list (for example the homes.com list you built before) from a CSV.

Put it at data/imported_agents.csv. Column names are matched loosely:
  name (or first name + last name), phone/mobile/cell/direct, email, brokerage/company/office/agency,
  city, state, county, and anything else is ignored.
Imported agents are checked against the license file like everyone else, so agents whose license has
lapsed are visible, and they are scored if they also appear on listings that match your criteria.
"""
from __future__ import annotations

import csv
import hashlib
import re
from pathlib import Path

import config
import enrich_brokerages
import parse_licenses as pl

FIELD_ALIASES = {
    "name": ["name", "full name", "agent name", "agent", "realtor"],
    "first_name": ["first name", "first", "firstname"],
    "last_name": ["last name", "last", "lastname", "surname"],
    "phone": ["phone", "phone number", "mobile", "cell", "direct", "direct phone", "cell phone", "agent phone"],
    "email": ["email", "e mail", "email address", "e mail address", "agent email", "agent e mail"],
    "brokerage": ["brokerage", "company", "office", "agency", "broker", "brokerage name", "office name"],
    "city": ["city", "town"],
    "state": ["state", "st"],
    "county": ["county"],
}


def _map(headers: list[str]) -> dict[str, str]:
    normed = {h: pl.norm(h) for h in headers if h}
    out: dict[str, str] = {}
    for field, aliases in FIELD_ALIASES.items():
        for a in aliases:
            hit = next((h for h, n in normed.items() if n == a and h not in out.values()), None)
            if hit:
                out[field] = hit
                break
    return out


def county_for(city: str, county: str) -> tuple[str, str]:
    c = pl.norm_county(county)
    if c in config.TARGET_COUNTIES:
        return c, "import"
    guess = config.CITY_COUNTY.get(pl.norm(city))
    return (guess, "city-approx") if guess else ("", "")


def load(path: Path | None = None) -> list[dict]:
    path = Path(path or config.IMPORT_FILE)
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        m = _map(reader.fieldnames or [])
        out: dict[str, dict] = {}
        for row in reader:
            def g(field):
                return pl.clean(row.get(m[field])) if field in m else ""

            first, last = g("first_name"), g("last_name")
            if not (first or last):
                first, last = pl.split_name(g("name"))
            if not (first or last):
                continue
            state = g("state").upper()
            if state and state not in ("OH", "OHIO"):
                continue
            city = g("city").title()
            county, county_source = county_for(city, g("county"))
            phone = enrich_brokerages.norm_phone(g("phone"))
            key = f"{pl.norm(first)}|{pl.norm(last)}|{re.sub(chr(92) + 'D', '', phone)}"
            rid = "I-" + hashlib.sha1(key.encode()).hexdigest()[:10]
            new = {
                "id": rid, "source": "import", "license_number": "", "first_name": first,
                "last_name": last, "name": f"{first} {last}".strip(), "license_type": "",
                "status": "", "brokerage": g("brokerage"), "county": county,
                "county_source": county_source, "city": city, "state": "OH", "zip": "",
                "phone": phone, "email": g("email").lower(),
                "phone_source": "imported list" if phone else "", "expiration": "", "claimed": None,
            }
            old = out.get(rid)
            if old is None:
                out[rid] = new
            else:                                   # duplicate row: keep the first, fill its blanks
                for k, v in new.items():
                    if v and not old.get(k):
                        old[k] = v
    return list(out.values())
