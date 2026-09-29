"""
Resolves a registered business entity (LLC, Inc, Corp, ...) to a real
person — the registered agent, or failing that an officer/manager — via
Florida's Division of Corporations public entity search (Sunbiz), so a
property owned by an LLC doesn't just get skipped outright.

⚠️ Unverified against the live site. Sunbiz is blocked by this sandbox's
network policy (same restriction noted for FOREWARN in the project docs),
so every selector/label here is best-effort from Sunbiz's known, long-
stable page structure rather than something actually clicked through in
this environment. Treat the first real LLC row Mario runs through this as
the calibration run — use --debug (CLI) to pause with Playwright Inspector
right where the search/detail page loads if anything doesn't match, the
same way FOREWARN's card-click selectors were dialed in.

Deliberately conservative about guessing: if we can't tell whether a name
found on Sunbiz is a person or another company, or can't confidently split
it into first/last, we give up with a clear skip reason rather than take
a wrong guess — the exact same rule this app already applies to ambiguous
owner-name columns (see input_parser.py).
"""

import re
import time

from playwright.sync_api import TimeoutError as PWTimeoutError

SUNBIZ_SEARCH_URL = "https://search.sunbiz.org/Inquiry/CorporationSearch/ByName"

# Sunbiz sits behind Cloudflare, which can serve a "Just a moment..." bot-
# check interstitial instead of the real page — confirmed live (see build
# order #150, a real skip Mario hit reading exactly this page's title/body).
# Most of these are Cloudflare's automatic, time-based JS challenge (no
# human action needed, just a few seconds for its own script to run and
# redirect) rather than an interactive CAPTCHA, so it's worth waiting out
# rather than giving up on the very first check.
CLOUDFLARE_CHALLENGE_MARKERS = ("just a moment", "checking your browser", "verify you are human")


# Cloudflare Turnstile's simplest mode is a plain "I am human" checkbox
# inside an iframe — Mario reported having to click this by hand (see build
# order #153). This is genuinely different from the automatic, self-
# resolving JS challenge _wait_out_bot_challenge() already waits out below:
# it's a real interactive widget, and this is a best-effort attempt to click
# it, not a CAPTCHA-solving service — it only ever looks for a plain
# checkbox and clicks it once, it makes no attempt at a harder image/puzzle
# challenge. Whether this actually gets past Cloudflare is genuinely
# unconfirmed: Turnstile is specifically built to fingerprint automated
# browsers (Playwright's own CDP connection, `navigator.webdriver`, etc.)
# regardless of whether the window is headed or headless, so it may still
# refuse the click even in Mario's own real, visible Chromium window — the
# only way to know is a real retry on his PC.
_TURNSTILE_FRAME_SELECTORS = [
    "iframe[src*='challenges.cloudflare.com']",
    "iframe[title*='Cloudflare' i]",
    "iframe[title*='challenge' i]",
    "iframe[title*='human' i]",
]


def _try_click_bot_challenge_checkbox(page) -> bool:
    """Looks for a Turnstile-style checkbox inside a known challenge iframe
    and clicks it once. Returns whether it found (and clicked) anything —
    never raises, since a failed attempt here should just fall through to
    the normal "challenge didn't clear" skip_reason, not crash the row."""
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
    """Polls the page's title for up to max_wait_seconds, returning True once
    it no longer looks like a Cloudflare-style bot-check interstitial (or
    never did). Most of these clear on their own within a few seconds (the
    automatic JS challenge, see build order #150) — but if it's still
    showing partway through the wait, makes ONE attempt to click through a
    plain Turnstile checkbox (see _try_click_bot_challenge_checkbox above)
    before continuing to wait out whatever's left of the budget, in case
    that click's own redirect/reload takes a moment to settle. False means
    it's still showing after all of that — most likely a harder challenge
    (an image puzzle) that no amount of waiting or a plain checkbox click
    can get through."""
    deadline = time.time() + max_wait_seconds
    click_after = time.time() + min(8.0, max_wait_seconds / 2)
    click_attempted = False
    while True:
        try:
            title = page.title().strip().lower()
        except Exception:
            return True  # can't read the page at all -- let the normal flow's own diagnostics handle it
        if not any(marker in title for marker in CLOUDFLARE_CHALLENGE_MARKERS):
            return True
        now = time.time()
        if not click_attempted and now >= click_after:
            click_attempted = True
            if _try_click_bot_challenge_checkbox(page):
                time.sleep(2.0)  # let a real click's own redirect/reload settle
                continue
        if now >= deadline:
            return False
        time.sleep(1.5)

# Sunbiz's own long-stable section headers on an entity detail page. Text-
# anchored extraction (rather than CSS classes/ids we can't verify) so this
# survives minor markup changes.
REGISTERED_AGENT_LABEL = re.compile(r"Registered Agent Name\s*&?\s*Address", re.IGNORECASE)
OFFICER_SECTION_LABELS = [
    re.compile(r"Authorized Person\(?s?\)?\s*Detail", re.IGNORECASE),  # LLCs
    re.compile(r"Officer\s*/?\s*Director\s*Detail", re.IGNORECASE),     # Corporations
]
NEXT_SECTION_LABEL = re.compile(
    r"(Registered Agent Name|Authorized Person|Officer\s*/?\s*Director|"
    r"Annual Reports|Document Images|Name History|Filing Information)",
    re.IGNORECASE,
)

# A 5-digit zip (optionally +4) inside a Sunbiz address block — used so a
# FOREWARN search for a Sunbiz-resolved person can be run under THEIR OWN
# zip code, not the subject property's, since a registered agent/officer
# very often lives somewhere else entirely (see build order #138).
ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

# Common registered-agent *service* companies (not a real person) — checked
# in addition to the generic entity-suffix check, since firms like these
# often don't contain a token like "LLC"/"INC" in their commercial name.
# These are professional registrars, not another link in a real ownership
# chain — never worth recursing into (see _is_agent_service_firm below).
AGENT_SERVICE_KEYWORDS = [
    "REGISTERED AGENT", "CORPORATION SERVICE COMPANY", "CT CORPORATION",
    "COGENCY GLOBAL", "INCORP SERVICES", "NORTHWEST REGISTERED",
    "NATIONAL REGISTERED AGENTS", "LEGALINC", "ZENBUSINESS", "LEGALZOOM",
    "ROCKET LAWYER", "HARBOR COMPLIANCE", "RASI",
]

# A Sunbiz-extracted "person" name that's actually a law firm/practice name
# (e.g. a PA/PLLC name like "Valdes Firm" that _try_extract_pa_person happily
# parses as First="Firm" Last="Valdes", since "FIRM" isn't a registered-
# entity suffix ENTITY_TOKENS would ever catch) — not a real person to spend
# a FOREWARN search on. Deliberately narrow (Mario's own two examples) and
# only ever used to drop ONE candidate out of the list `resolve_entity_owner`
# builds, never to skip the whole entity by itself — if Sunbiz also lists a
# real individual (an officer/manager, a different PA name, ...), that
# person is still tried exactly as before (see build order #153, and Mario's
# own explicit correction: an entity with one law-firm-shaped name AND one
# real officer name should still resolve via the real one, which it already
# did — this only closes the gap where the firm-shaped name was the ONLY
# candidate and would otherwise have been searched on FOREWARN for nothing).
FIRM_NAME_TOKENS = {"FIRM", "LAW"}


def _looks_like_firm_name(first_name: str, last_name: str) -> bool:
    return bool({first_name.strip().upper(), last_name.strip().upper()} & FIRM_NAME_TOKENS)


# How many extra Sunbiz entities we'll follow when a registered agent or
# officer/manager turns out to be itself another company (e.g. an LLC's
# manager is another LLC) rather than an individual. Bounded so a chain of
# holding companies (or two entities that name each other) can't turn one
# input row into an unbounded number of real Sunbiz page loads.
MAX_ENTITY_RESOLUTION_DEPTH = 2


def _extract_zip(text: str) -> str:
    """First 5-digit zip found in a block of Sunbiz address text, or ''."""
    m = ZIP_RE.search(text)
    return m.group(1) if m else ""


def _strip_first_line(text: str, first_line: str) -> str:
    """Returns `text` with its first occurrence of `first_line` (a name line
    already pulled out separately) removed, leaving just the lines that
    follow it — used to isolate a registered agent's ADDRESS lines from the
    name line sitting right above them in the same section block."""
    if not first_line:
        return text
    idx = text.find(first_line)
    return text[idx + len(first_line):] if idx != -1 else text


def _clean_address_block(text: str) -> str:
    """Collapses a multi-line Sunbiz address block (street line, then city/
    state/zip line) into one flat comma-joined string suitable for both
    address_tokens() matching and showing in a note. Blank lines dropped."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return ", ".join(lines)


def _is_agent_service_firm(name: str) -> bool:
    return any(kw in name.upper() for kw in AGENT_SERVICE_KEYWORDS)


def _looks_like_company(name: str) -> bool:
    from input_parser import ENTITY_TOKENS  # local import avoids a cycle at module load

    upper = name.upper()
    if _is_agent_service_firm(upper):
        return True
    # Strip periods WITHOUT inserting a space first, so a dotted abbreviation
    # like "P.A." or "L.L.C." collapses to "PA"/"LLC" instead of splitting
    # into single letters ("P", "A") that never match ENTITY_TOKENS. Other
    # punctuation (commas/parens/asterisks) still becomes a space to keep
    # separating real name components.
    tokens = re.sub(r"[().,*]", " ", upper.replace(".", "")).split()
    return any(t in ENTITY_TOKENS for t in tokens)


# Professional-entity suffixes Florida requires an individual licensed
# professional (attorney, doctor, CPA, ...) to form their own practice under
# — the entity name itself IS that person's own legal name, not some
# unrelated company. "EUGENIO DUARTE, P.A." is Eugenio Duarte's own firm.
PA_STYLE_SUFFIXES = {"PA", "PLLC", "PC", "PL", "CHTD", "CHARTERED"}


def _try_extract_pa_person(name: str) -> dict:
    """Strips a trailing PA_STYLE_SUFFIXES token and reads the remaining
    tokens as the professional's own plain 'First [Middle] Last' name (this
    is a naming CONVENTION, not a per-name guess — same class of evidence-
    based extraction as input_parser.py's trust-name handling). Only fires
    when a recognized suffix is actually the last token; never guesses word
    order on a bare, un-suffixed name."""
    upper = name.upper().replace(".", "")
    tokens = re.sub(r"[(),*]", " ", upper).split()
    if not tokens or tokens[-1] not in PA_STYLE_SUFFIXES:
        return {"first_name": "", "last_name": "", "ok": False}
    core = tokens[:-1]
    # A multi-partner firm ("NEIMAN & INTERIAN, PLLC") names more than one
    # person — there's no single individual to extract, so don't guess.
    if len(core) < 2 or "&" in core:
        return {"first_name": "", "last_name": "", "ok": False}
    return {"first_name": core[0].title(), "last_name": core[-1].title(), "ok": True}


_NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}


def _split_person_name(name: str) -> dict:
    """Sunbiz lists officer/agent individuals as 'LAST, FIRST [MIDDLE]'. Only
    parse when that comma convention is actually present — never guess word
    order on a bare 'A B' pair, the same caution input_parser.py uses for
    the general owner-name column.

    Someone with a generational suffix is often listed as a THIRD
    comma-separated segment instead — 'LAST, SUFFIX, FIRST [MIDDLE]' (e.g.
    'HACKETT, II, JOHN') — which a plain split-on-first-comma mangles into
    a garbage name (the suffix segment gets swallowed into "first name").
    Detected and handled as its own case rather than guessed at generically."""
    name = name.strip()
    if "," not in name:
        return {"first_name": "", "last_name": "", "ok": False}
    parts = [p.strip() for p in name.split(",")]
    if len(parts) == 3 and parts[1].upper().rstrip(".") in _NAME_SUFFIXES:
        last, rest = parts[0], parts[2]
    else:
        last, _, rest = name.partition(",")
        rest = rest.strip()
    rest_tokens = rest.split()
    if not last.strip() or not rest_tokens:
        return {"first_name": "", "last_name": "", "ok": False}
    return {"first_name": rest_tokens[0].title(), "last_name": last.strip().title(), "ok": True}


def _section_text(body_text: str, label_pattern) -> str:
    """Body text from `label_pattern` up to whichever known section label
    comes next (or end of text)."""
    m = label_pattern.search(body_text)
    if not m:
        return ""
    tail = body_text[m.end():]
    next_m = NEXT_SECTION_LABEL.search(tail)
    return tail[: next_m.start()] if next_m else tail


def _first_nonblank_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _officer_candidate_blocks(section_text: str) -> list:
    """Sunbiz's Authorized Person(s)/Officer section repeats short blocks of
    (Title, Name, Address...) lines per person — a title line is a short
    one/two-letter code (MGR, AMBR, P, VP, S, D, T, CP...), and the name is
    the next non-blank line after it. Returns (name, block_text) pairs —
    block_text is that person's own trailing lines (their address, notably
    their own zip) up to the next title code, so a zip can be attributed to
    THE RIGHT specific officer rather than anywhere in a section that can
    list several people with different addresses."""
    lines = [l.strip() for l in section_text.splitlines()]
    title_re = re.compile(r"^(Title\s*)?[A-Z]{1,4}$")
    blocks = []
    i, n = 0, len(lines)
    while i < n:
        if lines[i] and title_re.match(lines[i]):
            j = i + 1
            while j < n and not lines[j]:
                j += 1
            if j < n:
                name = lines[j]
                k = j + 1
                while k < n and not (lines[k] and title_re.match(lines[k])):
                    k += 1
                blocks.append((name, "\n".join(lines[j + 1:k])))
                i = k
                continue
        i += 1
    return blocks


def _click_best_match(page, entity_name: str) -> bool:
    """Clicks the search-results link whose text best matches entity_name.
    Returns False if nothing looks like a real match (rather than guessing
    and opening an unrelated entity)."""
    target = re.sub(r"[^A-Z0-9]", "", entity_name.upper())
    links = page.get_by_role("link")
    count = links.count()
    best_idx, best_score = -1, 0.0
    for i in range(min(count, 60)):  # cap: result pages can be long
        text = links.nth(i).inner_text().strip()
        if not text or len(text) < 3:
            continue
        candidate = re.sub(r"[^A-Z0-9]", "", text.upper())
        if not candidate:
            continue
        if candidate == target:
            best_idx, best_score = i, 1.0
            break
        if target in candidate or candidate in target:
            score = min(len(target), len(candidate)) / max(len(target), len(candidate))
            if score > best_score:
                best_idx, best_score = i, score
    if best_idx < 0 or best_score < 0.6:
        return False
    links.nth(best_idx).click()
    return True


def _resolve_nested_company(page, company_name: str, debug: bool, visited: set, depth: int) -> list:
    """Recurses into a company found as a registered agent or officer/
    manager (e.g. an LLC's own manager is another LLC), so a real person can
    still be found further down the ownership chain instead of giving up the
    moment the first name found isn't a person. Bounded by
    MAX_ENTITY_RESOLUTION_DEPTH and a `visited` set of already-seen entity
    names (both per the module docstring's shared caution: don't guess, and
    don't let a cycle between two entities that name each other loop
    forever). Returns a (possibly empty) list of candidate dicts, each
    tagged with the chain it came through."""
    if depth >= MAX_ENTITY_RESOLUTION_DEPTH or _is_agent_service_firm(company_name):
        return []
    key = re.sub(r"[^A-Z0-9]", "", company_name.upper())
    if not key or key in visited:
        return []
    visited.add(key)
    nested = resolve_entity_owner(page, company_name, debug=debug, _visited=visited, _depth=depth + 1)
    if nested["skip_reason"]:
        return []
    tagged = []
    for c in nested.get("candidates") or []:
        tagged.append({**c, "resolved_via": f"{c['resolved_via']}, via nested entity '{company_name}'"})
    return tagged


def resolve_entity_owner(page, entity_name: str, debug: bool = False,
                          _visited: set = None, _depth: int = 0) -> dict:
    """Attempts to resolve `entity_name` to a real person via Sunbiz.

    Returns {"first_name", "last_name", "resolved_via", "skip_reason"} —
    skip_reason is set (and the name fields blank) whenever resolution
    can't be done with confidence. Each entry in "candidates" also carries
    a "zip" (the person's own address zip on file with Sunbiz, when one
    could be read) — the caller should search FOREWARN under THAT zip
    rather than the subject property's, since a registered agent/officer
    very often lives somewhere else entirely (see build order #138) — and
    an "address" (that same address, cleaned to one flat string), which the
    caller should verify a FOREWARN candidate's Address History against
    INSTEAD of the subject property's address: this person was reached as
    an LLC's registered agent/officer, not as someone who ever necessarily
    lived at (or had any real connection to) the property itself, so
    requiring the property's address to show up in their personal history
    is the wrong check entirely and silently rejects real matches (see
    build order #142).

    `_visited`/`_depth` are internal recursion state for following a
    registered agent or officer/manager that's itself another company —
    callers should never pass these themselves.
    """
    visited = _visited if _visited is not None else set()
    try:
        page.goto(SUNBIZ_SEARCH_URL, wait_until="domcontentloaded", timeout=20000)

        # A slow client-side render (not just the initial HTML) could still
        # be filling in the search form after domcontentloaded fires — give
        # it a real chance to settle before giving up on finding an input.
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeoutError:
            pass

        # Sunbiz can answer with a Cloudflare "Just a moment..." bot-check
        # instead of the real search page — give its own automatic JS
        # challenge a real chance to clear before ever looking for a search
        # box that genuinely isn't there yet (see build order #150).
        cleared = _wait_out_bot_challenge(page)
        if cleared:
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PWTimeoutError:
                pass

        # Best-effort search-box locator — label text and known ids first,
        # falling back progressively to any visible input Sunbiz's markup
        # exposes, regardless of its exact type/attributes (see build order
        # #147 — a real live-site skip Mario hit here with every one of the
        # narrower selectors below coming up empty).
        search_box = None
        for getter in (
            lambda: page.get_by_label(re.compile("entity name", re.IGNORECASE)),
            lambda: page.locator("#SearchTerm"),
            lambda: page.get_by_role("textbox").first,
            lambda: page.locator("input[type=search]:visible").first,
            lambda: page.locator("input[type=text]:visible").first,
            lambda: page.locator("input:visible").first,
        ):
            try:
                loc = getter()
                if loc.count() > 0:
                    search_box = loc.first
                    break
            except Exception:
                continue
        if search_box is None:
            # A black-box "couldn't find it" with no further detail meant
            # every future occurrence needed Mario to reproduce it live and
            # screenshot it before anything could be fixed. Capturing what
            # Sunbiz actually served instead (a CAPTCHA/rate-limit
            # interstitial, a cookie-consent overlay, a maintenance notice,
            # or genuinely changed markup) turns this into something
            # diagnosable straight from the results CSV's own Notes column.
            try:
                title = page.title()
                diag = (f"page title: '{title}', url: {page.url}, "
                        f"body starts: '{page.inner_text('body')[:200].strip()}'")
            except Exception:
                title = ""
                diag = "(could not read the page itself for diagnostics)"
            if not cleared or any(m in title.strip().lower() for m in CLOUDFLARE_CHALLENGE_MARKERS):
                return {"first_name": "", "last_name": "", "resolved_via": "",
                        "skip_reason": f"Sunbiz showed a bot-check page that didn't clear in time (this is Sunbiz's "
                                       f"own Cloudflare protection, not a real code problem) — try this row again "
                                       f"later, or on its own in a smaller batch — {diag}"}
            return {"first_name": "", "last_name": "", "resolved_via": "",
                    "skip_reason": f"Could not find Sunbiz's search box (page layout may have changed) — {diag}"}

        if debug:
            page.pause()

        search_box.click()
        search_box.fill(entity_name)

        clicked_search = False
        for getter in (
            lambda: page.get_by_role("button", name=re.compile("search", re.IGNORECASE)),
            lambda: page.get_by_text(re.compile(r"^\s*search\s*$", re.IGNORECASE)),
        ):
            try:
                loc = getter()
                if loc.count() > 0:
                    loc.first.click()
                    clicked_search = True
                    break
            except Exception:
                continue
        if not clicked_search:
            search_box.press("Enter")  # generic fallback that works on almost any search form

        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except PWTimeoutError:
            pass

        if debug:
            page.pause()

        # If Sunbiz went straight to a single entity's detail page (exact
        # unique match), there's nothing to click through — check for the
        # registered-agent label before assuming we're on a results list.
        body_text = page.inner_text("body")
        if not REGISTERED_AGENT_LABEL.search(body_text):
            if not _click_best_match(page, entity_name):
                return {"first_name": "", "last_name": "", "resolved_via": "",
                        "skip_reason": f"No matching entity found on Sunbiz for '{entity_name}'"}
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except PWTimeoutError:
                pass
            body_text = page.inner_text("body")

        if debug:
            page.pause()

        agent_section = _section_text(body_text, REGISTERED_AGENT_LABEL)
        agent_name = _first_nonblank_line(agent_section)

        # Build every candidate Sunbiz can possibly surface for this entity
        # before ever giving up — see build order #147, Mario's explicit
        # ask to exhaust every name/zip combination rather than settling
        # for a single guess. This used to return immediately with ONLY the
        # registered agent once it parsed as a real person, never even
        # looking at the officers/managers section — meaning a NOT_FOUND on
        # FOREWARN for that one name ended the whole row right there, even
        # when a different, equally real person (an officer/manager) was
        # sitting right there on the same Sunbiz page the whole time.
        candidates = []
        if agent_name and not _looks_like_company(agent_name):
            parsed = _split_person_name(agent_name)
            if parsed["ok"]:
                candidates.append({"first_name": parsed["first_name"], "last_name": parsed["last_name"],
                                    "resolved_via": "registered agent", "zip": _extract_zip(agent_section),
                                    "address": _clean_address_block(_strip_first_line(agent_section, agent_name))})

        # PA/PLLC extraction is tried whenever the agent's own comma-parse
        # above didn't already give us a candidate — covers BOTH a name
        # that plainly looks like a company (PLLC/PA/PC/...) AND one that
        # merely carries a PA_STYLE_SUFFIXES token ENTITY_TOKENS doesn't
        # separately recognize as a company (e.g. a trailing "PL"/"CHTD")
        # but that also isn't a plain comma-formatted individual name.
        # Florida requires a PA/PLLC/PC-style entity to be named after the
        # actual licensed professional running it — search FOREWARN under
        # that embedded name FIRST (faster, usually correct) rather than
        # only going through the officers/managers list below — but still
        # keep that list as a fallback too: a same-named PA occasionally
        # isn't run by that exact person, so trying both raises the odds of
        # landing on the right one instead of committing to just one guess.
        if agent_name and not candidates:
            pa_person = _try_extract_pa_person(agent_name)
            if pa_person["ok"]:
                candidates.append({"first_name": pa_person["first_name"], "last_name": pa_person["last_name"],
                                    "resolved_via": "PA/PLLC name", "zip": _extract_zip(agent_section),
                                    "address": _clean_address_block(_strip_first_line(agent_section, agent_name))})

        for label in OFFICER_SECTION_LABELS:
            officer_section = _section_text(body_text, label)
            if not officer_section:
                continue
            for candidate_name, block_text in _officer_candidate_blocks(officer_section):
                if _looks_like_company(candidate_name):
                    # A manager/authorized person that's itself another
                    # company (e.g. an LLC managed by another LLC) isn't a
                    # dead end the way a registered-agent-service firm is --
                    # there's still a real person somewhere behind IT too.
                    candidates.extend(_resolve_nested_company(page, candidate_name, debug, visited, _depth))
                    continue
                parsed = _split_person_name(candidate_name)
                if parsed["ok"]:
                    candidates.append({"first_name": parsed["first_name"], "last_name": parsed["last_name"],
                                        "resolved_via": "officer/manager", "zip": _extract_zip(block_text),
                                        "address": _clean_address_block(block_text)})

        # The registered agent itself being a company is the same situation,
        # just one level up -- follow it too, unless it's a known agent-
        # service firm (a professional registrar has no further real person
        # behind it, it's a dead end by definition).
        if agent_name and _looks_like_company(agent_name):
            candidates.extend(_resolve_nested_company(page, agent_name, debug, visited, _depth))

        # De-dupe — the same person sometimes shows up as both the PA-name
        # match and an officer — while preserving priority order.
        seen, deduped = set(), []
        for c in candidates:
            key = (c["first_name"].lower(), c["last_name"].lower())
            if key in seen:
                continue
            seen.add(key)
            deduped.append(c)
        candidates = deduped

        # Mario: "if a prop is owned only by a law firm... if the ONLY name
        # in the Sunbiz says 'firm' or 'law' then skip it" — but explicitly
        # NOT when a real individual is also available (his own example: an
        # entity with one law-firm-shaped candidate name and one genuine
        # officer name should still resolve via the real one, exactly as it
        # already did). So this only ever drops the firm-shaped candidate(s)
        # from the list — it can never cause a skip on its own unless doing
        # so empties the list entirely.
        firm_candidates = [c for c in candidates if _looks_like_firm_name(c["first_name"], c["last_name"])]
        candidates = [c for c in candidates if not _looks_like_firm_name(c["first_name"], c["last_name"])]

        if not candidates:
            if firm_candidates:
                firm_name = f"{firm_candidates[0]['first_name']} {firm_candidates[0]['last_name']}"
                return {"first_name": "", "last_name": "", "resolved_via": "",
                        "skip_reason": f"Found '{entity_name}' on Sunbiz, but the only resolvable name "
                                        f"('{firm_name}') looks like a law firm/practice, not an individual — "
                                        f"skipping rather than searching FOREWARN for a non-person name",
                        "candidates": []}
            return {"first_name": "", "last_name": "", "resolved_via": "",
                    "skip_reason": f"Found '{entity_name}' on Sunbiz but couldn't confidently resolve it to a person "
                                    f"(registered agent didn't parse as an individual, and no officer/manager did either)",
                    "candidates": []}

        primary = candidates[0]
        return {"first_name": primary["first_name"], "last_name": primary["last_name"],
                "resolved_via": primary["resolved_via"], "skip_reason": "", "candidates": candidates}

    except PWTimeoutError:
        return {"first_name": "", "last_name": "", "resolved_via": "",
                "skip_reason": "Sunbiz search timed out"}
    except Exception as e:
        return {"first_name": "", "last_name": "", "resolved_via": "",
                "skip_reason": f"Sunbiz lookup error: {e}"}


# Sunbiz's own "Search by Officer/Registered Agent Name" index -- a
# genuinely different search than resolve_entity_owner() above, which
# searches BY ENTITY NAME. This one searches directly by a PERSON's name to
# find any business record listing them, used only as a last-resort
# fallback (see build order #151) once every zip already tried for a name
# has come back NOT_FOUND on FOREWARN -- Mario's own real case: a plain
# individual owner ("Molly Reichman") whose property-zip FOREWARN search
# found nobody, but who turned out to have her own address on file on an
# LLC's Sunbiz page he found by searching her name directly. Never
# confirmed against the live site -- same caveat as everything else in this
# file -- built defensively with real diagnostics on any failure.
SUNBIZ_OFFICER_SEARCH_URL = "https://search.sunbiz.org/Inquiry/CorporationSearch/ByOfficerRA"


def _page_diag(page) -> str:
    try:
        return (f"page title: '{page.title()}', url: {page.url}, "
                f"body starts: '{page.inner_text('body')[:200].strip()}'")
    except Exception:
        return "(could not read the page itself for diagnostics)"


def _name_key(last_name: str, first_name: str) -> str:
    return re.sub(r"[^A-Z]", "", f"{last_name}{first_name}".upper())


def find_person_zip_via_sunbiz_officer_search(page, first_name: str, last_name: str, debug: bool = False) -> dict:
    """Searches Sunbiz's officer/registered-agent name index for
    `first_name`/`last_name` and, if a business record lists that exact
    person, extracts THEIR OWN on-file address/zip from it -- the same way
    `resolve_entity_owner()` already extracts a registered agent's address,
    just reached by searching a person's name directly instead of an
    entity's. Returns {"zip", "address"} on success, or {"skip_reason": ...}
    when nothing usable is found -- a failure here is never fatal to the
    row, it just means this fallback had nothing new to offer."""
    target = _name_key(last_name, first_name)
    if not target:
        return {"skip_reason": "No name to search Sunbiz's officer/registered-agent index with"}
    try:
        page.goto(SUNBIZ_OFFICER_SEARCH_URL, wait_until="domcontentloaded", timeout=20000)
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

        last_box = None
        for getter in (
            lambda: page.get_by_label(re.compile("last name", re.IGNORECASE)),
            lambda: page.locator("#LastName"),
            lambda: page.get_by_role("textbox").first,
        ):
            try:
                loc = getter()
                if loc.count() > 0:
                    last_box = loc.first
                    break
            except Exception:
                continue
        if last_box is None:
            return {"skip_reason": "Could not find Sunbiz's officer/registered-agent name search fields "
                                    f"(page layout may have changed) — {_page_diag(page)}"}
        first_box = None
        for getter in (
            lambda: page.get_by_label(re.compile("first name", re.IGNORECASE)),
            lambda: page.locator("#FirstName"),
        ):
            try:
                loc = getter()
                if loc.count() > 0:
                    first_box = loc.first
                    break
            except Exception:
                continue

        last_box.click()
        last_box.fill(last_name)
        if first_box is not None:
            first_box.click()
            first_box.fill(first_name)

        clicked_search = False
        for getter in (
            lambda: page.get_by_role("button", name=re.compile("search", re.IGNORECASE)),
            lambda: page.get_by_text(re.compile(r"^\s*search\s*$", re.IGNORECASE)),
        ):
            try:
                loc = getter()
                if loc.count() > 0:
                    loc.first.click()
                    clicked_search = True
                    break
            except Exception:
                continue
        if not clicked_search:
            last_box.press("Enter")

        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except PWTimeoutError:
            pass
        _wait_out_bot_challenge(page)

        if debug:
            page.pause()

        body_text = page.inner_text("body")

        # A single unique match can go straight to an entity detail page
        # with no results list at all -- only click through a results link
        # when we're actually still looking at one (no registered-agent/
        # officer section present yet on the current page).
        if not REGISTERED_AGENT_LABEL.search(body_text) and not any(
            label.search(body_text) for label in OFFICER_SECTION_LABELS
        ):
            links = page.get_by_role("link")
            count = links.count()
            best_idx, best_score = -1, 0.0
            for i in range(min(count, 60)):
                try:
                    text = links.nth(i).inner_text().strip()
                except Exception:
                    continue
                if not text or len(text) < 3:
                    continue
                candidate = re.sub(r"[^A-Z]", "", text.upper())
                if not candidate:
                    continue
                if candidate == target:
                    best_idx, best_score = i, 1.0
                    break
                if target in candidate or candidate in target:
                    score = min(len(target), len(candidate)) / max(len(target), len(candidate))
                    if score > best_score:
                        best_idx, best_score = i, score
            if best_idx < 0 or best_score < 0.5:
                return {"skip_reason": f"No Sunbiz officer/registered-agent record found for "
                                        f"'{first_name} {last_name}' — {_page_diag(page)}"}
            links.nth(best_idx).click()
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except PWTimeoutError:
                pass
            body_text = page.inner_text("body")

        # Confirm THIS exact person (not some other name on the same page)
        # and pull their own address block -- check the registered-agent
        # line first, then every officer/manager block.
        agent_section = _section_text(body_text, REGISTERED_AGENT_LABEL)
        agent_name = _first_nonblank_line(agent_section)
        if agent_name:
            parsed = _split_person_name(agent_name)
            if parsed["ok"] and _name_key(parsed["last_name"], parsed["first_name"]) == target:
                zip_code = _extract_zip(agent_section)
                if zip_code:
                    return {"zip": zip_code,
                            "address": _clean_address_block(_strip_first_line(agent_section, agent_name))}

        for label in OFFICER_SECTION_LABELS:
            officer_section = _section_text(body_text, label)
            if not officer_section:
                continue
            for candidate_name, block_text in _officer_candidate_blocks(officer_section):
                parsed = _split_person_name(candidate_name)
                if not parsed["ok"] or _name_key(parsed["last_name"], parsed["first_name"]) != target:
                    continue
                zip_code = _extract_zip(block_text)
                if zip_code:
                    return {"zip": zip_code, "address": _clean_address_block(block_text)}

        return {"skip_reason": f"Found a Sunbiz record but couldn't confirm "
                                f"'{first_name} {last_name}' own address on it — {_page_diag(page)}"}
    except PWTimeoutError:
        return {"skip_reason": "Sunbiz officer/registered-agent search timed out"}
    except Exception as e:
        return {"skip_reason": f"Sunbiz officer/registered-agent search failed: {e}"}
