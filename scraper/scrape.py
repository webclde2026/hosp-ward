#!/usr/bin/env python3
"""
Singapore Hospital Room & Board Rate Tracker — monthly scraper.

What this does
---------------
Re-fetches each hospital's official room-rate page, tries to extract a
ward-class -> daily-rate table, and writes an updated data/hospitals.json.

Design principles (per the project's constraints):
  - Never fabricate or estimate a rate. If extraction fails, the hospital
    is left with its last known-good data and a "stale" note; the whole
    run must not crash because one hospital's page changed.
  - Never silently overwrite a good number with a guess. For the hospitals
    whose rates are published as an image (not machine-readable text), this
    script does NOT attempt OCR — it instead hashes the source page and
    compares it to the hash from the last run. If the page is byte-identical,
    nothing has changed and the existing (human-verified) figures are kept
    as still current. If the page HAS changed, the hospital is flagged
    data_status="needs_review" — the last known figures are kept (never
    blanked out), but a human should re-open the source page, re-read the
    image, and update data/hospitals.json by hand. The GitHub Actions
    workflow opens/updates a tracking issue automatically when this happens
    (see .github/workflows/monthly-refresh.yml).
  - Respect robots.txt for every source host before fetching.
  - Keep this dependency-light: requests + BeautifulSoup4 only (stdlib
    hashlib for the image-hospital change detection — no OCR, no paid API).

Usage:
    python3 scrape.py                  # re-scrape everything, write hospitals.json
    python3 scrape.py --dry-run        # scrape and print a diff, don't write
"""
import hashlib
import json
import logging
import re
import sys
import time
import urllib.robotparser
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_FILE = DATA_DIR / "hospitals.json"
LOG_FILE = DATA_DIR / "scrape_log.json"

HEADERS = {
    "User-Agent": "SG-Hospital-Rate-Tracker/1.0 (+non-commercial research; contact via site)"
}
TIMEOUT = 20
REQUEST_DELAY_SECONDS = 2  # be polite between requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("scraper")

_robots_cache = {}


def robots_allowed(url: str) -> bool:
    """Check robots.txt for the given URL's host, caching parsers per host."""
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if base not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(base + "/robots.txt")
        try:
            rp.read()
        except Exception as exc:  # noqa: BLE001 - robots.txt fetch is best-effort
            log.warning("Could not read robots.txt for %s (%s); assuming allowed", base, exc)
            _robots_cache[base] = None
            return True
        _robots_cache[base] = rp
    rp = _robots_cache[base]
    if rp is None:
        return True
    return rp.can_fetch(HEADERS["User-Agent"], url)


def fetch(url: str) -> str | None:
    if not url:
        return None
    if not robots_allowed(url):
        log.warning("robots.txt disallows fetching %s — skipping", url)
        return None
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        log.error("Fetch failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Per-hospital parsers.
#
# Each parser takes the raw HTML and returns a list of
# {"class": ..., "rate": float|None, "rate_display": str, "citizenship": str}
# dicts, or raises/returns [] if it can't find what it expects (structure
# changed) — the caller treats an empty result as a parse failure and keeps
# the previous data with a staleness note rather than wiping it out.
# ---------------------------------------------------------------------------

def parse_generic_rate_table(html: str, ward_keywords=None) -> list[dict]:
    """
    Best-effort generic parser: looks for an HTML <table> whose rows contain
    a ward-class-like label in the first cell and a $-prefixed number in a
    later cell. Works for several of the Parkway/Raffles/Farrer-style sites
    and the NHG (TTSH) site that publish plain HTML tables.
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []
    money_re = re.compile(r"\$?\s?([\d,]+(?:\.\d{1,2})?)")

    for row in soup.select("table tr"):
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) < 2:
            continue
        label = cells[0]
        if not label or len(label) > 60:
            continue
        # find first cell (after the label) that looks like a dollar amount
        rate_val = None
        rate_disp = None
        for cell in cells[1:]:
            m = money_re.search(cell)
            if m and ("$" in cell or re.search(r"\d", cell)):
                try:
                    rate_val = float(m.group(1).replace(",", ""))
                    rate_disp = cell
                    break
                except ValueError:
                    continue
        if rate_val is None:
            continue
        results.append(
            {
                "class": label,
                "rate": rate_val,
                "rate_display": rate_disp,
                "citizenship": "All",
            }
        )
    return results


# Registry: hospital id -> parser function. Hospitals not listed here fall
# back to parse_generic_rate_table.
PARSERS = {
    # Most entries use the generic table parser.
}

# Hospitals whose room-rate table is published as an embedded IMAGE rather
# than text, as of the last manual check. These can't be parsed with
# requests+BeautifulSoup, so instead of trying (and risking a bad OCR read),
# this script just watches the source page for any change (see
# check_image_hospital below) and flags it for a human to re-read when it
# changes. Update this set if a hospital switches to a text-based table.
IMAGE_BASED_HOSPITALS = {"ah", "nuh", "ntfgh"}


def page_hash(html: str) -> str:
    """Stable hash of a page's content, used to detect when an image-based
    hospital's rate page has changed since the last run (new image = new
    surrounding HTML, e.g. a new filename/date in the <img> src)."""
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


def check_image_hospital(entry: dict) -> dict:
    """
    For a hospital whose rates are an image: fetch the source page, hash it,
    and compare to the hash stored from the last run.
      - First run ever (no stored hash): store the hash, keep existing
        data_status/wards untouched (this run just establishes a baseline).
      - Hash unchanged: nothing to do, keep existing data_status/wards as-is.
      - Hash changed: keep the existing wards (never blank them out) but set
        data_status="needs_review" and a note explaining a human needs to
        re-read the image and update the figures by hand.
      - Fetch failed: mark "stale", keep existing wards.
    Returns a dict of fields to merge into the entry (does not mutate entry).
    """
    url = entry.get("source_url")
    html = fetch(url)
    time.sleep(REQUEST_DELAY_SECONDS)

    if html is None:
        return {
            "data_status": "stale",
            "notes": entry.get("notes", "")
            + f" [Auto-check: fetch failed this run; last confirmed {entry.get('retrieved_date', 'unknown date')}.]",
        }

    new_hash = page_hash(html)
    old_hash = entry.get("page_hash")

    if old_hash is None:
        # First run with hash-tracking enabled: establish baseline, don't flag.
        return {"page_hash": new_hash}

    if new_hash == old_hash:
        # Source page unchanged since last check — existing figures still hold.
        return {"page_hash": new_hash}

    # Page changed: don't guess new numbers, flag for a human to re-read the image.
    return {
        "page_hash": new_hash,
        "data_status": "needs_review",
        "notes": entry.get("notes", "")
        + f" [Auto-check: source page changed since it was last read on "
        f"{entry.get('retrieved_date', 'unknown date')} — the rate image may be "
        f"out of date. Figures below are the last confirmed values; please "
        f"re-check {url} and update by hand.]",
    }


def scrape_hospital(entry: dict) -> tuple[list[dict], str, str]:
    """
    Returns (wards, data_status, note) for one hospital entry.
    Does not mutate `entry`. Used for text-based (HTML table) hospitals only
    — image-based hospitals are handled separately by check_image_hospital.
    """
    hospital_id = entry["id"]
    url = entry.get("source_url")

    if hospital_id == "crawfurd" or not url:
        return [], "unavailable", "No official room & board rate page could be located for this hospital."

    html = fetch(url)
    time.sleep(REQUEST_DELAY_SECONDS)
    if html is None:
        return (
            entry.get("wards", []),
            "stale",
            f"Fetch failed this run; keeping last known data from {entry.get('retrieved_date', 'unknown date')}.",
        )

    parser = PARSERS.get(hospital_id, parse_generic_rate_table)
    wards = parser(html)

    if not wards:
        return (
            entry.get("wards", []),
            "stale",
            "Could not extract a rate table from the page this run (structure may have "
            f"changed); keeping last known data from {entry.get('retrieved_date', 'unknown date')}.",
        )

    return wards, "ok", entry.get("notes", "")


def main(dry_run: bool = False):
    if not DATA_FILE.exists():
        log.error("No existing %s found — nothing to refresh from.", DATA_FILE)
        sys.exit(1)

    with open(DATA_FILE, encoding="utf-8") as f:
        dataset = json.load(f)

    today = date.today().isoformat()
    run_log = {"run_at": datetime.now().isoformat(), "results": [], "needs_review": []}

    for entry in dataset["hospitals"]:
        hospital_id = entry["id"]
        log.info("Checking %s ...", entry["name"])

        if hospital_id in IMAGE_BASED_HOSPITALS:
            updates = check_image_hospital(entry)
            new_status = updates.get("data_status", entry.get("data_status"))
            run_log["results"].append(
                {
                    "id": hospital_id,
                    "name": entry["name"],
                    "status": new_status,
                    "changed": "data_status" in updates,
                    "note": updates.get("notes", entry.get("notes", "")),
                }
            )
            if new_status == "needs_review":
                run_log["needs_review"].append({"id": hospital_id, "name": entry["name"], "url": entry.get("source_url")})
            if not dry_run:
                entry.update(updates)
            continue

        wards, status, note = scrape_hospital(entry)
        changed = wards != entry.get("wards", [])
        run_log["results"].append(
            {
                "id": hospital_id,
                "name": entry["name"],
                "status": status,
                "changed": changed,
                "note": note,
            }
        )

        if not dry_run:
            entry["wards"] = wards
            entry["data_status"] = status
            entry["notes"] = note
            if status == "ok":
                entry["retrieved_date"] = today

    if dry_run:
        print(json.dumps(run_log, indent=2))
        return

    dataset["last_updated"] = today
    # next_refresh: the 1st of next month
    year, month = date.today().year, date.today().month
    if month == 12:
        next_refresh = date(year + 1, 1, 1)
    else:
        next_refresh = date(year, month + 1, 1)
    dataset["next_refresh"] = next_refresh.isoformat()

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(dataset, f, indent=2, ensure_ascii=False)

    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(run_log, f, indent=2, ensure_ascii=False)

    ok = sum(1 for r in run_log["results"] if r["status"] == "ok")
    stale = sum(1 for r in run_log["results"] if r["status"] == "stale")
    unavailable = sum(1 for r in run_log["results"] if r["status"] == "unavailable")
    needs_review = len(run_log["needs_review"])
    log.info(
        "Done. %d hospitals: %d ok, %d stale, %d needs review (image page changed), %d unavailable.",
        len(run_log["results"]), ok, stale, needs_review, unavailable,
    )


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
