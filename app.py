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
import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, render_template, request, send_file
from playwright.sync_api import sync_playwright

import crm
from input_parser import OUTPUT_FIELDS, build_output_row, load_rows
from lookup_engine import FOREWARN_SEARCH_URL, process_row

app = Flask(__name__)

# Bump this by hand whenever a change is shipped, so Mario can tell at a
# glance (bottom of every page) which build he's actually running. The DATE
# shown alongside it is NOT hand-typed (that used to drift out of sync with
# reality) — see _get_last_updated_display() below, which reads the real
# install moment straight off whatever PC is actually running this.
APP_VERSION = "1.8.2"

LAST_UPDATED_MARKER = Path(__file__).parent / "last_updated.txt"


def _get_last_updated_display() -> str:
    """The URTO Updater writes LAST_UPDATED_MARKER with this machine's own
    clock at the moment it finishes installing (urto_updater.pyw's
    install_update()) — that's the real "when did this PC last get updated"
    answer, not a guess typed into source code weeks in advance. Falls back
    to this file's own mtime (e.g. a fresh git clone, or an update installed
    via the old manual ZIP method before the marker file existed).

    Always displayed converted to America/New_York (Mario's own Miami-Dade
    timezone), regardless of what timezone the installing PC's system clock
    is actually set to — a plain datetime.now().astimezone() only carries a
    fixed UTC-offset tzinfo, whose %Z prints an ugly "UTC-04:00" instead of
    a real "EST"/"EDT" abbreviation, so this explicitly re-zones it."""
    try:
        dt = datetime.fromisoformat(LAST_UPDATED_MARKER.read_text().strip())
    except Exception:
        try:
            dt = datetime.fromtimestamp(Path(__file__).stat().st_mtime).astimezone()
        except Exception:
            return "unknown"
    try:
        dt = dt.astimezone(ZoneInfo("America/New_York"))
        tz_label = dt.strftime("%Z")
    except Exception:
        tz_label = dt.strftime("%Z") or "local time"
    return f"{dt.strftime('%b %d, %Y %I:%M %p')} {tz_label} (Miami Time)".strip()


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

# The desktop launcher starts this server detached, with no window — so
# nothing closes it automatically when Mario's done, and he was having to
# hunt it down in Task Manager every time. Each browser tab generates its
# own tab_id on load and pings /api/heartbeat every few seconds, and fires
# /api/shutdown (with that same tab_id) the instant it closes; the watchdog
# thread below is the fallback for anything that skips that (a crash,
# force-closing the browser, the PC sleeping) — if a tab hasn't been heard
# from in a while, it's dropped from the active set. The server only
# actually exits once the active set is EMPTY — tracking per-tab instead of
# one global "last heartbeat" is what makes this safe with more than one
# URTO tab open: closing one tab used to fire a global shutdown and kill
# the server out from under a SECOND tab that was still genuinely in use
# (Mario hit this — "New Transaction" failed with "couldn't reach the
# server" moments after the page had loaded fine). Same general pattern
# already shipped on Wardrobe, adapted here for multi-tab correctness.
#
# HEARTBEAT_TIMEOUT used to be 20s, which was ALSO a real bug on its own:
# most browsers throttle a BACKGROUND tab's JS timers hard (sometimes to
# once a minute or less) — so switching away from URTO for a bit (e.g. to
# the OS file picker to grab a document to upload) could silently blow past
# 20s with the tab never actually closed, and the watchdog would kill the
# server out from under an in-progress upload/save. Raised to a much more
# forgiving window, and _request_in_progress() below adds a second, more
# direct guard: never exit while ANY real request (upload, note save,
# transaction edit, ...) is actually being handled, not just a FOREWARN job.
HEARTBEAT_TIMEOUT = 180

_active_tabs = {}  # tab_id -> last-heartbeat time.time()
_active_tabs_lock = threading.Lock()
_ever_connected = {"v": False}  # don't let the watchdog fire before the very first tab has even had a chance to check in
_server_started_at = time.time()
STARTUP_GRACE_SECONDS = 15  # a just-launched tab needs a moment for its first heartbeat to land

_active_requests = {"count": 0}
_active_requests_lock = threading.Lock()
_EXEMPT_PATHS = {"/api/heartbeat", "/api/shutdown"}


@app.before_request
def _track_request_start():
    if request.path not in _EXEMPT_PATHS:
        with _active_requests_lock:
            _active_requests["count"] += 1


@app.teardown_request
def _track_request_end(exc=None):
    if request.path not in _EXEMPT_PATHS:
        with _active_requests_lock:
            _active_requests["count"] = max(0, _active_requests["count"] - 1)


def _request_in_progress() -> bool:
    with _active_requests_lock:
        return _active_requests["count"] > 0


def _job_in_progress() -> bool:
    """Never auto-close mid-lookup — a real headed Chromium window may be
    scraping FOREWARN in a background thread, and killing the process would
    abandon that run with no partial-results file ever written."""
    with JOBS_LOCK:
        return any(j["status"] in ("starting", "awaiting_login", "running") for j in JOBS.values())


def _safe_to_exit() -> bool:
    return not _job_in_progress() and not _request_in_progress()


def _get_tab_id() -> str:
    data = request.get_json(silent=True) or {}
    return data.get("tab_id") or "default"


@app.route("/api/heartbeat", methods=["POST"])
def heartbeat():
    with _active_tabs_lock:
        _active_tabs[_get_tab_id()] = time.time()
        _ever_connected["v"] = True
    return jsonify({"ok": True})


@app.route("/api/shutdown", methods=["POST"])
def shutdown():
    with _active_tabs_lock:
        _active_tabs.pop(_get_tab_id(), None)
        any_tabs_left = bool(_active_tabs)
    # A brand-new tab (e.g. one the URTO Updater just launched) needs a
    # moment for its own first heartbeat to land before it's fairly counted
    # as "active" — without this, closing an OLD tab in that same instant
    # could see zero active tabs and shut down the server on the new one.
    within_startup_grace = (time.time() - _server_started_at) < STARTUP_GRACE_SECONDS
    if not any_tabs_left and not within_startup_grace and _safe_to_exit():
        threading.Timer(0.2, lambda: os._exit(0)).start()
    return jsonify({"ok": True})


def _watchdog():
    while True:
        time.sleep(5)
        now = time.time()
        with _active_tabs_lock:
            stale = [tid for tid, t in _active_tabs.items() if now - t > HEARTBEAT_TIMEOUT]
            for tid in stale:
                del _active_tabs[tid]
            any_tabs_left = bool(_active_tabs)
        within_startup_grace = (now - _server_started_at) < STARTUP_GRACE_SECONDS
        if _ever_connected["v"] and not any_tabs_left and not within_startup_grace and _safe_to_exit():
            os._exit(0)


threading.Thread(target=_watchdog, daemon=True).start()


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
    return render_template("index.html", app_version=APP_VERSION, app_version_date=_get_last_updated_display())


@app.route("/api/version")
def api_version():
    # Cheap, unauthenticated version check — lets launch_desktop.pyw detect
    # "something's already on this port, but it's a stale pre-update process"
    # and self-heal instead of just opening a browser tab to the old code.
    return jsonify({"version": APP_VERSION})


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
            start_date=(data.get("start_date") or "").strip() or None,
        )
    except crm.DuplicatePhoneError as e:
        return jsonify({"error": str(e)}), 409
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
    except crm.DuplicatePhoneError as e:
        return jsonify({"error": str(e)}), 409
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


@app.route("/api/crm/clients/<int:client_id>/log_call", methods=["POST"])
def crm_log_call(client_id):
    client = crm.log_call(client_id)
    if not client:
        abort(404)
    return jsonify(client)


@app.route("/api/crm/clients/<int:client_id>/call_history", methods=["GET"])
def crm_call_history(client_id):
    if not crm.get_client(client_id):
        abort(404)
    return jsonify(crm.list_call_history(client_id))


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


@app.route("/api/crm/contact_lists/<int:list_id>/members/add", methods=["POST"])
def crm_add_list_members(list_id):
    data = request.get_json(force=True)
    client_ids = data.get("client_ids") or []
    contact_list = crm.add_list_members(list_id, client_ids)
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


@app.route("/api/crm/import_recurring_csv/preview", methods=["POST"])
def crm_import_recurring_csv_preview():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    raw_text = request.files["file"].read().decode("utf-8", errors="replace")
    rows = crm.preview_recurring_followups_csv(raw_text)
    if rows is None:
        return jsonify({"error": "File doesn't match the expected recurring-follow-up export columns."}), 400
    return jsonify({"rows": rows})


@app.route("/api/crm/import_recurring_csv", methods=["POST"])
def crm_import_recurring_csv():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    raw_text = request.files["file"].read().decode("utf-8", errors="replace")
    selected_indices = None
    raw_indices = request.form.get("selected_indices")
    if raw_indices is not None:
        try:
            selected_indices = set(json.loads(raw_indices))
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid selected_indices"}), 400
    result = crm.import_recurring_followups_csv(raw_text, selected_indices=selected_indices)
    return jsonify(result)


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
    list_id = data.get("list_id") or None
    if list_id and not crm.get_contact_list(list_id):
        return jsonify({"error": "Contact list not found"}), 404
    event = crm.create_event(
        client_id=client_id, list_id=list_id, title=title, date_str=date_str,
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
    list_id = data.get("list_id") or None
    if list_id and not crm.get_contact_list(list_id):
        return jsonify({"error": "Contact list not found"}), 404
    event = crm.update_event(
        event_id, client_id=client_id, list_id=list_id, title=title, date_str=date_str,
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


@app.route("/api/backup/google_drive_dir", methods=["POST"])
def set_google_drive_dir():
    path = (request.get_json(force=True) or {}).get("path", "").strip()
    if not path:
        return jsonify({"error": "Enter a folder path."}), 400
    try:
        crm.set_google_drive_dir(path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(crm.get_backup_status())


@app.route("/api/backup/health")
def backup_health():
    return jsonify(crm.check_data_health())


@app.route("/api/backup/snapshots")
def backup_snapshots():
    return jsonify(crm.list_available_snapshots())


@app.route("/api/backup/restore", methods=["POST"])
def backup_restore():
    path = (request.get_json(force=True) or {}).get("path", "").strip()
    if not path:
        return jsonify({"error": "No backup file specified."}), 400
    try:
        result = crm.restore_from_snapshot(path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result)


@app.route("/api/text_presets", methods=["GET"])
def get_text_presets():
    return jsonify(crm.list_text_presets())


@app.route("/api/text_presets", methods=["POST"])
def create_text_preset():
    text = (request.get_json(force=True) or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "Type a message first."}), 400
    return jsonify(crm.add_text_preset(text)), 201


@app.route("/api/text_presets/<int:preset_id>", methods=["DELETE"])
def remove_text_preset(preset_id):
    crm.delete_text_preset(preset_id)
    return jsonify({"ok": True})


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
