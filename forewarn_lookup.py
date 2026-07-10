#!/usr/bin/env python3
"""
FOREWARN owner-lookup automation.

Usage:
    python forewarn_lookup.py --input input.csv --output output.csv [--delay 5] [--debug]

Input CSV columns (header row required):
    first_name,last_name,address,city,state,zip

Output CSV adds:
    phone,status,notes

Login: the script opens a real (headed) Chromium window and pauses so you can
log into FOREWARN by hand. Once you're on the search page, press Enter in the
terminal to start the lookups.
"""

import argparse
import csv
import re
import sys
import time
import random

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

FOREWARN_SEARCH_URL = "https://app.forewarn.com/search"

# Words normalized to a common form so "Street" vs "St" style differences
# don't cause false non-matches.
ABBREVIATIONS = {
    "STREET": "ST", "AVENUE": "AVE", "BOULEVARD": "BLVD", "DRIVE": "DR",
    "LANE": "LN", "ROAD": "RD", "COURT": "CT", "CIRCLE": "CIR",
    "PLACE": "PL", "TERRACE": "TER", "PARKWAY": "PKWY", "HIGHWAY": "HWY",
    "APARTMENT": "APT", "UNIT": "APT", "SUITE": "STE", "#": "APT",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
}

PHONE_RE = re.compile(r"\b\d{3}-\d{3}-\d{4}\b")
RESULTS_COUNT_RE = re.compile(r"Found\s+(\d+)\s+results?", re.IGNORECASE)
AGE_TAG_RE = re.compile(r"Age\s*\(\s*\d+\s*\)", re.IGNORECASE)


def normalize(text: str) -> str:
    text = text.upper()
    text = re.sub(r"[.,]", "", text)
    tokens = text.split()
    tokens = [ABBREVIATIONS.get(t, t) for t in tokens]
    return " ".join(tokens)


def address_tokens(address: str, city: str, state: str, zip_code: str) -> set:
    """Key tokens that must all appear in a candidate address for it to count
    as a match: house number, street-name words, and zip code. Unit/apt
    numbers are deliberately NOT required, since the same person can be
    listed with or without a unit across records."""
    norm = normalize(f"{address} {city} {state} {zip_code}")
    tokens = set(norm.split())
    # Drop unit/suite tokens and the number right after them from the
    # required set, since those vary a lot between records.
    drop = set()
    tlist = norm.split()
    for i, t in enumerate(tlist):
        if t in ("APT", "STE") and i + 1 < len(tlist):
            drop.add(tlist[i + 1])
    return tokens - drop


def text_contains_address(blob: str, required_tokens: set) -> bool:
    norm_blob = normalize(blob)
    blob_tokens = set(norm_blob.split())
    return required_tokens.issubset(blob_tokens)


def get_result_cards(page):
    """Return locators anchored on each 'Age (N)' tag in the results list.
    NOTE: the ancestor level below (3) is a best-guess at the card
    container based on screenshots, not an inspected DOM. If clicking a
    card doesn't open the expected detail view, run with --debug and use
    Playwright Inspector to find the right ancestor level or a better
    selector, then update CARD_ANCESTOR_XPATH below.
    """
    return page.get_by_text(AGE_TAG_RE)


CARD_ANCESTOR_XPATH = "xpath=ancestor::*[3]"


def wait_for_results_or_none(page, timeout_ms=15000):
    """Wait for either the results count text or an empty-results indicator.
    Returns the integer result count, or 0 if none found."""
    try:
        page.wait_for_selector(f"text=/{RESULTS_COUNT_RE.pattern}/i", timeout=timeout_ms)
    except PWTimeoutError:
        return 0
    body_text = page.inner_text("body")
    m = RESULTS_COUNT_RE.search(body_text)
    return int(m.group(1)) if m else 0


def run_search(page, first_name, last_name, zip_code):
    page.goto(FOREWARN_SEARCH_URL, wait_until="domcontentloaded")
    page.get_by_text("SEARCH BY NAME", exact=False).click()
    page.get_by_placeholder("First Name").fill(first_name)
    page.get_by_placeholder("Last Name").fill(last_name)
    page.get_by_placeholder("Zip Code").fill(zip_code)
    page.get_by_role("button", name="SEARCH", exact=True).click()


def extract_first_phone(page) -> str:
    page.get_by_text("Phone Records", exact=False).click()
    try:
        page.wait_for_selector(f"text=/{PHONE_RE.pattern}/", timeout=10000)
    except PWTimeoutError:
        return ""
    body_text = page.inner_text("body")
    match = PHONE_RE.search(body_text)
    return match.group(0) if match else ""


def process_row(page, row, debug=False) -> dict:
    first_name = row["first_name"].strip()
    last_name = row["last_name"].strip()
    address = row["address"].strip()
    city = row.get("city", "").strip()
    state = row.get("state", "").strip()
    zip_code = row["zip"].strip()

    required_tokens = address_tokens(address, city, state, zip_code)

    result = {"phone": "", "status": "NOT_FOUND", "notes": ""}

    run_search(page, first_name, last_name, zip_code)
    count = wait_for_results_or_none(page)

    if count == 0:
        result["notes"] = "No results returned by FOREWARN for this name/zip."
        return result

    cards = get_result_cards(page)
    n = cards.count()

    for i in range(n):
        card_anchor = get_result_cards(page).nth(i)  # re-fetch: DOM may have changed after navigation
        card = card_anchor.locator(CARD_ANCESTOR_XPATH)
        try:
            card_text = card.inner_text()
        except Exception:
            card_text = card_anchor.inner_text()

        # Quick win: target address already shown directly under the name.
        if text_contains_address(card_text, required_tokens):
            card.click()
            phone = extract_first_phone(page)
            if phone:
                result["phone"] = phone
                result["status"] = "FOUND"
                result["notes"] = "Matched on results-list address."
                return result
            # Address matched but no phone on file.
            result["status"] = "FOUND_NO_PHONE"
            result["notes"] = "Address matched but no phone number on record."
            return result

        # Otherwise open the candidate and check full Address History.
        if debug:
            page.pause()
        card.click()
        page.get_by_text("Address History", exact=False).click()
        try:
            page.wait_for_load_state("domcontentloaded")
        except PWTimeoutError:
            pass
        history_text = page.inner_text("body")

        if text_contains_address(history_text, required_tokens):
            page.go_back()  # -> summary page
            phone = extract_first_phone(page)
            if phone:
                result["phone"] = phone
                result["status"] = "FOUND"
                result["notes"] = "Matched in address history."
                return result
            result["status"] = "FOUND_NO_PHONE"
            result["notes"] = "Address matched in history but no phone on record."
            return result
        else:
            page.go_back()  # -> summary page
            page.go_back()  # -> results list

    result["notes"] = f"Checked {n} candidate(s), none matched the target address."
    return result


def main():
    parser = argparse.ArgumentParser(description="FOREWARN owner-lookup automation")
    parser.add_argument("--input", required=True, help="Input CSV path")
    parser.add_argument("--output", required=True, help="Output CSV path")
    parser.add_argument("--delay", type=float, default=5.0,
                         help="Base delay in seconds between lookups (default 5)")
    parser.add_argument("--debug", action="store_true",
                         help="Pause with Playwright Inspector before each card click")
    args = parser.parse_args()

    with open(args.input, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    required_cols = {"first_name", "last_name", "address", "zip"}
    if rows and not required_cols.issubset(rows[0].keys()):
        sys.exit(f"Input CSV must have at least these columns: {sorted(required_cols)}")

    out_fields = list(rows[0].keys()) + ["phone", "status", "notes"] if rows else []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(FOREWARN_SEARCH_URL, wait_until="domcontentloaded")

        input("\nLog into FOREWARN in the browser window that just opened.\n"
              "Once you're on the search page, press Enter here to start...\n")

        with open(args.output, "w", newline="", encoding="utf-8") as out_f:
            writer = csv.DictWriter(out_f, fieldnames=out_fields)
            writer.writeheader()

            for idx, row in enumerate(rows, start=1):
                print(f"[{idx}/{len(rows)}] Looking up {row['first_name']} {row['last_name']} "
                      f"/ {row['address']}...")
                try:
                    result = process_row(page, row, debug=args.debug)
                except Exception as e:
                    result = {"phone": "", "status": "ERROR", "notes": str(e)}
                    print(f"  -> ERROR: {e}")

                out_row = dict(row)
                out_row.update(result)
                writer.writerow(out_row)
                out_f.flush()

                print(f"  -> {result['status']}"
                      + (f" ({result['phone']})" if result["phone"] else ""))

                if idx < len(rows):
                    time.sleep(args.delay + random.uniform(0, 1.5))

        browser.close()

    print(f"\nDone. Results written to {args.output}")


if __name__ == "__main__":
    main()
