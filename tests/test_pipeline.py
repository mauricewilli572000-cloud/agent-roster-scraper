import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import enrich_brokerages as eb  # noqa: E402
import parse_licenses as pl  # noqa: E402
import run_pipeline  # noqa: E402

HEADERS = ["License Number", "First Name", "Last Name", "License Type", "Status",
           "Affiliation", "Mailing City", "Mailing State", "Mailing Zip", "County",
           "Organization: Account Name"]

ROWS = [
    ["SAL.001", "Ann", "Alder", "Real Estate Salesperson", "Active", "Buckeye Realty", "Columbus", "OH", "43215", "Franklin", "Ann Alder"],
    ["SAL.002", "Bob", "Birch", "Real Estate Salesperson", "Inactive", "Buckeye Realty", "Columbus", "OH", "43215", "Franklin", ""],
    ["BRK.003", "Cara", "Cedar", "Real Estate Broker", "Active", "Lake Erie Homes", "Cleveland", "OH", "44113", "Cuyahoga County", ""],
    ["APP.004", "Dan", "Dogwood", "Certified Residential Appraiser", "Active", "", "Cincinnati", "OH", "45202", "Hamilton", ""],
    ["SAL.005", "Eve", "Elm", "Real Estate Salesperson", "Active", "Summit Co", "Akron", "OH", "44308", "Summit", ""],
    ["SAL.006", "Finn", "Fir", "Real Estate Salesperson", "Active", "Queen City RE", "Cincinnati", "OH", "45202", "", ""],
    ["CMP.007", "", "Buckeye Realty LLC", "Real Estate Company", "Active", "", "Columbus", "OH", "43215", "Franklin", ""],
    ["SAL.008", "Gus", "Gum", "Real Estate Salesperson", "Active", "Nowhere", "Toledo", "OH", "43604", "", ""],
]


def make_xlsx(path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Daily license database - generated 2026-11-05"])  # junk title row
    ws.append(HEADERS)
    for r in ROWS:
        ws.append(r)
    wb.save(path)


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "lic.xlsx"
        make_xlsx(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_mapping(self):
        m = pl.map_columns(HEADERS)
        self.assertEqual(m["license_number"], "License Number")
        self.assertEqual(m["claimed"], "Organization: Account Name")
        self.assertEqual(m["brokerage"], "Affiliation")
        self.assertEqual(m["county"], "County")
        self.assertEqual(m["city"], "Mailing City")
        self.assertEqual(m["zip"], "Mailing Zip")

    def test_filters(self):
        agents, stats, _ = pl.parse_file(self.path)
        ids = {a["id"] for a in agents}
        self.assertEqual(ids, {"SAL.001", "BRK.003", "SAL.006"})
        self.assertEqual(stats["skipped_status"], 1)
        self.assertEqual(stats["skipped_type"], 1)
        self.assertEqual(stats["brokerages_seen"], 1)
        by_id = {a["id"]: a for a in agents}
        self.assertEqual(by_id["BRK.003"]["county"], "Cuyahoga")
        self.assertEqual(by_id["SAL.006"]["county"], "Hamilton")
        self.assertEqual(by_id["SAL.006"]["county_source"], "zip-approx")
        self.assertTrue(by_id["SAL.001"]["claimed"])

    def test_merge_keeps_enriched_contact(self):
        agents, _, _ = pl.parse_file(self.path)
        existing = {"SAL.001": {"id": "SAL.001", "first_seen": "2026-10-01", "phone": "(614) 555-0100",
                                "phone_source": "x.com", "county": "Franklin",
                                "last_name": "Alder", "first_name": "Ann"},
                    "OLD.9": {"id": "OLD.9", "county": "Franklin", "last_name": "Zed", "first_name": "Z"}}
        merged = run_pipeline.merge(existing, agents, "2026-11-05")
        by_id = {r["id"]: r for r in merged}
        self.assertEqual(by_id["SAL.001"]["phone"], "(614) 555-0100")
        self.assertEqual(by_id["SAL.001"]["first_seen"], "2026-10-01")
        self.assertFalse(by_id["OLD.9"]["in_latest_file"])
        self.assertTrue(by_id["BRK.003"]["in_latest_file"])


class EnrichTests(unittest.TestCase):
    HTML = """
    <div class="agent-card"><h3 class="agent-name">Ann M. Alder</h3>
      <a href="tel:+16145550123">614-555-0123</a><a href="mailto:Ann@buckeye.example?subject=Hi">e</a></div>
    <div class="agent-card"><h3 class="agent-name">Cara Cedar</h3><span>Call (216) 555-0188</span></div>
    <div class="agent-card"><h3 class="agent-name">Nobody Known</h3><a href="tel:6145550000">x</a></div>
    """
    SRC = {"item": ".agent-card", "name": ".agent-name"}

    def test_extract_and_apply(self):
        cards = eb.extract_cards(self.HTML, self.SRC)
        self.assertEqual(cards[0]["phone"], "(614) 555-0123")
        self.assertEqual(cards[0]["email"], "ann@buckeye.example")
        self.assertEqual(cards[1]["phone"], "(216) 555-0188")
        recs = [
            {"first_name": "Ann", "last_name": "Alder", "brokerage": "Buckeye Realty", "phone": "", "email": ""},
            {"first_name": "Cara", "last_name": "Cedar", "brokerage": "Lake Erie Homes", "phone": "", "email": ""},
        ]
        stats = eb.apply_cards(recs, cards, "Buckeye Realty", "buckeye.example")
        self.assertEqual(recs[0]["phone"], "(614) 555-0123")
        self.assertEqual(recs[0]["phone_source"], "buckeye.example")
        # Cara works for a different brokerage than the roster page -> not applied
        self.assertEqual(recs[1]["phone"], "")
        self.assertEqual(stats["enriched"], 1)
        self.assertEqual(stats["unmatched"], 2)

    def test_norm_phone(self):
        self.assertEqual(eb.norm_phone("1-614-555-0123"), "(614) 555-0123")
        self.assertEqual(eb.norm_phone("555-0123"), "")


if __name__ == "__main__":
    unittest.main()
