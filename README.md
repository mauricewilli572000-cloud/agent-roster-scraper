# Agent roster + distressed-seller scoring

Franklin, Cuyahoga, Summit and Hamilton counties (OH). Separate from the foreclosure repo.

## Where the data comes from
- **Agents with individual phone numbers and cities**
  - `data/imported_agents.csv` is your own list (for example the homes.com list you built before). Any column
    names work if they are close to: name (or first + last), phone/cell/direct, email, brokerage/company,
    city, state. Rows outside Ohio are skipped.
  - **RentCast listings** add agents whose listings match the criteria below. They bring the phone and email
    that the listing carries, plus the county the listing is in.
- **Ohio Division of Real Estate daily license file** is used **only to verify agents**: does this person hold an
  active salesperson/broker license, which license number, which brokerage, when does it expire. The file has no
  city, county, phone or email, so it cannot be a roster. "Active ✓" / "none found" / "name match?" on the
  dashboard come from it.
- Optional brokerage roster pages (`brokerage_sources.json`, see the `.example.json`): fill missing phones/emails.

## What is tracked from listings
Only these three signals (weights and thresholds in `config.py`), over the last 12 months. A listing is stored
only if it matches at least one:
- foreclosure or short-sale listings: 3 points each
- priced well under market: asking $/sqft at most 75% of the median for the same ZIP and property type
  (needs 5+ comparable active listings, falls back to the county median): 2 points
- on market more than 120 days: 2 points
- +2 when the listing address is also one of your own leads (`data/lead_addresses.csv`, column `address`)

"New" means a flagged listing listed within the last 14 days. "Under market" is measured against other asking
prices, not an appraisal.

## Setup
1. Push this folder to its own GitHub repo; Settings > Pages > deploy from `/docs`.
2. Settings > Actions > General > Workflow permissions: Read and write.
3. Optional: add your agent list at `data/imported_agents.csv` and commit it.
4. **Listings (phase 2):** add the secret `RENTCAST_API_KEY`, then tick "Also fetch RentCast listings" on a
   manual run, or create a repository variable `ENABLE_LISTINGS` = `true` so the weekly schedule includes them.
5. Actions > "Agent roster nightly" > Run workflow. It also runs every Monday after the Division's 9am ET refresh.

Local run: `python run_pipeline.py` (`--inspect` profiles the license file's columns, `--skip-listings` skips
the API). The data folder and `docs/data` are public if the repo is public.

## RentCast usage (free plan = 50 calls/month, overage fees, no hard stop)
Each weekly run makes two narrow queries per county (listed in the last 10 days; listed over 120 days ago),
single/multi-family at or under $250k: about 8 calls, roughly 35 a month. `RENTCAST_MONTHLY_LIMIT` (45) stops
the pipeline from calling RentCast once the month's count in `data/rentcast_usage.json` reaches it, and
`RENTCAST_MAX_REQUESTS` (14) caps a single run. Listings that were already 11-120 days old when you start are
only picked up if they turn stale or are caught later, so coverage builds over the first weeks.

## Notes
- Phone numbers can be agents' direct lines. Scrub against the Do Not Call registry before calling and don't
  text without consent.
- Agents who appear on listings but not in your list are added once their score reaches `MIN_LISTING_ONLY_SCORE`.
