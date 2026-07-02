#!/usr/bin/env python3
"""
StretchLab Competitive Pricing — Indianapolis (Block 3a)
========================================================

Full priced Indy dataset. Nine studios, each normalized to a comparable number:

  * StretchMed (Carmel) ....... AUTO-SCRAPED (requests + BeautifulSoup). Prices are
                                server-rendered text; credit-based catalog.
  * iFlex (Noblesville) ....... HYBRID. Singles / intros / summer drop-in are static
                                text on /memberships; the recurring membership tiers
                                live in a booking widget — Playwright if reachable,
                                else last-known seed.
  * StretchLab (5 studios) .... SCRAPE-then-SEED. Microsites publish no menu prices
                                (behind the ClubReady member portal), so the full menu
                                falls back to data/stretchlab_retail.csv (retail_seed);
                                any publicly-visible offer is validated against retail.
  * Stretch Zone (Carmel) ..... MANUAL (phone). data/manual_entries.csv.
  * Stretch Indy (Broad Rip.).. MANUAL (phone). data/manual_entries.csv.

Design discipline (unchanged from V0, extended here):
config-driven catalog, provenance on every row, an offer-description parser that
extracts (session_count, session_min) from any brand's naming, a StretchLab retail
validation oracle, two normalization anchors (headline single + best member rate),
Nominatim geocoding (cached), a validation pass, and snapshot-never-overwrite storage.

Run:  python scraper.py
Deps: pip install requests beautifulsoup4
      pip install playwright && playwright install chromium   # optional; JS-rendered menus
"""

import collections
import csv
import datetime
import json
import logging
import math
import pathlib
import re
import statistics
import sys
import time

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing deps. Run: pip install requests beautifulsoup4")

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger("benchmark")

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"
SNAPS = DATA / "snapshots"
DATA.mkdir(exist_ok=True)
SNAPS.mkdir(exist_ok=True)

STRETCHLAB_RETAIL = DATA / "stretchlab_retail.csv"
GEOCODE_CACHE = DATA / "geocode_cache.json"

# ───────────────────────── CONFIG ─────────────────────────
# In the full tool this graduates to config.yaml. Kept inline here so the
# script runs with zero extra files. Structure is STABLE; prices are SCRAPED.
MARKET = "Indianapolis, IN"
ANCHOR_MIN = 50            # headline comparison: a ~50-minute session
CLOSE_MIN, CLOSE_MAX = 40, 60   # "close" comparability band around the 50-min anchor
DIRECT_RADIUS_MI = 5.0    # dashboard_view: competitors within this straight-line distance are
                          # "direct" (head-to-head trade area); beyond = "adjacent" (metro context)

# StretchMed sells credits, not fixed session lengths. Their conversion:
SM_CREDIT_MINUTES = {1: 25, 2: 40, 3: 55, 4: 70}
SM_ANCHOR_CREDITS = 3     # nearest bucket to 50 min = 55 min (3 credits)
SM_ANCHOR_MIN = 55

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

# Nominatim (OpenStreetMap) — free geocoder. Requires a real UA + contact.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
GEO_HEADERS = {
    "User-Agent": "stretchlab-indy-pricing/1.0 (competitive benchmark; contact maxkomen@gmail.com)",
    "Accept-Language": "en-US,en;q=0.9",
}
GEO_RATE_SEC = 1.1        # Nominatim policy: <= 1 request/second

STRETCHMED = {
    "brand": "StretchMed",
    "location": "Carmel",
    "address": "14550 Clay Terrace Blvd, Carmel, IN 46032",
    "url": "https://stretchmedstudios.com/stretchmed-carmel-memberships/",
    # seed_total = last-known price; used as fallback if the live fetch fails
    # (e.g. offline). On a live run the scraped value overrides it.
    "products": [
        {"name": "Starter",  "credits": 4,  "seed_total": 88,  "recurring": True,  "category": "membership"},
        {"name": "Standard", "credits": 8,  "seed_total": 168, "recurring": True,  "category": "membership"},
        {"name": "Advanced", "credits": 12, "seed_total": 240, "recurring": True,  "category": "membership"},
        {"name": "Elite",    "credits": 16, "seed_total": 304, "recurring": True,  "category": "membership"},
        {"name": "5 Pack",   "credits": 5,  "seed_total": 175, "recurring": False, "category": "pack"},
        {"name": "10 Pack",  "credits": 10, "seed_total": 300, "recurring": False, "category": "pack"},
        {"name": "20 Pack",  "credits": 20, "seed_total": 500, "recurring": False, "category": "pack"},
        {"name": "Drop In",  "credits": 1,  "seed_total": 40,  "recurring": False, "category": "single"},
    ],
}
# Which StretchMed product represents the brand's flagship membership:
SM_HEADLINE_PRODUCT = "Standard"

# iFlex — Noblesville. Singles/intros/summer-drop-in are static text on the menu
# page; the two recurring memberships hide in the booking widget.
IFLEX = {
    "brand": "iFlex",
    "location": "Noblesville",
    "address": "11170 E 146th St #110, Noblesville, IN 46060",
    "menu_url": "https://www.iflexstretchstudios.com/memberships",
    "booking_url": ("https://booking.iflexstretchstudios.com/webstoreNew/sales/"
                    "membership/c67a055e-97a2-47ec-8d7f-4e52fec5ec30"),
    # where: "static" (on the menu HTML) | "booking" (behind the widget)
    "offers": [
        {"name": "Membership 2x50", "structure": "2 x 50-min/mo", "category": "membership",
         "count": 2, "min": 50, "seed": 158, "where": "booking"},
        {"name": "Membership 2x25", "structure": "2 x 25-min/mo", "category": "membership",
         "count": 2, "min": 25, "seed": 79,  "where": "booking"},
        {"name": "Single 50-min",   "structure": "single 50-min", "category": "single",
         "count": 1, "min": 50, "seed": 109, "where": "static"},
        {"name": "Single 25-min",   "structure": "single 25-min", "category": "single",
         "count": 1, "min": 25, "seed": 59,  "where": "static"},
        {"name": "Summer Drop-In 50-min", "structure": "50-min drop-in (seasonal)", "category": "promo",
         "count": 1, "min": 50, "seed": 60,  "where": "static"},
        {"name": "Intro 50-min",    "structure": "first session (new client)", "category": "intro",
         "count": 1, "min": 50, "seed": 39,  "where": "static"},
        {"name": "Intro 25-min",    "structure": "first session (new client)", "category": "intro",
         "count": 1, "min": 25, "seed": 29,  "where": "static"},
    ],
}

# StretchLab — 5 Indy studios. Microsite slug locates the (price-less) pricing page;
# `street` is the stable match key against the studio finder if we ever resolve it live.
STRETCHLAB = {
    "brand": "StretchLab",
    "studios": [
        {"location": "Carmel",    "street": "2462",  "address": "2462 E 146th St, Carmel, IN 46033",
         "url": "https://www.stretchlab.com/location/carmel"},
        {"location": "Keystone",  "street": "8500",  "address": "8500 Keystone Crossing Ste 410, Indianapolis, IN 46240",
         "url": "https://www.stretchlab.com/location/northindy"},
        {"location": "Downtown",  "street": "745",   "address": "745 E 9th St, Indianapolis, IN 46202",
         "url": "https://www.stretchlab.com/location/downtownindy"},
        {"location": "Fishers",   "street": "11679", "address": "11679 Olio Rd, Fishers, IN 46037",
         "url": "https://www.stretchlab.com/location/fishers"},
        {"location": "Avon",      "street": "10722", "address": "10722 E US Hwy 36, Avon, IN 46123",
         "url": "https://www.stretchlab.com/location/indywestavon"},
    ],
}

# ───────────────────────── HTTP / RENDER HELPERS ─────────────────────────
def _fetch(url, timeout=20):
    """GET a URL and return whitespace-collapsed page text, or None on failure."""
    try:
        r = requests.get(url, timeout=timeout, headers=HEADERS)
        r.raise_for_status()
        return re.sub(r"\s+", " ", BeautifulSoup(r.text, "html.parser").get_text(" "))
    except Exception as e:
        log.warning("fetch failed for %s (%s)", url, e)
        return None


def _looks_static(text, min_prices=2):
    """Fetch-then-detect gate: are numeric prices actually in the static HTML?
    A full pricing menu shows several dollar figures; a booking-widget stub shows
    at most an intro. Require >= min_prices distinct 2-4 digit dollar amounts."""
    if not text:
        return False
    return len(re.findall(r"\$\s*\d{2,4}", text)) >= min_prices


def _have_playwright():
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def _render_js(url, wait_for=None, click_next_selector=None, max_clicks=8):
    """Render a JS page with headless Chromium and return its HTML text, or None.
    Optionally drive a 'next'/carousel arrow, accumulating lazily-rendered offers.
    Never raises — a missing Playwright or a render error returns None so callers
    fall back to seed/retail instead of crashing."""
    if not _have_playwright():
        return None
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, timeout=30000, wait_until="networkidle")
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=8000)
                except Exception:
                    pass
            html = page.content()
            if click_next_selector:
                for _ in range(max_clicks):
                    try:
                        btn = page.query_selector(click_next_selector)
                        if not btn or not btn.is_enabled():
                            break
                        btn.click()
                        page.wait_for_timeout(700)
                        nxt = page.content()
                        if nxt == html:
                            break
                        html += "\n" + nxt
                    except Exception:
                        break
            browser.close()
            return re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" "))
    except Exception as e:
        log.warning("Playwright render failed for %s (%s)", url, e)
        return None


# ───────────────────── PARSE / CLASSIFY (every brand) ─────────────────────
def parse_offer(name="", structure="", sessions_per_month=None, session_min=None,
                credits=None, credit_minutes=None):
    """Extract (session_count, session_min) from an offer.

    Precedence: explicit columns > name/structure parse > credit reconciliation.
    Returns {"session_count", "session_min", "source"}.
    """
    # 1) EXPLICIT columns (manual CSV: Stretch Zone, Stretch Indy)
    if sessions_per_month is not None or session_min is not None:
        return {"session_count": sessions_per_month, "session_min": session_min,
                "source": "explicit_columns"}

    text = f"{name} {structure}".lower()

    # 2) NAME / STRUCTURE parse (StretchLab codes, iFlex names) — order matters
    m = re.search(r"(\d+)\s*x\s*(\d+)", text)                  # "4x25", "2 x 50-min/mo"
    if m:
        return {"session_count": int(m.group(1)), "session_min": int(m.group(2)),
                "source": "parsed_name"}
    m = re.search(r"(\d+)\s*pack\D*(\d+)", text)               # "3Pack-25", "10 Pack 50"
    if m:
        return {"session_count": int(m.group(1)), "session_min": int(m.group(2)),
                "source": "parsed_name"}
    m = re.search(r"single[\s\-]*(\d+)", text)                 # "Single-50", "Single 50"
    if m:
        return {"session_count": 1, "session_min": int(m.group(1)), "source": "parsed_name"}
    m = re.search(r"(\d+)\s*[-\s]?min", text)                  # bare "50-min" -> single
    if m:
        return {"session_count": 1, "session_min": int(m.group(1)), "source": "parsed_name"}

    # 3) CREDIT reconciliation (StretchMed) — credits != sessions; only the
    #    1-credit (Drop-In, 25 min) case feeds the single anchor.
    if credits is not None:
        return {"session_count": None, "session_min": credit_minutes, "source": "credits"}

    return {"session_count": None, "session_min": None, "source": "parse_failed"}


def classify_offer(offer):
    """Return {category, is_promo, is_intro, is_shared}. Prefers an explicit
    `category` hint set by a scraper that knows its own menu; otherwise infers
    from the name/structure/session shape."""
    name = (offer.get("name") or "").lower()
    struct = (offer.get("structure") or "").lower()
    text = name + " " + struct
    cat = offer.get("category")
    is_shared = bool(offer.get("is_shared")) or "(s)" in name or "shared" in text
    if cat is None:
        if "intro" in text or "consult" in text:
            cat = "intro"
        elif offer.get("sessions_per_month"):
            cat = "membership"
        elif (offer.get("session_count") or 0) > 1:
            cat = "pack"
        else:
            cat = "single"
    return {"category": cat, "is_promo": cat == "promo",
            "is_intro": cat == "intro", "is_shared": is_shared}


def _compute_rates(offer):
    """Fill price_per_session and price_per_min from price + session_count + session_min.
    Session-based brands only (StretchMed keeps its own credit-anchor math)."""
    price = offer.get("price")
    if price is None:
        return
    n = offer.get("session_count") or 1
    per_sess = price / n if n else price
    offer["price_per_session"] = round(per_sess, 2)
    smin = offer.get("session_min")
    if smin:
        offer["price_per_min"] = round(per_sess / smin, 2)


def _finalize(offer):
    """Apply classify + rate math to a session-based offer (in place)."""
    offer.update(classify_offer(offer))
    _compute_rates(offer)
    return offer


# ───────────────────────── STRETCHMED SCRAPE ─────────────────────────
def _scrape_price(clean, product):
    """Extract a StretchMed product's total price from the normalized page text.

    The live page (WordPress/Elementor) renders each plan as a card:
        "<Name> <N> Credits $ <total> $<perCredit> per credit"   (memberships)
        "<Name> $ <total> $<perCredit> per credit"                (credit packs)
    where the "$" and the digits sit in SEPARATE DOM nodes. We anchor the total
    to the product NAME and, for memberships, its CREDIT COUNT — matching the
    right credit count proves the correct card was matched and survives a future
    repricing (so we do NOT hard-assert total == per_credit * credits, which
    would false-fail when per-credit rounds).

    Returns (total:int, "ok") on success, or (None, reason) if not found/validated.
    """
    name = re.escape(product["name"])
    if product["recurring"]:
        m = re.search(name + r"\s+(\d+)\s+Credits\s+\$\s*([\d,]+)\s+\$\s*([\d,]+)\s*per credit",
                      clean)
        if not m:
            return None, "name_not_found"
        page_credits = int(m.group(1))
        total = int(m.group(2).replace(",", ""))
        per_credit = int(m.group(3).replace(",", ""))
        if page_credits != product["credits"]:        # wrong card matched
            return None, f"credits_mismatch_{page_credits}"
        if per_credit * product["credits"] != total:  # informational only — don't fail
            log.warning("StretchMed %s: $%d/credit × %d ≠ total $%d (repriced?) — "
                        "trusting scraped total", product["name"], per_credit,
                        product["credits"], total)
        return total, "ok"
    # credit pack: no credit count in the text, so gate on a positive, in-range total
    m = re.search(name + r"\s+\$\s*([\d,]+)\s+\$\s*([\d,]+)\s*per credit", clean)
    if not m:
        return None, "name_not_found"
    total = int(m.group(1).replace(",", ""))
    if not 0 < total <= 2000:
        return None, f"total_out_of_range_{total}"
    return total, "ok"


def scrape_stretchmed():
    cfg = STRETCHMED
    now = datetime.datetime.now().isoformat(timespec="seconds")
    clean = _fetch(cfg["url"])
    method = "scraped" if clean is not None else "fallback_seed"
    if clean is None:
        log.warning("StretchMed live fetch failed — using last-known seed prices")
    else:
        log.info("StretchMed: fetched live page (%d chars)", len(clean))

    offers = []
    for p in cfg["products"]:
        if method == "fallback_seed":            # network down: seed, flagged via source.method
            total, status = p["seed_total"], "ok"
        else:
            total, reason = _scrape_price(clean, p)
            if total is None:
                total, status = p["seed_total"], "seed_used_" + reason
                log.error("StretchMed %s: live scrape unverified (%s) — seed shown, NEEDS REVIEW",
                          p["name"], reason)
            else:
                status = "ok"
        per_credit = round(total / p["credits"], 2)
        # normalize to the ~50-min anchor (StretchMed: 55 min = 3 credits)
        anchor_price = round(per_credit * SM_ANCHOR_CREDITS, 2)
        # session shape for the parser/anchors: only the 1-credit Drop-In is a true single
        if p["category"] == "single":
            s_count, s_min = 1, SM_CREDIT_MINUTES.get(p["credits"])
        elif p["category"] == "pack":
            s_count, s_min = p["credits"], None
        else:                                    # membership: credits, not fixed sessions
            s_count, s_min = None, None
        offer = {
            "name": p["name"],
            "structure": f"{p['credits']} credits" + ("/mo" if p["recurring"] else " pack"),
            "price": total,
            "unit_price": per_credit,
            "unit": "credit",
            "credits": p["credits"],
            "session_count": s_count,
            "session_min": s_min,
            "category": p["category"],
            "anchor_session_min": SM_ANCHOR_MIN,
            "price_per_session": anchor_price,
            "price_per_min": round(anchor_price / SM_ANCHOR_MIN, 2),
            "is_headline": p["name"] == SM_HEADLINE_PRODUCT,
            "status": status,
        }
        offer.update(classify_offer(offer))     # is_promo/is_intro/is_shared (all False here)
        offers.append(offer)
    return {
        "brand": cfg["brand"], "location": cfg["location"], "address": cfg["address"],
        "source": {"method": method, "url": cfg["url"], "collected_at": now},
        "offers": offers,
    }


# ───────────────────────── iFLEX SCRAPE (hybrid) ─────────────────────────
def scrape_iflex():
    cfg = IFLEX
    now = datetime.datetime.now().isoformat(timespec="seconds")
    menu = _fetch(cfg["menu_url"])
    method = "scraped" if menu is not None else "fallback_seed"

    # The recurring memberships hide in the booking widget — render it once if we can.
    booking = None
    if any(o["where"] == "booking" for o in cfg["offers"]) and _have_playwright():
        booking = _render_js(cfg["booking_url"], wait_for="text=$")

    offers = []
    for o in cfg["offers"]:
        seed = o["seed"]
        price, status = seed, "ok"
        if method == "fallback_seed":
            status = "seed_used_fetch_failed"
        elif o["where"] == "static":
            # confirm the price appears on the live menu page (detects drift as "not_confirmed")
            if menu and re.search(rf"\$\s*{seed}\b", menu):
                status = "ok"
            else:
                status = "seed_used_not_confirmed"
                log.warning("iFlex %s: $%s not confirmed on live menu — seed shown, REVIEW", o["name"], seed)
        else:  # booking-widget membership
            if booking and re.search(rf"\$\s*{seed}\b", booking):
                status = "ok"
            else:
                status = "seed_used_booking_widget"
        offer = {
            "name": o["name"], "structure": o["structure"], "price": float(price),
            "session_count": o["count"], "session_min": o["min"], "category": o["category"],
            "is_headline": o["category"] == "membership" and o["count"] == 2 and o["min"] == 50,
            "status": status,
        }
        _finalize(offer)
        offers.append(offer)
    return {
        "brand": cfg["brand"], "location": cfg["location"], "address": cfg["address"],
        "source": {"method": method, "url": cfg["menu_url"], "collected_at": now},
        "offers": offers,
    }


# ───────────────────── STRETCHLAB RETAIL + SCRAPE + ORACLE ─────────────────
def load_stretchlab_retail():
    if not STRETCHLAB_RETAIL.exists():
        log.warning("No stretchlab_retail.csv — StretchLab validation/seed unavailable.")
        return []
    rows = []
    with open(STRETCHLAB_RETAIL, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({
                "offer_code": r["offer_code"], "category": r["category"],
                "sessions": int(r["sessions"]), "session_min": int(r["session_min"]),
                "price": float(r["price"]),
                "shared": r["shared"].strip().lower() == "yes",
                "promo": r["promo"].strip().lower() == "yes",
                # socks distinguishes the intro variants (Intro-50 $49 vs Intro-50-Socks $69);
                # .get default keeps a column-less confidential CSV loading (socks -> False).
                "socks": (r.get("socks") or "no").strip().lower() == "yes",
            })
    return rows


def _studio_from_retail(st, retail):
    """Build a full StretchLab studio from the retail list (unscrapable → retail_seed)."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    offers = []
    for r in retail:
        offer = {
            "name": r["offer_code"],
            "structure": f"{r['sessions']} x {r['session_min']}-min {r['category']}"
                         + (" (shared)" if r["shared"] else ""),
            "price": r["price"],
            "session_count": r["sessions"], "session_min": r["session_min"],
            "category": "promo" if (r["promo"] and r["category"] != "intro") else r["category"],
            "is_shared": r["shared"],
            "is_socks": r["socks"],
            "variant": "socks" if r["socks"] else "standard",
            "is_headline": False,
            "status": "retail_seed",
            "deviation_delta": None,
        }
        _finalize(offer)
        offers.append(offer)
    return {
        "brand": STRETCHLAB["brand"], "location": st["location"], "address": st["address"],
        "source": {"method": "retail_seed", "url": st["url"], "collected_at": now},
        "offers": offers,
    }


def _match_retail(offer, retail):
    """Match an offer to a retail row by IDENTITY: (category, sessions, session_min, shared, socks).
    is_socks distinguishes the intro variants (Intro-50 $49 vs Intro-50-Socks $69) that used to
    collapse to one key — intros now bind on identity, NEVER on price, so moving a variant toward
    the other's corporate value can no longer silently mis-bind it. Nearest-price survives only as
    a genuine last-resort tie-break for non-intro categories (and never fires today)."""
    key = (offer.get("category"), offer.get("session_count"),
           offer.get("session_min"), bool(offer.get("is_shared")), bool(offer.get("is_socks")))
    cands = [r for r in retail
             if (r["category"], r["sessions"], r["session_min"], r["shared"], r["socks"]) == key]
    if not cands:
        return None
    if offer.get("category") == "intro" or len(cands) == 1:
        return cands[0]            # intros bind by identity; unique key elsewhere needs no tie-break
    price = offer.get("price")
    if price is None:
        return cands[0]
    return min(cands, key=lambda r: abs(r["price"] - price))   # last-resort, non-intro only


def annotate_corporate_position(studio, retail):
    """Annotate each scraped offer with its position vs the corporate RECOMMENDED card.
    NOT a validator — a gap between the studio's LISTED (scraped) price and corporate's
    recommended price is an intentional pricing-position FINDING, not an error.
    `deviation_delta` and `pricing_position` are CONFIDENTIAL (they expose the corporate
    card by inference): they live ONLY in gitignored data (latest.json/js, snapshots),
    never in dashboard_view or any committed file."""
    if studio["source"].get("method") != "scraped":   # fallback (seed) studios: nothing to compare
        return studio
    for o in studio["offers"]:
        r = _match_retail(o, retail)
        if r is None:
            o["pricing_position"], o["deviation_delta"] = "no_baseline", None
            continue
        delta = round(o["price"] - r["price"], 2)          # + = listed above corporate recommended
        o["deviation_delta"] = delta
        o["pricing_position"] = "at" if abs(delta) < 0.01 else ("above" if delta > 0 else "below")
        if o["pricing_position"] != "at":
            log.info("StretchLab %s %s: listed $%s vs corporate $%s (%+.2f -> %s)",
                     studio["location"], o["name"], o["price"], r["price"], delta, o["pricing_position"])
    return studio


# ── StretchLab microsite pricing API (members.stretchlab.com) ──
# Each microsite renders its menu client-side from a public JSON endpoint keyed by an
# API slug (e.g. "stretchlab-north-indy"). The slug is NOT the URL slug ("northindy")
# and the /api/locations/<slug>/packages PATH is not in the page HTML — but the slug
# itself is (a server-rendered data-nav-location id + members.stretchlab.com login links).
MEMBERS_API = "https://members.stretchlab.com/api/locations"
_SLUG_RES = [
    re.compile(r"location_id=(stretchlab-[a-z0-9-]+)"),
    re.compile(r"""["']id["']\s*:\s*["'](stretchlab-[a-z0-9-]+)["']"""),
    re.compile(r"members\.stretchlab\.com[^\"'\s]*?(stretchlab-[a-z0-9-]+)"),
]


def _discover_stretchlab_slug(url):
    """Find a microsite's members.stretchlab.com API slug. requests-on-raw-HTML first
    (the slug is server-rendered), Playwright network-capture fallback (recon-proven).
    Returns (slug, how) on success or (None, reason)."""
    try:
        html = requests.get(url, headers=HEADERS, timeout=20).text
    except Exception as e:
        log.warning("StretchLab slug fetch failed for %s (%s)", url, e)
        html = ""
    for rx in _SLUG_RES:
        hits = rx.findall(html)
        if hits:
            return collections.Counter(hits).most_common(1)[0][0], "html"
    slug = _slug_via_playwright(url)
    return (slug, "playwright") if slug else (None, "slug_not_found")


def _slug_via_playwright(url):
    """Fallback: load the microsite and capture the slug from the packages request the
    page's own JS fires (the exact call recon observed). Returns slug or None."""
    if not _have_playwright():
        return None
    try:
        from playwright.sync_api import sync_playwright
        rx = re.compile(r"/api/locations/(stretchlab-[a-z0-9-]+)/packages")
        found = {}

        def _on_response(resp):
            m = rx.search(resp.url)
            if m and "slug" not in found:
                found["slug"] = m.group(1)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.on("response", _on_response)
            page.goto(url, timeout=30000, wait_until="networkidle")
            browser.close()
        return found.get("slug")
    except Exception as e:
        log.warning("slug playwright fallback failed for %s (%s)", url, e)
        return None


def _fetch_stretchlab_packages(slug):
    """GET the public packages JSON for a slug. Returns the packages list or None."""
    try:
        r = requests.get(f"{MEMBERS_API}/{slug}/packages",
                         headers={**HEADERS, "Accept": "application/json"}, timeout=20)
        r.raise_for_status()
        return (r.json().get("packages") or []) or None
    except Exception as e:
        log.warning("StretchLab packages API failed for %s (%s)", slug, e)
        return None


def _smin_from(pkg):
    """Session length in minutes: from the credit label ("Stretch - 50 mins") then name."""
    for c in (pkg.get("credits") or []):
        m = re.search(r"(\d+)\s*min", (c.get("name") or "").lower())
        if m:
            return int(m.group(1))
    m = re.search(r"(\d+)\s*minute", (pkg.get("name") or "").lower())
    return int(m.group(1)) if m else None


def _api_category(pkg):
    """Map an API package to the offer enum {addon, membership, intro, promo, pack, single}.
    First match wins. Packages and single-sessions both arrive as package_type=package, so
    they are told apart by credit_count; non-stretch add-ons (NormaTec) are split out."""
    name = (pkg.get("name") or "").lower()
    if pkg.get("is_addon") or pkg.get("is_service") or "normatec" in name or "compression" in name:
        return "addon"
    if pkg.get("package_type") == "membership" or pkg.get("is_membership") or pkg.get("is_recurring"):
        return "membership"
    # intro by NAME only — the API also sets is_first_timer_booking on discounted non-member
    # SINGLES, so keying on that flag would misfile "Single … Non-Member Rate" as an intro.
    if "introduct" in name or "first time" in name or name.startswith("first"):
        return "intro"
    if pkg.get("is_free") or (pkg.get("price") or {}).get("numeric") in (0, 0.0):
        return "promo"
    return "pack" if (pkg.get("credit_count") or 1) > 1 else "single"


def _map_api_package(pkg):
    """One API package -> one offer dict in the project's schema (pre-_finalize).
    Confidential fields (deviation_delta, pricing_position) are filled later by
    annotate_corporate_position. Returns None for priceless rows."""
    price = (pkg.get("price") or {}).get("numeric")
    if price is None:
        return None
    cat = _api_category(pkg)
    smin = _smin_from(pkg)
    cnt = pkg.get("credit_count") or 1
    name_l = (pkg.get("name") or "").lower()
    is_socks = "socks" in name_l          # intro variant token (e.g. "... with Grip Socks")
    offer = {
        "name": pkg.get("name"),
        "structure": pkg.get("description") or (f"{cnt} x {smin}-min"
                                                + ("/mo" if cat == "membership" else "")),
        "price": float(price),
        "session_count": cnt,          # per-session-in-month math for memberships (matches retail path)
        "session_min": smin,
        "category": cat,
        "is_addon": cat == "addon",
        "is_socks": is_socks,
        "variant": "socks" if is_socks else "standard",
        "is_headline": False,
        "deviation_delta": None,       # CONFIDENTIAL — filled by annotate_corporate_position
        "pricing_position": None,      # CONFIDENTIAL
        # flag genuine unknowns for needs_review; add-ons legitimately lack a session_min
        "mapping_flag": (None if (cat == "addon"
                                  or (smin and cat in {"membership", "single", "pack", "intro", "promo"}))
                         else "unresolved_session_min"),
    }
    if pkg.get("is_unlimited"):
        offer["is_unlimited"] = True
        offer["mapping_flag"] = offer["mapping_flag"] or "unlimited_no_permin"
    return offer


def scrape_stretchlab():
    """Per studio: discover the API slug, GET the packages JSON, map to offers -> scraped.
    retail_seed is now a genuine FALLBACK only (slug/API failure), logged loudly and never
    labeled scraped."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    retail = load_stretchlab_retail()
    out = []
    for st in STRETCHLAB["studios"]:
        slug, how = _discover_stretchlab_slug(st["url"])
        pkgs = _fetch_stretchlab_packages(slug) if slug else None
        if pkgs:
            offers = [o for o in (_map_api_package(p) for p in pkgs) if o]
            for o in offers:
                o["status"] = "scraped"
                _finalize(o)
            studio = {"brand": STRETCHLAB["brand"], "location": st["location"],
                      "address": st["address"],
                      "source": {"method": "scraped", "url": st["url"],
                                 "api": f"{MEMBERS_API}/{slug}/packages", "collected_at": now},
                      "offers": offers}
            annotate_corporate_position(studio, retail)   # CONFIDENTIAL variance annotation (local only)
            addons = sum(1 for o in offers if o.get("is_addon"))
            log.info("StretchLab %s: scraped %d offers via API (slug=%s via %s%s)",
                     st["location"], len(offers), slug, how,
                     f", {addons} add-on" if addons else "")
        else:
            reason = "api_empty" if slug else how
            studio = _studio_from_retail(st, retail)
            studio["source"]["method"] = f"seed_used_{reason}"   # never labeled scraped
            log.error("StretchLab %s: API scrape FAILED (%s) -> retail_seed fallback",
                      st["location"], reason)
        out.append(studio)
    return out


# ───────────────────── MANUAL SOURCES ─────────────────────
def load_manual():
    path = DATA / "manual_entries.csv"
    if not path.exists():
        log.warning("No manual_entries.csv found — competitors will be empty.")
        return []
    studios = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["brand"], row["location"])
            s = studios.setdefault(key, {
                "brand": row["brand"], "location": row["location"],
                "address": row["address"],
                "source": {"method": row["method"], "url": row["source_note"],
                           "collected_at": datetime.date.today().isoformat()},
                "offers": [],
            })
            price = row["price"].strip()
            spm = row["sessions_per_month"].strip()
            smin = row["session_min"].strip()
            spm_i = int(spm) if spm else None
            smin_i = int(smin) if smin else None
            parsed = parse_offer(name=row["offer_name"], structure=row["structure"],
                                 sessions_per_month=spm_i, session_min=smin_i)
            offer = {
                "name": row["offer_name"], "structure": row["structure"],
                "price": float(price) if price else None,
                "sessions_per_month": spm_i,
                "session_count": parsed["session_count"] or (1 if price else None),
                "session_min": parsed["session_min"],
                "status": row["status"],
                "is_headline": row.get("is_headline", "").strip().lower() == "yes",
            }
            _finalize(offer)                     # classify + per-session/per-min
            s["offers"].append(offer)
    return list(studios.values())


# ───────────────────── TWO-ANCHOR NORMALIZATION ─────────────────────
def normalize_anchors(studios):
    """Per studio, compute two comparable anchors:
       headline  = standard SINGLE session nearest ~50 min (+ comparability flag)
       secondary = best (lowest) committed MEMBERSHIP $/min
    Promotions, intros and shared (S) rows are excluded from both."""
    for s in studios:
        brand = s["brand"]
        offers = s["offers"]

        def standard(o):
            return (not o.get("is_promo") and not o.get("is_intro")
                    and not o.get("is_shared") and not o.get("is_addon"))

        # HEADLINE — single-session standard rate at ~50 min
        singles = [o for o in offers if brand != "StretchMed" and o.get("category") == "single"
                   and standard(o) and o.get("price") is not None and o.get("session_min")]
        head = None
        if singles:
            best = min(singles, key=lambda o: abs(o["session_min"] - ANCHOR_MIN))
            head = {"pps": best["price_per_session"], "min": best["session_min"], "name": best["name"]}
        elif brand == "StretchMed":
            # StretchMed sells credits, not fixed-length singles. Build the single-session credit
            # ladder (1-4 credits -> 25/40/55/70 min) at the 1-credit drop-in rate and apply the SAME
            # nearest-50-min rule the other brands use. The 3-credit/55-min unit wins — IMPUTED, since
            # there is no advertised standalone 55-min single (do NOT pick the smallest atomic unit).
            di = next((o for o in offers if o.get("category") == "single" and o.get("price") is not None), None)
            if di:
                per_credit = float(di["price"])            # 1-credit drop-in rack rate
                ladder = [{"min": SM_CREDIT_MINUTES[n], "pps": round(per_credit * n, 2), "n": n}
                          for n in sorted(SM_CREDIT_MINUTES)]
                best = min(ladder, key=lambda c: abs(c["min"] - ANCHOR_MIN))
                head = {"pps": best["pps"], "min": best["min"], "n": best["n"],
                        "name": f"{best['n']}-credit {best['min']}-min single", "imputed": best["n"] != 1}
        if head and head["min"]:
            m = head["min"]
            comp = "exact" if m == ANCHOR_MIN else "close" if CLOSE_MIN <= m <= CLOSE_MAX else "caveat"
            anchor = {
                "price_per_session": head["pps"], "price_per_min": round(head["pps"] / m, 2),
                "session_min": m, "comparability": comp, "offer_name": head["name"],
            }
            if head.get("imputed"):
                anchor["provenance"] = f"imputed_{head['n']}credit_{m}min"
            s["headline_anchor"] = anchor
        else:
            s["headline_anchor"] = None

        # SECONDARY — best committed membership $/min
        mems = [o for o in offers if o.get("category") == "membership" and standard(o)
                and o.get("price_per_min") is not None]
        if mems:
            best = min(mems, key=lambda o: o["price_per_min"])
            s["secondary_anchor"] = {"price_per_min": best["price_per_min"], "offer_name": best["name"]}
        else:
            s["secondary_anchor"] = None


# ───────────────────────── GEOCODING ─────────────────────────
def _load_geocode_cache():
    if GEOCODE_CACHE.exists():
        try:
            return json.loads(GEOCODE_CACHE.read_text())
        except Exception:
            log.warning("geocode_cache.json unreadable — starting fresh")
    return {}


def _address_variants(addr):
    """Progressively-looser query variants so suite/unit tokens and highway
    abbreviations don't defeat Nominatim's structured match. Ordered most- to
    least-specific; the first hit wins (street-level preferred, zip centroid last)."""
    a = " ".join(addr.split())
    variants = []
    # normalize "US Hwy 36" -> "US Highway 36" (Nominatim prefers the long form)
    a = re.sub(r"\bUS Hwy (\d+)\b", r"US Highway \1", a, flags=re.I)
    variants.append(a)
    # drop suite/unit designators (Ste 410, #110, Unit 4, Apt 2, ...)
    stripped = re.sub(r",?\s*(?:ste|suite|unit|apt|#)\s*[\w-]+", "", a, flags=re.I)
    stripped = " ".join(stripped.split())
    variants.append(stripped)
    parts = [p.strip() for p in stripped.split(",")]
    if len(parts) >= 3:
        street = parts[0]
        # trailing bare unit number after a street suffix ("Winthrop Ave 4" -> "Winthrop Ave")
        if re.search(r"(ave|st|dr|rd|blvd|ln|ct|way|pl|ter)\s+\d+$", street, re.I):
            street = re.sub(r"\s+\d+$", "", street)
        city_state_zip = ", ".join(parts[1:])
        variants.append(f"{street}, {city_state_zip}")
        variants.append(city_state_zip)                 # city, state zip centroid fallback
    seen, out = set(), []
    for v in variants:
        v = v.strip(" ,")
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _geocode_one(address):
    """Return (lat, lng) for the first query variant Nominatim resolves, else
    (None, None). Rate-limits internally (Nominatim policy: <= 1 req/sec)."""
    for q in _address_variants(address):
        time.sleep(GEO_RATE_SEC)
        try:
            r = requests.get(NOMINATIM_URL, headers=GEO_HEADERS, timeout=15,
                             params={"q": q, "format": "json", "limit": 1, "countrycodes": "us"})
            r.raise_for_status()
            data = r.json()
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
        except Exception as e:
            log.warning("geocode attempt failed for %r (%s)", q, e)
    return None, None


def geocode_studios(studios):
    """Add lat/lng to each studio. Cache-first (data/geocode_cache.json); only
    uncached, real addresses hit Nominatim. Failures are NOT cached, so they retry
    on the next run; a persistent miss leaves null coords and the studio still
    renders in the tables (just not on the map)."""
    cache = _load_geocode_cache()
    dirty = False
    for s in studios:
        addr = (s.get("address") or "").strip()
        if not addr or "ADD ADDRESS" in addr.upper():
            s["lat"], s["lng"] = None, None
            continue
        if addr in cache:
            s["lat"], s["lng"] = cache[addr]["lat"], cache[addr]["lng"]
            continue
        lat, lng = _geocode_one(addr)
        s["lat"], s["lng"] = lat, lng
        if lat is None:
            log.warning("no geocode for %s %s (%s)", s["brand"], s["location"], addr)
        else:
            cache[addr] = {"lat": lat, "lng": lng}      # cache successes only
            dirty = True
    if dirty:
        GEOCODE_CACHE.write_text(json.dumps(cache, indent=2))
        log.info("geocode cache updated (%d addresses)", len(cache))


# ─────────────────────── VALIDATE ─────────────────────────
def validate(snapshot):
    # pricing_findings is a FINDING (intentional pricing position vs the corporate card),
    # NOT an error — and CONFIDENTIAL (delta exposes the corporate card): it stays in the
    # gitignored snapshot only, never in dashboard_view or any committed file.
    issues, pricing_findings, needs_review, geocode_missing = [], [], [], []
    for s in snapshot["studios"]:
        if not s["source"].get("method") or not s["source"].get("collected_at"):
            issues.append(f"{s['brand']} {s['location']}: missing provenance")
        if s.get("lat") is None and (s.get("address") and "ADD ADDRESS" not in s["address"].upper()):
            geocode_missing.append(f"{s['brand']} {s['location']}")
        for o in s["offers"]:
            if o.get("price") is not None and o["price"] < 0:
                issues.append(f"{s['brand']} {o['name']}: negative price")
            if o.get("pricing_position") in ("above", "below"):
                pricing_findings.append({"studio": f"{s['brand']} {s['location']}", "offer": o["name"],
                                         "delta": o.get("deviation_delta"), "position": o.get("pricing_position")})
            if o.get("mapping_flag") or (o.get("session_min") is None and o.get("category") == "single"):
                needs_review.append(f"{s['brand']} {s['location']} · {o['name']} "
                                    f"({o.get('mapping_flag') or 'missing session_min'})")
            # an unexpected no_baseline (a NON-add-on with no corporate match — e.g. an intro
            # variant absent from the card) is surfaced, not pooled with baseline-less add-ons.
            if o.get("pricing_position") == "no_baseline" and not o.get("is_addon"):
                needs_review.append(f"{s['brand']} {s['location']} · {o['name']} (no corporate baseline)")
    incomplete = [f"{s['brand']} {s['location']}" for s in snapshot["studios"]
                  if all(o.get("price") is None for o in s["offers"])]
    snapshot["validation"] = {
        "errors": issues,
        "incomplete_studios": incomplete,
        "pricing_findings": pricing_findings,   # CONFIDENTIAL — local/gitignored only
        "needs_review": needs_review,
        "geocode_missing": geocode_missing,
        "checked_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    for i in issues:
        log.error("VALIDATION: %s", i)
    if incomplete:
        log.info("Awaiting data: %s", ", ".join(incomplete))
    return snapshot


# ──────────────────────── STORE ───────────────────────────
def store(snapshot):
    today = datetime.date.today().isoformat()
    (SNAPS / f"{today}.json").write_text(json.dumps(snapshot, indent=2))   # never overwrite history
    (DATA / "latest.json").write_text(json.dumps(snapshot, indent=2))
    # latest.js lets preview.html load by double-click (no local server / CORS)
    (DATA / "latest.js").write_text("window.PRICING_DATA = " + json.dumps(snapshot, indent=2) + ";")
    log.info("Wrote snapshot %s + latest.json + latest.js", today)


# ──────────────────────── SUMMARY / REPORT ─────────────────────────
def _headline_view(s):
    h = s.get("headline_anchor")
    if h:
        return h["price_per_session"], h["price_per_min"], h["comparability"]
    return None, None, None


def print_summary(snapshot):
    print("\n" + "═" * 72)
    print(f" HEADLINE ANCHOR — standard single ~{ANCHOR_MIN}-min  |  {snapshot['market']}")
    print("═" * 72)
    print(f" {'Brand':<13}{'Location':<14}{'$/session':>10}{'$/min':>7}{'sec $/min':>11}  compare")
    print("─" * 72)
    for s in snapshot["studios"]:
        pps, ppm, comp = _headline_view(s)
        sec = s.get("secondary_anchor")
        sec_ppm = f"${sec['price_per_min']}" if sec else "—"
        if pps is not None:
            print(f" {s['brand']:<13}{s['location']:<14}${pps:>8}{ppm:>7}{sec_ppm:>11}  {comp}")
        else:
            print(f" {s['brand']:<13}{s['location']:<14}{'—':>10}{'—':>7}{sec_ppm:>11}  awaiting data")
    print("═" * 72)


def print_report(snapshot):
    """Per-site scrape method + StretchLab validation oracle outcome (Block 3a req 8)."""
    print("\n SCRAPE METHOD PER SITE")
    print(" " + "─" * 52)
    for s in snapshot["studios"]:
        print(f"  {s['brand']:<12}{s['location']:<14}{s['source']['method']}")

    sl = [s for s in snapshot["studios"] if s["brand"] == "StretchLab"]
    if sl:
        print("\n STRETCHLAB PRICING POSITION vs corporate card  [CONFIDENTIAL — local only]")
        print(" " + "─" * 52)
        for s in sl:
            counts = {}
            for o in s["offers"]:
                key = o.get("pricing_position") or o["status"]   # at/above/below/no_baseline, else seed
                counts[key] = counts.get(key, 0) + 1
            summary = ", ".join(f"{k}×{v}" for k, v in sorted(counts.items()))
            print(f"  {s['location']:<12}{s['source']['method']:<20}{summary}")
        finds = snapshot["validation"].get("pricing_findings", [])
        if finds:
            print("\n  Off-corporate positions (listed − recommended):")
            for d in finds:
                print(f"    {d['studio']} · {d['offer']}: {d['position']} {d['delta']:+.2f}")
        else:
            print("\n  No off-corporate positions — every scraped price matches the corporate card.")
    print("═" * 72 + "\n")


# ──────────────────── DASHBOARD VIEW ──────────────────────
# Synthesis step: derive the nested, aggregated structure dashboard.html consumes from the
# flat studios[] (already anchored + geocoded), and write it into the SAME snapshot alongside
# studios[]. One run produces both the preview view (studios[], untouched) and the dashboard
# view (dashboard_view) — so they can never drift. No new scraping happens here.

def _round_dollar(x):
    """Round half-up to the nearest whole dollar (Python's round() is banker's rounding)."""
    return int(math.floor(x + 0.5))


def _haversine_mi(lat1, lng1, lat2, lng2):
    """Straight-line miles between two lat/lng points. Mirrors dashboard.html's miles()."""
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _parse_zip(address):
    """Trailing 5-digit ZIP from an address string; '' if none."""
    m = re.search(r"(\d{5})(?:-\d{4})?\s*$", address or "")
    return m.group(1) if m else ""


def _status_from_method(method, price):
    """Map a competitor's source.method → the dashboard's competitor status enum."""
    if price is None:
        return "none"
    return {"scraped": "scraped", "callin": "phone", "retail_seed": "login"}.get(method, "phone")


# The dashboard's Snapshot tier table is a fixed 5-row template. Each row maps to one real
# StretchLab offer. Rows whose only backing is the retail card (retail_seed) are flagged
# observed=False so the UI can mark them "not observed"; the intro row is the one scraped
# per-location price (observed=True). See plan honesty note #1.
_TIER_TEMPLATE = [
    # (display name,   length,   category,      session_min, session_count, prefer_scraped)
    ("Single session", "50 min", "single",     50, 1,    False),
    ("Single session", "25 min", "single",     25, 1,    False),
    ("4 / month",      "50 min", "membership", 50, 4,    False),
    ("8 / month",      "50 min", "membership", 50, 8,    False),
    ("Intro offer",    "50 min", "intro",      50, None, True),
]


def _pick_offer(offers, category, session_min, session_count, prefer_scraped):
    """Find the real offer backing a dashboard tier. Non-shared only. When prefer_scraped,
    return a live scraped row if present; otherwise prefer the retail-card row."""
    cands = [
        o for o in offers
        if o.get("category") == category
        and o.get("session_min") == session_min
        and not o.get("is_shared")
        and (session_count is None or o.get("session_count") == session_count)
        and o.get("price") is not None
    ]
    if not cands:
        return None
    if prefer_scraped:
        scraped = next((o for o in cands if o.get("status") == "scraped"), None)
        if scraped:
            return scraped
    return next((o for o in cands if o.get("status") == "retail_seed"), cands[0])


def _build_tiers(studio):
    """Map the fixed 5-row tier template to this studio's real offers. Both retail and listed
    carry the PUBLIC scraped price (this view never reads the confidential deviation_delta or
    the corporate card, so no variance leaks); `observed` is true when the studio was scraped
    live. Real retail-vs-listed variance is deferred to the comparison block."""
    tiers = []
    scraped_studio = studio["source"].get("method") == "scraped"
    for name, length, category, smin, scount, prefer_scraped in _TIER_TEMPLATE:
        o = _pick_offer(studio["offers"], category, smin, scount, prefer_scraped)
        price = _round_dollar(o["price"]) if o and o.get("price") is not None else None
        tiers.append({
            "name": name,
            "len": length,
            "retail": price,
            "listed": price,
            "observed": bool(o and scraped_studio),
        })
    return tiers


def _pick_intro(studio):
    """Per-studio 50-min intro price — prefer the live scraped observation (the genuinely
    per-location price: Carmel/Fishers/Avon 49, Keystone/Downtown 69)."""
    o = _pick_offer(studio["offers"], "intro", 50, None, prefer_scraped=True)
    return _round_dollar(o["price"]) if o and o.get("price") is not None else None


def build_dashboard_view(studios):
    """One nested object per StretchLab studio, matching the shape dashboard.html consumes.
    Each competitor is assigned to every StretchLab studio and tagged direct (≤ DIRECT_RADIUS_MI)
    or adjacent by straight-line distance; market aggregates span the full competitor set."""
    competitors = [s for s in studios if s["brand"] != "StretchLab"]

    # market aggregate over the full competitor set — metro-wide, identical across studios
    pps = [c["headline_anchor"]["price_per_session"] for c in competitors
           if (c.get("headline_anchor") or {}).get("price_per_session") is not None]
    ppm = [c["headline_anchor"]["price_per_min"] for c in competitors
           if (c.get("headline_anchor") or {}).get("price_per_min") is not None]
    market = {
        "min": _round_dollar(min(pps)) if pps else 0,
        "median": _round_dollar(statistics.median(pps)) if pps else 0,
        "max": _round_dollar(max(pps)) if pps else 0,
        "perMin": {
            "min": round(min(ppm), 2) if ppm else 0,
            "median": round(statistics.median(ppm), 2) if ppm else 0,
            "max": round(max(ppm), 2) if ppm else 0,
        },
    }

    view = []
    for s in studios:
        if s["brand"] != "StretchLab":
            continue
        lat, lng = s.get("lat"), s.get("lng")

        direct, adjacent = [], []
        for c in competitors:
            ha = c.get("headline_anchor") or {}
            price = ha.get("price_per_session")
            entry = {
                "name": f"{c['brand']} — {c['location']}",
                "ll": [c.get("lat"), c.get("lng")],
                "price": _round_dollar(price) if price is not None else None,
                "perMin": round(ha["price_per_min"], 2) if ha.get("price_per_min") is not None else None,
                "status": _status_from_method(c["source"].get("method"), price),
            }
            can_measure = None not in (lat, lng, c.get("lat"), c.get("lng"))
            dist = _haversine_mi(lat, lng, c["lat"], c["lng"]) if can_measure else None
            # unmeasurable competitors fall to adjacent (context), never fabricated as direct
            if dist is not None and dist <= DIRECT_RADIUS_MI:
                direct.append(entry)
            else:
                adjacent.append(entry)

        sha = s.get("headline_anchor") or {}
        view.append({
            "id": "stretchlab-" + s["location"].lower().replace(" ", "-"),
            "name": f"{s['brand']} — {s['location']}",
            "zip": _parse_zip(s.get("address", "")),
            "addr": s.get("address", ""),
            "ll": [lat, lng],
            "slPerSession": _round_dollar(sha["price_per_session"]) if sha.get("price_per_session") is not None else None,
            "intro": _pick_intro(s),
            "market": market,
            "tiers": _build_tiers(s),
            "competitors": {"direct": direct, "adjacent": adjacent},
        })
    return view


# ───────────────────────── MAIN ───────────────────────────
def main():
    studios = [scrape_stretchmed()]
    studios.append(scrape_iflex())
    studios.extend(scrape_stretchlab())
    studios.extend(load_manual())              # Stretch Zone + Stretch Indy
    # StretchLab offers are annotated with their corporate pricing-position at scrape time
    # (annotate_corporate_position, inside scrape_stretchlab) — no second pass needed here.

    normalize_anchors(studios)                 # needs all offers classified/priced
    geocode_studios(studios)                   # LAST — a Nominatim stall must not block pricing
    dashboard_view = build_dashboard_view(studios)   # derived shape for dashboard.html's four tabs

    snapshot = {
        "snapshot_date": datetime.date.today().isoformat(),
        "market": MARKET,
        "anchor_session_min": ANCHOR_MIN,
        "studios": studios,
        "dashboard_view": dashboard_view,
    }
    snapshot = validate(snapshot)
    store(snapshot)
    print_summary(snapshot)
    print_report(snapshot)


if __name__ == "__main__":
    main()
