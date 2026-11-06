"""Optional step: add business phone/email from public brokerage roster pages.

Sources are listed in brokerage_sources.json (see brokerage_sources.example.json).
Only pages allowed by robots.txt are fetched, with a delay between requests.
Nothing here is site-specific: you supply the CSS selectors per brokerage.
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import re
import time
from collections import Counter, defaultdict
from urllib import robotparser
from urllib.parse import urlparse

import config

SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def name_key(s: str) -> str:
    toks = [t for t in re.sub(r"[^a-z\s]", " ", s.lower()).split()
            if t not in SUFFIXES and len(t) > 1]
    return f"{toks[0]} {toks[-1]}" if len(toks) >= 2 else ""


def norm_phone(s: str) -> str:
    digits = re.sub(r"\D", "", s or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return ""
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, re.sub(r"[^a-z0-9]", "", a.lower()),
                                   re.sub(r"[^a-z0-9]", "", b.lower())).ratio()


def extract_cards(html: str, src: dict) -> list[dict]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    cards = []
    for item in soup.select(src["item"]):
        name_el = item.select_one(src.get("name", "h2, h3, .name"))
        if not name_el:
            continue
        phone_el = item.select_one(src.get("phone", "a[href^='tel:']"))
        email_el = item.select_one(src.get("email", "a[href^='mailto:']"))
        phone_raw = ""
        if phone_el:
            phone_raw = phone_el.get("href", "").replace("tel:", "") or phone_el.get_text()
        else:
            m = re.search(r"\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}", item.get_text(" "))
            phone_raw = m.group(0) if m else ""
        email = ""
        if email_el:
            email = email_el.get("href", "").replace("mailto:", "").split("?")[0].strip().lower()
        cards.append({
            "name": name_el.get_text(" ", strip=True),
            "phone": norm_phone(phone_raw),
            "email": email,
        })
    return cards


def apply_cards(records: list[dict], cards: list[dict], brokerage: str, domain: str) -> Counter:
    index = defaultdict(list)
    for r in records:
        k = name_key(f'{r["first_name"]} {r["last_name"]}')
        if k:
            index[k].append(r)

    stats: Counter = Counter()
    today = dt.date.today().isoformat()
    for c in cards:
        cands = index.get(name_key(c["name"]), [])
        if brokerage and cands:
            scored = [(similar(brokerage, r["brokerage"]), r) for r in cands if r["brokerage"]]
            good = [r for s, r in scored if s >= 0.6]
            cands = good if good else ([] if scored else cands)
        if not cands:
            stats["unmatched"] += 1
            continue
        if len(cands) > 1:
            stats["ambiguous"] += 1
            continue
        r = cands[0]
        changed = False
        if c["phone"] and not r.get("phone"):
            r["phone"], r["phone_source"], changed = c["phone"], domain, True
        if c["email"] and not r.get("email"):
            r["email"], changed = c["email"], True
        if changed:
            r["enriched_at"] = today
            stats["enriched"] += 1
        else:
            stats["already_had_contact"] += 1
    return stats


def run(records: list[dict], sources_path=None) -> Counter:
    import requests

    sources_path = sources_path or config.BROKERAGE_SOURCES_FILE
    total: Counter = Counter()
    try:
        sources = json.loads(sources_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return total
    robots: dict[str, robotparser.RobotFileParser] = {}
    sess = requests.Session()
    sess.headers["User-Agent"] = config.USER_AGENT

    def allowed(url: str) -> bool:
        host = urlparse(url)
        base = f"{host.scheme}://{host.netloc}"
        if base not in robots:
            rp = robotparser.RobotFileParser()
            try:
                rp.set_url(base + "/robots.txt")
                rp.read()
            except Exception:
                rp = None
            robots[base] = rp
        rp = robots[base]
        return True if rp is None else rp.can_fetch(config.USER_AGENT, url)

    for src in sources:
        pages = src.get("pages") or [1]
        domain = urlparse(src["url"]).netloc
        for p in pages:
            url = src["url"].format(page=p)
            if not allowed(url):
                total["blocked_by_robots"] += 1
                break
            try:
                resp = sess.get(url, timeout=30)
                resp.raise_for_status()
            except Exception:
                total["fetch_errors"] += 1
                continue
            cards = extract_cards(resp.text, src)
            total["cards"] += len(cards)
            total.update(apply_cards(records, cards, src.get("brokerage", ""), domain))
            time.sleep(config.REQUEST_DELAY_SECONDS)
    return total
