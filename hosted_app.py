#!/usr/bin/env python3
"""
URTO's hosted deployment: CRM, Dialer, Transactions, and Calendar, reachable
from anywhere (unlike the desktop app.py, which only runs on Mario's PC).
Deliberately does NOT include Owner Lookup/Skip Trace — that needs a real
headed Chromium window logging into FOREWARN by hand, which can't run
unattended on a server. See CLAUDE.md "Hosted CRM/Dialer" for the full
picture: this is meant to be the ONE real copy of the CRM data going
forward (crm.py's URTO_DATA_DIR env var points it at a persistent disk),
with the desktop app's own CRM/Dialer/Transactions tabs pointing here
instead of a separate local database.

Run in production via gunicorn (see render.yaml):
    gunicorn hosted_app:app

Local testing:
    python hosted_app.py
"""

import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, make_response, redirect, render_template, request, session, url_for
from pywebpush import WebPushException, webpush

import crm
import google_drive_api
import microsoft_graph_api
from crm_routes import crm_bp

app = Flask(__name__)
app.register_blueprint(crm_bp)

APP_VERSION = "1.14.1-hosted"

# Render redeploys automatically on every git push -- there's no per-PC
# "updater" moment to read back the way the desktop app's
# _get_last_updated_display() does (see app.py), so the closest honest
# equivalent is "when this server process last started" (i.e. the last
# deploy/restart), computed once at import time. Same display format/
# timezone convention as the desktop footer for consistency.
SERVER_STARTED_DISPLAY = datetime.now(ZoneInfo("America/New_York")).strftime("%b %d, %Y %I:%M %p %Z (Miami Time)")

crm.init_db()
crm.backup_now("startup")


def _backup_watcher():
    while True:
        time.sleep(60)
        crm.backup_if_dirty()


threading.Thread(target=_backup_watcher, daemon=True).start()


# ===================== Access control =====================
# This server holds real client PII and is reachable from the open internet
# (unlike the desktop app, which only ever listens on 127.0.0.1). Originally
# gated with HTTP Basic Auth, but that turned out to be a bad fit for an
# installed iOS PWA: standalone (Add to Home Screen) web apps run in an
# isolated WebKit context that does NOT reliably keep the credentials a
# Basic Auth prompt captured in a regular Safari tab, so Mario kept landing
# on a blank "Authentication required" page when reopening the app icon --
# there's no way to pre-fill/remember HTTP Basic Auth in that context on
# iOS. Replaced with a real login page backed by a signed session cookie
# (Flask's `session`, HttpOnly + SameSite=Lax) -- an ordinary cookie, which
# standalone PWAs persist completely normally, same as any other website.
HOSTED_USERNAME = os.environ.get("HOSTED_USERNAME", "")
HOSTED_PASSWORD = os.environ.get("HOSTED_PASSWORD", "")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# "Save the password once" -- Mario explicitly asked for once a year, not
# just "a while." A long-lived permanent session, refreshed on every visit
# (Flask's default SESSION_REFRESH_EACH_REQUEST re-issues the cookie with a
# fresh expiry on every request), means he only re-enters the password if
# he's genuinely inactive for a full year straight, not on every relaunch
# of the Home Screen icon.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)

_MISSING_AUTH_VARS = [
    name for name, val in (
        ("HOSTED_USERNAME", HOSTED_USERNAME),
        ("HOSTED_PASSWORD", HOSTED_PASSWORD),
        ("FLASK_SECRET_KEY", app.secret_key),
    ) if not val
]
_AUTH_CONFIGURED = not _MISSING_AUTH_VARS

# Routes reachable without a session -- just the login page itself and the
# handful of static PWA files a not-yet-logged-in browser/service worker
# needs to even function. None of these expose any real client data.
_PUBLIC_ENDPOINTS = {"login", "manifest", "service_worker", "static", "notification_briefing_public"}


@app.before_request
def _require_auth():
    if not _AUTH_CONFIGURED:
        # Fail closed: if the env vars weren't set, refuse everything rather
        # than accidentally serving real client data with no password at all.
        return f"Server isn't configured yet (missing {', '.join(_MISSING_AUTH_VARS)}).", 503
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    if not session.get("authed"):
        return redirect(url_for("login", next=request.path))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("username") == HOSTED_USERNAME and request.form.get("password") == HOSTED_PASSWORD:
            session.permanent = True
            session["authed"] = True
            next_url = request.form.get("next") or url_for("index")
            if not next_url.startswith("/"):
                next_url = url_for("index")
            return redirect(next_url)
        error = "Wrong username or password."
    return render_template("login.html", error=error, next=request.args.get("next", ""))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ===================== Pages =====================

@app.route("/")
def index():
    # An installed iOS PWA can hold onto a cached copy of this page more
    # stubbornly than a normal Safari tab -- Mario reported having to
    # delete and re-"Add to Home Screen" the app after every update just to
    # see the fix. Forcing the HTML shell itself to always be refetched
    # (never cached) means every relaunch always sees this deploy's markup,
    # which in turn references this deploy's exact static asset URLs (see
    # the ?v={{ app_version }} cache-busting on hosted-app.js below) --
    # between the two, a fresh deploy is always visible on next open, no
    # reinstall ever needed again.
    resp = make_response(render_template("index.html", app_version=APP_VERSION, app_version_date=SERVER_STARTED_DISPLAY, hosted=True))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/manifest.json")
def manifest():
    resp = app.send_static_file("manifest.json")
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@app.route("/sw.js")
def service_worker():
    # Service workers must be served with a JS mimetype and (conventionally)
    # from the root path, not /static/sw.js, so its default scope covers the
    # whole origin instead of just /static/. Never cache the service worker
    # file itself -- the browser's own update-detection for a service
    # worker relies on actually being able to refetch and diff it.
    resp = app.send_static_file("sw.js")
    resp.headers["Content-Type"] = "application/javascript"
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


# ===================== Web Push =====================

VAPID_PRIVATE_KEY_PEM = os.environ.get("VAPID_PRIVATE_KEY_PEM", "")
VAPID_PUBLIC_KEY_B64 = os.environ.get("VAPID_PUBLIC_KEY_B64", "")

# os.environ.get(key, default) only falls back to `default` when the key is
# completely absent -- an env var that EXISTS but is set to an empty string
# (e.g. left blank in Render's Blueprint setup wizard) still wins, silently
# producing "" here instead of the intended default. That's exactly what
# happened live: py_vapid's _check_sub() rejected the blank "sub" claim on
# every single push attempt with "Missing 'sub' from claims." Normalizing
# here (treat blank as unset, auto-add a missing "mailto:" prefix if Mario
# ever pastes just the bare address) makes this robust regardless of
# exactly what ends up in the env var.
_raw_claim_email = (os.environ.get("VAPID_CLAIM_EMAIL") or "").strip()
if _raw_claim_email and not _raw_claim_email.lower().startswith("mailto:"):
    _raw_claim_email = f"mailto:{_raw_claim_email}"
VAPID_CLAIM_EMAIL = _raw_claim_email or "mailto:mariobengocheare@gmail.com"


@app.route("/api/push/vapid_public_key")
def push_vapid_public_key():
    return jsonify({"public_key": VAPID_PUBLIC_KEY_B64})


@app.route("/api/push/subscribe", methods=["POST"])
def push_subscribe():
    data = request.get_json(force=True)
    endpoint = data.get("endpoint")
    keys = data.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return jsonify({"error": "Invalid subscription"}), 400
    crm.save_push_subscription(endpoint, keys["p256dh"], keys["auth"])
    return jsonify({"ok": True})


@app.route("/api/push/unsubscribe", methods=["POST"])
def push_unsubscribe():
    endpoint = (request.get_json(force=True) or {}).get("endpoint")
    if endpoint:
        crm.remove_push_subscription(endpoint)
    return jsonify({"ok": True})


def _send_push_to_all(title: str, body: str, url: str = "/"):
    if not VAPID_PRIVATE_KEY_PEM:
        return
    for sub in crm.list_push_subscriptions():
        subscription_info = {
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=json.dumps({"title": title, "body": body, "url": url}),
                vapid_private_key=VAPID_PRIVATE_KEY_PEM,
                vapid_claims={"sub": VAPID_CLAIM_EMAIL},
                # pywebpush sets no timeout by default -- an unresponsive
                # push endpoint could otherwise hang the underlying request
                # indefinitely, tying up whatever thread called this.
                timeout=15,
            )
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                # The browser/OS says this subscription is gone for good
                # (uninstalled, expired) -- stop trying to push to it.
                crm.remove_push_subscription(sub["endpoint"])
            else:
                # Anything else (e.g. a 400 from a VAPID key mismatch, a
                # malformed subscription) used to be silently swallowed
                # here with zero trace anywhere -- "sent to N devices"
                # from push_test() only ever meant "N subscriptions exist
                # in the DB," never "delivery actually succeeded," so a
                # real failure here looked identical to success from
                # Mario's side. Printed so it shows up in Render's own
                # Logs tab -- the only way to actually diagnose this
                # without guessing.
                body_preview = getattr(e.response, "text", "")[:300] if e.response is not None else ""
                print(f"[push] WebPushException (status={status}) for {sub['endpoint'][:70]}: {e} | {body_preview}")
        except requests.exceptions.RequestException as e:
            # pywebpush's webpush() doesn't wrap the underlying requests
            # call in its own try/except, so a timeout or connection error
            # (a genuinely unresponsive push endpoint, even with the
            # timeout= above) surfaces as a raw requests exception, not a
            # WebPushException -- catch it here too so one bad/slow
            # subscription can't abort delivery to every other one in this
            # loop, and never propagates out of this function uncaught.
            print(f"[push] RequestException for {sub['endpoint'][:70]}: {e}")
        except ValueError as e:
            # py_vapid's Vapid.from_string() raises a bare ValueError (not
            # a WebPushException) if VAPID_PRIVATE_KEY_PEM isn't in the
            # exact raw base64url-encoded-DER format it expects -- this is
            # exactly what happened live (Mario had a full PEM block with
            # -----BEGIN/END----- headers pasted into the Render env var
            # instead of just the raw key). Previously this crashed the
            # background thread with an unhandled traceback on every
            # subscription. Logged the same way as the other failure
            # modes above rather than left to crash silently.
            print(f"[push] ValueError (likely a malformed VAPID_PRIVATE_KEY_PEM) for {sub['endpoint'][:70]}: {e}")


@app.route("/api/push/test", methods=["POST"])
def push_test():
    # _send_push_to_all() makes a real synchronous outbound HTTPS call
    # (webpush(), no timeout set) to the push service per subscription --
    # calling it inline here meant a slow/stuck push delivery could stall
    # this whole request long enough to look like "Couldn't reach URTO's
    # server" from the tapped button, the same class of bug fixed for
    # on_data_changed() in build order #83. The subscriber count is known
    # synchronously and cheaply from the DB, so respond with that
    # immediately and let the actual push delivery happen in the
    # background, not on the request Mario is waiting on.
    if not VAPID_PRIVATE_KEY_PEM:
        # _send_push_to_all() itself just silently no-ops when this isn't
        # set -- reporting "sent to N devices" based purely on subscriber
        # count regardless would have claimed success while literally zero
        # push attempts happened, which is exactly indistinguishable from
        # a real delivery failure from Mario's side.
        return jsonify({"ok": False, "sent_to": 0, "error": "Push isn't configured on the server (missing VAPID keys)."})
    sent_to = len(crm.list_push_subscriptions())
    threading.Thread(
        target=lambda: _send_push_to_all("URTO", "Test notification — if you see this, it's working."),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "sent_to": sent_to})


def _format_time_12h(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    period = "PM" if h >= 12 else "AM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {period}" if m else f"{h12} {period}"


def send_morning_digest():
    # Only automatic recurring client follow-ups count toward "follow-ups"
    # -- a manual calendar item (a showing, a personal reminder, anything
    # Mario adds by hand) is a genuinely different thing and gets counted
    # separately here, plus its own dedicated reminders (see
    # _check_event_reminders below) rather than being folded into this
    # count, which used to conflate the two.
    todays = crm.get_today_followups()
    followup_count = len([i for i in todays if i["kind"] == "auto"])
    manual_items = [i for i in todays if i["kind"] == "manual"]

    if followup_count == 0:
        body = "No follow-ups today."
    elif followup_count == 1:
        body = "1 follow-up today."
    else:
        body = f"{followup_count} follow-ups today."
    if manual_items:
        body += f" {len(manual_items)} other item{'s' if len(manual_items) != 1 else ''} scheduled."
    # ?briefing=1 is the signal templates/index.html looks for on load to
    # speak the full voice briefing (see build order #75) -- only the
    # morning digest push should trigger that, not every other push (e.g.
    # the "Test" button) or a normal app open.
    _send_push_to_all("URTO — Today's Follow-ups", body, url="/?briefing=1")


# ===================== Per-event reminders (1 hour / 15 min before) =====================
# Mario's explicit ask: an automatic follow-up NEVER gets its own push (a
# busy 50-follow-up day would be 50 notifications) -- those only ever
# contribute to the single morning digest's count above. Anything he adds
# by hand to the calendar with a specific time (a showing, a closing, a
# personal reminder -- doesn't matter what he calls it) gets two dedicated
# reminders of its own: one an hour before, one 15 minutes before. An item
# with NO time only ever shows up in the morning digest's count, nothing
# more (there's no specific moment to remind him of).
_sent_event_reminders = set()  # {(date_str, event_id, minutes_before)}


def _check_event_reminders():
    now = datetime.now(ZoneInfo("America/New_York"))
    today_str = now.date().isoformat()

    # Drop anything not from today so this set never grows unbounded across
    # however long the server process stays up between deploys.
    for key in list(_sent_event_reminders):
        if key[0] != today_str:
            _sent_event_reminders.discard(key)

    for item in crm.get_today_followups():
        if item["kind"] != "manual" or not item.get("time") or not item.get("event_id"):
            continue
        try:
            event_time = datetime.strptime(item["time"], "%H:%M").time()
        except ValueError:
            continue
        event_dt = datetime.combine(now.date(), event_time, tzinfo=ZoneInfo("America/New_York"))
        for minutes_before, phrase in ((60, "in 1 hour"), (15, "in 15 minutes")):
            key = (today_str, item["event_id"], minutes_before)
            trigger_dt = event_dt - timedelta(minutes=minutes_before)
            # Fire once the trigger moment has passed, within a grace
            # window -- an exact-minute match (the original approach) can
            # silently miss the window entirely: this watcher only ticks
            # every 60 real seconds, unsynced to clock boundaries, so a
            # trigger minute that falls between two ticks (e.g. an event
            # created only a few minutes out, where T-15 is mere seconds
            # away) would never get checked at exactly the right minute.
            # Capped at 3 minutes late so a long server outage doesn't
            # dump a flood of stale reminders once it's back up.
            if key not in _sent_event_reminders and trigger_dt <= now <= trigger_dt + timedelta(minutes=3):
                _sent_event_reminders.add(key)
                body = f"{item['title']} at {_format_time_12h(item['time'])} ({phrase})"
                _send_push_to_all("URTO — Upcoming", body, url="/")


def _event_reminder_watcher():
    while True:
        time.sleep(60)
        try:
            _check_event_reminders()
        except Exception:
            pass  # never let a push failure kill the watcher thread


threading.Thread(target=_event_reminder_watcher, daemon=True).start()


_last_alert_date = {"date": None}


@app.route("/api/notifications/settings", methods=["GET"])
def notification_settings_get():
    return jsonify({"time": crm.get_notification_time()})


@app.route("/api/notifications/settings", methods=["POST"])
def notification_settings_set():
    value = (request.get_json(force=True) or {}).get("time", "")
    try:
        crm.set_notification_time(value)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"time": crm.get_notification_time()})


@app.route("/api/notifications/briefing_text")
def notification_briefing_text():
    return jsonify({"text": crm.get_morning_briefing_text()})


# ===================== Unattended "talking alarm" (iOS Shortcuts) =====================
# The morning push notification can only ever open the app and speak once
# Mario actually taps it -- iOS will not run page JS in the background from a
# push, full stop (see build order #75/CLAUDE.md). The closest thing to a
# real hands-free alarm is an iOS Shortcuts "Time of Day" Personal
# Automation that fetches this endpoint and speaks the result with no tap
# needed -- but Shortcuts can't do the normal session-cookie login flow, so
# this one endpoint is reachable with a long random token instead of a
# session (see crm.get_briefing_token()). It only ever returns the one
# spoken sentence -- nothing else in the CRM is reachable this way.

@app.route("/api/notifications/briefing_token")
def notification_briefing_token():
    return jsonify({"token": crm.get_briefing_token()})


@app.route("/api/notifications/briefing_token/regenerate", methods=["POST"])
def notification_briefing_token_regenerate():
    return jsonify({"token": crm.regenerate_briefing_token()})


@app.route("/api/notifications/briefing_public")
def notification_briefing_public():
    if not secrets.compare_digest(request.args.get("token", ""), crm.get_briefing_token()):
        return jsonify({"error": "Invalid or missing token"}), 403
    # Shortcuts' "Get Dictionary Value" step is configured against this
    # exact "text" key on Mario's real automation (see build order #76) --
    # keep the response shape stable.
    return jsonify({"text": crm.get_morning_briefing_text()})


def _morning_alert_watcher():
    # Reads crm.get_notification_time() fresh every tick (rather than a
    # fixed constant read once at startup) so Mario changing the time
    # in-app takes effect immediately, no redeploy needed -- this used to
    # be a MORNING_ALERT_HOUR env var, which meant every time change was a
    # code-adjacent chore instead of a setting he could just click.
    while True:
        time.sleep(60)
        now = datetime.now(ZoneInfo("America/New_York"))
        target = crm.get_notification_time()
        if now.strftime("%H:%M") == target and _last_alert_date["date"] != now.date():
            _last_alert_date["date"] = now.date()
            try:
                send_morning_digest()
            except Exception:
                pass  # never let a push failure kill the watcher thread


threading.Thread(target=_morning_alert_watcher, daemon=True).start()


# ===================== One-time data import =====================
# Brings Mario's real existing CRM data over from a backup he already has
# (the full JSON export crm.py already produces on every save, see
# _write_full_data_export in crm.py) -- reusing that existing export instead
# of asking him to re-enter everything by hand into the fresh hosted DB.

@app.route("/api/admin/import_backup_json", methods=["POST"])
def admin_import_backup_json():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400
    try:
        data = json.loads(file.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return jsonify({"error": "That doesn't look like a valid URTO_Full_Data_Export.json file."}), 400
    result = crm.import_full_data_export(data)
    return jsonify(result)


# ===================== Real Google Drive backup (OAuth) =====================
# Render has no filesystem access to a locally-synced Google Drive folder
# (that's a desktop-only trick -- Google Drive for Desktop). This is the
# real Google Drive API instead, so a change made from the phone backs up
# to Mario's actual Google Drive even when his desktop PC is off. One-time
# consent flow: visiting /admin/google_auth (a normal link, reachable only
# while logged in like everything else) sends Mario to Google's own
# consent screen; Google redirects back to the callback below with a code,
# which gets exchanged for a refresh token and stored (crm.py's
# app_settings, same pattern as the desktop-only google_drive_dir setting)
# -- from then on, every backup silently also pushes to Google Drive with
# no further action from Mario. See CLAUDE.md for the Google Cloud setup
# steps this required Mario to do once on his end.

def _google_auth_redirect_uri():
    # Must exactly match the "Authorized redirect URI" configured on the
    # Google Cloud OAuth client, including scheme -- request.url_root
    # already reflects Render's own https:// front door correctly.
    return url_for("google_auth_callback", _external=True)


@app.route("/admin/google_auth")
def google_auth_start():
    if not google_drive_api.is_configured():
        return "Google Drive API isn't configured on this server yet (missing GOOGLE_OAUTH_CLIENT_ID/SECRET).", 503
    state = secrets.token_urlsafe(16)
    session["google_auth_state"] = state
    return redirect(google_drive_api.build_auth_url(_google_auth_redirect_uri(), state))


@app.route("/admin/google_auth/callback")
def google_auth_callback():
    error = request.args.get("error")
    if error:
        return f"Google Drive connection was cancelled ({error}). <a href='/'>Back to URTO</a>", 400
    if request.args.get("state") != session.pop("google_auth_state", None):
        return "That authorization link expired or was already used. Try connecting again from URTO CRM.", 400
    code = request.args.get("code")
    if not code:
        return "Missing authorization code from Google.", 400
    try:
        tokens = google_drive_api.exchange_code(_google_auth_redirect_uri(), code)
    except Exception as e:
        return f"Couldn't finish connecting to Google Drive: {e}", 502
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        # Google only hands back a refresh_token on the FIRST consent for a
        # given account+scope -- if Mario had previously authorized this
        # exact app without revoking it, a re-auth can come back without
        # one. prompt=consent (see build_auth_url) is meant to prevent
        # this, but if it still happens the fix is revoking URTO's access
        # at https://myaccount.google.com/permissions and trying again.
        return (
            "Google didn't return a long-lived connection this time. If you've connected URTO to "
            "Google Drive before, remove its access at "
            "<a href='https://myaccount.google.com/permissions' target='_blank'>myaccount.google.com/permissions</a> "
            "and then try connecting again.",
            502,
        )
    crm.save_google_oauth_refresh_token(refresh_token)
    crm.save_google_oauth_scope(tokens.get("scope", ""))
    return redirect(url_for("index"))


# ===================== Microsoft Teams meetings (OAuth) =====================
# A completely separate connection from Google's above -- Microsoft and
# Google don't share credentials or consent screens. Mirrors the Google
# flow exactly (see microsoft_graph_api.py for why). Needed for URTO's
# Virtual Meeting feature (build order #89): creating an event with
# isOnlineMeeting=true gets a real Teams join link back from Mario's own
# Outlook calendar.

def _microsoft_auth_redirect_uri():
    return url_for("microsoft_auth_callback", _external=True)


@app.route("/admin/microsoft_auth")
def microsoft_auth_start():
    if not microsoft_graph_api.is_configured():
        return "Microsoft Teams isn't configured on this server yet (missing MICROSOFT_OAUTH_CLIENT_ID/SECRET).", 503
    state = secrets.token_urlsafe(16)
    session["microsoft_auth_state"] = state
    return redirect(microsoft_graph_api.build_auth_url(_microsoft_auth_redirect_uri(), state))


@app.route("/admin/microsoft_auth/callback")
def microsoft_auth_callback():
    error = request.args.get("error")
    if error:
        return f"Microsoft connection was cancelled ({error}). <a href='/'>Back to URTO</a>", 400
    if request.args.get("state") != session.pop("microsoft_auth_state", None):
        return "That authorization link expired or was already used. Try connecting again from URTO CRM.", 400
    code = request.args.get("code")
    if not code:
        return "Missing authorization code from Microsoft.", 400
    try:
        tokens = microsoft_graph_api.exchange_code(_microsoft_auth_redirect_uri(), code)
    except Exception as e:
        return f"Couldn't finish connecting to Microsoft: {e}", 502
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return (
            "Microsoft didn't return a long-lived connection this time. If you've connected URTO to "
            "Microsoft before, remove its access at "
            "<a href='https://myaccount.microsoft.com/security-info' target='_blank'>myaccount.microsoft.com</a> "
            "and then try connecting again.",
            502,
        )
    crm.save_microsoft_oauth_refresh_token(refresh_token)
    crm.save_microsoft_oauth_scope(tokens.get("scope", ""))
    return redirect(url_for("index"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
