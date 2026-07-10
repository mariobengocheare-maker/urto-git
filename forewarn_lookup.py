#!/usr/bin/env python3
"""
FOREWARN owner-lookup automation (command-line version).

Usage:
    python forewarn_lookup.py --input input.csv --output output.csv [--delay 5] [--debug]

Input CSV columns (header row required):
    first_name,last_name,address,city,state,zip

Output CSV adds:
    phone,status,notes

Login: the script opens a real (headed) Chromium window and pauses so you can
log into FOREWARN by hand. Once you're on the search page, press Enter in the
terminal to start the lookups.

For a drag-and-drop web UI instead of the command line, run app.py.
"""

import argparse
import csv
import random
import sys
import time

from playwright.sync_api import sync_playwright

from input_parser import OUTPUT_FIELDS, build_output_row, load_rows
from lookup_engine import FOREWARN_SEARCH_URL, process_row


def main():
    parser = argparse.ArgumentParser(description="FOREWARN owner-lookup automation")
    parser.add_argument("--input", required=True, help="Input CSV path")
    parser.add_argument("--output", required=True, help="Output CSV path")
    parser.add_argument("--delay", type=float, default=5.0,
                         help="Base delay in seconds between lookups (default 5)")
    parser.add_argument("--debug", action="store_true",
                         help="Pause with Playwright Inspector before each card click")
    args = parser.parse_args()

    with open(args.input, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        raw_rows = list(reader)
        fieldnames = reader.fieldnames

    if not raw_rows:
        sys.exit("Input CSV is empty.")

    try:
        rows = load_rows(fieldnames, raw_rows)
    except ValueError as e:
        sys.exit(str(e))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(FOREWARN_SEARCH_URL, wait_until="domcontentloaded")

        input("\nLog into FOREWARN in the browser window that just opened.\n"
              "Once you're on the search page, press Enter here to start...\n")

        with open(args.output, "w", newline="", encoding="utf-8") as out_f:
            writer = csv.DictWriter(out_f, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()

            for idx, row in enumerate(rows, start=1):
                owner = row.get("owner_name_raw") or f"{row.get('first_name','')} {row.get('last_name','')}"
                skip_reason = row.pop("skip_reason", "")

                if skip_reason:
                    print(f"[{idx}/{len(rows)}] Skipping {owner}: {skip_reason}")
                    result = {"phone": "", "status": "SKIPPED", "notes": skip_reason}
                else:
                    print(f"[{idx}/{len(rows)}] Looking up {owner} / {row['address']}...")
                    try:
                        result = process_row(page, row, debug=args.debug)
                    except Exception as e:
                        result = {"phone": "", "status": "ERROR", "notes": str(e)}
                        print(f"  -> ERROR: {e}")

                out_row = build_output_row(row, result)
                writer.writerow(out_row)
                out_f.flush()

                print(f"  -> {result['status']}"
                      + (f" ({result['phone']})" if result["phone"] else ""))

                if idx < len(rows) and not skip_reason:
                    time.sleep(args.delay + random.uniform(0, 1.5))

        browser.close()

    print(f"\nDone. Results written to {args.output}")


if __name__ == "__main__":
    main()
