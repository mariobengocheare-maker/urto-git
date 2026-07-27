# FOREWARN Owner-Lookup Automation

Automates property-owner phone lookups on FOREWARN for internal outreach use.
You log in manually; the script drives the search + matching logic from there.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Input

Two CSV formats are accepted — the tool auto-detects which one you gave it.

**Clean format**, columns:

```
first_name,last_name,address,city,state,zip
```

`address` is the street line only (e.g. `15730 SW 127th Ave Apt 301`). See
`input_sample.csv` for an example.

**Raw county property-tax-roll export**, columns:

```
Owner Name 1,House Number,Prefix Direction,Street Name,Street Type,Post Direction,Unit Type,Unit Number,Zip Code
```

(the format Miami-Dade and similar county appraiser exports use). The tool
parses `Owner Name 1` (handling `LAST FIRST MIDDLE` order, `LE `-prefixed
life-estate entries, and `JR`/`SR`/`III` suffixes) and builds the address
from the split columns automatically.

A row owned by a registered business entity (LLC, Inc, Corp, ...) isn't
skipped outright — it's automatically resolved to a real person first (the
entity's registered agent, or an officer/manager if the agent is itself a
company) via a live Florida Sunbiz search, then searched on FOREWARN like
any individual. See "LLC / business-entity owners" below.

Everything else that clearly isn't an individual at a real street address —
trusts, estates, `***CONFIDENTIAL OWNER***` / `ONLY REFERENCE` placeholders,
PO boxes, blank rows, or ambiguous multi-owner names — is marked `SKIPPED`
with a reason instead of being searched, so you can review it by hand
rather than getting a wasted or wrong lookup.

## Run — web UI (drag & drop)

**Windows desktop shortcut (recommended for everyday use):** double-click
`Create URTO Desktop Icon.vbs` once — it adds a "URTO" icon to your Desktop.
From then on, double-click that icon any time: it starts the server with no
console window and opens the app in your browser automatically. If you ever
move/re-extract the project to a new folder, just double-click the `.vbs`
again to recreate the icon pointing at the new location.

**Manual / any OS:**

```bash
python app.py
```

Then open **http://localhost:5000** in your browser. This must run on your
own machine, not a remote server — FOREWARN requires you to log in by hand,
and the automation drives that same logged-in browser session.

1. Drag your CSV onto the page (or click to browse).
2. Click **Start Lookup** — a real Chromium window opens to FOREWARN.
3. Log into FOREWARN in that window, then click **"I've logged in — continue"**
   back on the web page.
4. Watch results fill in live in the table.
5. Click **Download results CSV** when it's done.

Only one lookup job can run at a time. Output files are also saved under
`outputs/` (git-ignored, since they contain names/phone numbers).

## Run — command line

```bash
python forewarn_lookup.py --input input.csv --output output.csv --delay 5
```

- A Chromium window opens to the FOREWARN search page. Log in by hand, then
  press Enter in the terminal to start.
- Results are written to `output.csv` as they complete (one row in, one row
  out), adding `phone`, `status`, `notes`.
- `status` is one of: `FOUND`, `FOUND_NO_PHONE`, `NOT_FOUND`, `ERROR`.
- `--delay` is the base pause (seconds) between lookups; a small random
  jitter is added on top.

Both the web UI and the CLI share the same matching logic, in
`lookup_engine.py`.

## How matching works

For each input row, the script:

1. Searches FOREWARN by First Name / Last Name / Zip.
2. If the address shown directly under a result matches the input address
   (house number + street name + zip, ignoring apt/unit differences), it
   opens that candidate and takes the phone number.
3. Otherwise it opens each candidate one at a time, checks their full
   **Address History**, and matches there instead.
4. Takes the **first** (top-ranked) phone number FOREWARN returns for a
   matched person — matches how you described using the tool manually.
5. If no candidate's address history matches, the row is marked
   `NOT_FOUND` and the script moves on.

## LLC / business-entity owners

When a row's owner name looks like a registered business entity (contains
`LLC`, `Inc`, `Corp`, `Co`, `LP`, `Holdings`, etc. — see
`REGISTERED_ENTITY_TOKENS` in `input_parser.py`), the tool searches for that
entity on Florida's [Sunbiz](https://search.sunbiz.org) business registry
(free, public, no login needed) and pulls out:

1. The **registered agent**'s name, if it's a real person.
2. Otherwise, the first **officer/manager** on the entity that's a real
   person (registered-agent-service companies like CT Corporation System
   are recognized and skipped in favor of an actual officer).

Whichever name it resolves, that person is then searched on FOREWARN
exactly like any individual owner — the results table shows their name as
`owner`, the original LLC name in `input_owner_name`, and a note like
`Resolved via Sunbiz (registered agent): 'Sunshine Properties LLC' ->
Jane Doe.` explaining where the name came from.

If Sunbiz has no matching entity, or the entity's registered agent is a
company and no officer parses as a confident person match, the row is
marked `SKIPPED` with a specific reason instead of guessing — same rule
this tool already applies to ambiguous owner names.

Trusts, churches, banks, and other non-individual owners that aren't
registered business entities on Sunbiz (see the plain `ENTITY_TOKENS` set)
are still skipped immediately rather than wasting a Sunbiz search on
something that was never going to resolve there.

## Known fragile spots

Clicking a specific result card out of the list of candidates relies on a
best-guess DOM structure (I built this from screenshots, not the live page).
If a run throws errors when trying to open a card:

```bash
python forewarn_lookup.py --input input.csv --output output.csv --debug
```

`--debug` pauses on Playwright Inspector before each card click so you can
point-and-click the right element and report back what needs to change in
`get_result_cards` / `CARD_ANCESTOR_XPATH` in `forewarn_lookup.py`.

**The Sunbiz LLC-resolution selectors in `llc_lookup.py` are unverified
against the live site** — Sunbiz is blocked by network policy in the
sandbox this was built in, so it was written from Sunbiz's known, stable
page structure rather than actually clicked through. The first real CSV
with an LLC row is effectively the calibration run: pass `--debug` to the
CLI (same flag as above) and it'll also pause with Playwright Inspector
right at the Sunbiz search box and again after the search/detail page
loads, so you can point-and-click whatever doesn't match and report back
what needs adjusting in `llc_lookup.py`.

## Notes

- Strictly for your own business outreach — not for resale/marketing lists.
- Respect FOREWARN's rate limits; keep `--delay` reasonable.
