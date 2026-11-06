"""Central settings for the agent roster scraper."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DOCS_DATA_DIR = ROOT / "docs" / "data"      # served by GitHub Pages
EXPORT_DIR = ROOT / "exports"

RAW_FILE = RAW_DIR / "license_db.xlsx"
IMPORT_FILE = DATA_DIR / "imported_agents.csv"      # optional: your own agent list
RECORDS_FILE = DOCS_DATA_DIR / "records.json"
META_FILE = DOCS_DATA_DIR / "meta.json"
BROKERAGE_SOURCES_FILE = ROOT / "brokerage_sources.json"

# Official daily license database posted by the Ohio Division of Real Estate
# & Professional Licensing (refreshed ~9am ET). Source page:
# https://com.ohio.gov/divisions-and-programs/real-estate-and-professional-licensing/salespersons-and-brokers/guides-and-resources/notice-license-lookup
LICENSE_DB_URL = (
    "https://dam.assets.ohio.gov/raw/upload/com.ohio.gov/REPL/LPI/"
    "repl_LicenseLookUpwithAffiliations.xlsx"
)

USER_AGENT = "agent-roster-scraper/1.0 (public-records research)"
REQUEST_DELAY_SECONDS = 2.0     # politeness delay for brokerage crawling

TARGET_COUNTIES = ["Franklin", "Cuyahoga", "Summit", "Hamilton"]

# --- License type / status filtering -------------------------------------
AGENT_TYPES = ("salesperson", "sales person", "broker", "agent")
BROKERAGE_TYPES = ("brokerage", "company", "corporation", "partnership", "association")
EXCLUDE_TYPES = (
    "appraiser", "inspector", "cemetery", "manufactured", "land professional",
    "management company", "foreign", "developer", "development",
)

# --- Column mapping -------------------------------------------------------
# Header text is normalised (lowercase, punctuation -> spaces). A pattern that
# starts with "=" must match the whole header; otherwise "contains" is used as
# a fallback after exact matches. Run `python run_pipeline.py --inspect` to see
# how the real file's headers were mapped, then add aliases here if needed.
COLUMN_ALIASES = {
    "license_number": ["=license number", "=license no", "=license", "license number", "license no", "=name"],
    "first_name": ["=first name", "=first", "first name"],
    "last_name": ["=last name", "=last", "last name"],
    "claimed": ["organization account name"],
    "full_name": ["=full name", "=licensee name", "=licensee", "=name", "=individual name"],
    "license_type": ["=license type", "=type", "=license category", "license type", "record type",
                     "regulatory authorization type"],
    "status": ["=status", "=license status", "license status", "status"],
    "brokerage": ["=primary licensee", "brokerage", "broker affiliation", "affiliation", "employing broker",
                  "=company", "=business name", "=dba"],
    "county": ["=county", "=mailing county", "=business county", "county"],
    "city": ["=city", "=mailing city", "=business city", "city"],
    "state": ["=state", "=mailing state", "=business state", "state"],
    "zip": ["=zip", "=zip code", "=postal code", "=mailing zip", "=mailing postal code",
            "zip", "postal code"],
    "phone": ["=phone", "=phone number", "=business phone", "phone"],
    "email": ["=email", "=email address", "email"],
    "expiration": ["=expiration date", "=expiry date", "expiration", "expire"],
}

# --- County fallback by ZIP (APPROXIMATE) ---------------------------------
# Used only when the license file has no county column or the cell is blank.
# ZIPs straddle county lines, so records matched this way are tagged
# county_source="zip-approx" and shown with an asterisk on the dashboard.
ZIP_PREFIX_COUNTY = {
    "432": "Franklin",
    "441": "Cuyahoga",
    "443": "Summit",
    "452": "Hamilton",
}
ZIP_COUNTY = {
    # Franklin
    **{z: "Franklin" for z in (
        "43004", "43016", "43017", "43026", "43054", "43068", "43081", "43082",
        "43085", "43109", "43110", "43119", "43123", "43125", "43126", "43137")},
    # Cuyahoga
    **{z: "Cuyahoga" for z in ("44017", "44022", "44040", "44070")},
    # Summit (443xx Akron ZIPs are covered by the prefix above)
    **{z: "Summit" for z in ("44056", "44067", "44087", "44203", "44221", "44223", "44224",
                             "44236", "44237", "44262", "44264", "44278", "44286")},
    # Hamilton
    **{z: "Hamilton" for z in ("45002", "45030", "45033", "45052", "45140", "45174")},
}

# --- RentCast sale listings -> distressed-agent scoring --------------------
# API key comes from the RENTCAST_API_KEY environment variable (never stored in files).
LISTINGS_HISTORY_FILE = DATA_DIR / "listings_history.json"   # accumulates across nightly runs
LEAD_ADDRESSES_FILE = DATA_DIR / "lead_addresses.csv"        # optional: CSV with an "address" column
                                                             # (your foreclosure / code-enforcement leads)

# RentCast has no county filter, so each county is searched as a circle around its
# center and listings are kept only when the returned `county` field matches.
RENTCAST_AREAS = {
    "Franklin":  {"latitude": 39.97, "longitude": -83.00, "radius": 22},
    "Cuyahoga":  {"latitude": 41.43, "longitude": -81.68, "radius": 22},
    "Summit":    {"latitude": 41.12, "longitude": -81.52, "radius": 18},
    "Hamilton":  {"latitude": 39.20, "longitude": -84.54, "radius": 20},
}
RENTCAST_PAGE_SIZE = 500                      # API maximum
RENTCAST_MAX_PAGES = 2                        # pages per county per query (500 listings each)

# Free-plan mode: instead of downloading every active listing, run two narrow queries per county.
#   fresh = listed in the last ~10 days (a weekly run sees every new listing once, and these give
#           the new-distressed signal plus the comps for "under market")
#   stale = listed more than 120 days ago (the long-sitting signal, nothing else needed)
# That is about 1 call per query per county, 8 calls per run. Filters use RentCast range syntax
# ("min:max", "*" for open-ended, "|" between multiple values).
RENTCAST_QUERIES = [
    {"name": "fresh", "daysOld": "*:10"},
    {"name": "stale", "daysOld": "121:*"},
]
RENTCAST_COMMON_FILTERS = {                   # applied to every query; set to {} to remove all
    "propertyType": "Single Family|Multi-Family",
    "price": "*:250000",                      # wholesale targets; raise it to widen coverage
}
RENTCAST_MAX_REQUESTS = 14                    # hard cap on API calls per run (8 expected)
RENTCAST_MONTHLY_LIMIT = 45                   # free plan is 50/month, with overage fees and NO hard stop
RENTCAST_USAGE_FILE = DATA_DIR / "rentcast_usage.json"   # calls used per calendar month, committed by the workflow
RENTCAST_DELAY_SECONDS = 0.15                 # API limit is 20 req/s; stay far below

SCORE_WINDOW_DAYS = 365                       # listings older than this stop counting
LISTING_RETENTION_DAYS = 550                  # prune stored listings not seen for this long
STALE_DAYS_ON_MARKET = 120               # a listing counts as stale when days on market is MORE than this
UNDER_MARKET_RATIO = 0.75                 # flagged when asking $/sqft is at most 75% of the local median
COMP_MIN_ZIP = 5                          # comparable listings needed for a ZIP-level benchmark
COMP_MIN_COUNTY = 10                      # ...and for the county-level fallback
NEW_WINDOW_DAYS = 14                      # "new" = flagged listing listed within this many days
SCORE_WEIGHTS = {
    "distressed": 3,        # listingType Foreclosure / Short Sale
    "lead_match": 2,        # listing address is also one of your own leads
    "under_market": 2,      # asking $/sqft far below comparable local listings
    "stale": 2,             # on market longer than STALE_DAYS_ON_MARKET
}
MIN_LISTING_ONLY_SCORE = 3    # agents not found in the license file are added only at/above this score
GHL_MIN_SCORE = 3             # minimum score for the distressed-agents GoHighLevel export

# City -> county for unambiguous principal cities, used only for imported lists that have a city but no
# county. Marked county_source="city-approx". Add cities as needed; leave out ones that span counties.
CITY_COUNTY = {
    **{c: "Franklin" for c in ("columbus", "dublin", "westerville", "grove city", "hilliard", "gahanna",
                               "reynoldsburg", "upper arlington", "worthington", "bexley", "whitehall",
                               "canal winchester", "grandview heights", "obetz")},
    **{c: "Cuyahoga" for c in ("cleveland", "parma", "lakewood", "euclid", "strongsville", "brook park",
                               "westlake", "solon", "beachwood", "shaker heights", "cleveland heights",
                               "north olmsted", "garfield heights", "maple heights", "bedford",
                               "rocky river", "mayfield heights", "berea", "brecksville", "independence",
                               "fairview park", "bay village", "south euclid", "lyndhurst", "willoughby hills")},
    **{c: "Summit" for c in ("akron", "cuyahoga falls", "stow", "barberton", "hudson", "tallmadge", "twinsburg",
                             "fairlawn", "munroe falls", "macedonia", "norton", "green", "copley")},
    **{c: "Hamilton" for c in ("cincinnati", "norwood", "blue ash", "sharonville", "forest park", "springdale",
                               "montgomery", "madeira", "deer park", "reading", "wyoming", "harrison", "mount healthy")},
}
