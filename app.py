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

from input_parser import OUTPUT_FIELDS, load_rows
from lookup_engine import FOREWARN_SEARCH_URL, process_row

app = Flask(__name__)

OUTPUT_DIR = Path(__file__).parent / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

JOBS = {}
JOBS_LOCK = threading.Lock()


def run_job(job_id, rows):
    job = JOBS[job_id]
    output_path = OUTPUT_DIR / f"{job_id}.csv"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(FOREWARN_SEARCH_URL, wait_until="domcontentloaded")

        job["status"] = "awaiting_login"
        job["login_event"].wait()

        job["status"] = "running"
        with open(output_path, "w", newline="", encoding="utf-8") as out_f:
            writer = csv.DictWriter(out_f, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()

            for row in rows:
                skip_reason = row.pop("skip_reason", "")
                if skip_reason:
                    result = {"phone": "", "status": "SKIPPED", "notes": skip_reason}
                else:
                    try:
                        result = process_row(page, row)
                    except Exception as e:
                        result = {"phone": "", "status": "ERROR", "notes": str(e)}

                out_row = {k: row.get(k, "") for k in OUTPUT_FIELDS if k in row}
                out_row.update(result)
                writer.writerow(out_row)
                out_f.flush()

                with JOBS_LOCK:
                    job["rows"].append(out_row)
                    job["processed"] += 1

                if not skip_reason:
                    time.sleep(job["delay"])

        browser.close()

    job["output_path"] = str(output_path)
    job["status"] = "done"


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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, threaded=True)
