"""One-off utility: import a phone vCard (.vcf) export as a URTO Contact List.

Usage (run this while URTO's app.py is already running):
    python import_contact_list.py contacts.vcf --list-name "9350 Bay Harbor Dr" ^
        --address "9350 W Bay Harbor Drive, Bay Harbor Islands, FL" --frequency 2_weeks

Each vCard becomes a CRM client (name + phone + the given shared address),
then all of them are added as members of a new Contact List with the given
recurring call schedule. Safe to re-run with a different --list-name for a
different building/group; it always creates new clients rather than
de-duplicating against existing ones.
"""

import argparse
import json
import re
import sys
import urllib.request

FREQUENCY_CHOICES = ["none", "2_weeks", "3_weeks", "4_weeks", "2_months", "6_months"]


def post(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def parse_vcf(path):
    text = open(path, encoding="utf-8").read()
    cards = text.split("BEGIN:VCARD")[1:]
    contacts = []
    for card in cards:
        fn_match = re.search(r"^FN:(.+)$", card, re.M)
        tel_match = re.search(r"^TEL[^:]*:(.+)$", card, re.M)
        if fn_match and tel_match:
            contacts.append((fn_match.group(1).strip(), tel_match.group(1).strip()))
    return contacts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vcf_path", help="Path to the exported .vcf file")
    ap.add_argument("--list-name", required=True, help="Name for the new Contact List, e.g. '9350 Bay Harbor Dr'")
    ap.add_argument("--address", default="", help="Shared address to set on every imported client")
    ap.add_argument("--frequency", default="2_weeks", choices=FREQUENCY_CHOICES,
                     help="Recurring call schedule for the whole list (default: 2_weeks)")
    ap.add_argument("--base-url", default="http://127.0.0.1:5000", help="URTO server URL (default: local)")
    args = ap.parse_args()

    contacts = parse_vcf(args.vcf_path)
    if not contacts:
        print("No name+phone vCards found in that file.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(contacts)} contacts:")
    for name, phone in contacts:
        print(f"  {name}  ->  {phone}")

    client_ids = []
    for name, phone in contacts:
        result = post(f"{args.base_url}/api/crm/clients", {
            "name": name, "phone": phone, "address": args.address, "frequency_key": "none",
        })
        client_ids.append(result["id"])
    print(f"\nCreated {len(client_ids)} CRM clients.")

    list_result = post(f"{args.base_url}/api/crm/contact_lists", {
        "name": args.list_name, "frequency_key": args.frequency,
    })
    list_id = list_result["id"]

    result = post(f"{args.base_url}/api/crm/contact_lists/{list_id}/members", {"client_ids": client_ids})
    print(f"Created contact list '{args.list_name}' with {result['member_count']} members "
          f"(next round due {result['next_call_date']}).")


if __name__ == "__main__":
    main()
