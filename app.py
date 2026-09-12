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
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_file, url_for
from playwright.sync_api import sync_playwright

import crm
import hosted_sync
from input_parser import OUTPUT_FIELDS, build_output_row, load_rows
from lookup_engine import FOREWARN_SEARCH_URL, process_row

app = Flask(__name__)

# ===================== "One cloud" — hosted sync =====================
# Every CRM/Dialer/Transactions/Calendar route is forwarded to the same
# hosted database the phone app uses (see hosted_sync.py) instead of being
# handled locally — there is only ever one real copy of the data. Skip
# Trace/Owner Lookup routes below are unaffected (they're desktop-only,
# can't run on a server, and aren't registered through this proxy).


def _hosted_proxy_view(subpath=""):
    prefix = request.url_rule.rule.split("/<path:subpath>")[0].rstrip("/")
    full_path = f"{prefix}/{subpath}" if subpath else prefix
    body, status, headers = hosted_sync.proxy_request(request, full_path)
    return Response(body, status=status, headers=headers)


for _prefix in hosted_sync.PROXY_PREFIXES:
    app.add_url_rule(_prefix, endpoint=f"proxy{_prefix}", view_func=_hosted_proxy_view,
                      methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    app.add_url_rule(f"{_prefix}/<path:subpath>", endpoint=f"proxy{_prefix}_sub", view_func=_hosted_proxy_view,
                      methods=["GET", "POST", "PUT", "DELETE", "PATCH"])


@app.route("/hosted_setup", methods=["GET", "POST"])
def hosted_setup():
    existing = hosted_sync.load_config() or {}
    error = None
    if request.method == "POST":
        base_url = request.form.get("base_url", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        error = hosted_sync.test_login(base_url, username, password)
        if not error:
            hosted_sync.save_config(base_url, username, password)
            return redirect(url_for("index"))
    return render_template(
        "hosted_setup.html", error=error,
        base_url=request.form.get("base_url", existing.get("base_url", "")),
        username=request.form.get("username", existing.get("username", "")),
        already_configured=bool(existing),
    )


_UNGATED_ENDPOINTS = {"hosted_setup", "static", "heartbeat", "shutdown", "api_version"}


@app.before_request
def _require_hosted_setup():
    # Every page needs URTO Cloud connected before it's useful (CRM/Dialer/
    # Transactions all proxy there now) -- force the one-time setup form
    # first rather than letting Mario land on a broken-looking CRM tab.
    # Skip Trace's own infrastructure routes (heartbeat/shutdown/version)
    # are desktop-only plumbing unrelated to hosted sync and must keep
    # working regardless of setup status.
    if request.endpoint in _UNGATED_ENDPOINTS or (request.endpoint or "").startswith("proxy"):
        return None
    if not hosted_sync.is_configured():
        return redirect(url_for("hosted_setup"))
    return None

# Bump this by hand whenever a change is shipped, so Mario can tell at a
# glance (bottom of every page) which build he's actually running. The DATE
# shown alongside it is NOT hand-typed (that used to drift out of sync with
# reality) — see _get_last_updated_display() below, which reads the real
# install moment straight off whatever PC is actually running this.
APP_VERSION = "2.13.17"

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
#
# Raised again, much further, after Mario reported the app "just stops
# working" after being left open a long while, then refuses to connect at
# all on reload — the real culprit was Chrome's own background-tab
# throttling/discarding (Memory Saver), which can silently stop a
# background tab's heartbeats for far longer than a few minutes, or tear
# the tab down outright (see templates/index.html's `pagehide` handler for
# the other half of this fix, which stops treating a Chrome-initiated
# discard as a real tab close). This timeout is now purely the backstop
# for a truly abandoned server (browser force-killed, PC crashed) — not
# the everyday "Mario alt-tabbed away for a while" case — so it can
# tolerate hours of ordinary idling/throttling without falsely killing a
# server Mario is still going to come back to.
HEARTBEAT_TIMEOUT = 3 * 60 * 60  # 3 hours

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


# A plain same-tab reload (F5) fires the OLD document's pagehide/shutdown
# beacon an instant before the RELOADED page has had any chance to parse
# this whole file's one giant inline <script> and fire its own first
# heartbeat — if that reload was the only open tab, the old fixed 0.2s exit
# delay could easily win the race and kill the server before the new page
# ever registered, so the reload lands on a dead process (Mario: "was able
# to refresh once but now it does this"). SHUTDOWN_GRACE_SECONDS gives a
# reloading page real time to land its immediate on-load heartbeat (see
# STARTUP_GRACE_SECONDS/build order #49's same fix for the fresh-process
# case) before the shutdown actually commits — `_delayed_shutdown_check()`
# re-verifies `_active_tabs` is STILL empty when the timer fires, not just
# when `/api/shutdown` was first called.
SHUTDOWN_GRACE_SECONDS = 5


def _delayed_shutdown_check():
    with _active_tabs_lock:
        any_tabs_left = bool(_active_tabs)
    if not any_tabs_left and _safe_to_exit():
        os._exit(0)


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
        threading.Timer(SHUTDOWN_GRACE_SECONDS, _delayed_shutdown_check).start()
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
    # /admin/google_auth and /admin/microsoft_auth only exist on the HOSTED
    # server (hosted_app.py) -- they're not among hosted_sync's proxied
    # prefixes, so a plain relative link to them from desktop's own page
    # resolves against 127.0.0.1:5000 and 404s there (hit for real: Mario
    # clicked "Connect Microsoft (Teams)" from the desktop app and got a
    # 404). Passing the real hosted base_url lets the template build an
    # absolute link on desktop while staying a normal relative link on the
    # hosted deployment itself (see templates/index.html).
    config = hosted_sync.load_config()
    hosted_base_url = config["base_url"] if config else ""
    return render_template(
        "index.html", app_version=APP_VERSION, app_version_date=_get_last_updated_display(),
        hosted_base_url=hosted_base_url,
    )


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



if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, threaded=True)
