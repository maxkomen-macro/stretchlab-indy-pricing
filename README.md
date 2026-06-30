# StretchLab Competitive Pricing — Indianapolis (V0)

A small, config-driven competitive-pricing tool benchmarking StretchLab against
**Stretch Zone, StretchMed, and iFlex** in the Indianapolis metro. Built as the
first real module of the larger StretchLab pricing tool — same schema, same
provenance discipline, same snapshot model.

## What it does
- **Auto-scrapes** StretchMed (Carmel) — prices are server-rendered, so a simple
  `requests` + `BeautifulSoup` pass collects them.
- **Captures by hand** the sources that can't be scraped, with provenance:
  iFlex (recurring prices sit in a JS booking widget), Stretch Zone (phone only),
  and StretchLab (internal, confidential).
- **Normalizes** incompatible structures (credits vs. sessions vs. memberships)
  to a common basis: effective **$/session** and **$/minute** at a ~50-min anchor.
- **Validates** (provenance present, no negative prices) and flags rows still
  awaiting call-in / internal data.
- **Snapshots, never overwrites** — each run writes `data/snapshots/<date>.json`,
  so monthly runs become price history.

## Run
```
pip install requests beautifulsoup4
python scraper.py          # scrapes + merges manual + writes data/
open preview.html          # double-click to view (loads data/latest.js)
```

## Honest scope (say this in interviews, it's the strong version)
The real skill on display isn't "scraped 3 sites" — it's handling **heterogeneous
data sources**: scrape what's published, capture the rest with provenance, and
normalize everything to one comparable number. One competitor is phone-only and
one hides prices behind a booking widget; the design accounts for that instead of
pretending otherwise.

## Fill these in
Edit `data/manual_entries.csv`:
- **Stretch Zone** — call the north-side studio, add tiers + prices + session length.
- **iFlex** — confirm the $158 / $79 figures (they're from an Aug-2024 article).
- **StretchLab** — paste your internal pricing for both Indy studios. *Confidential —
  keep this file local; never push it to a public repo.*

## Output schema (what your dashboard reads)
`data/latest.json`:
```
{ snapshot_date, market, anchor_session_min,
  studios: [ { brand, location, address,
              source:{ method, url, collected_at },
              offers:[ { name, structure, price, unit_price, unit,
                         price_per_session, price_per_min, status, is_headline } ] } ],
  validation:{ errors, incomplete_studios } }
```

## Wire into your real dashboard
`preview.html` is a throwaway stand-in. To use your existing six-tab dashboard,
point its data loader at `data/latest.json` (or include `data/latest.js`, which
sets `window.PRICING_DATA`). Send me the dashboard HTML and I'll do the merge.

## Next bricks toward the full tool
- `config.yaml` instead of the inline CONFIG dict
- Google Places discovery (pending API key) to auto-find competitors near a studio
- Scheduled monthly run → the snapshot folder becomes a trend series
