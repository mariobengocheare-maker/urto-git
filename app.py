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
import re
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_file, url_for

import crm
import hosted_sync
from input_parser import OUTPUT_FIELDS, build_output_row, load_rows, address_tokens, text_contains_address

# `playwright.sync_api` (and lookup_engine.py/llc_lookup.py, which both
# import it too) is deliberately NOT imported here at module load time --
# see build order #120. It's a genuinely slow import (over a second on its
# own), and every one of app.py's other routes (the hosted-sync proxy,
# Skip Trace's own heartbeat/shutdown/version plumbing) is used constantly
# without ever touching Skip Trace itself. Paying that cost on every single
# server cold-start -- which happens on every "open URTO" now that the
# auto-close feature shuts the process down between sessions -- made the
# whole app feel slow to open even for someone who never runs a lookup.
# `run_job()` below does the actual `from lookup_engine import ...` /
# `from playwright.sync_api import sync_playwright` imports lazily, the
# first time a Skip Trace job is actually started, so that one-time cost
# only lands on the person actually using that feature, not every launch.

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


_UNGATED_ENDPOINTS = {"hosted_setup", "static", "heartbeat", "shutdown", "api_version", "api_check_update"}


@app.before_request
def _require_hosted_setup():
    # Every page needs URTO Cloud connected before it's useful (CRM/Dialer/
    # Transactions all proxy there now) -- force the one-time setup form
    # first rather than letting Mario land on a broken-looking CRM tab.
    # Skip Trace's own infrastructure routes (heartbeat/shutdown/version/
    # check_update) are desktop-only plumbing unrelated to hosted sync and
    # must keep working regardless of setup status.
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
APP_VERSION = "2.57.0"

LAST_UPDATED_MARKER = Path(__file__).parent / "last_updated.txt"

# Kept in sync with launch_desktop.pyw's own copy of this same URL (see
# build order #131's "refresh the page to update" feature) -- update both if
# the branch ever changes.
REPO_RAW_APP_URL = "https://raw.githubusercontent.com/mariobengocheare-maker/urto-git/claude/context-window-dgen3k/app.py"


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

# Batch Sunbiz entity-resolution jobs (build order #163) — a genuinely
# separate, smaller job system from JOBS above. These never touch FOREWARN
# or the weekly lookup counter (Sunbiz resolution never has), never need a
# login, and finish (or fail) well before a real Skip Trace job would ever
# be created — see resolve_entities()/resolve_entities_job() below.
ENTITY_JOBS = {}
ENTITY_JOBS_LOCK = threading.Lock()

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


# A job stuck at "starting"/"awaiting_login" (a closed/abandoned Chromium
# window, or nobody ever clicking login-confirm — see build order #144/#145)
# would otherwise block every future lookup, the auto-updater, AND the
# auto-close watchdog forever, with no way out short of killing python in
# Task Manager. A real login takes seconds to a couple minutes at most, so
# anything stuck this long is treated as abandoned (see build order #146).
STUCK_JOB_TIMEOUT_SECONDS = 20 * 60


def _force_stop_job(job):
    """Same wake the Stop button already used (see stop()) — sets both
    events so a thread genuinely blocked at job["login_event"].wait()
    notices and exits cleanly (closes the browser, marks itself stopped)
    on its own."""
    job["stop_event"].set()
    job["login_event"].set()


def _expire_stale_jobs_locked():
    """Directly marks a too-long-stuck job "stopped" (not just waking its
    events) so _job_in_progress() clears even if the background thread is
    truly hung somewhere before it ever reaches login_event.wait() (e.g. a
    broken Chromium launch) and never notices the wake at all — the thread
    harmlessly overwrites this with the same "stopped" status if/when it
    does eventually catch up. Assumes JOBS_LOCK is already held."""
    now = time.time()
    for job in JOBS.values():
        if job["status"] in ("starting", "awaiting_login") and now - job.get("created_at", now) > STUCK_JOB_TIMEOUT_SECONDS:
            _force_stop_job(job)
            job["status"] = "stopped"
            job["error"] = "Automatically stopped after sitting idle too long waiting for FOREWARN login."


def _job_in_progress() -> bool:
    """True while a real headed Chromium window is (or might soon be)
    scraping FOREWARN in a background thread. Used to be an unconditional
    "never auto-close" gate — see _stop_all_jobs() right below for why
    that's no longer the whole story: Mario explicitly wants closing URTO
    to also stop a still-running scrape, not leave it orphaned forever."""
    with JOBS_LOCK:
        _expire_stale_jobs_locked()
        return any(j["status"] in ("starting", "awaiting_login", "running") for j in JOBS.values())


def _stop_all_jobs():
    """Mario: "if i close urto, it should STOP running forewarn in the
    chromium window." Every browser tab being gone used to just mean
    auto-close politely waited forever for a still-running job to finish on
    its own (see _job_in_progress()'s old docstring) — nothing ever told it
    to stop, so a scrape left running with the tab closed just kept going
    to completion, headed Chromium window and all, with nobody watching it.
    This is the SAME graceful stop the Stop button already triggers
    (_force_stop_job(), reused as-is): run_job()'s loop only checks
    stop_event between rows, so whatever row is already mid-flight still
    finishes and gets written to the partial-results file before the loop
    breaks and the browser closes cleanly — nothing already found is lost,
    only rows not yet reached are left unprocessed. Safe to call on every
    watchdog tick (idempotent — re-setting an already-set Event is a no-op),
    so callers never need to track "did I already ask this job to stop.\""""
    with JOBS_LOCK:
        for job in JOBS.values():
            if job["status"] in ("starting", "awaiting_login", "running"):
                _force_stop_job(job)


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
    if not any_tabs_left:
        # Every tab is gone -- tell any still-running FOREWARN scrape to
        # stop (see _stop_all_jobs()) before checking whether it's actually
        # safe to exit yet. A stop is graceful, not instant, so the first
        # check right after this often still finds the job mid-shutdown
        # (closing its browser) -- _watchdog()'s own recurring 5s tick is
        # what actually finishes the exit once that settles.
        _stop_all_jobs()
        if _safe_to_exit():
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
        if _ever_connected["v"] and not any_tabs_left and not within_startup_grace:
            # Every tab has been gone for a while -- same as
            # _delayed_shutdown_check(), stop any running scrape first, then
            # exit once it (and any in-flight request) has actually settled.
            # This tick repeats every 5s for as long as the process is up,
            # so it's what eventually completes an exit that a scrape's own
            # graceful shutdown took longer than SHUTDOWN_GRACE_SECONDS to
            # finish.
            _stop_all_jobs()
            if _safe_to_exit():
                os._exit(0)


threading.Thread(target=_watchdog, daemon=True).start()


def run_job(job_id, rows):
    # Deliberately lazy -- see the comment on this file's imports up top.
    from playwright.sync_api import sync_playwright
    from lookup_engine import FOREWARN_SEARCH_URL, process_row

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


def resolve_entities_job(job_id, rows, entity_indices):
    """Resolves every LLC/entity row's real person via Sunbiz up front, all
    in one pass, BEFORE any FOREWARN search or login ever happens — see
    build order #163, Mario: "does it run it behind the scenes real quick
    for all the llcs at once and then give me the list for approval before
    we start skip tracing?" This never counts against the weekly FOREWARN
    lookup counter (that only ever increments inside
    lookup_engine._run_forewarn_search — a real FOREWARN search — and this
    function never calls it), and reuses the exact same shared-page Sunbiz
    automation `process_row()` normally calls live, per row, during the real
    run — the only difference is WHEN it runs and that the result gets shown
    to Mario for approval first instead of being spent immediately."""
    from playwright.sync_api import sync_playwright
    from llc_lookup import resolve_entity_owner

    job = ENTITY_JOBS[job_id]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        with ENTITY_JOBS_LOCK:
            job["status"] = "running"

        for idx in entity_indices:
            if job["stop_event"].is_set():
                break
            row = rows[idx]
            entity_name = row.get("entity_name", "")
            try:
                resolved = resolve_entity_owner(page, entity_name)
                skip_reason = resolved.get("skip_reason")
                candidates = [] if skip_reason else (resolved.get("candidates") or [
                    {"first_name": resolved.get("first_name", ""), "last_name": resolved.get("last_name", ""),
                     "resolved_via": resolved.get("resolved_via", "")}
                ])
            except Exception as e:
                skip_reason = f"Error while resolving via Sunbiz: {e}"
                candidates = []

            with ENTITY_JOBS_LOCK:
                job["results"].append({
                    "index": idx,
                    "entity_name": entity_name,
                    "skip_reason": skip_reason,
                    "candidates": candidates,
                })
                job["processed"] += 1

        browser.close()

    with ENTITY_JOBS_LOCK:
        job["status"] = "stopped" if job["stop_event"].is_set() else "done"


def resolve_entities_job_safe(job_id, rows, entity_indices):
    try:
        resolve_entities_job(job_id, rows, entity_indices)
    except Exception as e:
        with ENTITY_JOBS_LOCK:
            ENTITY_JOBS[job_id]["status"] = "error"
            ENTITY_JOBS[job_id]["error"] = str(e)


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


def _remote_app_version():
    """Same lightweight "read APP_VERSION out of the raw GitHub source, no
    zip download yet" trick launch_desktop.pyw's own _remote_version()
    already uses (see build order #123) — duplicated here rather than
    imported, since importing a .pyw file needs the same explicit-loader
    dance that module already exists to demonstrate, and this is a handful
    of lines. Returns None on any failure (no internet, GitHub hiccup) —
    an update check must never be able to break a normal page load."""
    try:
        with urllib.request.urlopen(REPO_RAW_APP_URL, timeout=6) as r:
            text = r.read().decode("utf-8", errors="replace")
        m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
        return m.group(1) if m else None
    except Exception:
        return None


@app.route("/api/check_update", methods=["POST"])
def api_check_update():
    """Mario: "make it so that i can refresh the page and update urto" —
    see build order #131. Previously the only auto-update trigger was
    launch_desktop.pyw's check_for_auto_update(), which only ever runs
    before a FRESH server start (nothing already up) — a plain browser
    refresh against an already-running server never checked anything.
    This route runs the same GitHub version check on every page load
    instead, and if a newer version exists, spawns launch_desktop.pyw as a
    separate DETACHED process to actually perform the kill+update+relaunch.

    That has to happen in a genuinely separate process, not inline here:
    urto_updater.pyw's run_update() (which check_for_auto_update() calls)
    starts by killing whatever's running app.py from this folder, matched
    on command line — which is THIS exact process. Running that update
    logic directly inside the request that's asking for it would kill
    itself mid-update, before the download/install ever finished.

    Never fires while a real Skip Trace scrape is running (_job_in_progress())
    — a hard external process-kill has no chance to write a partial-results
    file the way Stop-button/heartbeat-timeout shutdowns already take care
    to do; the next page load after the scrape finishes will pick the
    update back up."""
    if _job_in_progress():
        return jsonify({"update_available": False, "deferred": True})
    remote = _remote_app_version()
    if not remote or remote == APP_VERSION:
        return jsonify({"update_available": False})
    here = os.path.dirname(os.path.abspath(__file__))
    creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [sys.executable, os.path.join(here, "launch_desktop.pyw"), "--relaunch"],
            cwd=here, creationflags=creationflags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
    except Exception:
        return jsonify({"update_available": False})
    return jsonify({"update_available": True, "version": remote})


def _find_rows_already_in_crm(rows: list) -> tuple:
    """Drops any row whose ADDRESS already matches an existing CRM client,
    before a new Skip Trace job is ever started -- Mario's explicit ask
    ("it should not skip trace the ones i already have in my crm... this
    would happen BEFORE signing into forewarn," see build order #158).

    Matches by ADDRESS, not name, per Mario's own explicit correction ("i
    imagine blocking would be by ADDRESS, not name as there can be two
    people with the same name but 2 people with the same address is SUPER
    unlikely") -- reuses the exact same house-number/street-name/zip
    subset-matching logic (address_tokens/text_contains_address) FOREWARN
    match-verification already relies on, so "does this address already
    exist in the CRM" is answered the same proven way, not a fresh string
    comparison. Only a row that HAS an address+zip to build a real required-
    token set from is ever checked; an is_entity row, an already-skipped
    row, or a row missing address/zip data is left untouched -- there's
    nothing reliable to match on yet for those (an entity's Sunbiz-resolved
    address isn't known until process_row() actually runs).

    Fails OPEN if the hosted CRM can't be reached (unconfigured, offline,
    a Render redeploy in progress, etc.): returns every row untouched with
    zero dropped, rather than silently blocking a real lookup because a
    network check happened to fail -- consistent with this codebase's other
    "never let a diagnostic check itself cause data loss" patterns (see
    check_data_health()'s fail-open-on-ambiguity design).

    Returns (rows_to_process, already_in_crm_count).
    """
    try:
        clients = hosted_sync.get_json("/api/crm/clients")
    except hosted_sync.HostedUnreachableError:
        return rows, 0

    client_addresses = [c.get("address") or "" for c in clients if (c.get("address") or "").strip()]
    if not client_addresses:
        return rows, 0

    rows_to_process = []
    already_in_crm = 0
    for row in rows:
        if row.get("is_entity") or row.get("skip_reason"):
            rows_to_process.append(row)
            continue
        address = (row.get("address") or "").strip()
        zip_code = (row.get("zip") or "").strip()
        if not address or not zip_code:
            rows_to_process.append(row)
            continue
        required = address_tokens(address, zip_code)
        if any(text_contains_address(text, required) for text in client_addresses):
            already_in_crm += 1
        else:
            rows_to_process.append(row)

    return rows_to_process, already_in_crm


@app.route("/api/preview_names", methods=["POST"])
def preview_names():
    """Parses an uploaded CSV the exact same way /api/upload will, but never
    creates a job or touches FOREWARN/the CRM -- this is the read-only
    "do these names look right?" review step (see build order #162, Mario's
    explicit ask to replace the automatic FOREWARN-side name-swap retry,
    which wasted a real search on every reversed-order row, with a manual
    review he confirms once BEFORE any search is ever spent). The returned
    `index` values are positions in this exact parse -- since /api/upload
    runs the identical load_rows() call against the identical file content
    moments later, they line up perfectly with whatever `swapped_indices`
    the frontend sends back once Mario confirms."""
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400

    text = file.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    raw_rows = list(reader)
    if not raw_rows:
        return jsonify({"error": "CSV is empty"}), 400

    try:
        rows = load_rows(reader.fieldnames, raw_rows)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    preview = [
        {
            "index": i,
            "first_name": row["first_name"],
            "last_name": row["last_name"],
            "is_entity": row["is_entity"],
            "entity_name": row["entity_name"],
            "address": row["address"],
            "city": row["city"],
            "zip": row["zip"],
            "skip_reason": row["skip_reason"],
        }
        for i, row in enumerate(rows)
    ]
    return jsonify({"rows": preview, "total": len(preview)})


def _apply_name_swaps(rows: list, swapped_indices_raw: str) -> list:
    """Shared by /api/upload and /api/resolve_entities so both agree on the
    exact same deterministic swap application -- extracted out of upload()
    verbatim (see build order #162). Never touches an entity row -- those
    use entity_name, not first/last, and have nothing to swap. Returns the
    parsed swapped_indices list (callers that don't need it can ignore it)."""
    try:
        swapped_indices = json.loads(swapped_indices_raw or "[]")
    except (ValueError, TypeError):
        swapped_indices = []
    for idx in swapped_indices:
        if isinstance(idx, int) and 0 <= idx < len(rows) and not rows[idx]["is_entity"]:
            rows[idx]["first_name"], rows[idx]["last_name"] = rows[idx]["last_name"], rows[idx]["first_name"]
    return swapped_indices


@app.route("/api/resolve_entities", methods=["POST"])
def resolve_entities():
    """Kicks off build order #163's batch Sunbiz resolution: every LLC/
    entity row in the file gets resolved up front, in one background pass,
    before any FOREWARN search or login. Parses the file the exact same
    deterministic way /api/preview_names and /api/upload already do, so the
    `index` values returned in polling results line up perfectly with what
    Mario already reviewed in the name-swap screen and with the row order
    /api/upload will use moments later.

    Name swaps never affect which rows are entities or what their
    entity_name is, so this deliberately doesn't need swapped_indices at all
    -- Sunbiz resolution only ever reads entity_name.

    Returns {"job_id": None, "total": 0} with no browser ever opened when
    the file has no entity/LLC rows at all -- a plain individual-owner batch
    should never pop a Sunbiz browser window for nothing."""
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400

    text = file.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    raw_rows = list(reader)
    if not raw_rows:
        return jsonify({"error": "CSV is empty"}), 400

    try:
        rows = load_rows(reader.fieldnames, raw_rows)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    entity_indices = [i for i, row in enumerate(rows) if row.get("is_entity")]
    if not entity_indices:
        return jsonify({"job_id": None, "total": 0})

    job_id = uuid.uuid4().hex
    with ENTITY_JOBS_LOCK:
        ENTITY_JOBS[job_id] = {
            "status": "starting",
            "total": len(entity_indices),
            "processed": 0,
            "results": [],
            "stop_event": threading.Event(),
            "error": None,
            "created_at": time.time(),
        }

    thread = threading.Thread(target=resolve_entities_job_safe, args=(job_id, rows, entity_indices), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "total": len(entity_indices)})


@app.route("/api/resolve_entities/<job_id>")
def resolve_entities_status(job_id):
    job = ENTITY_JOBS.get(job_id)
    if not job:
        abort(404)
    with ENTITY_JOBS_LOCK:
        return jsonify({
            "status": job["status"],
            "total": job["total"],
            "processed": job["processed"],
            "results": job["results"],
            "error": job["error"],
        })


@app.route("/api/resolve_entities/<job_id>/stop", methods=["POST"])
def resolve_entities_stop(job_id):
    job = ENTITY_JOBS.get(job_id)
    if not job:
        abort(404)
    job["stop_event"].set()
    return jsonify({"ok": True})


@app.route("/api/upload", methods=["POST"])
def upload():
    with JOBS_LOCK:
        # A truly abandoned job (stuck long past STUCK_JOB_TIMEOUT_SECONDS)
        # is cleared automatically here — see build order #146 — so most of
        # the time this never even reaches the stuck_job_id branch below.
        _expire_stale_jobs_locked()
        # A job can end up genuinely orphaned -- its Chromium window closed
        # or abandoned while stuck at awaiting_login (e.g. build order
        # #144's now-fixed stuck-disabled login button), with the PAGE that
        # started it long since reloaded/closed and its jobId forgotten.
        # /api/stop/<job_id> already knows how to wake a job parked at
        # login_event.wait() and mark it stopped (see stop()) -- but the
        # current page has no way to call it without knowing which job_id
        # is stuck, since a bare error string gives it nothing to act on.
        # Handing the blocking job's own id back here is what lets the
        # frontend offer a real "force stop" recovery instead of leaving
        # Mario with no option but killing python in Task Manager.
        stuck_job_id = next(
            (jid for jid, j in JOBS.items() if j["status"] in ("starting", "awaiting_login", "running")),
            None,
        )
        if stuck_job_id:
            return jsonify({
                "error": "A lookup is already running. Wait for it to finish.",
                "stuck_job_id": stuck_job_id,
            }), 409

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

    # Apply the first/last-name swaps Mario confirmed in the pre-search
    # "do these names look right?" review step (see build order #162,
    # `/api/preview_names`) -- the indices are positions in this exact same
    # deterministic load_rows() parse of the identical file content, so they
    # line up perfectly with what the review screen showed him moments
    # earlier. Never applied to an entity row -- those use `entity_name`,
    # not first/last, and have nothing to swap.
    _apply_name_swaps(rows, request.form.get("swapped_indices", "[]"))

    # If Mario already approved a batch Sunbiz resolution for this exact
    # file (build order #163's /api/resolve_entities, run right before this
    # upload from the same reviewed file), reuse those results instead of
    # letting process_row() resolve each entity row all over again live --
    # same row indices, since name swaps never change which rows are
    # entities or what their entity_name is. Consumed once and discarded --
    # nothing else will ever read this job again once its own upload lands.
    entity_resolve_job_id = request.form.get("entity_resolve_job_id", "")
    if entity_resolve_job_id:
        with ENTITY_JOBS_LOCK:
            resolve_job = ENTITY_JOBS.pop(entity_resolve_job_id, None)
        if resolve_job:
            resolved_by_index = {r["index"]: r for r in resolve_job["results"]}
            for idx, row in enumerate(rows):
                entry = resolved_by_index.get(idx)
                if entry is not None and row.get("is_entity"):
                    row["_resolved_skip_reason"] = entry["skip_reason"]
                    row["_resolved_candidates"] = entry["candidates"] or None

    # Pre-upload CRM dedup (build order #158) -- must run BEFORE any job is
    # created, since a job's existence is what triggers the FOREWARN-login
    # browser window; Mario was explicit this has to happen before that.
    total_input = len(rows)
    rows, already_in_crm_count = _find_rows_already_in_crm(rows)

    if not rows:
        return jsonify({
            "already_in_crm_count": already_in_crm_count,
            "total_input": total_input,
            "all_already_in_crm": True,
        })

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
        "created_at": time.time(),
    }

    thread = threading.Thread(target=run_job_safe, args=(job_id, rows), daemon=True)
    thread.start()

    return jsonify({
        "job_id": job_id,
        "already_in_crm_count": already_in_crm_count,
        "total_input": total_input,
    })


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
    _force_stop_job(job)
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


@app.route("/api/lookup/accounts")
def lookup_accounts():
    return jsonify({"accounts": crm.LOOKUP_ACCOUNTS, "active": crm.get_active_lookup_account()})


@app.route("/api/lookup/account", methods=["POST"])
def set_lookup_account():
    account = (request.get_json(silent=True) or {}).get("account", "")
    try:
        crm.set_active_lookup_account(account)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(crm.get_weekly_lookup_count())



if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, threaded=True)
