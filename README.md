# Agent roster + distressed-seller scoring

Franklin, Cuyahoga, Summit and Hamilton counties (OH). Separate from the foreclosure repo.

**Sources**
- Ohio Division of Real Estate daily license file: who is a licensed, active agent.
- RentCast sale listings: who lists foreclosure / short-sale properties, price cuts, long days on market.
- Optional brokerage roster pages (`brokerage_sources.json`, see the `.example.json`): phones and emails.

**What is tracked** (only these three; weights and thresholds in `config.py`). A listing is stored only if
it matches at least one, and agents are scored over the last 12 months:
- foreclosure or short-sale listings: 3 points each
- priced well under market: asking $/sqft at most 75% of the median for the same ZIP and property type
  (needs 5+ comparable active listings, falls back to the county median): 2 points
- on market more than 120 days: 2 points
- +2 when the listing address is also one of your own leads (`data/lead_addresses.csv`)
"New" means a flagged listing listed within the last 14 days. "Under market" is measured against other asking
prices, not an appraisal.

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
