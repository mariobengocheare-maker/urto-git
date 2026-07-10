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

OUTPUT_DIR = Path(__file__).parent / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

crm.init_db()

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
    return render_template("index.html")


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


@app.route("/api/crm/calendar")
def crm_calendar():
    try:
        year = int(request.args.get("year"))
        month = int(request.args.get("month"))
    except (TypeError, ValueError):
        return jsonify({"error": "year and month are required"}), 400
    return jsonify(crm.get_calendar_events(year, month))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, threaded=True)
