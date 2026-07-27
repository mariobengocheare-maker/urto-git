"""
Core FOREWARN search/matching logic, shared by the CLI (forewarn_lookup.py)
and the web app (app.py).
"""

import random
import re
import time

from playwright.sync_api import TimeoutError as PWTimeoutError

from llc_lookup import resolve_entity_owner

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
# Accessible name of the SEARCH button. Case-insensitive because MUI
# buttons often show as "Search" in the DOM and get uppercased purely via
# CSS text-transform. Used with get_by_role("button", ...) rather than
# get_by_text() because the button also contains a nested <span>Search</span>,
# which text-based matching resolves as a second, ambiguous match.
SEARCH_BUTTON_RE = re.compile(r"^\s*search\s*$", re.IGNORECASE)
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
    return page.get_by_text(AGE_TAG_RE)


def wait_for_results_or_none(page, timeout_ms=15000) -> int:
    try:
        page.wait_for_selector(f"text=/{RESULTS_COUNT_RE.pattern}/i", timeout=timeout_ms)
    except PWTimeoutError:
        return 0
    body_text = page.inner_text("body")
    m = RESULTS_COUNT_RE.search(body_text)
    return int(m.group(1)) if m else 0


def human_pause(low=0.3, high=0.9):
    time.sleep(random.uniform(low, high))


def human_fill(locator, text: str):
    """Types like a person: variable per-character delay, with an
    occasional longer pause, instead of instantly filling the field."""
    locator.click()
    human_pause(0.15, 0.4)
    locator.fill("")
    for ch in text:
        locator.press_sequentially(ch)
        time.sleep(random.uniform(0.07, 0.24))
        if random.random() < 0.12:
            time.sleep(random.uniform(0.25, 0.7))


def run_search(page, first_name, last_name, zip_code):
    page.goto(FOREWARN_SEARCH_URL, wait_until="domcontentloaded")
    human_pause(1.0, 2.2)  # settle in on the fresh page before doing anything
    page.get_by_text("SEARCH BY NAME", exact=False).click()
    human_pause(0.6, 1.4)
    human_fill(page.get_by_label("First Name", exact=True), first_name)
    human_pause(0.25, 0.7)
    human_fill(page.get_by_label("Last Name", exact=True), last_name)
    human_pause(0.25, 0.7)
    human_fill(page.get_by_label("Zip Code", exact=True), zip_code)
    human_pause(0.6, 1.5)
    page.get_by_role("button", name=SEARCH_BUTTON_RE).click()


def extract_first_phone(page) -> str:
    page.get_by_text("Phone Records", exact=False).click()
    try:
        page.wait_for_selector(f"text=/{PHONE_RE.pattern}/", timeout=10000)
    except PWTimeoutError:
        return ""
    body_text = page.inner_text("body")
    match = PHONE_RE.search(body_text)
    return match.group(0) if match else ""


def process_row(page, row: dict, debug: bool = False) -> dict:
    entity_note = ""
    if row.get("is_entity"):
        entity_name = row.get("entity_name", "")
        resolved = resolve_entity_owner(page, entity_name, debug=debug)
        if resolved["skip_reason"]:
            return {"phone": "", "status": "SKIPPED", "notes": resolved["skip_reason"]}
        # Mutate the row itself: build_output_row() reads first/last name
        # back out of it afterward, so the resolved person becomes the
        # "owner" that was actually searched — same as for an individual.
        row["first_name"] = resolved["first_name"]
        row["last_name"] = resolved["last_name"]
        entity_note = (f"Resolved via Sunbiz ({resolved['resolved_via']}): "
                        f"'{entity_name}' -> {resolved['first_name']} {resolved['last_name']}. ")

    result = _run_forewarn_search(page, row, debug=debug)
    result["notes"] = entity_note + result["notes"]
    return result


def _run_forewarn_search(page, row: dict, debug: bool = False) -> dict:
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

    n = get_result_cards(page).count()

    # Always verify against each candidate's full Address History rather
    # than the address shown on the results-list card — that card only
    # shows one associated address per person, and trusting it directly
    # risks picking the wrong same-name/same-zip person entirely.
    for i in range(n):
        if debug:
            page.pause()

        # Re-fetch fresh each time: the DOM resets after each back-navigation.
        get_result_cards(page).nth(i).click()
        human_pause(0.4, 1.0)
        page.get_by_text("Address History", exact=False).click()
        try:
            page.wait_for_load_state("domcontentloaded")
        except PWTimeoutError:
            pass
        history_text = page.inner_text("body")

        if text_contains_address(history_text, required_tokens):
            human_pause(0.3, 0.8)
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
            human_pause(0.3, 0.8)
            page.go_back()  # -> summary page
            page.go_back()  # -> results list
            human_pause(0.3, 0.7)

    result["notes"] = f"Checked {n} candidate(s), none matched the target address."
    return result
