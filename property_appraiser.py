"""
Looks up a property on Miami-Dade's public Property Appraiser search by
OWNER NAME, for a row whose zip code (and very often its whole address) is
missing from the input CSV — see build order #148.

Mario asked for this specifically: "if no zip code is provided plug the
property into miami dade property appraiser until you get the zip code."
A folio/address-based lookup would be far more precise than a name search,
but there's nothing to look up BY if the row has no address at all (the
exact real rows that triggered this — "18933 JJ LLC"/"THE LAST COMPANY
LLC" — had a blank address AND a blank zip), and Mario explicitly confirmed
owner-name search is the right tool regardless: "you dont need a zip code
to look up a property on property appraiser."

A name search can return more than one parcel under the same owner. Mario
gave an explicit, three-tier order for picking the right one rather than
ever guessing blind:

  1. Prefer whichever result matches the row's own already-known address
     (a row can have an address but be missing only the zip).
  2. Failing that, prefer whichever result matches a Sunbiz-resolved
     entity's own registered-agent address (an entity row already has this
     from llc_lookup.py by the time this runs — see build order #142).
  3. Failing that, pick any result — as long as the owner name actually
     matches — rather than skip. This is Mario's own explicit call, made
     fully aware it accepts some real risk of picking the wrong one of
     several parcels under an ambiguous name; it is still never a blind
     guess among UNMATCHED names, and it's never reached when tier 1 or 2
     can settle it.

UPDATE (build order #211): Mario's own screenshots finally showed the real
page (https://apps.miamidadepa.gov/PropertySearch/#/, an Angular app that
covers itself with "Loading, please wait..."): a SEARCH row with four tabs
(ADDRESS, OWNER NAME, SUBDIVISION NAME, FOLIO), an input, a "Suite" input and
a magnifier icon. It opens on the ADDRESS tab, and the first version typed
the owner's name straight into the ADDRESS box. The search now clicks the
OWNER NAME tab first and waits out the loading spinner. The results page
itself has still never been seen, so result extraction below is still a guess.

⚠️ Even less verified against the live site than Sunbiz originally was:
`miamidade.gov` is blocked by this sandbox's network policy, the same as
`app.forewarn.com`/`search.sunbiz.org`, so every selector/regex here is a
best-effort guess at the real site's structure from its known public
search tool, never something actually clicked through. Treat the first
real "no zip" row Mario runs through this as the calibration run — the
same as Sunbiz's own build order #18 — and use --debug to pause with
Playwright Inspector if anything doesn't line up. Every failure path below
carries real page diagnostics (title/URL/body snippet) in its skip_reason
specifically so the next occurrence is fixable straight from the results
CSV's own Notes column, without needing Mario to reproduce it live first —
same lesson build order #147 already learned the hard way for Sunbiz.
"""

import re
import time

from playwright.sync_api import TimeoutError as PWTimeoutError

PROPERTY_SEARCH_URL = "https://www.miamidade.gov/Apps/PA/PropertySearch/"

# Sunbiz turned out to sit behind a Cloudflare bot-check that could serve a
# "Just a moment..." interstitial instead of the real page (confirmed live,
# see build order #150) -- this site's own bot-defenses (if any) have never
# been seen at all, so the same defensive wait is applied here too rather
# than waiting for Mario to hit the identical failure a second time on a
# different site before it gets the same treatment.
BOT_CHALLENGE_MARKERS = ("just a moment", "checking your browser", "verify you are human")


_TURNSTILE_FRAME_SELECTORS = [
    "iframe[src*='challenges.cloudflare.com']",
    "iframe[title*='Cloudflare' i]",
    "iframe[title*='challenge' i]",
    "iframe[title*='human' i]",
]


def _try_click_bot_challenge_checkbox(page) -> bool:
    """See llc_lookup.py's identical helper (build order #153) for the full
    rationale — a best-effort attempt at a plain Turnstile checkbox, never a
    real CAPTCHA solve, and never raises on failure."""
    for sel in _TURNSTILE_FRAME_SELECTORS:
        try:
            frame = page.frame_locator(sel)
            checkbox = frame.locator("input[type='checkbox'], [role='checkbox']").first
            checkbox.wait_for(state="visible", timeout=1000)
            checkbox.click(timeout=3000)
            return True
        except Exception:
            continue
    return False


def _wait_out_bot_challenge(page, max_wait_seconds: float = 25) -> bool:
    """See llc_lookup.py's identical helper for the full rationale. Returns
    True once the page's title no longer looks like a bot-check interstitial
    (or never did), False if it's still showing after max_wait_seconds. Makes
    one attempt at a plain Turnstile checkbox partway through the wait if the
    automatic-challenge case (the common one) hasn't already cleared it."""
    deadline = time.time() + max_wait_seconds
    click_after = time.time() + min(8.0, max_wait_seconds / 2)
    click_attempted = False
    while True:
        try:
            title = page.title().strip().lower()
        except Exception:
            return True
        if not any(marker in title for marker in BOT_CHALLENGE_MARKERS):
            return True
        now = time.time()
        if not click_attempted and now >= click_after:
            click_attempted = True
            if _try_click_bot_challenge_checkbox(page):
                time.sleep(2.0)
                continue
        if now >= deadline:
            return False
        time.sleep(1.5)

# A full address embedded in the page's own text: house number + street,
# then city, then "FL" and a 5-digit zip (optionally +4). Text-anchored
# rather than tied to any specific CSS class/id, matching the same
# philosophy llc_lookup.py already uses for Sunbiz's own long-stable but
# equally unverified-here markup.
_ADDRESS_RE = re.compile(
    r"(?P<street>\d+[A-Za-z0-9 .#/-]*?)\s*[,\n]\s*"
    r"(?P<city>[A-Za-z][A-Za-z .]*?),?\s*FL\.?\s*(?P<zip>\d{5})(?:-\d{4})?",
    re.IGNORECASE,
)


def _diag(page) -> str:
    try:
        return (f"page title: '{page.title()}', url: {page.url}, "
                f"body starts: '{page.inner_text('body')[:500].strip()}'")
    except Exception:
        return "(could not read the page itself for diagnostics)"


def _loading_visible(page) -> bool:
    """The real site is an Angular single-page app that covers itself with a
    "Loading, please wait..." spinner while it works (seen in Mario's own
    screenshot of a run that read the page mid-load and reported "no property
    found")."""
    try:
        loc = page.get_by_text(re.compile(r"loading,?\s*please\s*wait", re.IGNORECASE))
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False


def _wait_until_not_loading(page, timeout_s: float = 20.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _loading_visible(page):
            return True
        page.wait_for_timeout(400)
    return False


def _click_owner_name_tab(page) -> bool:
    """The search form opens on its ADDRESS tab (the box shows the example
    "111 NW 1 St"); the other tabs are OWNER NAME, SUBDIVISION NAME and FOLIO.
    An owner's name typed into the ADDRESS box can never find anything, so the
    OWNER NAME tab must be clicked first (Mario caught the first version
    typing "Doors 2 Heaven Llc" straight into the ADDRESS box)."""
    pattern = re.compile(r"^\s*owner\s*name\s*$", re.IGNORECASE)
    for getter in (
        lambda: page.get_by_role("tab", name=pattern),
        lambda: page.get_by_role("button", name=pattern),
        lambda: page.get_by_text(pattern),
    ):
        try:
            loc = getter()
            for i in range(min(loc.count(), 4)):
                el = loc.nth(i)
                if el.is_visible():
                    el.click()
                    return True
        except Exception:
            continue
    return False


def _find_owner_search_box(page):
    """After the OWNER NAME tab is open: the main search input. The ADDRESS
    tab also has a small separate "Suite" input, so skip anything that looks
    like that one and take the widest remaining visible text box."""
    try:
        inputs = page.locator("input:visible")
        best, best_width = None, 0
        for i in range(inputs.count()):
            el = inputs.nth(i)
            kind = (el.get_attribute("type") or "text").lower()
            if kind not in ("text", "search", ""):
                continue
            hint = " ".join((el.get_attribute(k) or "") for k in ("placeholder", "aria-label", "id", "name")).lower()
            if "suite" in hint or "unit" in hint:
                continue
            box = el.bounding_box()
            width = box["width"] if box else 0
            if width > best_width:
                best, best_width = el, width
        return best
    except Exception:
        return None


def _click_search_icon(page) -> bool:
    """The real submit control is a magnifier icon next to the box, not a
    button labelled "Search"."""
    for getter in (
        lambda: page.locator("button[aria-label*='search' i], button[title*='search' i]"),
        lambda: page.locator("button:has(mat-icon:text-is('search')), button:has(.fa-search), button:has(.fa-magnifying-glass)"),
        lambda: page.locator("[class*='search' i] button, [class*='search' i] mat-icon, [class*='search' i] i"),
        lambda: page.get_by_role("button", name=re.compile("search", re.IGNORECASE)),
    ):
        try:
            loc = getter()
            for i in range(min(loc.count(), 4)):
                el = loc.nth(i)
                if el.is_visible():
                    el.click()
                    return True
        except Exception:
            continue
    return False


_NO_RESULTS_RE = re.compile(r"no\s+(results|records|properties|matches)|0\s+results|not\s+found", re.IGNORECASE)


def _extract_results(page) -> list:
    """Returns every address-shaped block of text found on the results
    page, each tagged with a chunk of surrounding text (`context`) to
    check the owner's name against — coarse, text-based extraction rather
    than relying on any specific DOM structure this site might actually
    use, since that structure has never been seen directly."""
    try:
        body_text = page.inner_text("body")
    except Exception:
        return []
    lines = [l.strip() for l in body_text.splitlines() if l.strip()]
    joined = "\n".join(lines)
    results = []
    for m in _ADDRESS_RE.finditer(joined):
        context_start = max(0, m.start() - 300)
        results.append({
            "address": re.sub(r"\s+", " ", m.group("street")).strip().upper(),
            "city": m.group("city").strip().upper(),
            "state": "FL",
            "zip": m.group("zip"),
            "context": joined[context_start:m.end()],
        })
    return results


def _name_matches(owner_name: str, context_text: str) -> bool:
    target_tokens = {t for t in re.sub(r"[^A-Z0-9 ]", "", owner_name.upper()).split() if len(t) > 1}
    if not target_tokens:
        return False
    context_norm = re.sub(r"[^A-Z0-9 ]", "", context_text.upper())
    hits = sum(1 for t in target_tokens if t in context_norm)
    # Allow at most one token to not appear — an LLC suffix ("LLC"/"INC"),
    # a middle name, or a punctuation difference shouldn't sink an
    # otherwise-real match, but this still refuses a genuinely unrelated
    # name (see the module docstring's tier-3 rule: any match still has to
    # actually be a name match, never just "any result at all").
    return hits >= max(1, len(target_tokens) - 1)


def _address_key(addr: str):
    """House number + first alpha word of the street name — stable across
    'ST'/'STREET'/'AVE'/'AVENUE' abbreviation differences and an added
    unit/apartment number, unlike comparing the full string."""
    m = re.match(r"\s*(\d+)\s+([A-Za-z]+)", addr or "")
    return (m.group(1), m.group(2).upper()) if m else None


def _best_address_match(known_address: str, candidates: list):
    key = _address_key(known_address)
    if not key:
        return None
    for c in candidates:
        if _address_key(c["address"]) == key:
            return c
    return None


def find_property_by_owner_name(page, owner_name: str, subject_address: str = None,
                                 tiebreaker_address: str = None, debug: bool = False) -> dict:
    """Searches Miami-Dade Property Appraiser by owner name (never a zip —
    see the module docstring) to recover a property's address/zip for a row
    the input CSV gave neither for. Returns
    {"address", "city", "state", "zip", "note"} on success, or
    {"skip_reason": "..."} when nothing usable came back."""
    owner_name = (owner_name or "").strip()
    if not owner_name:
        return {"skip_reason": "No owner name to search Property Appraiser with"}

    try:
        page.goto(PROPERTY_SEARCH_URL, timeout=20000)
    except Exception as e:
        return {"skip_reason": f"Could not reach the Miami-Dade Property Appraiser site: {e}"}

    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except PWTimeoutError:
        pass

    cleared = _wait_out_bot_challenge(page)
    if cleared:
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeoutError:
            pass

    if debug:
        page.pause()

    # The single-page app shows "Loading, please wait..." while it starts up.
    _wait_until_not_loading(page, 20)

    # Open the OWNER NAME tab BEFORE typing -- the form opens on ADDRESS.
    if not _click_owner_name_tab(page):
        try:
            still_challenged = any(m in page.title().strip().lower() for m in BOT_CHALLENGE_MARKERS)
        except Exception:
            still_challenged = False
        if not cleared or still_challenged:
            return {"skip_reason": "Miami-Dade Property Appraiser showed a bot-check page that didn't clear in "
                                    f"time — try this row again later, or on its own in a smaller batch — {_diag(page)}"}
        return {"skip_reason": "Could not find the OWNER NAME tab on Property Appraiser's search "
                                f"(page layout may have changed) — {_diag(page)}"}
    page.wait_for_timeout(700)

    search_box = _find_owner_search_box(page)
    if search_box is None:
        return {"skip_reason": "Could not find Property Appraiser's owner-name search box after opening "
                                f"the OWNER NAME tab (page layout may have changed) — {_diag(page)}"}

    search_box.click()
    search_box.fill(owner_name)
    try:
        if (search_box.input_value() or "").strip().lower() != owner_name.lower():
            search_box.fill("")
            search_box.press_sequentially(owner_name, delay=40)  # a box that rewrites pasted text
    except Exception:
        pass

    # Submit: Enter first, then the magnifier icon if nothing seemed to happen.
    search_box.press("Enter")
    page.wait_for_timeout(1500)
    started = _loading_visible(page) or bool(_extract_results(page))
    if not started:
        _click_search_icon(page)
        page.wait_for_timeout(1500)

    # Wait for the results to actually load (the spinner goes away and either
    # addresses or a "no results" message appears).
    deadline = time.time() + 25
    while time.time() < deadline:
        if _loading_visible(page):
            page.wait_for_timeout(500)
            continue
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        if _extract_results(page) or _NO_RESULTS_RE.search(body):
            break
        page.wait_for_timeout(700)

    if debug:
        page.pause()

    results = _extract_results(page)
    name_matched = [r for r in results if _name_matches(owner_name, r["context"])]

    if not name_matched:
        return {"skip_reason": f"No property found on Miami-Dade Property Appraiser for owner "
                                f"'{owner_name}' — {_diag(page)}"}

    chosen, tier_note = None, ""
    if subject_address:
        chosen = _best_address_match(subject_address, name_matched)
        if chosen:
            tier_note = "matched the property's own already-known address"
    if chosen is None and tiebreaker_address:
        chosen = _best_address_match(tiebreaker_address, name_matched)
        if chosen:
            tier_note = ("matched the Sunbiz-resolved agent's own registered address — "
                         "the only way to tell which of several parcels was the right one")
    if chosen is None:
        chosen = name_matched[0]
        tier_note = ("the only property found under this name" if len(name_matched) == 1 else
                     f"picked from {len(name_matched)} properties found under this name with "
                     f"no address to disambiguate by — double-check this one manually")

    return {
        "address": chosen["address"], "city": chosen["city"], "state": chosen["state"], "zip": chosen["zip"],
        "note": (f"Found this property via Miami-Dade Property Appraiser (owner-name search — the "
                 f"input file gave no zip code) — {tier_note}. "),
    }
