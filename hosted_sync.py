"""
Desktop-side half of "one cloud" — makes the desktop app read/write the
SAME hosted database as the phone (hosted_app.py on Render), instead of its
own local urto_crm.db, for every CRM/Dialer/Transactions/Calendar route.

Mario asked for this explicitly ("It should be like if its one cloud,
mandatory") after already choosing "one source of truth" earlier — rather
than syncing two separate databases (real distributed-sync engineering,
with all the conflict/drift risk that implies for very little benefit to a
solo user), this makes the desktop app a thin proxy: every /api/crm,
/api/backup, /api/txn, /api/text_presets request the desktop frontend makes
is forwarded verbatim to the hosted server and the response relayed back.
There is only ever ONE database, on Render — desktop just becomes another
client of it, the same way the phone already is. This is why it's called
"mandatory": there's no local fallback copy of the data to silently drift
out of sync with the real one.

Local urto_crm.db still exists and is still used for the ONE thing that's
genuinely desktop-only and can't run on a server: the weekly Owner Lookup
counter (Skip Trace needs a real headed Chromium window). Everything else
CRM-shaped goes over the network to Render from here on.
"""

import json
import mimetypes

import requests

import crm

CONFIG_PATH = crm.DATA_DIR / "hosted_sync.json"

# The set of URL prefixes that get forwarded to the hosted server rather
# than handled locally -- must match exactly what crm_routes.py's crm_bp
# exposes (every route it defines lives under one of these four prefixes).
PROXY_PREFIXES = ("/api/crm", "/api/backup", "/api/txn", "/api/text_presets")

_session = requests.Session()
_logged_in_url = None  # the base_url _session currently holds a valid cookie for


def load_config():
    """Returns {"base_url", "username", "password"} or None if never set up."""
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text())
        if data.get("base_url") and data.get("username") and data.get("password"):
            return data
    except (OSError, ValueError):
        pass
    return None


def save_config(base_url: str, username: str, password: str):
    base_url = base_url.strip().rstrip("/")
    crm.DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"base_url": base_url, "username": username, "password": password}))
    global _logged_in_url
    _logged_in_url = None  # force a fresh login on the new config


def is_configured() -> bool:
    return load_config() is not None


class HostedUnreachableError(Exception):
    pass


def _ensure_logged_in(config: dict):
    global _logged_in_url
    base_url = config["base_url"]
    if _logged_in_url == base_url:
        return
    try:
        res = _session.post(
            f"{base_url}/login",
            data={"username": config["username"], "password": config["password"]},
            timeout=15,
            allow_redirects=False,
        )
    except requests.RequestException:
        raise HostedUnreachableError("Can't reach URTO Cloud right now — check your internet connection.")
    # A successful login redirects (302) to the app; a wrong password
    # re-renders the login page (200) with an error instead.
    if res.status_code not in (302, 303):
        raise HostedUnreachableError(
            "Couldn't log into the hosted server with the saved username/password. "
            "Check them under Sync Settings."
        )
    _logged_in_url = base_url


def test_login(base_url: str, username: str, password: str) -> str:
    """Used by the setup form to validate credentials before saving them.
    Returns an error string, or None on success."""
    base_url = base_url.strip().rstrip("/")
    try:
        res = requests.post(
            f"{base_url}/login",
            data={"username": username, "password": password},
            timeout=15,
            allow_redirects=False,
        )
    except requests.RequestException:
        return f"Couldn't reach {base_url} — double-check the address (it should start with https://) and that the hosted server is running."
    if res.status_code not in (302, 303):
        return "Wrong username or password."
    return None


def proxy_request(flask_request, subpath_with_prefix: str):
    """Forwards one incoming Flask request to the hosted server under the
    same path, and returns (body_bytes, status_code, headers_dict) ready to
    hand back to the desktop browser as-is -- including file downloads
    (signed documents, templates) and file uploads (vCard/CSV imports,
    document uploads), so the desktop frontend needs zero changes; it's
    still just talking to relative /api/... URLs, same as always."""
    config = load_config()
    if not config:
        return (
            json.dumps({"error": "Not connected to URTO Cloud yet. Go to Sync Settings to connect."}).encode(),
            503,
            {"Content-Type": "application/json"},
        )

    try:
        _ensure_logged_in(config)
        url = config["base_url"] + subpath_with_prefix
        kwargs = {"timeout": 30, "allow_redirects": False}
        if flask_request.query_string:
            url += "?" + flask_request.query_string.decode()

        if flask_request.files:
            files = {
                name: (f.filename, f.stream, f.mimetype or mimetypes.guess_type(f.filename)[0])
                for name, f in flask_request.files.items()
            }
            kwargs["files"] = files
            kwargs["data"] = {k: v for k, v in flask_request.form.items()}
        elif flask_request.data:
            kwargs["data"] = flask_request.data
            if flask_request.content_type:
                kwargs["headers"] = {"Content-Type": flask_request.content_type}

        res = _session.request(flask_request.method, url, **kwargs)

        # The session cookie may have expired server-side (long idle
        # period) -- a stale-session redirect to /login looks like this.
        # Re-login once and retry the exact same request before giving up.
        if res.status_code in (302, 303) and "/login" in res.headers.get("Location", ""):
            global _logged_in_url
            _logged_in_url = None
            _ensure_logged_in(config)
            res = _session.request(flask_request.method, url, **kwargs)

    except HostedUnreachableError as e:
        return json.dumps({"error": str(e)}).encode(), 502, {"Content-Type": "application/json"}
    except requests.RequestException:
        return (
            json.dumps({"error": "Can't reach URTO Cloud right now — check your internet connection."}).encode(),
            502,
            {"Content-Type": "application/json"},
        )

    out_headers = {}
    for h in ("Content-Type", "Content-Disposition", "Content-Length"):
        if h in res.headers:
            out_headers[h] = res.headers[h]
    return res.content, res.status_code, out_headers
