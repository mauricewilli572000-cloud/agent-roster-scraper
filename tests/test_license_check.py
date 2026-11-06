import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import import_agents  # noqa: E402
import license_check as lc  # noqa: E402
import parse_licenses as pl  # noqa: E402
import run_pipeline  # noqa: E402

# the real Division file's columns (profiled from the live download)
REAL_HEADERS = ["Name", "Legacy First Name", "Legacy Last Name", "Organization: Account Name", "Preferred Name",
                "Effective Date", "Post Licensure Due Date", "Post Licensure Received Date", "Expiration Date",
                "Status", "Status Reason", "Primary License: Name", "Primary Licensee",
                "Regulatory Authorization Type: Name"]
REAL_ROWS = [
    ["SAL.2019000111", "Ann", "Alder", "Ann Alder", "", "1/2/2019", "", "", "1/2/2027", "Active", "", "BRK.2001000999", "Buckeye Realty LLC, LLC", "Salesperson"],
    ["SAL.2018000222", "Sam", "Smith", "", "", "1/2/2018", "", "", "1/2/2026", "Active", "", "BRK.2001000999", "Buckeye Realty LLC, LLC", "Salesperson"],
    ["SAL.2017000333", "Sam", "Smith", "", "", "1/2/2017", "", "", "1/2/2026", "Active", "", "BRK.2002000888", "Lake Erie Homes, INC", "Salesperson"],
    ["SAL.2016000444", "Old", "Timer", "", "", "1/2/2016", "", "", "1/2/2020", "Inactive", "Lapsed", "", "", "Salesperson"],
    ["BRK.2001000999", "", "", "Buckeye Realty LLC", "", "1/2/2001", "", "", "1/2/2027", "Active", "", "", "", "Brokerage"],
    ["APP.2010000555", "Pat", "Appraiser", "", "", "1/2/2010", "", "", "1/2/2026", "Active", "", "", "", "Appraiser"],
    ["SAL.2022000666", "", "", "Newt Newlicense", "", "1/2/2022", "", "", "1/2/2028", "Active", "", "BRK.2002000888", "Lake Erie Homes, INC", "Salesperson"],
    ["BRK.2003000777", "Bea", "Broker", "", "", "1/2/2003", "", "", "1/2/2027", "Active", "", "", "", "Broker"],
]


def rec(first, last, brokerage="", **kw):
    return {"id": f"{first}{last}", "first_name": first, "last_name": last, "name": f"{first} {last}",
            "brokerage": brokerage, **kw}


class LicenseCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(REAL_HEADERS)
        for r in REAL_ROWS:
            ws.append(r)
        self.path = Path(self.tmp.name) / "lic.xlsx"
        wb.save(self.path)
        self.index, self.stats = lc.build_index(self.path)

    def test_real_headers_map(self):
        m, _ = pl.read_table(self.path)
        self.assertEqual(m["license_number"], "Name")
        self.assertEqual(m["brokerage"], "Primary Licensee")
        self.assertEqual(m["license_type"], "Regulatory Authorization Type: Name")
        self.assertEqual(m["status"], "Status")
        self.assertEqual(m["claimed"], "Organization: Account Name")

    def test_index_keeps_only_active_agents(self):
        self.assertEqual(self.stats["indexed"], 5)         # Ann, 2x Sam, Newt (account-name fallback), Bea
        self.assertEqual(self.stats["skipped_status"], 1)  # the lapsed one
        self.assertNotIn(("pat", "appraiser"), self.index)  # appraisers excluded
        self.assertNotIn(("old", "timer"), self.index)

    def test_verified_fills_license_and_brokerage(self):
        r = rec("Ann", "Alder")
        self.assertEqual(lc.verify(r, self.index), "verified")
        self.assertEqual(r["license_number"], "SAL.2019000111")
        self.assertEqual(r["license_expires"], "1/2/2027")
        self.assertEqual(r["brokerage"], "Buckeye Realty LLC, LLC")      # filled from the license

    def test_account_name_fallback_for_new_licenses(self):
        self.assertEqual(lc.verify(rec("Newt", "Newlicense"), self.index), "verified")

    def test_same_name_resolved_by_brokerage_else_ambiguous(self):
        a = rec("Sam", "Smith", "Lake Erie Homes")
        self.assertEqual(lc.verify(a, self.index), "verified")
        self.assertEqual(a["license_number"], "SAL.2017000333")
        b = rec("Sam", "Smith", "Some Other Realty")
        self.assertEqual(lc.verify(b, self.index), "ambiguous")
        self.assertEqual(b["license_number"], "")
        self.assertFalse(b["license_verified"])

    def test_lapsed_and_unknown_are_not_found(self):
        self.assertEqual(lc.verify(rec("Old", "Timer"), self.index), "not found")
        self.assertEqual(lc.verify(rec("No", "Body"), self.index), "not found")

    def test_middle_initial_and_suffix_do_not_block_match(self):
        self.assertEqual(lc.verify(rec("Ann M.", "Alder Jr."), self.index), "verified")


class ImportTests(unittest.TestCase):
    def test_loose_columns_city_county_and_filters(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "agents.csv"
            with open(p, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["Agent Name", "Cell Phone", "E-mail", "Company", "City", "State"])
                w.writerow(["Ann Alder", "614-555-0101", "ANN@x.com", "Buckeye Realty", "Columbus", "OH"])
                w.writerow(["Alder, Ann", "(614) 555-0101", "", "", "Columbus", "OH"])      # duplicate
                w.writerow(["Tom Texan", "512-555-0100", "", "", "Austin", "TX"])          # not Ohio
                w.writerow(["Cal Cleveland", "216-555-0102", "", "Lake Erie", "Rocky River", "OH"])
                w.writerow(["Una Unknown", "330-555-0103", "", "", "Medina", "OH"])
            out = {r["name"]: r for r in import_agents.load(p)}
        self.assertEqual(set(out), {"Ann Alder", "Cal Cleveland", "Una Unknown"})
        self.assertEqual(out["Ann Alder"]["phone"], "(614) 555-0101")
        self.assertEqual(out["Ann Alder"]["email"], "ann@x.com")
        self.assertEqual(out["Ann Alder"]["county"], "Franklin")
        self.assertEqual(out["Ann Alder"]["county_source"], "city-approx")
        self.assertEqual(out["Cal Cleveland"]["county"], "Cuyahoga")
        self.assertEqual(out["Una Unknown"]["county"], "")       # city not in the map: left blank, not guessed

    def test_missing_file_is_empty(self):
        self.assertEqual(import_agents.load(Path("/nonexistent/x.csv")), [])


class PipelineTests(unittest.TestCase):
    def test_carry_over_keeps_first_seen_and_contacts(self):
        recs = [{"id": "I-1", "phone": "", "email": ""}, {"id": "I-2", "phone": "(614) 555-0000"}]
        prev = {"I-1": {"first_seen": "2026-10-01", "phone": "(614) 555-0100", "phone_source": "x.com"}}
        run_pipeline.carry_over(recs, prev, "2026-11-06")
        self.assertEqual((recs[0]["first_seen"], recs[0]["phone"], recs[0]["phone_source"]),
                         ("2026-10-01", "(614) 555-0100", "x.com"))
        self.assertEqual(recs[1]["first_seen"], "2026-11-06")
        self.assertEqual(recs[1]["phone"], "(614) 555-0000")


if __name__ == "__main__":
    unittest.main()
