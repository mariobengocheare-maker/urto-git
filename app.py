#!/usr/bin/env python3
"""
FOREWARN owner-lookup web UI.

Run locally:
    python app.py

Then open http://localhost:5000 in your browser. This still opens a real
Chromium window for you to log into FOREWARN by hand (same as the CLI
version) — the web page is just a drag-and-drop front end for the same
automation, driven from your own machine.
"""

import csv
import io
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file
from playwright.sync_api import sync_playwright

import crm
from input_parser import OUTPUT_FIELDS, build_output_row, load_rows
from lookup_engine import FOREWARN_SEARCH_URL, process_row

app = Flask(__name__)

# Bump these two together whenever a change is shipped, so Mario can tell at
# a glance (bottom of every page) which build he's actually running.
APP_VERSION = "1.4.3"
APP_VERSION_DATE = "Jul 30, 2026 6:20 PM EST"

OUTPUT_DIR = Path(__file__).parent / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

crm.init_db()
crm.backup_now("startup")


def _backup_watcher():
    while True:
        time.sleep(60)
        crm.backup_if_dirty()


threading.Thread(target=_backup_watcher, daemon=True).start()

JOBS = {}
JOBS_LOCK = threading.Lock()


def run_job(job_id, rows):
    job = JOBS[job_id]
    output_path = OUTPUT_DIR / f"{job_id}.csv"
    job["output_path"] = str(output_path)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(FOREWARN_SEARCH_URL, wait_until="domcontentloaded")

        job["status"] = "awaiting_login"
        job["login_event"].wait()

        with open(output_path, "w", newline="", encoding="utf-8") as out_f:
            writer = csv.DictWriter(out_f, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()

            if not job["stop_event"].is_set():
                job["status"] = "running"

            for row in rows:
                if job["stop_event"].is_set():
                    break

                skip_reason = row.pop("skip_reason", "")
                if skip_reason:
                    result = {"phone": "", "status": "SKIPPED", "notes": skip_reason}
                else:
                    try:
                        result = process_row(page, row)
                    except Exception as e:
                        result = {"phone": "", "status": "ERROR", "notes": str(e)}

                out_row = build_output_row(row, result)
                writer.writerow(out_row)
                out_f.flush()

                with JOBS_LOCK:
                    job["rows"].append(out_row)
                    job["processed"] += 1

                if not skip_reason:
                    time.sleep(job["delay"])

        browser.close()

    job["status"] = "stopped" if job["stop_event"].is_set() else "done"


def run_job_safe(job_id, rows):
    try:
        run_job(job_id, rows)
    except Exception as e:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html", app_version=APP_VERSION, app_version_date=APP_VERSION_DATE)


@app.route("/api/upload", methods=["POST"])
def upload():
    with JOBS_LOCK:
        if any(j["status"] in ("starting", "awaiting_login", "running") for j in JOBS.values()):
            return jsonify({"error": "A lookup is already running. Wait for it to finish."}), 409

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400

    try:
        delay = float(request.form.get("delay", 5))
    except ValueError:
        delay = 5.0

    text = file.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    raw_rows = list(reader)

    if not raw_rows:
        return jsonify({"error": "CSV is empty"}), 400

    try:
        rows = load_rows(reader.fieldnames, raw_rows)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "status": "starting",
        "total": len(rows),
        "processed": 0,
        "rows": [],
        "login_event": threading.Event(),
        "stop_event": threading.Event(),
        "delay": delay,
        "output_path": None,
        "error": None,
    }

    thread = threading.Thread(target=run_job_safe, args=(job_id, rows), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/confirm_login/<job_id>", methods=["POST"])
def confirm_login(job_id):
    job = JOBS.get(job_id)
    if not job:
        abort(404)
    job["login_event"].set()
    return jsonify({"ok": True})


@app.route("/api/stop/<job_id>", methods=["POST"])
def stop(job_id):
    job = JOBS.get(job_id)
    if not job:
        abort(404)
    job["stop_event"].set()
    # In case it's paused waiting on login confirmation, wake it so it can
    # exit cleanly instead of hanging forever.
    job["login_event"].set()
    return jsonify({"ok": True})


@app.route("/api/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        abort(404)
    with JOBS_LOCK:
        return jsonify({
            "status": job["status"],
            "total": job["total"],
            "processed": job["processed"],
            "rows": job["rows"],
            "error": job["error"],
        })


@app.route("/api/download/<job_id>")
def download(job_id):
    job = JOBS.get(job_id)
    if not job or not job["output_path"]:
        abort(404)
    return send_file(job["output_path"], as_attachment=True, download_name="forewarn_results.csv")


@app.route("/api/lookup/weekly_count")
def lookup_weekly_count():
    return jsonify(crm.get_weekly_lookup_count())


@app.route("/api/crm/clients", methods=["GET"])
def crm_list_clients():
    return jsonify(crm.list_clients())


@app.route("/api/crm/clients", methods=["POST"])
def crm_create_client():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    try:
        client = crm.create_client(
            name=name,
            phone=(data.get("phone") or "").strip(),
            contact_info=(data.get("contact_info") or "").strip(),
            address=(data.get("address") or "").strip(),
            frequency_key=data.get("frequency_key"),
            custom_amount=data.get("custom_amount"),
            custom_unit=data.get("custom_unit"),
            list_ids=data.get("list_ids"),
        )
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid custom follow-up amount"}), 400
    return jsonify(client)


@app.route("/api/crm/clients/<int:client_id>", methods=["GET"])
def crm_get_client(client_id):
    client = crm.get_client(client_id)
    if not client:
        abort(404)
    client["notes"] = crm.list_notes(client_id)
    client["list_ids"] = crm.get_client_list_ids(client_id)
    return jsonify(client)


@app.route("/api/crm/clients/<int:client_id>", methods=["PUT"])
def crm_update_client(client_id):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    try:
        client = crm.update_client(
            client_id,
            name=name,
            phone=(data.get("phone") or "").strip(),
            contact_info=(data.get("contact_info") or "").strip(),
            address=(data.get("address") or "").strip(),
            frequency_key=data.get("frequency_key"),
            custom_amount=data.get("custom_amount"),
            custom_unit=data.get("custom_unit"),
            list_ids=data.get("list_ids"),
        )
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid custom follow-up amount"}), 400
    if not client:
        abort(404)
    return jsonify(client)


@app.route("/api/crm/clients/<int:client_id>", methods=["DELETE"])
def crm_delete_client(client_id):
    crm.delete_client(client_id)
    return jsonify({"ok": True})


@app.route("/api/crm/clients/<int:client_id>/notes", methods=["POST"])
def crm_add_note(client_id):
    data = request.get_json(force=True)
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Note text is required"}), 400
    if not crm.get_client(client_id):
        abort(404)
    note = crm.add_note(client_id, text)
    return jsonify(note)


@app.route("/api/crm/clients/<int:client_id>/complete_followup", methods=["POST"])
def crm_complete_followup(client_id):
    client = crm.complete_followup(client_id)
    if not client:
        abort(404)
    return jsonify(client)


@app.route("/api/crm/contact_lists", methods=["GET"])
def crm_list_contact_lists():
    return jsonify(crm.list_contact_lists())


@app.route("/api/crm/contact_lists", methods=["POST"])
def crm_create_contact_list():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    try:
        contact_list = crm.create_contact_list(
            name=name,
            frequency_key=data.get("frequency_key"),
            custom_amount=data.get("custom_amount"),
            custom_unit=data.get("custom_unit"),
        )
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid custom call schedule amount"}), 400
    return jsonify(contact_list)


@app.route("/api/crm/contact_lists/<int:list_id>", methods=["GET"])
def crm_get_contact_list(list_id):
    contact_list = crm.get_contact_list(list_id)
    if not contact_list:
        abort(404)
    return jsonify(contact_list)


@app.route("/api/crm/contact_lists/<int:list_id>", methods=["PUT"])
def crm_update_contact_list(list_id):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    try:
        contact_list = crm.update_contact_list(
            list_id,
            name=name,
            frequency_key=data.get("frequency_key"),
            custom_amount=data.get("custom_amount"),
            custom_unit=data.get("custom_unit"),
        )
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid custom call schedule amount"}), 400
    if not contact_list:
        abort(404)
    return jsonify(contact_list)


@app.route("/api/crm/contact_lists/<int:list_id>", methods=["DELETE"])
def crm_delete_contact_list(list_id):
    crm.delete_contact_list(list_id)
    return jsonify({"ok": True})


@app.route("/api/crm/contact_lists/<int:list_id>/members", methods=["POST"])
def crm_set_list_members(list_id):
    data = request.get_json(force=True)
    client_ids = data.get("client_ids") or []
    contact_list = crm.set_list_members(list_id, client_ids)
    if not contact_list:
        abort(404)
    return jsonify(contact_list)


@app.route("/api/crm/contact_lists/<int:list_id>/complete_round", methods=["POST"])
def crm_complete_list_round(list_id):
    contact_list = crm.complete_list_round(list_id)
    if not contact_list:
        abort(404)
    return jsonify(contact_list)


@app.route("/api/crm/contact_lists/import_vcard", methods=["POST"])
def crm_import_vcard():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify({"error": "List name is required"}), 400
    address = (request.form.get("address") or "").strip()
    try:
        raw_text = request.files["file"].read().decode("utf-8", errors="replace")
        contact_list = crm.import_contact_list_file(
            name=name,
            address=address,
            frequency_key=request.form.get("frequency_key"),
            custom_amount=request.form.get("custom_amount"),
            custom_unit=request.form.get("custom_unit"),
            raw_text=raw_text,
        )
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid custom call schedule amount"}), 400
    if not contact_list:
        return jsonify({"error": "No contacts with both a name and phone number were found in that file."}), 400
    return jsonify(contact_list)


@app.route("/api/crm/events", methods=["POST"])
def crm_create_event():
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    date_str = (data.get("date") or "").strip()
    if not title or not date_str:
        return jsonify({"error": "Title and date are required"}), 400
    client_id = data.get("client_id") or None
    if client_id and not crm.get_client(client_id):
        return jsonify({"error": "Client not found"}), 404
    event = crm.create_event(
        client_id=client_id, title=title, date_str=date_str,
        time_str=(data.get("time") or "").strip(), notes=(data.get("notes") or "").strip(),
    )
    return jsonify(event)


@app.route("/api/crm/events/<int:event_id>", methods=["PUT"])
def crm_update_event(event_id):
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    date_str = (data.get("date") or "").strip()
    if not title or not date_str:
        return jsonify({"error": "Title and date are required"}), 400
    client_id = data.get("client_id") or None
    if client_id and not crm.get_client(client_id):
        return jsonify({"error": "Client not found"}), 404
    event = crm.update_event(
        event_id, client_id=client_id, title=title, date_str=date_str,
        time_str=(data.get("time") or "").strip(), notes=(data.get("notes") or "").strip(),
    )
    if not event:
        abort(404)
    return jsonify(event)


@app.route("/api/crm/events/<int:event_id>", methods=["DELETE"])
def crm_delete_event(event_id):
    crm.delete_event(event_id)
    return jsonify({"ok": True})


@app.route("/api/crm/followups/today")
def crm_followups_today():
    return jsonify(crm.get_today_followups())


@app.route("/api/backup/status")
def backup_status():
    return jsonify(crm.get_backup_status())


@app.route("/api/backup/run", methods=["POST"])
def backup_run():
    return jsonify(crm.backup_now("manual"))


@app.route("/api/crm/calendar")
def crm_calendar():
    try:
        year = int(request.args.get("year"))
        month = int(request.args.get("month"))
    except (TypeError, ValueError):
        return jsonify({"error": "year and month are required"}), 400
    return jsonify(crm.get_calendar_events(year, month))


# ===================== Transaction Manager =====================

@app.route("/api/txn/types")
def txn_types():
    return jsonify([{"key": k, "label": label} for k, label in crm.TRANSACTION_TYPES])


@app.route("/api/txn/document_types")
def txn_document_types():
    txn_type = request.args.get("type", "")
    if txn_type not in crm.TRANSACTION_TYPE_KEYS:
        return jsonify({"error": "Unknown or missing transaction type"}), 400
    return jsonify(crm.list_document_types(txn_type))


@app.route("/api/txn/document_types", methods=["POST"])
def txn_add_document_type():
    data = request.get_json(force=True)
    txn_type = data.get("transaction_type", "")
    name = (data.get("name") or "").strip()
    if txn_type not in crm.TRANSACTION_TYPE_KEYS:
        return jsonify({"error": "Unknown or missing transaction type"}), 400
    if not name:
        return jsonify({"error": "Document name is required"}), 400
    return jsonify(crm.add_document_type(txn_type, name))


@app.route("/api/txn/document_types/<int:doc_type_id>", methods=["DELETE"])
def txn_delete_document_type(doc_type_id):
    crm.delete_document_type(doc_type_id)
    return jsonify({"ok": True})


@app.route("/api/txn/document_types/reorder", methods=["POST"])
def txn_reorder_document_types():
    data = request.get_json(force=True)
    txn_type = data.get("transaction_type", "")
    ordered_ids = data.get("ordered_ids") or []
    if txn_type not in crm.TRANSACTION_TYPE_KEYS:
        return jsonify({"error": "Unknown or missing transaction type"}), 400
    if not isinstance(ordered_ids, list):
        return jsonify({"error": "ordered_ids must be a list"}), 400
    return jsonify(crm.reorder_document_types(txn_type, ordered_ids))


@app.route("/api/txn/document_types/<int:doc_type_id>/template", methods=["POST"])
def txn_upload_document_type_template(doc_type_id):
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400
    updated = crm.set_document_type_template(doc_type_id, file)
    if not updated:
        abort(404)
    return jsonify(updated)


@app.route("/api/txn/document_types/<int:doc_type_id>/template", methods=["DELETE"])
def txn_remove_document_type_template(doc_type_id):
    crm.remove_document_type_template(doc_type_id)
    return jsonify({"ok": True})


@app.route("/api/txn/document_types/<int:doc_type_id>/template")
def txn_download_document_type_template(doc_type_id):
    doc_type = crm.get_document_type(doc_type_id)
    if not doc_type or not doc_type["template_filename"]:
        abort(404)
    return send_file(
        crm.TEMPLATES_DIR / doc_type["template_filename"],
        as_attachment=True,
        download_name=doc_type["template_original_name"] or "template",
    )


@app.route("/api/txn/transactions")
def txn_list_transactions():
    folder = request.args.get("folder", "active")
    return jsonify(crm.list_transactions(folder=folder))


@app.route("/api/txn/transactions", methods=["POST"])
def txn_create_transaction():
    data = request.get_json(force=True)
    txn_type = data.get("transaction_type", "")
    title = (data.get("title") or "").strip()
    if txn_type not in crm.TRANSACTION_TYPE_KEYS:
        return jsonify({"error": "Unknown or missing transaction type"}), 400
    if not title:
        return jsonify({"error": "Title is required"}), 400
    return jsonify(crm.create_transaction(txn_type, title))


@app.route("/api/txn/transactions/<int:txn_id>")
def txn_get_transaction(txn_id):
    txn = crm.get_transaction(txn_id)
    if not txn:
        abort(404)
    return jsonify(txn)


@app.route("/api/txn/transactions/<int:txn_id>", methods=["PUT"])
def txn_update_transaction(txn_id):
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    status = data.get("status", "")
    if not title:
        return jsonify({"error": "Title is required"}), 400
    if status not in crm.TRANSACTION_STATUSES:
        return jsonify({"error": "Invalid status"}), 400
    updated = crm.update_transaction(txn_id, title, status)
    if not updated:
        abort(404)
    return jsonify(updated)


@app.route("/api/txn/transactions/<int:txn_id>", methods=["DELETE"])
def txn_delete_transaction(txn_id):
    crm.delete_transaction(txn_id)
    return jsonify({"ok": True})


@app.route("/api/txn/transactions/<int:txn_id>/folder", methods=["POST"])
def txn_set_transaction_folder(txn_id):
    data = request.get_json(force=True)
    folder = data.get("folder", "")
    if folder not in crm.TRANSACTION_FOLDERS:
        return jsonify({"error": "Invalid folder"}), 400
    updated = crm.set_transaction_folder(txn_id, folder)
    if not updated:
        abort(404)
    return jsonify(updated)


@app.route("/api/txn/transaction_documents/<int:doc_id>/signed", methods=["POST"])
def txn_upload_signed(doc_id):
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400
    updated = crm.upload_signed_document(doc_id, file)
    if not updated:
        abort(404)
    return jsonify(updated)


@app.route("/api/txn/transaction_documents/<int:doc_id>/signed", methods=["DELETE"])
def txn_remove_signed(doc_id):
    updated = crm.remove_signed_document(doc_id)
    if not updated:
        abort(404)
    return jsonify(updated)


@app.route("/api/txn/transaction_documents/<int:doc_id>/signed")
def txn_download_signed(doc_id):
    row = crm.get_transaction_document(doc_id)
    if not row or not row["signed_filename"]:
        abort(404)
    return send_file(
        crm.SIGNED_DIR / row["signed_filename"],
        as_attachment=True,
        download_name=row["signed_original_name"] or "signed_document",
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, threaded=True)
