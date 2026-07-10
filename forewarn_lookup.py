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
        rows = list(csv.DictReader(f))

    required_cols = {"first_name", "last_name", "address", "zip"}
    if rows and not required_cols.issubset(rows[0].keys()):
        sys.exit(f"Input CSV must have at least these columns: {sorted(required_cols)}")

    out_fields = list(rows[0].keys()) + ["phone", "status", "notes"] if rows else []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(FOREWARN_SEARCH_URL, wait_until="domcontentloaded")

        input("\nLog into FOREWARN in the browser window that just opened.\n"
              "Once you're on the search page, press Enter here to start...\n")

        with open(args.output, "w", newline="", encoding="utf-8") as out_f:
            writer = csv.DictWriter(out_f, fieldnames=out_fields)
            writer.writeheader()

            for idx, row in enumerate(rows, start=1):
                print(f"[{idx}/{len(rows)}] Looking up {row['first_name']} {row['last_name']} "
                      f"/ {row['address']}...")
                try:
                    result = process_row(page, row, debug=args.debug)
                except Exception as e:
                    result = {"phone": "", "status": "ERROR", "notes": str(e)}
                    print(f"  -> ERROR: {e}")

                out_row = dict(row)
                out_row.update(result)
                writer.writerow(out_row)
                out_f.flush()

                print(f"  -> {result['status']}"
                      + (f" ({result['phone']})" if result["phone"] else ""))

                if idx < len(rows):
                    time.sleep(args.delay + random.uniform(0, 1.5))

        browser.close()

    print(f"\nDone. Results written to {args.output}")


if __name__ == "__main__":
    main()
