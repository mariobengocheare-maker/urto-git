"""
Core FOREWARN search/matching logic, shared by the CLI (forewarn_lookup.py)
and the web app (app.py).
"""

import random
import re
import time

from playwright.sync_api import TimeoutError as PWTimeoutError

import crm
from llc_lookup import resolve_entity_owner, find_person_zip_via_sunbiz_officer_search
from property_appraiser import find_property_by_owner_name

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


def address_tokens(address: str, zip_code: str) -> set:
    """Key tokens that must all appear in a candidate address for it to count
    as a match: house number, street-name words, and zip code. City/state are
    deliberately EXCLUDED -- FOREWARN (like most data providers) labels many
    unincorporated Miami-Dade addresses under "Miami" regardless of the
    county's own incorporated-municipality name (Palmetto Bay, Cutler Bay,
    etc.), so requiring the input row's raw city name to appear verbatim on
    FOREWARN's own page silently rejected real, correct matches (a real row
    proved this: FOREWARN showed "...MIAMI FL 33158" for an address whose
    county record said "PALMETTO BAY", and the old code required "PALMETTO"/
    "BAY" to appear literally in that page's text, which they never would).
    House number + street name + zip is already unique enough on its own to
    identify one specific real-world address. Unit/apt numbers are
    deliberately NOT required either, since the same person can be listed
    with or without a unit across records."""
    norm = normalize(f"{address} {zip_code}")
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
    # FOREWARN added a global top-nav "Search" button (data-testid="nav-search")
    # that now collides with the actual search FORM's own submit button --
    # both match a bare page-wide get_by_role("button", name="Search"), which
    # Playwright correctly refuses to click ambiguously (a real strict-mode
    # error, not a bug in our selector logic). Scope to the form itself,
    # which is where the real submit button lives -- the nav button isn't
    # inside any form. .first is extra defense in case a future FOREWARN
    # change adds a second form too, matching the same defensive pattern
    # already used for Sunbiz's own search button in llc_lookup.py.
    page.locator("form").get_by_role("button", name=SEARCH_BUTTON_RE).first.click()


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
    # A note input_parser.py may have already attached at parse time (e.g.
    # the owner name was recovered from the case's own Defendant field, or
    # the property address was rebuilt from a homesteaded owner's own
    # mailing address — see build order #141) — surfaced up front so Mario
    # sees exactly what stood in for missing data, same transparency
    # principle as the entity-resolution/zip-substitution notes below.
    entity_note = row.get("parse_note", "")
    candidates = None
    if row.get("is_entity"):
        entity_name = row.get("entity_name", "")
        resolved = resolve_entity_owner(page, entity_name, debug=debug)
        if resolved["skip_reason"]:
            return {"phone": "", "status": "SKIPPED", "notes": resolved["skip_reason"]}
        # resolve_entity_owner may hand back more than one candidate person
        # (e.g. a PA/PLLC's own embedded name, plus an officer/manager
        # fallback) — try the top pick first, and only spend a second real
        # FOREWARN search on a fallback candidate if the first comes back
        # NOT_FOUND, rather than committing to a single guess.
        candidates = resolved.get("candidates") or [
            {"first_name": resolved["first_name"], "last_name": resolved["last_name"],
             "resolved_via": resolved["resolved_via"]}
        ]
        primary = candidates[0]
        # Mutate the row itself: build_output_row() reads first/last name
        # back out of it afterward, so the resolved person becomes the
        # "owner" that was actually searched — same as for an individual.
        row["first_name"] = primary["first_name"]
        row["last_name"] = primary["last_name"]
        search_zip = primary.get("zip") or None
        # A registered agent/officer was reached as an LLC's legal contact,
        # not as someone who necessarily ever lived at (or has any real
        # connection to) the property their LLC owns — requiring the
        # PROPERTY's address to show up in their personal FOREWARN Address
        # History is the wrong check and silently rejects real matches (see
        # build order #142). Verify against THEIR OWN on-file address
        # instead, when Sunbiz gave us one.
        match_address = primary.get("address") or None
        entity_note += (f"Resolved via Sunbiz ({primary['resolved_via']}): "
                         f"'{entity_name}' -> {primary['first_name']} {primary['last_name']}. ")
    else:
        # A row can carry the OWNER's own mailing zip straight from the
        # input file (e.g. a "Tax - Owner - Postal Code" column on a
        # foreclosure/tax-roll export) even when they're an individual, not
        # an entity — a defendant in a foreclosure very often no longer
        # lives at the property being foreclosed on, same underlying reason
        # a Sunbiz-resolved registered agent usually doesn't either (see
        # build order #138). `_run_forewarn_search()` explains the
        # substitution in its own notes whenever it's actually used. Match
        # verification for a direct individual owner still correctly stays
        # anchored to the property's own address — unlike an entity's
        # registered agent, a direct owner presumably has some real
        # historical connection to the property they themselves own.
        search_zip = row.get("owner_zip") or None
        match_address = None

    # input_parser.py deliberately no longer skips a row just for missing a
    # zip (see build order #148) — it has no page/browser access to do
    # anything about it. This is the one place that DOES, so a still-blank
    # zip gets one real shot at a Miami-Dade Property Appraiser owner-name
    # search before the row is given up on. `match_address` doubles as the
    # tiebreaker here for an entity row: it's already the Sunbiz-resolved
    # candidate's own registered address (see above), exactly the address
    # Mario said to prefer when the property itself isn't already known.
    if not row["zip"].strip():
        owner_name_for_pa = (row["entity_name"] if row.get("is_entity")
                              else f"{row['last_name']} {row['first_name']}".strip())
        pa_result = find_property_by_owner_name(
            page, owner_name_for_pa, subject_address=row.get("address") or None,
            tiebreaker_address=match_address, debug=debug,
        )
        if pa_result.get("skip_reason"):
            return {"phone": "", "status": "SKIPPED", "notes": entity_note + pa_result["skip_reason"]}
        row["address"] = pa_result["address"]
        row["city"] = pa_result["city"]
        row["state"] = pa_result["state"]
        row["zip"] = pa_result["zip"]
        entity_note += pa_result.get("note", "")

    result = _run_forewarn_search(page, row, debug=debug, search_zip=search_zip,
                                   match_address=match_address)

    if candidates and result["status"] == "NOT_FOUND":
        for fallback in candidates[1:]:
            row["first_name"] = fallback["first_name"]
            row["last_name"] = fallback["last_name"]
            search_zip = fallback.get("zip") or None
            match_address = fallback.get("address") or None
            entity_note += (f"No FOREWARN match for that name — retrying via Sunbiz "
                             f"({fallback['resolved_via']}): {fallback['first_name']} {fallback['last_name']}. ")
            result = _run_forewarn_search(page, row, debug=debug, search_zip=search_zip,
                                           match_address=match_address)
            if result["status"] != "NOT_FOUND":
                break

    # Last resort before giving up entirely: Mario's own real case (build
    # order #151) — a plain individual owner whose FOREWARN search under
    # every zip already tried came back NOT_FOUND can still be found on
    # Sunbiz under a business record they're an officer/registered agent
    # of, with their own on-file address there, even though nothing in the
    # input CSV pointed at that entity at all. Only spends this extra
    # Sunbiz search once nothing else has worked, and only retries FOREWARN
    # if it turns up a genuinely different zip than what's already failed —
    # a match found here is verified against THIS on-file address, same
    # reasoning as an entity-resolved candidate (see build order #142).
    if result["status"] == "NOT_FOUND":
        current_first = row["first_name"].strip()
        current_last = row["last_name"].strip()
        if current_first and current_last:
            sunbiz_person = find_person_zip_via_sunbiz_officer_search(
                page, current_first, current_last, debug=debug)
            candidate_zip = sunbiz_person.get("zip")
            already_tried_zips = {z for z in (search_zip, row["zip"].strip()) if z}
            if candidate_zip and candidate_zip not in already_tried_zips:
                entity_note += (f"FOREWARN found nothing under the zip(s) already tried for "
                                 f"'{current_first} {current_last}' — found them listed on a Sunbiz business "
                                 f"record with an on-file address in zip {candidate_zip} instead; retrying "
                                 f"FOREWARN under that zip. ")
                result = _run_forewarn_search(page, row, debug=debug, search_zip=candidate_zip,
                                               match_address=sunbiz_person.get("address"))

    result["notes"] = entity_note + result["notes"]
    return result


def _run_forewarn_search(page, row: dict, debug: bool = False, search_zip: str = None,
                          match_address: str = None) -> dict:
    # Counts once per actual FOREWARN search attempt — this function is only
    # ever reached for rows that weren't skipped (at input-parsing time or,
    # for an entity row, by a failed Sunbiz resolution), so it's an accurate
    # tally of FOREWARN usage rather than every input row.
    try:
        crm.increment_weekly_lookup_count()
    except Exception:
        pass  # never let counter bookkeeping break an actual lookup

    first_name = row["first_name"].strip()
    last_name = row["last_name"].strip()
    address = row["address"].strip()
    zip_code = row["zip"].strip()

    # The SEARCH is run under `search_zip` when one was given (a Sunbiz-
    # resolved person's own zip, which is very often different from the
    # subject property's — a registered agent/officer usually lives
    # somewhere else entirely, see build order #138). MATCH VERIFICATION
    # normally stays anchored to the property's own real address/zip, since
    # that's the address we're trying to prove a direct owner is connected
    # to -- but when `match_address` is given (a Sunbiz-resolved person's
    # OWN on-file address), verification uses THAT instead: this person was
    # reached as an LLC's registered agent/officer, not as someone with any
    # necessary personal connection to the property itself, so requiring the
    # property's address to appear in their FOREWARN history is the wrong
    # check and silently rejects real matches (see build order #142) --
    # confirmed live: FOREWARN's own single returned candidate for "Orlando
    # Landaeta" showed his current on-file address as exactly the Sunbiz
    # registered-agent address used to search, with zero overlap with the
    # unrelated LLC-owned property, yet he was still genuinely the right
    # person to call.
    effective_search_zip = (search_zip or zip_code).strip()
    if match_address:
        required_tokens = address_tokens(match_address, effective_search_zip)
        zip_note = (f"Verified against this person's own on-file address ({match_address}) "
                     f"rather than the property's ({address}, {zip_code}) — they were reached "
                     f"via Sunbiz corporate resolution, not as a direct owner. ")
    else:
        required_tokens = address_tokens(address, zip_code)
        zip_note = (f"Searched FOREWARN under zip {effective_search_zip} instead of the "
                    f"property's {zip_code} (that's where this person's own address is on file). "
                    if effective_search_zip and effective_search_zip != zip_code else "")

    result = {"phone": "", "status": "NOT_FOUND", "notes": ""}

    run_search(page, first_name, last_name, effective_search_zip)
    count = wait_for_results_or_none(page)

    # Mario: "it is putting last name first name into forewarn... how can we
    # make this smart enough to know if the given data is in reverse" (see
    # build order #152). Rather than trying to GUESS a file's name order up
    # front (exactly the per-row/per-file guessing this codebase has always
    # refused to do -- see input_parser.py's own standing caution), this
    # applies Mario's own "try every combination... until you get a number"
    # philosophy to name order too: if the name AS GIVEN returns literally
    # nothing, try it with first/last swapped before giving up. This can
    # never cause a wrong-person match -- every candidate this search turns
    # up (in either order) still has to pass the exact same Address History
    # verification below -- it only ever turns a genuine zero-result miss
    # into a chance at a real match when this particular file's owner-name
    # column turned out to be in the opposite order from what was assumed.
    swap_note = ""
    if count == 0 and first_name and last_name and first_name.lower() != last_name.lower():
        run_search(page, last_name, first_name, effective_search_zip)
        count = wait_for_results_or_none(page)
        if count > 0:
            first_name, last_name = last_name, first_name
            swap_note = (f"'{row['first_name']} {row['last_name']}' returned nothing on FOREWARN — retried "
                         f"with first/last swapped ('{first_name} {last_name}') in case this file's name "
                         f"order is reversed, and that's what actually returned results. ")

    if count == 0:
        result["notes"] = swap_note + zip_note + "No results returned by FOREWARN for this name/zip."
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
            if swap_note:
                # The swapped order is what actually worked -- correct the
                # row's own names so build_output_row() shows the real name,
                # not the original (apparently reversed) guess.
                row["first_name"], row["last_name"] = first_name, last_name
            if phone:
                result["phone"] = phone
                result["status"] = "FOUND"
                result["notes"] = swap_note + zip_note + "Matched in address history."
                return result
            result["status"] = "FOUND_NO_PHONE"
            result["notes"] = swap_note + zip_note + "Address matched in history but no phone on record."
            return result
        else:
            human_pause(0.3, 0.8)
            page.go_back()  # -> summary page
            page.go_back()  # -> results list
            human_pause(0.3, 0.7)

    result["notes"] = swap_note + zip_note + f"Checked {n} candidate(s), none matched the target address."
    return result
