import os
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import rentcast_listings as rc  # noqa: E402

TODAY = "2026-11-06"          # a Friday


def raw(id_, agent, county="Franklin", ltype="Standard", office="Buckeye Realty", price=200000,
        history=None, dom=10, listed="2026-10-01", phone="6145550101", address=None, sqft=1500, zip_="43215"):
    return {
        "id": id_, "formattedAddress": address or f"{id_} Main St, Columbus, OH 43215",
        "county": county, "zipCode": zip_, "status": "Active", "listingType": ltype,
        "propertyType": "Single Family", "squareFootage": sqft, "price": price, "daysOnMarket": dom, "listedDate": listed + "T00:00:00.000Z",
        "history": history or {},
        "listingAgent": {"name": agent, "phone": phone, "email": "Agent@Example.com"},
        "listingOffice": {"name": office, "phone": "6145550000"},
    }


def lic(id_, first, last, brokerage, county="Franklin", phone=""):
    return {"id": id_, "first_name": first, "last_name": last, "name": f"{first} {last}",
            "brokerage": brokerage, "county": county, "phone": phone, "email": "", "phone_source": "",
            "in_latest_file": True}


class FakeResp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Serves pages out of per-(lat, status) lists and records every call."""

    def __init__(self, pages=None, status=200):
        self.pages, self.calls, self.status = pages or {}, [], status

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((params, headers))
        if self.status != 200:
            return FakeResp({}, self.status)
        data = self.pages.get((params["latitude"], params["status"]), [])
        off = params["offset"]
        return FakeResp(data[off: off + params["limit"]])


class Basics(unittest.TestCase):
    def test_price_cuts(self):
        h = {"2026-08-01": {"price": 250000}, "2026-09-01": {"price": 230000}, "2026-10-01": {"price": 230000}}
        self.assertEqual(rc.count_price_cuts(h, 215000), 2)
        self.assertEqual(rc.count_price_cuts({}, 100000), 0)
        self.assertEqual(rc.count_price_cuts({"2026-08-01": {"price": 100}}, 120), 0)

    def test_normalize_flags_distress_from_history(self):
        n = rc.normalize(raw("A1", "Ann Alder", history={"2026-06-01": {"price": 1, "listingType": "Short Sale"}}))
        self.assertTrue(n["distressed"])
        self.assertEqual(n["agent_phone"], "(614) 555-0101")
        self.assertEqual(n["agent_email"], "agent@example.com")
        self.assertEqual(n["listed_date"], "2026-10-01")
        self.assertFalse(rc.normalize(raw("A2", "Ann Alder"))["distressed"])

    def test_name_key_and_brokerage(self):
        self.assertEqual(rc.name_key(*rc.split_agent_name("Ann M. Alder Jr.")), ("ann", "alder"))
        self.assertEqual(rc.name_key(*rc.split_agent_name("Alder, Ann")), ("ann", "alder"))
        self.assertTrue(rc.brokerage_similar("Keller Williams Capital Partners", "Keller Williams Realty"))
        self.assertFalse(rc.brokerage_similar("RE/MAX Affiliates", "Keller Williams Realty"))

    def test_address_norm(self):
        self.assertEqual(rc.norm_address("123 North Main Street, Columbus, OH"), rc.norm_address("123 N Main St"))


class Fetch(unittest.TestCase):
    def setUp(self):
        self.sleep = mock.patch("time.sleep").start()
        self.addCleanup(mock.patch.stopall)

    def test_paginates_and_filters_by_county(self):
        lat = config.RENTCAST_AREAS["Franklin"]["latitude"]
        page = ([raw(f"F{i}", "Ann Alder") for i in range(500)])          # full page -> fetch another
        page2 = [raw("X1", "Ann Alder", county="Delaware"), raw("F500", "Ann Alder")]
        s = FakeSession({(lat, "Active"): page + page2})
        budget = Counter()
        got = rc.fetch_county(s, "KEY", "Franklin", "Active", budget)
        self.assertEqual(len(got), 501)                     # Delaware listing dropped
        self.assertEqual(budget["requests"], 2)
        self.assertEqual(s.calls[0][1]["X-Api-Key"], "KEY")
        self.assertEqual(s.calls[1][0]["offset"], 500)

    def test_county_falls_back_to_zip(self):
        r = raw("Z1", "Ann Alder", county="")
        r["zipCode"] = "45202"
        self.assertEqual(rc.listing_county(r), "Hamilton")

    def test_request_budget(self):
        with mock.patch.object(config, "RENTCAST_MAX_REQUESTS", 1):
            with self.assertRaises(rc.RentCastError):
                rc.fetch_all(FakeSession(), "KEY", Counter())


class Scoring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hist = Path(self.tmp.name) / "h.json"
        mock.patch("time.sleep").start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.tmp.cleanup)

    def records(self):
        return [lic("SAL.1", "Ann", "Alder", "Buckeye Realty"),
                lic("SAL.2", "Bob", "Birch", "Lake Erie Homes", county="Cuyahoga")]

    def seed(self, listings):
        h = {}
        rc.update_history(h, listings, TODAY)
        rc.save_history(h, self.hist)

    def test_matches_scores_and_fills_phone(self):
        self.seed([
            raw("L1", "Ann M. Alder", ltype="Foreclosure"),
            raw("L2", "Ann Alder", ltype="Short Sale", listed="2026-10-30"),    # listed within 14 days -> new
            raw("L3", "Ann Alder", dom=130),                                    # stale (> 120 days)
            raw("L4", "Ann Alder", ltype="Foreclosure", listed="2024-01-01"),   # outside 12-month window
        ])
        os.environ.pop("RENTCAST_API_KEY", None)
        recs, stats = rc.run(self.records(), TODAY, history_path=self.hist, lead_path=Path("/nonexistent"))
        ann = next(r for r in recs if r["id"] == "SAL.1")
        self.assertEqual(ann["distressed_listings"], 2)
        self.assertEqual(ann["stale_listings"], 1)
        self.assertEqual(ann["under_market_listings"], 0)
        self.assertEqual(ann["flagged_listings"], 3)
        self.assertEqual(ann["new_listings"], 1)
        self.assertEqual(ann["distress_score"], 3 * 2 + 2)
        self.assertEqual(ann["phone"], "(614) 555-0101")
        self.assertEqual(ann["phone_source"], "rentcast-listing")
        bob = next(r for r in recs if r["id"] == "SAL.2")
        self.assertEqual(bob["distress_score"], 0)
        self.assertEqual(stats["skipped_no_api_key"], 1)

    def test_only_flagged_listings_are_stored(self):
        h = {}
        n = rc.update_history(h, [
            raw("PLAIN", "Ann Alder", dom=40),                    # no signal
            raw("EDGE", "Ann Alder", dom=120),                    # exactly 120 days: not "longer than"
            raw("STALE", "Ann Alder", dom=121),
            raw("FC", "Ann Alder", ltype="Foreclosure"),
        ], TODAY)
        self.assertEqual(set(h), {"STALE", "FC"})
        self.assertEqual(n, 2)

    def test_under_market_uses_local_comps(self):
        comps_src = [raw(f"C{i}", "Comp Agent", price=300000, sqft=1500) for i in range(6)]   # $200/sqft
        cheap = raw("CHEAP", "Ann Alder", price=210000, sqft=1500)      # $140/sqft = 70% of median
        fair = raw("FAIR", "Ann Alder", price=270000, sqft=1500)        # $180/sqft = 90%
        nosqft = raw("NOSQ", "Ann Alder", price=100000, sqft=None)
        distressed_comp = raw("DC", "Comp Agent", ltype="Foreclosure", price=50000, sqft=1500)
        comps = rc.build_comps(comps_src + [cheap, fair, nosqft, distressed_comp])
        self.assertAlmostEqual(comps[("zip", "43215", "Single Family")], 200.0)    # distressed one excluded
        self.assertTrue(rc.normalize(cheap, comps)["under_market"])
        self.assertEqual(rc.normalize(cheap, comps)["below_market_pct"], 30)
        self.assertFalse(rc.normalize(fair, comps)["under_market"])
        self.assertFalse(rc.normalize(nosqft, comps)["under_market"])
        self.assertFalse(rc.normalize(cheap)["under_market"])                       # no comps -> never flagged

    def test_too_few_comps_means_no_benchmark(self):
        comps = rc.build_comps([raw(f"C{i}", "X", price=300000) for i in range(4)])
        self.assertEqual(comps, {})

    def test_county_fallback_benchmark(self):
        src = [raw(f"C{i}", "X", price=300000, zip_=f"432{i:02d}") for i in range(12)]   # one per ZIP
        comps = rc.build_comps(src)
        self.assertNotIn(("zip", "43200", "Single Family"), comps)
        cheap = raw("CH", "Ann Alder", price=150000, zip_="43299")
        self.assertTrue(rc.normalize(cheap, comps)["under_market"])

    def test_existing_phone_is_not_overwritten(self):
        self.seed([raw("L1", "Ann Alder", ltype="Foreclosure")])
        records = [lic("SAL.1", "Ann", "Alder", "Buckeye Realty", phone="(614) 555-9999")]
        os.environ.pop("RENTCAST_API_KEY", None)
        recs, _ = rc.run(records, TODAY, history_path=self.hist)
        self.assertEqual(recs[0]["phone"], "(614) 555-9999")

    def test_ambiguous_names_use_brokerage_then_county(self):
        records = [lic("A", "Sam", "Smith", "Keller Williams Realty", county="Franklin"),
                   lic("B", "Sam", "Smith", "Lake Erie Homes", county="Cuyahoga")]
        self.seed([raw("L1", "Sam Smith", ltype="Foreclosure", office="Lake Erie Homes", county="Cuyahoga")])
        os.environ.pop("RENTCAST_API_KEY", None)
        recs, _ = rc.run(records, TODAY, history_path=self.hist)
        self.assertEqual({r["id"]: r["distressed_listings"] for r in recs}, {"A": 0, "B": 1})

    def test_unresolvable_ambiguity_is_not_guessed(self):
        records = [lic("A", "Sam", "Smith", "Alpha Realty"), lic("B", "Sam", "Smith", "Beta Realty")]
        self.seed([raw("L1", "Sam Smith", ltype="Foreclosure", office="Gamma Homes")])
        os.environ.pop("RENTCAST_API_KEY", None)
        recs, stats = rc.run(records, TODAY, history_path=self.hist)
        self.assertEqual(sum(r["distressed_listings"] for r in recs if r["id"] in "AB"), 0)
        self.assertEqual(stats["agents_ambiguous"], 1)

    def test_listing_only_agent_added_above_threshold_and_rebuilt(self):
        self.seed([raw("L1", "Zed Zimmer", ltype="Foreclosure", office="Other Realty"),
                   raw("L2", "Low Score", office="Other Realty")])
        stale = {"id": "L-old", "source": "listing", "county": "Franklin", "first_name": "Old", "last_name": "One"}
        os.environ.pop("RENTCAST_API_KEY", None)
        recs, stats = rc.run(self.records() + [stale], TODAY, history_path=self.hist)
        listing_only = [r for r in recs if r.get("source") == "listing"]
        self.assertEqual([r["name"] for r in listing_only], ["Zed Zimmer"])   # stale one dropped, low score skipped
        z = listing_only[0]
        self.assertIs(z["license_verified"], False)
        self.assertEqual(z["distress_score"], 3)
        self.assertEqual(z["brokerage"], "Other Realty")
        self.assertEqual(z["phone"], "(614) 555-0101")

    def test_lead_address_bonus(self):
        lead = Path(self.tmp.name) / "leads.csv"
        lead.write_text("address\n12 Main Street, Columbus OH\n", encoding="utf-8")
        self.seed([raw("L1", "Ann Alder", ltype="Foreclosure", address="12 Main St, Columbus, OH 43215")])
        os.environ.pop("RENTCAST_API_KEY", None)
        recs, _ = rc.run(self.records(), TODAY, history_path=self.hist, lead_path=lead)
        ann = next(r for r in recs if r["id"] == "SAL.1")
        self.assertEqual(ann["lead_match_listings"], 1)
        self.assertEqual(ann["distress_score"], 3 + 2)

    def test_run_with_key_fetches_accumulates_and_survives_bad_key(self):
        lat = config.RENTCAST_AREAS["Franklin"]["latitude"]
        s = FakeSession({(lat, "Active"): [raw("L1", "Ann Alder", ltype="Foreclosure")]})
        with mock.patch.dict(os.environ, {"RENTCAST_API_KEY": "KEY"}):
            recs, stats = rc.run(self.records(), TODAY, session=s, history_path=self.hist)
            self.assertEqual(stats["new_listings"], 1)
            self.assertEqual(len(s.calls), 4)                        # one Active call per county
            self.assertTrue(all(c[0]["status"] == "Active" for c in s.calls))   # never sold/withdrawn
            # second run with a rejected key keeps the stored history and still scores
            recs2, stats2 = rc.run(self.records(), "2026-11-13", session=FakeSession(status=401),
                                   history_path=self.hist)
            self.assertIn("rejected the API key", stats2["error"])
            self.assertEqual(next(r for r in recs2 if r["id"] == "SAL.1")["distressed_listings"], 1)

    def test_history_keeps_first_seen_and_prunes(self):
        h = {}
        rc.update_history(h, [raw("L1", "Ann Alder", ltype="Foreclosure")], "2026-01-01")
        rc.update_history(h, [raw("L1", "Ann Alder")], "2026-02-01")        # later fetch lacks the flag
        self.assertEqual(h["L1"]["first_seen"], "2026-01-01")
        self.assertTrue(h["L1"]["distressed"])
        rc.update_history(h, [], "2028-01-01")
        self.assertEqual(h, {})


if __name__ == "__main__":
    unittest.main()
