"""
Turns whatever CSV the user drops in into the canonical row format the
lookup engine expects: first_name, last_name, address, city, state, zip.

Column headers are matched by *meaning*, not by an exact hardcoded name —
resolve_headers() checks every column against a table of known aliases
(case/spacing/underscore-insensitive), so a county export can call it
"Zip Code" or "Postal Code" or "Zip", in any column order, and still be
recognized. Two logical name shapes are supported:

  1. Separate first/last name columns (already-clean CSVs) — the safe,
     unambiguous case, used as-is.
  2. A single combined owner-name column, e.g. a Miami-Dade property-tax-roll
     export's "Owner Name 1" — parsed under the county convention
     "LAST [SUFFIX] FIRST [MIDDLE] [& CO-OWNER...]". This ordering
     assumption only applies to that single-column case; we never guess at
     a generic ambiguous full-name column, since a wrong guess there is
     exactly the kind of silent wrong-person bug this app has to avoid.

The address itself can likewise come from one combined "Address" column or
be assembled from separate house number / street name / etc. columns.

Rows that clearly aren't an individual at a real street address are
flagged instead of being guessed at. Two different flavors of "not an
individual":

  - LLCs/corporations/other registered business entities (LLC, INC, CORP,
    etc.) are tagged `is_entity` with the raw entity name preserved —
    these get a shot at automatic resolution to a real person (the
    registered agent or an officer/manager, via `llc_lookup.py`'s Sunbiz
    search) before the lookup engine gives up on them. See lookup_engine.py.
  - Everything else that isn't a resolvable entity or a parseable person
    (trusts, estates, confidential/reference placeholders, blank/ambiguous
    names, PO boxes) gets an immediate skip_reason instead — these are
    surfaced in the output for manual review rather than burning a
    FOREWARN search (or a Sunbiz search) on input that was never going to
    resolve to a person.
"""

import re

OUTPUT_FIELDS = [
    "owner", "address", "city", "state", "zip",
    "phone", "status", "notes", "input_owner_name",
]

SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}

ENTITY_TOKENS = {
    "LLC", "L.L.C", "INC", "CORP", "CORPORATION", "CO", "LP", "LLP", "LTD",
    "PLLC", "PLC", "PA", "PC", "CHARTERED",
    "TRUST", "TRS", "JTRS", "HOLDINGS", "PROPERTIES", "PROPERTY",
    "ASSOCIATES", "ASSOC", "PARTNERSHIP", "PARTNERS", "GROUP",
    "ENTERPRISES", "INVESTMENTS", "REALTY", "CONDOMINIUM",
    "CHURCH", "FOUNDATION", "BANK",
}

# Subset of ENTITY_TOKENS that are actual registered-business-entity suffixes
# (as opposed to TRUST/CHURCH/BANK/etc., which aren't things Sunbiz's
# business-entity search can resolve to a registered agent/officer at all).
# Only rows matching one of these get a shot at automatic LLC resolution;
# see llc_lookup.py.
REGISTERED_ENTITY_TOKENS = {
    "LLC", "L.L.C", "INC", "CORP", "CORPORATION", "CO", "LP", "LLP", "LTD",
    "PLLC", "PLC", "PA", "PC",
    "HOLDINGS", "ENTERPRISES", "GROUP", "PARTNERS", "PARTNERSHIP",
}

ENTITY_PHRASES = ["EST OF", "ESTATE OF", "ONLY REFERENCE", "CONFIDENTIAL"]

# A revocable/living/family trust is very commonly named after its own
# settlor/trustee — "MAHONEY MARY ANN REVOCABLE TRUST" already contains the
# person's name under the exact same "LAST FIRST [MIDDLE]" county
# convention used elsewhere, just with trailing trust words (and often a
# creation date) tacked on. TRUST_TOKENS flags a row as worth attempting
# this on; TRUST_STOPWORDS are the trailing words stripped off first.
TRUST_TOKENS = {"TRUST", "TRS", "JTRS", "TR"}
TRUST_STOPWORDS = {
    "TRUST", "TRS", "JTRS", "REVOCABLE", "IRREVOCABLE", "LIVING", "FAMILY",
    "DECLARATION", "AGREEMENT", "AGR", "AGMT", "AMENDED", "RESTATED", "DTD",
    "REV", "LIV", "TR",
}
DATE_TOKEN_RE = re.compile(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$|^\d{4}$")

# canonical field -> every header spelling we've seen for it, normalized
# (lowercase, underscores/extra whitespace collapsed to single spaces).
#
# The property's own "situs"/"site"/"property" address, city, state, and zip
# are listed BEFORE the generic aliases on purpose. A county tax-roll export
# for an LLC/entity-owned parcel commonly has BOTH a generic "Address" column
# (often the owner's/registered agent's own MAILING address — where their tax
# bill goes) and a separate situs/property-address column for the parcel
# itself. If both are present, matching the generic one first would silently
# search FOREWARN against — and report back — the *owner's* address instead
# of the actual property Mario is trying to find a phone number for. See the
# "wrong-property-for-an-LLC" bug this shape caused.
HEADER_ALIASES = {
    "first_name": ["first name", "fname", "given name", "first"],
    "last_name": ["last name", "lname", "surname", "last"],
    "owner_name": ["owner name 1", "owner name", "owner", "owner 1", "name"],
    "address": ["situs address", "site address", "property address", "physical address",
                "address", "street address", "full address"],
    "house_number": ["house number", "street number", "housenum", "house no", "property house number"],
    "prefix_direction": ["prefix direction", "address pre direction", "pre direction", "predirection",
                         "property street pre direction", "property pre direction"],
    "street_name": ["street name", "property street name"],
    "street_type": ["street type", "street suffix", "property street type"],
    "post_direction": ["post direction", "address post direction", "postdirection",
                       "property post direction", "property street post direction"],
    "unit_type": ["unit type"],
    "unit_number": ["unit number", "unit", "apt", "apartment", "apt number", "unit apartment suite"],
    "city": ["situs city", "site city", "property city", "city"],
    "state": ["situs state", "site state", "property state", "state", "state abbreviation", "st"],
    "zip": ["situs zip", "site zip", "property zip", "property zip code", "zip code", "zip",
            "zipcode", "postal code", "zip5", "postal", "postal zip code"],
}


def _normalize_header(h: str) -> str:
    h = (h or "").strip().lower()
    h = h.replace("_", " ")
    # Strip punctuation some exports decorate headers with (parens, slashes,
    # hashes, dashes) so e.g. "Postal (Zip) Code" and "Unit/Apartment/Suite #"
    # normalize down to plain word sequences an alias can actually match.
    h = re.sub(r"[()#/\-]", " ", h)
    h = re.sub(r"\s+", " ", h).strip()
    return h


def resolve_headers(fieldnames) -> dict:
    """Maps each canonical field name to the actual header string present in
    this CSV (by normalized alias match), for whichever fields it has."""
    by_normalized = {}
    for f in fieldnames or []:
        if f:
            norm = _normalize_header(f)
            # A "Mailing Address/City/State/Zip" column is where the owner's
            # tax bill goes, not the property itself — never let it stand in
            # for the property's own address/city/state/zip, even if a
            # generic alias would otherwise match its normalized name.
            if "mailing" in norm:
                continue
            by_normalized.setdefault(norm, f)

    resolved = {}
    for canonical, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if alias in by_normalized:
                resolved[canonical] = by_normalized[alias]
                break
    return resolved


def _get(row: dict, header: str) -> str:
    if not header:
        return ""
    return (row.get(header) or "").strip()


# A CSV that's been round-tripped through Excel commonly renders a
# numeric-looking column (a house number, unit number, zip) as e.g.
# "1130.0" instead of "1130" — Excel infers the column as a number and
# appends a trailing ".0" to whole numbers on save. FOREWARN's real address
# text never has that ".0", so it silently breaks every single address
# match against a row with this artifact. Stripped wherever it can appear.
_FLOAT_ARTIFACT_RE = re.compile(r"(?<!\d)(\d+)\.0(?!\d)")


def _strip_float_artifacts(text: str) -> str:
    return _FLOAT_ARTIFACT_RE.sub(r"\1", text)


def _clean_zip(raw_zip: str) -> str:
    """Normalizes a zip field down to its plain 5-digit form. Handles the
    dashed ZIP+4 shape ("12345-6789") already handled by the caller's own
    split, plus a second real-world shape some exports use: a bare 9-digit
    string with NO separator ("123456789" = zip "12345" + plus-4 "6789")
    -- the standard US ZIP+4 convention always puts the real zip in the
    first 5 digits, so truncating is safe rather than a guess."""
    z = _strip_float_artifacts(raw_zip.split("-")[0].strip())
    if len(z) == 9 and z.isdigit():
        z = z[:5]
    return z


def _clean(text: str) -> str:
    text = text.upper()
    text = re.sub(r"[().,*]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _no_entity(first_name="", last_name="", skip_reason=""):
    """Shared return shape so every parse_owner_name path — person, entity,
    or skip — carries the same keys."""
    return {
        "first_name": first_name, "last_name": last_name,
        "is_entity": False, "entity_name": "",
        "skip_reason": skip_reason,
    }


def _try_extract_trust_person(tokens: list, name_order: str = "last_first") -> dict:
    """Strips trailing trust-suffix words and a trailing creation date, then
    applies the known name convention (see parse_owner_name) to what's left.
    Returns None (never guesses) unless a real first name is left over —
    'AVILES FAMILY TRUST' has only a surname and stays unresolved rather
    than inventing a first name."""
    core = list(tokens)
    while core and (DATE_TOKEN_RE.match(core[-1]) or core[-1] in TRUST_STOPWORDS):
        core.pop()

    if len(core) < 2 or any(t in ENTITY_TOKENS for t in core) or "&" in core:
        return None

    if name_order == "first_last":
        if core[-1] in SUFFIXES and len(core) >= 3:
            first_name, last_name = core[0], core[-2]
        else:
            first_name, last_name = core[0], core[-1]
    else:
        if len(core) >= 3 and core[1] in SUFFIXES:
            last_name, first_name = core[0], core[2]
        else:
            last_name, first_name = core[0], core[1]
    return {"first_name": first_name.title(), "last_name": last_name.title()}


def parse_owner_name(owner_raw: str, name_order: str = "last_first") -> dict:
    """Parses a single combined owner-name field. `name_order` controls
    which convention it's assumed to follow:

    - "last_first" (default): the Miami-Dade tax-roll convention,
      'LAST [SUFFIX] FIRST [MIDDLE] [& CO-OWNER...]'.
    - "first_last": a normal-English-order name, 'FIRST [MIDDLE] LAST
      [SUFFIX]' — some third-party data aggregators (see
      _detect_name_order) present an already-cleaned owner name this way
      instead. Guessing the wrong one here silently reverses every name in
      the file, so which convention applies is detected once per file from
      its column shape, never per-row.

    Returns {first_name, last_name, is_entity, entity_name, skip_reason}.
    skip_reason is set instead of guessing when the field is blank, an
    unresolvable placeholder/trust/estate, or an ambiguous multi-owner
    listing. is_entity is set instead when the name looks like a registered
    business entity (LLC, Inc, Corp, ...) — those get a shot at automatic
    resolution to a real person in lookup_engine.py before anything gives
    up on them; entity_name carries the cleaned name to search."""
    if not owner_raw:
        return _no_entity(skip_reason="Blank owner name")

    cleaned = _clean(owner_raw)

    for phrase in ENTITY_PHRASES:
        if phrase in cleaned:
            return _no_entity(skip_reason=f"Placeholder/non-individual row ('{phrase.title()}')")

    tokens = cleaned.split()

    if any(t in REGISTERED_ENTITY_TOKENS for t in tokens):
        return {
            "first_name": "", "last_name": "",
            "is_entity": True, "entity_name": cleaned.title(),
            "skip_reason": "",
        }

    if TRUST_TOKENS & set(tokens):
        # A trust's name is always attempted as "LAST FIRST [MIDDLE]" —
        # deliberately NOT the file-level name_order. Even in a first-last
        # aggregator export (e.g. IMAPP), trust ownership names look to be
        # carried over verbatim from the raw county record rather than run
        # through whatever name-cleanup normalized the plain owner names:
        # confirmed on a real file where "SANCHEZ ELLIS A TRUST" cross-
        # referenced against that same row's recorded-mortgage borrower
        # ("LIVI ELLIS A SANCHEZ REVOCABLE") as Last=Sanchez, First=Ellis —
        # last-first — despite plain rows in the same file being first-last.
        extracted = _try_extract_trust_person(tokens, "last_first")
        if extracted:
            return _no_entity(first_name=extracted["first_name"], last_name=extracted["last_name"])
        return _no_entity(skip_reason="Trust name has a surname but no first name to search — needs manual lookup")

    if any(t in ENTITY_TOKENS for t in tokens):
        return _no_entity(skip_reason="Looks like a business/church/bank/other non-individual, not an individual or a registered business entity — needs manual lookup")

    if len(tokens) < 2:
        return _no_entity(skip_reason="Could not determine first/last name from owner field")

    if tokens[0] == "LE" and len(tokens) >= 3:
        # Life-estate holder: "LE FIRST [MIDDLE] LAST [SUFFIX]" — this county
        # marker convention is unrelated to name_order, always first-last.
        name_tokens = tokens[1:]
        if name_tokens[-1] in SUFFIXES and len(name_tokens) >= 3:
            name_tokens = name_tokens[:-1]
        first_name, last_name = name_tokens[0], name_tokens[-1]
    elif name_order == "first_last":
        if "&" in tokens:
            return _no_entity(skip_reason="Ambiguous multi-owner name format — needs manual review")
        if tokens[-1] in SUFFIXES and len(tokens) >= 3:
            first_name, last_name = tokens[0], tokens[-2]
        else:
            first_name, last_name = tokens[0], tokens[-1]
    else:
        # County default: "LAST [SUFFIX] FIRST [MIDDLE] [& CO-OWNER...]"
        if len(tokens) >= 3 and tokens[1] in SUFFIXES:
            last_name, first_name = tokens[0], tokens[2]
        elif tokens[1] == "&":
            return _no_entity(skip_reason="Ambiguous multi-owner name format — needs manual review")
        else:
            last_name, first_name = tokens[0], tokens[1]

    return _no_entity(first_name=first_name.title(), last_name=last_name.title())


def has_name_columns(headers: dict) -> bool:
    return ("first_name" in headers and "last_name" in headers) or "owner_name" in headers


def has_address_columns(headers: dict) -> bool:
    return "address" in headers or "house_number" in headers or "street_name" in headers


# A raw county tax-roll export's combined owner-name column follows "LAST
# FIRST [MIDDLE]" order. Some third-party property-data aggregators (e.g.
# IMAPP) instead ship an already-cleaned "Owner Name 1" column in normal
# "FIRST [MIDDLE] LAST" reading order — the opposite convention. Detected
# once per file from a signature of columns that ONLY this kind of export
# carries (never per-row, and never by guessing at a single ambiguous name),
# since applying the wrong convention silently reverses every name in the
# file. Confirmed against a real IMAPP export by cross-checking several
# owner names against that same file's recorded-mortgage borrower field
# (reliably "LAST FIRST MIDDLE"): "BRUCE WEMPLE" / mortgage "WEMPLE BRUCE M",
# "JOSE BENGOCHEA" / mortgage "BENGOCHEA JOSE".
_FIRST_LAST_FORMAT_SIGNATURE = {
    "owner address house number", "estimated current value", "total living heated area",
}


def _detect_name_order(fieldnames) -> str:
    normalized = {_normalize_header(f) for f in (fieldnames or []) if f}
    if len(_FIRST_LAST_FORMAT_SIGNATURE & normalized) >= 2:
        return "first_last"
    return "last_first"


def convert_row(raw_row: dict, headers: dict, name_order: str = "last_first") -> dict:
    out = {
        "owner_name_raw": "",
        "first_name": "",
        "last_name": "",
        "is_entity": False,
        "entity_name": "",
        "address": "",
        "city": _get(raw_row, headers.get("city")),
        "state": _get(raw_row, headers.get("state")),
        "zip": _clean_zip(_get(raw_row, headers.get("zip"))),
        "skip_reason": "",
    }

    if "first_name" in headers and "last_name" in headers:
        first_name = _get(raw_row, headers["first_name"])
        last_name = _get(raw_row, headers["last_name"])
        out["owner_name_raw"] = f"{first_name} {last_name}".strip()
        out["first_name"] = first_name
        out["last_name"] = last_name
        if not first_name or not last_name:
            out["skip_reason"] = "Missing first or last name"
    else:
        owner_raw = _get(raw_row, headers.get("owner_name"))
        out["owner_name_raw"] = owner_raw
        parsed = parse_owner_name(owner_raw, name_order)
        out["first_name"] = parsed["first_name"]
        out["last_name"] = parsed["last_name"]
        out["is_entity"] = parsed["is_entity"]
        out["entity_name"] = parsed["entity_name"]
        out["skip_reason"] = parsed["skip_reason"]

    if out["skip_reason"]:
        return out

    if "address" in headers:
        out["address"] = _strip_float_artifacts(_get(raw_row, headers["address"]))
    else:
        house = _strip_float_artifacts(_get(raw_row, headers.get("house_number")))
        prefix = _get(raw_row, headers.get("prefix_direction"))
        street = _get(raw_row, headers.get("street_name"))
        street_type = _get(raw_row, headers.get("street_type"))
        post = _get(raw_row, headers.get("post_direction"))
        unit_type = _get(raw_row, headers.get("unit_type"))
        unit_num = _strip_float_artifacts(_get(raw_row, headers.get("unit_number")))

        if street.upper() == "PO BOX" or street_type.upper() == "PO BOX":
            out["skip_reason"] = "PO Box address — not a property FOREWARN can match"
            return out

        if not house and not street:
            out["skip_reason"] = "No street address given"
            return out

        address_parts = [house, prefix, street, street_type, post]
        address = " ".join(p for p in address_parts if p)
        if unit_type or unit_num:
            address = f"{address} {unit_type} {unit_num}".strip()
        out["address"] = address

    if not out["zip"]:
        out["skip_reason"] = "No zip code given"

    return out


def load_rows(fieldnames, raw_rows) -> list:
    """Convert any supported input schema into the canonical row list,
    matching columns by meaning regardless of naming or order."""
    headers = resolve_headers(fieldnames)

    if not has_name_columns(headers):
        raise ValueError(
            "Couldn't find an owner name column. Expected either 'first_name' "
            "and 'last_name' columns, or a combined owner-name column "
            "(e.g. 'Owner Name 1', 'Owner', or 'Name')."
        )
    if not has_address_columns(headers):
        raise ValueError(
            "Couldn't find an address column. Expected either a combined "
            "'Address' column, or 'House Number'/'Street Name' columns."
        )
    if "zip" not in headers:
        raise ValueError(
            "Couldn't find a zip code column. Expected a column named "
            "'Zip', 'Zip Code', or 'Postal Code'."
        )

    name_order = _detect_name_order(fieldnames)
    return [convert_row(r, headers, name_order) for r in raw_rows]


def build_output_row(row: dict, result: dict) -> dict:
    """Build one output CSV row. 'owner' is the single resolved person that
    was actually searched (not the raw input field, which may list multiple
    co-owners) — falls back to the raw name only for skipped rows that were
    never parsed into a first/last name."""
    owner = f"{row.get('first_name', '')} {row.get('last_name', '')}".strip()
    owner = owner or row.get("owner_name_raw", "")
    return {
        "owner": owner,
        "address": row.get("address", ""),
        "city": row.get("city", ""),
        "state": row.get("state", ""),
        "zip": row.get("zip", ""),
        "phone": result.get("phone", ""),
        "status": result.get("status", ""),
        "notes": result.get("notes", ""),
        "input_owner_name": row.get("owner_name_raw", ""),
    }
