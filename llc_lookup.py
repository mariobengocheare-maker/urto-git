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

from playwright.sync_api import TimeoutError as PWTimeoutError

SUNBIZ_SEARCH_URL = "https://search.sunbiz.org/Inquiry/CorporationSearch/ByName"

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

# Common registered-agent *service* companies (not a real person) — checked
# in addition to the generic entity-suffix check, since firms like these
# often don't contain a token like "LLC"/"INC" in their commercial name.
AGENT_SERVICE_KEYWORDS = [
    "REGISTERED AGENT", "CORPORATION SERVICE COMPANY", "CT CORPORATION",
    "COGENCY GLOBAL", "INCORP SERVICES", "NORTHWEST REGISTERED",
    "NATIONAL REGISTERED AGENTS", "LEGALINC", "ZENBUSINESS", "LEGALZOOM",
    "ROCKET LAWYER", "HARBOR COMPLIANCE", "RASI",
]


def _looks_like_company(name: str) -> bool:
    from input_parser import ENTITY_TOKENS  # local import avoids a cycle at module load

    upper = name.upper()
    if any(kw in upper for kw in AGENT_SERVICE_KEYWORDS):
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


def _officer_candidate_names(section_text: str) -> list:
    """Sunbiz's Authorized Person(s)/Officer section repeats short blocks of
    (Title, Name, Address...) lines per person — a title line is a short
    one/two-letter code (MGR, AMBR, P, VP, S, D, T, CP...), and the name is
    the next non-blank line after it. Best-effort: collects every line that
    immediately follows something that looks like a title code."""
    lines = [l.strip() for l in section_text.splitlines()]
    title_re = re.compile(r"^(Title\s*)?[A-Z]{1,4}$")
    names = []
    i = 0
    while i < len(lines):
        if lines[i] and title_re.match(lines[i]):
            for j in range(i + 1, len(lines)):
                if lines[j]:
                    names.append(lines[j])
                    i = j
                    break
        i += 1
    return names


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


def resolve_entity_owner(page, entity_name: str, debug: bool = False) -> dict:
    """Attempts to resolve `entity_name` to a real person via Sunbiz.

    Returns {"first_name", "last_name", "resolved_via", "skip_reason"} —
    skip_reason is set (and the name fields blank) whenever resolution
    can't be done with confidence.
    """
    try:
        page.goto(SUNBIZ_SEARCH_URL, wait_until="domcontentloaded", timeout=20000)

        # Best-effort search-box locator — label text first, falling back to
        # the first visible text input if Sunbiz's markup doesn't expose a
        # matching label.
        search_box = None
        for getter in (
            lambda: page.get_by_label(re.compile("entity name", re.IGNORECASE)),
            lambda: page.locator("#SearchTerm"),
            lambda: page.locator("input[type=text]").first,
        ):
            try:
                loc = getter()
                if loc.count() > 0:
                    search_box = loc.first
                    break
            except Exception:
                continue
        if search_box is None:
            return {"first_name": "", "last_name": "", "resolved_via": "",
                    "skip_reason": "Could not find Sunbiz's search box (page layout may have changed)"}

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

        if agent_name and not _looks_like_company(agent_name):
            parsed = _split_person_name(agent_name)
            if parsed["ok"]:
                candidate = {"first_name": parsed["first_name"], "last_name": parsed["last_name"],
                             "resolved_via": "registered agent"}
                return {"first_name": candidate["first_name"], "last_name": candidate["last_name"],
                        "resolved_via": candidate["resolved_via"], "skip_reason": "",
                        "candidates": [candidate]}

        # Registered agent is a company (or unparseable). A PA/PLLC/PC-style
        # entity is itself required (Florida) to be named after the actual
        # licensed professional running it — search FOREWARN under that
        # embedded name FIRST (faster, usually correct) rather than only
        # going through the officers/managers list below — but still keep
        # that list as a fallback candidate too: a same-named PA occasionally
        # isn't run by that exact person, so trying both raises the odds of
        # landing on the right one instead of committing to just one guess.
        candidates = []
        if agent_name:
            pa_person = _try_extract_pa_person(agent_name)
            if pa_person["ok"]:
                candidates.append({"first_name": pa_person["first_name"], "last_name": pa_person["last_name"],
                                    "resolved_via": "PA/PLLC name"})

        for label in OFFICER_SECTION_LABELS:
            officer_section = _section_text(body_text, label)
            if not officer_section:
                continue
            for candidate_name in _officer_candidate_names(officer_section):
                if _looks_like_company(candidate_name):
                    continue
                parsed = _split_person_name(candidate_name)
                if parsed["ok"]:
                    candidates.append({"first_name": parsed["first_name"], "last_name": parsed["last_name"],
                                        "resolved_via": "officer/manager"})

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

        if not candidates:
            return {"first_name": "", "last_name": "", "resolved_via": "",
                    "skip_reason": f"Found '{entity_name}' on Sunbiz but couldn't confidently resolve it to a person "
                                    f"(registered agent is a company, and no officer/manager parsed as an individual)",
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
