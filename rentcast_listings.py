"""RentCast sale listings -> per-agent distressed-listing stats.

Each run:
  1. (if RENTCAST_API_KEY is set) fetch sale listings for the target counties and merge
     them into data/listings_history.json, which accumulates across nightly runs;
  2. score every listing agent over the last SCORE_WINDOW_DAYS;
  3. attach the stats to matching license-roster records, and add agents who are not in
     the license file (marked license_verified=False) once their score is high enough.

Listing-only records are rebuilt from the history on every run, so they never go stale.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import re
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

import config
import enrich_brokerages
import parse_licenses

API_URL = "https://api.rentcast.io/v1/listings/sale"
DISTRESS_TYPES = {"foreclosure", "short sale"}
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
BRAND_STOPWORDS = {"the", "of", "and", "llc", "inc", "co", "company", "group", "realty", "real",
                   "estate", "homes", "home", "properties", "associates", "team"}


class RentCastError(Exception):
    pass


class AuthError(RentCastError):
    pass


# --- helpers ----------------------------------------------------------------

def _clean(v) -> str:
    return re.sub(r"\s+", " ", str(v)).strip() if v else ""


def _tokens(s: str) -> list[str]:
    return [t for t in parse_licenses.norm(s).split() if t not in NAME_SUFFIXES]


def name_key(first: str, last: str) -> tuple[str, str]:
    """Matching key: first word of the first name + last word of the last name."""
    f, l = _tokens(first), _tokens(last)
    return (f[0] if f else "", l[-1] if l else "")


def split_agent_name(full: str) -> tuple[str, str]:
    # drop a trailing generational suffix first, or "Jr." would be read as the last name
    full = re.sub(r"[,\s]+(jr|sr|ii|iii|iv)\.?\s*$", "", full.strip(), flags=re.I)
    return parse_licenses.split_name(full)


def brand_tokens(s: str) -> set[str]:
    return {t for t in parse_licenses.norm(s).split() if t not in BRAND_STOPWORDS}


def brokerage_similar(a: str, b: str) -> bool:
    ta, tb = brand_tokens(a), brand_tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.5


_SUFFIX = {"street": "st", "avenue": "ave", "road": "rd", "drive": "dr", "lane": "ln",
           "court": "ct", "boulevard": "blvd", "place": "pl", "circle": "cir", "terrace": "ter",
           "north": "n", "south": "s", "east": "e", "west": "w"}


def norm_address(addr: str) -> str:
    street = str(addr).split(",")[0]
    toks = parse_licenses.norm(street).split()
    return " ".join(_SUFFIX.get(t, t) for t in toks)


def load_lead_addresses(path: Path | None = None) -> set[str]:
    path = Path(path or config.LEAD_ADDRESSES_FILE)
    if not path.exists():
        return set()
    out: set[str] = set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            for k, v in row.items():
                if k and k.strip().lower() in ("address", "property address", "property_address") and v:
                    n = norm_address(v)
                    if n:
                        out.add(n)
    return out


def count_price_cuts(history: dict, current_price) -> int:
    pts = sorted((d, e.get("price")) for d, e in (history or {}).items()
                 if isinstance(e, dict) and isinstance(e.get("price"), (int, float)) and e.get("price"))
    prices = [p for _, p in pts]
    if isinstance(current_price, (int, float)) and current_price:
        prices.append(current_price)
    return sum(1 for a, b in zip(prices, prices[1:]) if b < a)


def is_distressed(raw: dict) -> bool:
    hist = raw.get("history") or {}
    types = {str(raw.get("listingType") or "").lower()}
    types |= {str(e.get("listingType") or "").lower() for e in hist.values() if isinstance(e, dict)}
    return bool(types & DISTRESS_TYPES)


def _ppsf(raw: dict):
    price, sqft = raw.get("price"), raw.get("squareFootage")
    if isinstance(price, (int, float)) and isinstance(sqft, (int, float)) and price > 0 and sqft >= 300:
        return price / sqft
    return None


def build_comps(raws: list[dict]) -> dict:
    """Median asking $/sqft by (ZIP, property type) and (county, property type), from this run's
    Active listings. Distressed listings are left out so they don't drag the benchmark down.
    RentCast listings carry no market value, so local peers are the benchmark."""
    by_zip: dict = defaultdict(list)
    by_county: dict = defaultdict(list)
    for r in raws:
        p = _ppsf(r)
        if p is None or r.get("status") != "Active" or is_distressed(r):
            continue
        t = _clean(r.get("propertyType"))
        by_zip[(_clean(r.get("zipCode")), t)].append(p)
        by_county[(listing_county(r), t)].append(p)
    comps = {}
    for k, v in by_zip.items():
        if len(v) >= config.COMP_MIN_ZIP:
            comps[("zip",) + k] = statistics.median(v)
    for k, v in by_county.items():
        if len(v) >= config.COMP_MIN_COUNTY:
            comps[("county",) + k] = statistics.median(v)
    return comps


def benchmark(raw: dict, comps: dict):
    t = _clean(raw.get("propertyType"))
    return comps.get(("zip", _clean(raw.get("zipCode")), t)) or comps.get(("county", listing_county(raw), t))


# --- API --------------------------------------------------------------------

def _get(session, params: dict, key: str, budget: Counter) -> list:
    for attempt in range(4):
        if budget["requests"] >= (budget.get("cap") or config.RENTCAST_MAX_REQUESTS):
            raise RentCastError("request budget for this run reached")
        budget["requests"] += 1
        resp = session.get(API_URL, params=params, timeout=60, headers={
            "X-Api-Key": key, "Accept": "application/json", "User-Agent": config.USER_AGENT})
        if resp.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code in (401, 403):
            raise AuthError(f"RentCast rejected the API key (HTTP {resp.status_code})")
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("listings") or data.get("data") or []
        time.sleep(config.RENTCAST_DELAY_SECONDS)
        return data
    raise RentCastError("rate limited (HTTP 429) after retries")


def listing_county(raw: dict) -> str:
    c = parse_licenses.norm_county(_clean(raw.get("county")))
    return c or parse_licenses.county_from_zip(_clean(raw.get("zipCode")))


def fetch_county(session, key: str, county: str, query: dict, budget: Counter) -> list[dict]:
    """One narrow query for one county (Active listings only), following pages up to the page cap."""
    area = config.RENTCAST_AREAS[county]
    filters = {k: v for k, v in query.items() if k != "name"}
    out: list[dict] = []
    for page in range(config.RENTCAST_MAX_PAGES):
        params = {**area, **config.RENTCAST_COMMON_FILTERS, **filters, "status": "Active",
                  "limit": config.RENTCAST_PAGE_SIZE, "offset": page * config.RENTCAST_PAGE_SIZE}
        batch = _get(session, params, key, budget)
        budget[f"fetched_{query.get('name', 'all')}"] += len(batch)
        out.extend(r for r in batch if listing_county(r) == county)
        if len(batch) < config.RENTCAST_PAGE_SIZE:
            break
    return out


def fetch_all(session, key: str, budget: Counter) -> list[dict]:
    """Every query for every county. If the call budget runs out partway, keep what was fetched
    (the calls were already spent) and note it; a rejected API key still raises."""
    raws: dict[str, dict] = {}
    try:
        for query in config.RENTCAST_QUERIES:
            for county in config.TARGET_COUNTIES:
                if county not in config.RENTCAST_AREAS:
                    continue
                for r in fetch_county(session, key, county, query, budget):
                    raws.setdefault(str(r.get("id") or r.get("formattedAddress") or id(r)), r)
    except AuthError:
        raise
    except RentCastError as e:
        budget["partial"] = 1
        budget["partial_reason"] = str(e)
    return list(raws.values())


def month_used(today: str, path: Path | None = None) -> int:
    try:
        return int(json.loads(Path(path or config.RENTCAST_USAGE_FILE).read_text()).get(today[:7], 0))
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return 0


def record_usage(today: str, calls: int, path: Path | None = None):
    path = Path(path or config.RENTCAST_USAGE_FILE)
    try:
        usage = json.loads(path.read_text())
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        usage = {}
    usage[today[:7]] = int(usage.get(today[:7], 0)) + calls
    usage = dict(sorted(usage.items())[-12:])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(usage, indent=1), encoding="utf-8")


# --- normalise + history ----------------------------------------------------

def normalize(raw: dict, comps: dict | None = None) -> dict:
    agent = raw.get("listingAgent") or {}
    office = raw.get("listingOffice") or {}
    hist = raw.get("history") or {}
    ppsf, bench = _ppsf(raw), (benchmark(raw, comps) if comps else None)
    under = bool(ppsf and bench and ppsf <= config.UNDER_MARKET_RATIO * bench)
    return {
        "id": str(raw.get("id") or raw.get("formattedAddress") or ""),
        "address": _clean(raw.get("formattedAddress")),
        "county": listing_county(raw),
        "zip": _clean(raw.get("zipCode")),
        "status": _clean(raw.get("status")),
        "listing_type": _clean(raw.get("listingType")),
        "distressed": is_distressed(raw),
        "under_market": under,
        "below_market_pct": round(100 * (1 - ppsf / bench)) if under else 0,
        "stale": (raw.get("daysOnMarket") or 0) > config.STALE_DAYS_ON_MARKET,
        "property_type": _clean(raw.get("propertyType")),
        "price": raw.get("price"),
        "price_cuts": count_price_cuts(hist, raw.get("price")),
        "days_on_market": raw.get("daysOnMarket") or 0,
        "listed_date": _clean(raw.get("listedDate"))[:10],
        "removed_date": _clean(raw.get("removedDate"))[:10],
        "agent_name": _clean(agent.get("name")),
        "agent_phone": enrich_brokerages.norm_phone(agent.get("phone") or ""),
        "agent_email": _clean(agent.get("email")).lower(),
        "office": _clean(office.get("name")),
        "office_phone": enrich_brokerages.norm_phone(office.get("phone") or ""),
    }


def load_history(path: Path | None = None) -> dict:
    path = Path(path or config.LISTINGS_HISTORY_FILE)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def update_history(history: dict, raws: list[dict], today: str, comps: dict | None = None) -> int:
    """Store only listings that match a signal (distressed, well under market, or stale)."""
    new = 0
    for raw in raws:
        rec = normalize(raw, comps)
        if not rec["id"] or not (rec["distressed"] or rec["under_market"] or rec["stale"]):
            continue
        old = history.get(rec["id"])
        if old is None:
            new += 1
            rec["first_seen"] = today
        else:
            rec["first_seen"] = old.get("first_seen", today)
            # once flagged, stay flagged: a later fetch may omit history or show a changed price
            for flag in ("distressed", "under_market", "stale"):
                rec[flag] = rec[flag] or old.get(flag, False)
            rec["below_market_pct"] = max(rec["below_market_pct"], old.get("below_market_pct", 0))
        rec["last_seen"] = today
        history[rec["id"]] = rec
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=config.LISTING_RETENTION_DAYS)).isoformat()
    for lid in [k for k, v in history.items() if v.get("last_seen", today) < cutoff]:
        del history[lid]
    return new


def save_history(history: dict, path: Path | None = None):
    path = Path(path or config.LISTINGS_HISTORY_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, separators=(",", ":"), sort_keys=True), encoding="utf-8")


# --- scoring ----------------------------------------------------------------

def score(a: dict) -> int:
    w = config.SCORE_WEIGHTS
    return (w["distressed"] * a["distressed"] + w["lead_match"] * a["lead_match"]
            + w["under_market"] * a["under_market"] + w["stale"] * a["stale"])


def agent_key(l: dict) -> str:
    f, la = name_key(*split_agent_name(l["agent_name"]))
    tail = parse_licenses.norm(" ".join(sorted(brand_tokens(l["office"])))) or re.sub(r"\D", "", l["agent_phone"])
    return f"{la}|{f}|{tail}"


def aggregate(history: dict, today: str, lead_addrs: set[str]) -> dict[str, dict]:
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=config.SCORE_WINDOW_DAYS)).isoformat()
    new_cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=config.NEW_WINDOW_DAYS)).isoformat()
    agents: dict[str, dict] = {}
    for l in history.values():
        if not l.get("agent_name"):
            continue
        d = l.get("listed_date") or l.get("first_seen") or today
        if d < cutoff:
            continue
        a = agents.setdefault(agent_key(l), {
            "names": Counter(), "phones": Counter(), "emails": Counter(), "offices": Counter(),
            "counties": Counter(), "total": 0, "distressed": 0, "under_market": 0, "stale": 0,
            "lead_match": 0, "new": 0, "last_listing": "", "first_seen": today})
        a["names"][l["agent_name"]] += 1
        if l.get("agent_phone"):
            a["phones"][l["agent_phone"]] += 1
        if l.get("agent_email"):
            a["emails"][l["agent_email"]] += 1
        if l.get("office"):
            a["offices"][l["office"]] += 1
        if l.get("county"):
            a["counties"][l["county"]] += 1
        a["total"] += 1
        a["distressed"] += 1 if l.get("distressed") else 0
        a["under_market"] += 1 if l.get("under_market") else 0
        a["stale"] += 1 if l.get("stale") else 0
        a["new"] += 1 if d >= new_cutoff else 0
        a["lead_match"] += 1 if norm_address(l.get("address", "")) in lead_addrs else 0
        a["last_listing"] = max(a["last_listing"], d)
        a["first_seen"] = min(a["first_seen"], l.get("first_seen") or today)
    for a in agents.values():
        a["score"] = score(a)
    return agents


def _top(counter: Counter) -> str:
    return counter.most_common(1)[0][0] if counter else ""


# --- apply to records -------------------------------------------------------

STAT_FIELDS = ("distress_score", "flagged_listings", "distressed_listings", "under_market_listings",
               "stale_listings", "new_listings", "lead_match_listings", "last_listing")


def _reset(rec: dict):
    for f in STAT_FIELDS:
        rec[f] = "" if f == "last_listing" else 0


def _add_stats(rec: dict, a: dict):
    rec["distress_score"] += a["score"]
    rec["flagged_listings"] += a["total"]
    rec["distressed_listings"] += a["distressed"]
    rec["under_market_listings"] += a["under_market"]
    rec["stale_listings"] += a["stale"]
    rec["new_listings"] += a["new"]
    rec["lead_match_listings"] += a["lead_match"]
    rec["last_listing"] = max(rec["last_listing"], a["last_listing"])


def match_record(index: dict, a: dict) -> dict | None:
    name = _top(a["names"])
    cands = index.get(name_key(*split_agent_name(name)), [])
    if len(cands) == 1:
        return cands[0]
    if not cands:
        return None
    offices = list(a["offices"])
    by_brand = [c for c in cands if any(brokerage_similar(c.get("brokerage", ""), o) for o in offices)]
    if len(by_brand) == 1:
        return by_brand[0]
    county = _top(a["counties"])
    by_county = [c for c in (by_brand or cands) if c.get("county") == county]
    return by_county[0] if len(by_county) == 1 else None


def apply_to_records(records: list[dict], agents: dict[str, dict], stats: Counter) -> list[dict]:
    records = [r for r in records if r.get("source") != "listing"]
    index: dict[tuple, list] = {}
    for r in records:
        _reset(r)
        index.setdefault(name_key(r.get("first_name", ""), r.get("last_name", "")), []).append(r)

    listing_only: dict[str, dict] = {}
    for a in sorted(agents.values(), key=lambda x: -x["score"]):
        rec = match_record(index, a)
        if rec is not None:
            stats["agents_matched"] += 1
            _add_stats(rec, a)
            if not rec.get("phone") and a["phones"]:
                rec["phone"], rec["phone_source"] = _top(a["phones"]), "rentcast-listing"
            if not rec.get("email") and a["emails"]:
                rec["email"] = _top(a["emails"])
            continue
        if any(len(index.get(name_key(*split_agent_name(n)), [])) > 1 for n in a["names"]):
            stats["agents_ambiguous"] += 1
        if a["score"] < config.MIN_LISTING_ONLY_SCORE:
            stats["agents_below_threshold"] += 1
            continue
        name = _top(a["names"])
        first, last = split_agent_name(name)
        office = _top(a["offices"])
        rid = "L-" + hashlib.sha1(
            f"{name_key(first, last)}|{parse_licenses.norm(office)}".encode()).hexdigest()[:10]
        rec = listing_only.get(rid)
        if rec is None:
            rec = listing_only[rid] = {
                "id": rid, "source": "listing", "license_number": "", "first_name": first,
                "last_name": last, "name": f"{first} {last}".strip(),
                "license_type": "Listing agent (license not checked)", "status": "",
                "brokerage": office, "county": _top(a["counties"]), "county_source": "listing",
                "city": "", "state": "OH", "zip": "", "phone": _top(a["phones"]),
                "email": _top(a["emails"]), "phone_source": "rentcast-listing" if a["phones"] else "",
                "expiration": "", "claimed": None, "license_verified": False,
                "first_seen": a["first_seen"], "last_seen": a["last_listing"],
                "in_latest_file": None,
            }
            _reset(rec)
            stats["agents_listing_only"] += 1
        _add_stats(rec, a)
    return records + list(listing_only.values())


# --- entry point ------------------------------------------------------------

def run(records: list[dict], today: str, session=None, history_path: Path | None = None,
        lead_path: Path | None = None, usage_path: Path | None = None) -> tuple[list[dict], Counter]:
    stats: Counter = Counter()
    history = load_history(history_path)
    key = os.environ.get("RENTCAST_API_KEY", "").strip()

    if key:
        if session is None:
            import requests
            session = requests.Session()
        used = month_used(today, usage_path)
        remaining = config.RENTCAST_MONTHLY_LIMIT - used
        stats["month_calls_before"] = used
        budget: Counter = Counter()
        budget["cap"] = min(config.RENTCAST_MAX_REQUESTS, max(remaining, 0))
        try:
            if budget["cap"] <= 0:
                raise RentCastError(f"monthly limit reached ({used} of {config.RENTCAST_MONTHLY_LIMIT} calls used); "
                                    "skipping RentCast this run")
            raws = fetch_all(session, key, budget)
            comps = build_comps(raws)
            stats["comp_groups"] = len(comps)
            stats["new_listings"] = update_history(history, raws, today, comps)
            save_history(history, history_path)
        except AuthError as e:
            stats["error"] = str(e)
        except RentCastError as e:
            # keep whatever was fetched before the failure out of history; rescore from what we have
            stats["error"] = str(e)
        except Exception as e:  # noqa: BLE001 - network/JSON problems must not kill the roster run
            stats["error"] = f"{type(e).__name__}: {e}"
        if budget["requests"]:
            record_usage(today, budget["requests"], usage_path)
        stats.update({k: v for k, v in budget.items() if k not in ("cap", "partial_reason")})
        if budget.get("partial"):
            stats["warning"] = f"partial fetch: {budget['partial_reason']}"
    else:
        stats["skipped_no_api_key"] = 1

    stats["listings_in_history"] = len(history)
    if not history:
        return [r for r in records if r.get("source") != "listing"], stats
    agents = aggregate(history, today, load_lead_addresses(lead_path))
    stats["listing_agents"] = len(agents)
    return apply_to_records(records, agents, stats), stats
