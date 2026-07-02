# StretchLab Competitive Pricing — Indianapolis

A config-driven competitive-pricing tool benchmarking **StretchLab** against
**StretchMed, iFlex, Stretch Zone, and Stretch Indy** across the Indianapolis metro.
Heterogeneous sources — scrape what's published, seed what isn't, capture the rest by
phone — all normalized to one comparable number and rendered on a map.

## The priced set (9 studios)
| Brand | Location(s) | Source |
|-------|-------------|--------|
| StretchMed | Carmel | **auto-scraped** (server-rendered HTML) |
| iFlex | Noblesville | **hybrid** — singles/intros/summer-drop-in are static text; memberships hide in a booking widget (Playwright, else seed) |
| StretchLab | Carmel, Keystone, Downtown, Fishers, Avon | **auto-scraped** via the microsite packages API (`members.stretchlab.com/api/locations/<slug>/packages`); each offer annotated with its position vs the corporate card (confidential). `retail_seed` from `data/stretchlab_retail.csv` is a fallback only |
| Stretch Zone | Carmel | **manual** (phone) |
| Stretch Indy | Broad Ripple | **manual** (phone) |

## What it does
- **Scrapes what's published**, with a *fetch-then-detect* gate: if prices are in the
  static HTML → `requests` + `BeautifulSoup`; if they're JS-rendered → headless
  Chromium (Playwright). Falls back to last-known seed / retail on any failure, and
  never labels a seed as "live" — every row carries a `status`.
- **Parses offer descriptions** (`parse_offer`) — `"4x25"`, `"Single-50"`, `"2 x 50-min/mo"`,
  StretchMed credits — into `(session_count, session_min)` so `$/session` and `$/min`
  compute for any brand's naming.
- **Scrapes StretchLab live** from the microsite packages API (the API slug is discovered
  from the microsite, prices come from `members.stretchlab.com/api/locations/<slug>/packages`)
  → all 5 studios are `method="scraped"`. `retail_seed` from `data/stretchlab_retail.csv` is
  now a **fallback only** (slug/API failure), logged loudly as `seed_used_<reason>` and never
  labeled scraped.
- **Annotates each scraped offer's pricing position** vs the corporate recommended card
  (`data/stretchlab_retail.csv`): `pricing_position` = `at` / `above` / `below` / `no_baseline`,
  plus a signed `deviation_delta`. A gap is a *finding* (an intentional pricing choice), not an
  error. **`deviation_delta` and `pricing_position` are CONFIDENTIAL** — they reconstruct the
  corporate card by inference, so they live only in the gitignored data (`latest.json/js`,
  `snapshots/`) and are kept out of `dashboard_view` and any committed file. Shared `(S)` rates
  and non-stretch add-ons (e.g. NormaTec) are flagged and excluded from the anchors.
- **Normalizes to two anchors**:
  - **Headline** — the standard single-session rate nearest ~50 min, with a comparability
    flag (`exact` / `close` within 40–60 min / `caveat`).
  - **Secondary** — the best (lowest `$/min`) committed membership.
  - Promotions and new-client intros are captured but **excluded** from both anchors.
- **Geocodes** every studio (Nominatim, cached to `data/geocode_cache.json`) and renders
  the 9 priced pins on a Leaflet map in `preview.html`.
- **Snapshots, never overwrites** — each run writes `data/snapshots/<date>.json`, so
  monthly runs become price history.

## Run
```
pip install requests beautifulsoup4
pip install playwright && playwright install chromium   # optional; JS-rendered menus
python scraper.py          # scrapes + validates + geocodes + writes data/
open preview.html          # double-click: map + anchor tables (loads data/latest.js)
```
Playwright is optional: without it, the JS-rendered menus (iFlex memberships) fall back to
seed and StretchLab falls back to retail-seed — the tool still runs and renders all 9 pins.

## Output schema (what the dashboard reads)
`data/latest.json`:
```
{ snapshot_date, market, anchor_session_min,
  studios: [ { brand, location, address, lat, lng,
              source:{ method, url, collected_at },
              headline_anchor:{ price_per_session, price_per_min, session_min, comparability, offer_name },
              secondary_anchor:{ price_per_min, offer_name },
              offers:[ { name, structure, price, session_count, session_min,
                         price_per_session, price_per_min,
                         category, is_promo, is_intro, is_shared, is_addon,
                         status, is_headline,
                         pricing_position, deviation_delta } ] } ],   // last two CONFIDENTIAL
  validation:{ errors, incomplete_studios, pricing_findings, needs_review, geocode_missing } }
```
`source.method` per site: `scraped` | `fallback_seed` | `retail_seed` | `seed_used_<reason>` | `callin`.
Offer `status`: `scraped` (live) | `retail_seed` / `seed_used_<reason>` (fallback) | `ok` | `verified_call`.
The corporate comparison is carried **separately** per offer as `pricing_position`
(`at` / `above` / `below` / `no_baseline`) + signed `deviation_delta` — **CONFIDENTIAL, gitignored
data only, never in `dashboard_view` or any committed file.** `validation.pricing_findings` collects
the off-card positions (also confidential); `needs_review` is data-quality only (`mapping_flag`,
negative price, a `single` missing `session_min`).

## Confidentiality — read before committing
StretchLab is internal/confidential. These stay **gitignored, never pushed**:
`data/stretchlab_retail.csv`, `data/manual_entries.csv`, `data/latest.json`,
`data/latest.js`, `data/snapshots/`, `data/geocode_cache.json`. Only
`data/manual_entries.example.csv` (public competitor template) and the code are committed.
Before any commit: `git status` + `git check-ignore` the files above.

The **derived variance** (`deviation_delta` / `pricing_position` = scraped listed − corporate
recommended) is confidential too — it reconstructs the corporate card by inference. It lives only in
the gitignored snapshot data, is deliberately **kept out of `dashboard_view` and any committed file**,
and `preview.html` renders it only from local gitignored data behind a **CONFIDENTIAL** watermark.

## Limitations & caveats
- **StretchMed headline single is imputed.** StretchMed sells credits, not fixed-length singles,
  so the ~50-min headline is imputed as 3 × the 1-credit drop-in rate = **$120 / 55-min**
  (`provenance: imputed_3credit_55min`); no advertised standalone 55-min single exists.
- **Intro variant matching is price-based, not an identity key.** Two corporate intro variants can
  share the match key (e.g. `Intro-50` $49 vs `Intro-50-Socks` $69 — both intro/1/50/non-shared); the
  oracle disambiguates by *nearest price*, so a real deviation on a tied intro variant could be masked.
  Make socks-vs-non-socks a real match-key field before the comparison block relies on intro deltas.
  (Singles, packs, and memberships have unique keys and are unaffected.)
- **Market min/median/max is metro-wide.** `dashboard_view[].market` spans the whole competitor set —
  only 4 competitors exist in the metro — so it is identical across all 5 StretchLab studios: a metro
  benchmark, not a per-trade-area one (Downtown & Avon have zero direct competitors within the radius).

## Next bricks toward the full tool
- The rich all-locations dashboard (`dashboard.html`) wired to `data/latest.json`.
- `config.yaml` instead of the inline CONFIG dicts.
- Scheduled monthly run → the snapshot folder becomes a trend series.
