#!/usr/bin/env python3
"""
StretchLab Competitive Pricing — Indianapolis mini-tool (V0)
=============================================================

Scope (honest about it):
  * StretchMed (Carmel) ....... AUTO-SCRAPED. Prices are server-rendered text,
                                so a simple requests + BeautifulSoup pass works.
  * iFlex (Noblesville) ....... MANUAL. Recurring prices live in a JS booking
                                widget, not the page HTML — captured by hand
                                in data/manual_entries.csv.
  * Stretch Zone (N. Indy) .... MANUAL (phone). No pricing published anywhere.
  * StretchLab (your studios) . MANUAL (internal, confidential CSV).

This is deliberately the FIRST MODULE of the full tool, not a throwaway:
config-driven catalog, provenance on every row, normalization to a common
per-minute basis, a validation pass, and snapshot-never-overwrite storage so
each monthly run becomes price history instead of a one-off.

Run:  python scraper.py
Deps: pip install requests beautifulsoup4
"""

import csv
import datetime
import json
import logging
import pathlib
import re
import sys

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

# ───────────────────────── CONFIG ─────────────────────────
# In the full tool this graduates to config.yaml. Kept inline here so the
# script runs with zero extra files. Structure is STABLE; prices are SCRAPED.
MARKET = "Indianapolis, IN"
ANCHOR_MIN = 50  # headline comparison: a ~50-minute session

# StretchMed sells credits, not fixed session lengths. Their conversion:
SM_CREDIT_MINUTES = {1: 25, 2: 40, 3: 55, 4: 70}
SM_ANCHOR_CREDITS = 3     # nearest bucket to 50 min = 55 min (3 credits)
SM_ANCHOR_MIN = 55

STRETCHMED = {
    "brand": "StretchMed",
    "location": "Carmel",
    "address": "14550 Clay Terrace Blvd, Carmel, IN 46032",
    "url": "https://stretchmedstudios.com/stretchmed-carmel-memberships/",
    # seed_total = last-known price; used as fallback if the live fetch fails
    # (e.g. offline). On a live run the scraped value overrides it.
    "products": [
        {"name": "Starter",  "credits": 4,  "seed_total": 88,  "recurring": True},
        {"name": "Standard", "credits": 8,  "seed_total": 168, "recurring": True},
        {"name": "Advanced", "credits": 12, "seed_total": 240, "recurring": True},
        {"name": "Elite",    "credits": 16, "seed_total": 304, "recurring": True},
        {"name": "5 Pack",   "credits": 5,  "seed_total": 175, "recurring": False},
        {"name": "10 Pack",  "credits": 10, "seed_total": 300, "recurring": False},
        {"name": "20 Pack",  "credits": 20, "seed_total": 500, "recurring": False},
        {"name": "Drop In",  "credits": 1,  "seed_total": 40,  "recurring": False},
    ],
}
# Which StretchMed product represents the brand in the anchor table:
SM_HEADLINE_PRODUCT = "Standard"

# ───────────────────────── SCRAPE ─────────────────────────
def _scrape_price(clean, product):
    """Extract a StretchMed product's total price from the normalized page text.

    The live page (WordPress/Elementor) renders each plan as a card:
        "<Name> <N> Credits $ <total> $<perCredit> per credit"   (memberships)
        "<Name> $ <total> $<perCredit> per credit"                (credit packs)
    where the "$" and the digits sit in SEPARATE DOM nodes. We anchor the total
    to the product NAME and, for memberships, its CREDIT COUNT — matching the
    right credit count proves the correct card was matched and survives a future
    repricing (so we do NOT hard-assert total == per_credit * credits, which
    would false-fail when per-credit rounds). Promo cards (Share-the-Love 2×40,
    BOGO "5-Pack") are dodged: they lack the "<Name> <N> Credits" shape, and the
    BOGO block uses hyphenated "5-Pack".

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
    method, clean = "scraped", None
    try:
        r = requests.get(cfg["url"], timeout=20, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0 Safari/537.36"),
            "Accept-Language": "en-US,en;q=0.9",
        })
        r.raise_for_status()
        # collapse whitespace so the split "$" / "<digits>" nodes become matchable
        clean = re.sub(r"\s+", " ", BeautifulSoup(r.text, "html.parser").get_text(" "))
        log.info("StretchMed: fetched live page (%d chars)", len(clean))
    except Exception as e:                       # graceful degradation
        method = "fallback_seed"
        log.warning("StretchMed live fetch failed (%s) — using last-known seed prices", e)

    offers = []
    for p in cfg["products"]:
        if method == "fallback_seed":            # network down: seed, flagged via source.method
            total, status = p["seed_total"], "ok"
        else:
            total, reason = _scrape_price(clean, p)
            if total is None:
                # fetched OK but this product didn't validate — surface it loudly,
                # don't pass the seed off as a live scrape
                total, status = p["seed_total"], "seed_used_" + reason
                log.error("StretchMed %s: live scrape unverified (%s) — seed shown, NEEDS REVIEW",
                          p["name"], reason)
            else:
                status = "ok"
        per_credit = round(total / p["credits"], 2)
        # normalize to the ~50-min anchor (StretchMed: 55 min = 3 credits)
        anchor_price = round(per_credit * SM_ANCHOR_CREDITS, 2)
        offers.append({
            "name": p["name"],
            "structure": f"{p['credits']} credits" + ("/mo" if p["recurring"] else " pack"),
            "price": total,
            "unit_price": per_credit,
            "unit": "credit",
            "anchor_session_min": SM_ANCHOR_MIN,
            "price_per_session": anchor_price,
            "price_per_min": round(anchor_price / SM_ANCHOR_MIN, 2),
            "is_headline": p["name"] == SM_HEADLINE_PRODUCT,
            "status": status,
        })
    return {
        "brand": cfg["brand"], "location": cfg["location"], "address": cfg["address"],
        "source": {"method": method, "url": cfg["url"], "collected_at": now},
        "offers": offers,
    }

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
            offer = {
                "name": row["offer_name"], "structure": row["structure"],
                "price": float(price) if price else None,
                "session_min": int(smin) if smin else None,
                "sessions_per_month": int(spm) if spm else None,
                "status": row["status"],
                "is_headline": row.get("is_headline", "").strip().lower() == "yes",
            }
            # normalize where we have enough to: per-session, then per-minute
            if offer["price"] is not None:
                per_sess = offer["price"] / offer["sessions_per_month"] if offer["sessions_per_month"] else offer["price"]
                offer["price_per_session"] = round(per_sess, 2)
                if offer["session_min"]:
                    offer["price_per_min"] = round(per_sess / offer["session_min"], 2)
            studios[key] = s
            s["offers"].append(offer)
    return list(studios.values())

# ─────────────────────── VALIDATE ─────────────────────────
def validate(snapshot):
    issues = []
    for s in snapshot["studios"]:
        if not s["source"].get("method") or not s["source"].get("collected_at"):
            issues.append(f"{s['brand']} {s['location']}: missing provenance")
        for o in s["offers"]:
            if o.get("price") is not None and o["price"] < 0:
                issues.append(f"{s['brand']} {o['name']}: negative price")
    incomplete = [f"{s['brand']} {s['location']}" for s in snapshot["studios"]
                  if all(o.get("price") is None for o in s["offers"])]
    snapshot["validation"] = {
        "errors": issues,
        "incomplete_studios": incomplete,   # awaiting call-in / internal fill
        "checked_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    for i in issues:
        log.error("VALIDATION: %s", i)
    if incomplete:
        log.info("Awaiting data (expected): %s", ", ".join(incomplete))
    return snapshot

# ──────────────────────── STORE ───────────────────────────
def store(snapshot):
    today = datetime.date.today().isoformat()
    (SNAPS / f"{today}.json").write_text(json.dumps(snapshot, indent=2))   # never overwrite history
    (DATA / "latest.json").write_text(json.dumps(snapshot, indent=2))
    # latest.js lets preview.html load by double-click (no local server / CORS)
    (DATA / "latest.js").write_text("window.PRICING_DATA = " + json.dumps(snapshot, indent=2) + ";")
    log.info("Wrote snapshot %s + latest.json + latest.js", today)

# ──────────────────────── SUMMARY ─────────────────────────
def print_summary(snapshot):
    print("\n" + "═" * 64)
    print(f" ANCHOR — effective ~{ANCHOR_MIN}-min session  |  {snapshot['market']}")
    print("═" * 64)
    print(f" {'Brand':<13}{'Location':<12}{'$/session':>11}{'$/min':>8}  status")
    print("─" * 64)
    for s in snapshot["studios"]:
        head = next((o for o in s["offers"] if o.get("is_headline")), None) \
            or next((o for o in s["offers"] if o.get("price_per_session") is not None), None)
        if head and head.get("price_per_session") is not None:
            print(f" {s['brand']:<13}{s['location']:<12}"
                  f"${head['price_per_session']:>9}{head.get('price_per_min','?'):>8}  {head['status']}")
        else:
            print(f" {s['brand']:<13}{s['location']:<12}{'—':>11}{'—':>8}  awaiting data")
    print("═" * 64 + "\n")

# ───────────────────────── MAIN ───────────────────────────
def main():
    studios = [scrape_stretchmed()] + load_manual()
    snapshot = {
        "snapshot_date": datetime.date.today().isoformat(),
        "market": MARKET,
        "anchor_session_min": ANCHOR_MIN,
        "studios": studios,
    }
    snapshot = validate(snapshot)
    store(snapshot)
    print_summary(snapshot)


if __name__ == "__main__":
    main()
