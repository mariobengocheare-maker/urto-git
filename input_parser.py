"""
Turns whatever CSV the user drops in into the canonical row format the
lookup engine expects: first_name, last_name, address, city, state, zip.

Supports two input shapes:
  1. Already-clean: first_name,last_name,address,city,state,zip
  2. Raw county property-tax-roll export:
     "Owner Name 1","House Number","Prefix Direction","Street Name",
     "Street Type","Post Direction","Unit Type","Unit Number","Zip Code"

Raw county rows get parsed and, where they clearly aren't an individual
person at a real street address (LLCs, trusts, estates, confidential/
reference placeholders, PO boxes, blank rows), flagged with a skip_reason
instead of being guessed at — those are surfaced in the output for manual
review rather than burning a FOREWARN search on garbage input.
"""

import re

OUTPUT_FIELDS = [
    "owner_name_raw", "first_name", "last_name",
    "address", "city", "state", "zip",
    "phone", "status", "notes",
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


def _get(row: dict, name: str) -> str:
    for k, v in row.items():
        if k and k.strip().lower() == name.lower():
            return (v or "").strip()
    return ""


def _clean(text: str) -> str:
    text = text.upper()
    text = re.sub(r"[().,*]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_raw_county_format(fieldnames) -> bool:
    if not fieldnames:
        return False
    norm = {f.strip().lower() for f in fieldnames if f}
    return {"owner name 1", "house number", "zip code"}.issubset(norm)


def is_clean_format(fieldnames) -> bool:
    if not fieldnames:
        return False
    norm = {f.strip().lower() for f in fieldnames if f}
    return {"first_name", "last_name", "address", "zip"}.issubset(norm)


def convert_raw_county_row(raw_row: dict) -> dict:
    owner_raw = _get(raw_row, "Owner Name 1")
    house = _get(raw_row, "House Number")
    prefix = _get(raw_row, "Prefix Direction")
    street = _get(raw_row, "Street Name")
    street_type = _get(raw_row, "Street Type")
    post = _get(raw_row, "Post Direction")
    unit_type = _get(raw_row, "Unit Type")
    unit_num = _get(raw_row, "Unit Number")
    zip_full = _get(raw_row, "Zip Code")
    zip5 = zip_full.split("-")[0].strip()

    out = {
        "owner_name_raw": owner_raw,
        "first_name": "",
        "last_name": "",
        "address": "",
        "city": "",
        "state": "",
        "zip": zip5,
        "skip_reason": "",
    }

    if not owner_raw:
        out["skip_reason"] = "Blank owner name"
        return out

    cleaned = _clean(owner_raw)

    for phrase in ENTITY_PHRASES:
        if phrase in cleaned:
            out["skip_reason"] = f"Placeholder/non-individual row ('{phrase.title()}')"
            return out

    tokens = cleaned.split()
    if any(t in ENTITY_TOKENS for t in tokens):
        out["skip_reason"] = "Looks like a business/trust/estate, not an individual — needs manual lookup"
        return out

    if street.upper() == "PO BOX" or street_type.upper() == "PO BOX":
        out["skip_reason"] = "PO Box address — not a property FOREWARN can match"
        return out

    if not house and not street:
        out["skip_reason"] = "No street address given"
        return out

    if len(tokens) < 2:
        out["skip_reason"] = "Could not determine first/last name from owner field"
        return out

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
            out["skip_reason"] = "Ambiguous multi-owner name format — needs manual review"
            return out
        else:
            last_name, first_name = tokens[0], tokens[1]

    out["first_name"] = first_name.title()
    out["last_name"] = last_name.title()

    address_parts = [house, prefix, street, street_type, post]
    address = " ".join(p for p in address_parts if p)
    if unit_type or unit_num:
        address = f"{address} {unit_type} {unit_num}".strip()
    out["address"] = address

    return out


def convert_clean_row(row: dict) -> dict:
    first_name = _get(row, "first_name")
    last_name = _get(row, "last_name")
    return {
        "owner_name_raw": f"{first_name} {last_name}".strip(),
        "first_name": first_name,
        "last_name": last_name,
        "address": _get(row, "address"),
        "city": _get(row, "city"),
        "state": _get(row, "state"),
        "zip": _get(row, "zip"),
        "skip_reason": "",
    }


def load_rows(fieldnames, raw_rows) -> list:
    """Convert any supported input schema into the canonical row list."""
    if is_raw_county_format(fieldnames):
        return [convert_raw_county_row(r) for r in raw_rows]
    if is_clean_format(fieldnames):
        return [convert_clean_row(r) for r in raw_rows]
    raise ValueError(
        "Unrecognized CSV format. Expected either columns "
        "'first_name,last_name,address,city,state,zip' or a county export "
        "with 'Owner Name 1, House Number, ..., Zip Code' columns."
    )
