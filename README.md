# Agent roster + distressed-seller scoring

Franklin, Cuyahoga, Summit and Hamilton counties (OH). Separate from the foreclosure repo.

**Sources**
- Ohio Division of Real Estate daily license file: who is a licensed, active agent.
- RentCast sale listings: who lists foreclosure / short-sale properties, price cuts, long days on market.
- Optional brokerage roster pages (`brokerage_sources.json`, see the `.example.json`): phones and emails.

**Score** (last 12 months, weights in `config.py`): 3 per foreclosure/short-sale listing, 2 per listing
that matches one of your own leads, 1 per listing with a price cut, 1 per listing on market 90+ days.

## Setup
1. Push this folder to its own GitHub repo; Settings > Pages > deploy from `/docs`.
2. Settings > Secrets and variables > Actions > new secret `RENTCAST_API_KEY`.
3. Optional: put your foreclosure / code-enforcement addresses in `data/lead_addresses.csv`
   (column `address`) to boost agents who list those same properties.
4. Actions > "Agent roster nightly" > Run workflow (it also runs Mon/Wed/Fri after the Division's 9am ET refresh).

Local run: `RENTCAST_API_KEY=... python run_pipeline.py` (`--inspect` shows the license-file column mapping,
`--skip-listings` skips the API, `--force-inactive` also pulls sold/withdrawn listings now).

## Notes
- RentCast is called on each run (Mon/Wed/Fri: Active listings, one call per county, more if a county has over 500). Sold/withdrawn listings are
  pulled on Mondays. `RENTCAST_MAX_REQUESTS` in `config.py` caps calls per run.
- Listings accumulate in `data/listings_history.json`; scores get better as history builds. If the repo is
  public, that file and `docs/data` are public too.
- Agents who appear on listings but are not in the license file are added with "license not checked" once
  their score reaches `MIN_LISTING_ONLY_SCORE`.
- Phone numbers come from MLS listing data and brokerage pages. Scrub against the Do Not Call registry
  before calling or texting.
