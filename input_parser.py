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

Rows that clearly aren't an individual at a real street address (LLCs,
trusts, estates, confidential/reference placeholders, PO boxes, blank
fields) are flagged with a skip_reason instead of being guessed at — those
are surfaced in the output for manual review rather than burning a
FOREWARN search on garbage input.
"""

import re

OUTPUT_FIELDS = [
    "owner", "address", "city", "state", "zip",
    "phone", "status", "notes", "input_owner_name",
]

SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}

ENTITY_TOKENS = {
    "LLC", "INC", "CORP", "CO", "LP", "LLP", "LTD",
    "TRUST", "TRS", "JTRS", "HOLDINGS", "PROPERTIES", "PROPERTY",
    "ASSOCIATES", "ASSOC", "PARTNERSHIP", "PARTNERS", "GROUP",
    "ENTERPRISES", "INVESTMENTS", "REALTY", "CONDOMINIUM",
    "CHURCH", "FOUNDATION", "BANK",
}

ENTITY_PHRASES = ["EST OF", "ESTATE OF", "ONLY REFERENCE", "CONFIDENTIAL"]

# canonical field -> every header spelling we've seen for it, normalized
# (lowercase, underscores/extra whitespace collapsed to single spaces).
HEADER_ALIASES = {
    "first_name": ["first name", "fname", "given name", "first"],
    "last_name": ["last name", "lname", "surname", "last"],
    "owner_name": ["owner name 1", "owner name", "owner", "owner 1", "name"],
    "address": ["address", "street address", "full address", "property address", "situs address"],
    "house_number": ["house number", "street number", "housenum", "house no"],
    "prefix_direction": ["prefix direction", "address pre direction", "pre direction", "predirection"],
    "street_name": ["street name"],
    "street_type": ["street type", "street suffix"],
    "post_direction": ["post direction", "address post direction", "postdirection"],
    "unit_type": ["unit type"],
    "unit_number": ["unit number", "unit", "apt", "apartment", "apt number"],
    "city": ["city", "situs city"],
    "state": ["state", "state abbreviation", "st"],
    "zip": ["zip code", "zip", "zipcode", "postal code", "zip5", "postal"],
}


def _normalize_header(h: str) -> str:
    h = (h or "").strip().lower()
    h = h.replace("_", " ")
    h = re.sub(r"\s+", " ", h)
    return h


def resolve_headers(fieldnames) -> dict:
    """Maps each canonical field name to the actual header string present in
    this CSV (by normalized alias match), for whichever fields it has."""
    by_normalized = {}
    for f in fieldnames or []:
        if f:
            by_normalized.setdefault(_normalize_header(f), f)

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


def _clean(text: str) -> str:
    text = text.upper()
    text = re.sub(r"[().,*]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_owner_name(owner_raw: str) -> dict:
    """Parses a single combined owner-name field under the county convention
    'LAST [SUFFIX] FIRST [MIDDLE] [& CO-OWNER...]'. Returns
    {first_name, last_name, skip_reason} — skip_reason is set instead of
    guessing when the field is blank, a business/trust/placeholder, or an
    ambiguous multi-owner listing."""
    if not owner_raw:
        return {"first_name": "", "last_name": "", "skip_reason": "Blank owner name"}

    cleaned = _clean(owner_raw)

    for phrase in ENTITY_PHRASES:
        if phrase in cleaned:
            return {"first_name": "", "last_name": "", "skip_reason": f"Placeholder/non-individual row ('{phrase.title()}')"}

    tokens = cleaned.split()
    if any(t in ENTITY_TOKENS for t in tokens):
        return {"first_name": "", "last_name": "", "skip_reason": "Looks like a business/trust/estate, not an individual — needs manual lookup"}

    if len(tokens) < 2:
        return {"first_name": "", "last_name": "", "skip_reason": "Could not determine first/last name from owner field"}

    if tokens[0] == "LE" and len(tokens) >= 3:
        # Life-estate holder: "LE FIRST [MIDDLE] LAST [SUFFIX]"
        name_tokens = tokens[1:]
        if name_tokens[-1] in SUFFIXES and len(name_tokens) >= 3:
            name_tokens = name_tokens[:-1]
        first_name, last_name = name_tokens[0], name_tokens[-1]
    else:
        # County default: "LAST [SUFFIX] FIRST [MIDDLE] [& CO-OWNER...]"
        if len(tokens) >= 3 and tokens[1] in SUFFIXES:
            last_name, first_name = tokens[0], tokens[2]
        elif tokens[1] == "&":
            return {"first_name": "", "last_name": "", "skip_reason": "Ambiguous multi-owner name format — needs manual review"}
        else:
            last_name, first_name = tokens[0], tokens[1]

    return {"first_name": first_name.title(), "last_name": last_name.title(), "skip_reason": ""}


def has_name_columns(headers: dict) -> bool:
    return ("first_name" in headers and "last_name" in headers) or "owner_name" in headers


def has_address_columns(headers: dict) -> bool:
    return "address" in headers or "house_number" in headers or "street_name" in headers


def convert_row(raw_row: dict, headers: dict) -> dict:
    out = {
        "owner_name_raw": "",
        "first_name": "",
        "last_name": "",
        "address": "",
        "city": _get(raw_row, headers.get("city")),
        "state": _get(raw_row, headers.get("state")),
        "zip": _get(raw_row, headers.get("zip")).split("-")[0].strip(),
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
        parsed = parse_owner_name(owner_raw)
        out["first_name"] = parsed["first_name"]
        out["last_name"] = parsed["last_name"]
        out["skip_reason"] = parsed["skip_reason"]

    if out["skip_reason"]:
        return out

    if "address" in headers:
        out["address"] = _get(raw_row, headers["address"])
    else:
        house = _get(raw_row, headers.get("house_number"))
        prefix = _get(raw_row, headers.get("prefix_direction"))
        street = _get(raw_row, headers.get("street_name"))
        street_type = _get(raw_row, headers.get("street_type"))
        post = _get(raw_row, headers.get("post_direction"))
        unit_type = _get(raw_row, headers.get("unit_type"))
        unit_num = _get(raw_row, headers.get("unit_number"))

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

    return [convert_row(r, headers) for r in raw_rows]


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
