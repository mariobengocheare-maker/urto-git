# FOREWARN Owner-Lookup Automation

Automates property-owner phone lookups on FOREWARN for internal outreach use.
You log in manually; the script drives the search + matching logic from there.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Input

CSV with a header row, columns:

```
first_name,last_name,address,city,state,zip
```

`address` is the street line only (e.g. `15730 SW 127th Ave Apt 301`). See
`input_sample.csv` for an example.

## Run

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

## Known fragile spot

Clicking a specific result card out of the list of candidates relies on a
best-guess DOM structure (I built this from screenshots, not the live page).
If a run throws errors when trying to open a card:

```bash
python forewarn_lookup.py --input input.csv --output output.csv --debug
```

`--debug` pauses on Playwright Inspector before each card click so you can
point-and-click the right element and report back what needs to change in
`get_result_cards` / `CARD_ANCESTOR_XPATH` in `forewarn_lookup.py`.

## Notes

- Strictly for your own business outreach — not for resale/marketing lists.
- Respect FOREWARN's rate limits; keep `--delay` reasonable.
