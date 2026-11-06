"""Parse the Division's license database (xlsx or csv) into agent records."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import itertools
import re
from collections import Counter
from pathlib import Path

import config


def norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def map_columns(headers) -> dict:
    """Map our field names to the file's actual header strings."""
    normed = {h: norm(h) for h in headers if h and str(h).strip()}
    mapping: dict[str, str] = {}
    used: set[str] = set()

    def find(field, exact_pass):
        for alias in config.COLUMN_ALIASES[field]:
            is_exact = alias.startswith("=")
            if exact_pass != is_exact:
                continue
            pat = alias.lstrip("=")
            for h, n in normed.items():
                if h in used:
                    continue
                if (n == pat) if is_exact else (pat in n):
                    mapping[field] = h
                    used.add(h)
                    return

    for field in config.COLUMN_ALIASES:
        find(field, exact_pass=True)
    for field in config.COLUMN_ALIASES:
        if field not in mapping:
            find(field, exact_pass=False)
    return mapping


def _sheets(path: Path):
    if path.suffix.lower() in (".csv", ".txt"):
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
            yield csv.reader(f)
        return
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            yield (list(r) for r in ws.iter_rows(values_only=True))
    finally:
        wb.close()


def read_table(path):
    """Return (mapping, iterator of {header: value}) for the first sheet with a usable header."""
    path = Path(path)
    for sheet in _sheets(path):
        it = iter(sheet)
        buf = list(itertools.islice(it, 15))
        for i, row in enumerate(buf):
            headers = ["" if c is None else str(c) for c in row]
            m = map_columns(headers)
            has_id = any(k in m for k in ("license_number", "last_name", "full_name"))
            if len(m) >= 3 and has_id:
                rest = itertools.chain(buf[i + 1:], it)

                def rows(headers=headers, rest=rest):
                    for r in rest:
                        yield {h: v for h, v in zip(headers, r) if h.strip()}

                return m, rows()
    raise ValueError(
        "Could not find a header row with recognisable columns. "
        "Run with --inspect and add aliases to COLUMN_ALIASES in config.py."
    )


def clean(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dt.datetime, dt.date)):
        return v.date().isoformat() if isinstance(v, dt.datetime) else v.isoformat()
    return re.sub(r"\s+", " ", str(v)).strip()


def classify(license_type: str):
    t = license_type.lower()
    if any(k in t for k in config.EXCLUDE_TYPES):
        return None
    if any(k in t for k in config.BROKERAGE_TYPES):
        return "brokerage"
    if any(k in t for k in config.AGENT_TYPES):
        return "agent"
    return None


def is_active(status: str) -> bool:
    s = status.lower()
    return "active" in s and "inactive" not in s


def zip5(z: str) -> str:
    m = re.match(r"\d{5}", z.strip())
    return m.group(0) if m else ""


def county_from_zip(z: str) -> str:
    z = zip5(z)
    if not z:
        return ""
    return config.ZIP_COUNTY.get(z) or config.ZIP_PREFIX_COUNTY.get(z[:3], "")


def norm_county(c: str) -> str:
    return re.sub(r"\bcounty\b", "", c, flags=re.I).strip().title()


def split_name(full: str):
    full = full.strip()
    if "," in full:
        last, first = [p.strip() for p in full.split(",", 1)]
    else:
        parts = full.split()
        first, last = (" ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else ("", full)
    return first, last


def make_id(rec: dict) -> str:
    if rec["license_number"]:
        return rec["license_number"].upper()
    key = f'{rec["last_name"]}|{rec["first_name"]}|{zip5(rec["zip"])}'.lower()
    return "N-" + hashlib.sha1(key.encode()).hexdigest()[:10]


def diagnose(path, top: int = 12) -> dict:
    """Headers, how they were mapped, and the most common VALUES of the filter columns.
    Deliberately prints no names or contact details, so it is safe in public Actions logs."""
    mapping, rows = read_table(path)
    headers = list(next(iter(read_table(path)[1]), {}).keys())
    counters = {f: Counter() for f in ("license_type", "status", "county") if f in mapping}
    zip_prefix: Counter = Counter()
    n = 0
    for raw in rows:
        n += 1
        for f, c in counters.items():
            c[clean(raw.get(mapping[f])) or "(blank)"] += 1
        if "zip" in mapping:
            zip_prefix[zip5(clean(raw.get(mapping["zip"])))[:3] or "(blank)"] += 1
    return {"rows": n, "headers": headers, "mapping": mapping,
            "values": {f: c.most_common(top) for f, c in counters.items()},
            "zip_prefix": zip_prefix.most_common(top) if zip_prefix else None}


def parse_file(path):
    """Return (agents, stats, mapping)."""
    mapping, rows = read_table(path)
    stats: Counter = Counter()
    agents: list[dict] = []
    warn_no_type = "license_type" not in mapping

    for raw in rows:
        stats["rows"] += 1

        def g(field):
            h = mapping.get(field)
            return clean(raw.get(h)) if h else ""

        license_type = g("license_type")
        kind = classify(license_type) if license_type else "agent"
        if kind == "brokerage":
            stats["brokerages_seen"] += 1
            continue
        if kind != "agent":
            stats["skipped_type"] += 1
            continue

        status = g("status")
        if status and not is_active(status):
            stats["skipped_status"] += 1
            continue

        first, last = g("first_name"), g("last_name")
        if not (first or last):
            first, last = split_name(g("full_name"))
        if not (first or last):
            stats["skipped_no_name"] += 1
            continue

        county = norm_county(g("county"))
        county_source = "column"
        if not county:
            county = county_from_zip(g("zip"))
            county_source = "zip-approx"
        if county not in config.TARGET_COUNTIES:
            stats["skipped_county"] += 1
            continue

        rec = {
            "license_number": g("license_number"),
            "first_name": first,
            "last_name": last,
            "name": f"{first} {last}".strip(),
            "license_type": license_type,
            "status": status,
            "brokerage": g("brokerage"),
            "county": county,
            "county_source": county_source,
            "city": g("city"),
            "state": g("state"),
            "zip": zip5(g("zip")),
            "phone": g("phone"),
            "email": g("email"),
            "phone_source": "license-file" if g("phone") else "",
            "expiration": g("expiration"),
            "claimed": bool(g("claimed")) if "claimed" in mapping else None,
        }
        rec["id"] = make_id(rec)
        agents.append(rec)
        stats["kept"] += 1

    if warn_no_type:
        stats["warning_no_license_type_column"] = 1
    return agents, stats, mapping
