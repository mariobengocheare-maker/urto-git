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
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request, Response
from pywebpush import WebPushException, webpush

import crm
from crm_routes import crm_bp

app = Flask(__name__)
app.register_blueprint(crm_bp)

APP_VERSION = "1.0.0-hosted"

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
# (unlike the desktop app, which only ever listens on 127.0.0.1) -- every
# route requires HTTP Basic Auth against HOSTED_USERNAME/HOSTED_PASSWORD
# (set as Render environment variables, never committed to the repo).
# Safari (including an installed PWA) prompts once and remembers it for the
# session/Keychain, so this is a one-time bit of friction per device.
HOSTED_USERNAME = os.environ.get("HOSTED_USERNAME", "")
HOSTED_PASSWORD = os.environ.get("HOSTED_PASSWORD", "")


def _check_auth(auth) -> bool:
    if not HOSTED_USERNAME or not HOSTED_PASSWORD:
        # Fail closed: if the env vars weren't set, refuse everything rather
        # than accidentally serving real client data with no password at all.
        return False
    return auth is not None and auth.username == HOSTED_USERNAME and auth.password == HOSTED_PASSWORD


@app.before_request
def _require_auth():
    if not _check_auth(request.authorization):
        return Response(
            "Authentication required.", 401,
            {"WWW-Authenticate": 'Basic realm="URTO"'},
        )


# ===================== Pages =====================

@app.route("/")
def index():
    return render_template("index.html", app_version=APP_VERSION, app_version_date=SERVER_STARTED_DISPLAY, hosted=True)


@app.route("/manifest.json")
def manifest():
    return app.send_static_file("manifest.json")


@app.route("/sw.js")
def service_worker():
    # Service workers must be served with a JS mimetype and (conventionally)
    # from the root path, not /static/sw.js, so its default scope covers the
    # whole origin instead of just /static/.
    resp = app.send_static_file("sw.js")
    resp.headers["Content-Type"] = "application/javascript"
    return resp


# ===================== Web Push =====================

VAPID_PRIVATE_KEY_PEM = os.environ.get("VAPID_PRIVATE_KEY_PEM", "")
VAPID_PUBLIC_KEY_B64 = os.environ.get("VAPID_PUBLIC_KEY_B64", "")
VAPID_CLAIM_EMAIL = os.environ.get("VAPID_CLAIM_EMAIL", "mailto:mariobengochea@gmail.com")


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
            )
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                # The browser/OS says this subscription is gone for good
                # (uninstalled, expired) -- stop trying to push to it.
                crm.remove_push_subscription(sub["endpoint"])


@app.route("/api/push/test", methods=["POST"])
def push_test():
    _send_push_to_all("URTO", "Test notification — if you see this, it's working.")
    return jsonify({"ok": True, "sent_to": len(crm.list_push_subscriptions())})


def send_morning_digest():
    todays = crm.get_today_followups()
    count = len(todays)
    if count == 0:
        body = "No follow-ups today."
    elif count == 1:
        body = "1 follow-up today."
    else:
        body = f"{count} follow-ups today."
    _send_push_to_all("URTO — Today's Follow-ups", body, url="/")


MORNING_ALERT_HOUR = int(os.environ.get("MORNING_ALERT_HOUR", "7"))  # 7am Miami time by default
_last_alert_date = {"date": None}


def _morning_alert_watcher():
    while True:
        time.sleep(60)
        now = datetime.now(ZoneInfo("America/New_York"))
        if now.hour == MORNING_ALERT_HOUR and _last_alert_date["date"] != now.date():
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
