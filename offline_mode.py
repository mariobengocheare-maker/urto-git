"""
Offline mode for the desktop app (build order #185).

The desktop app is a client of the one real database on Render (see
hosted_sync.py). Mario asked for URTO to keep working when the internet is
down, so the desktop keeps a COPY of that database on the PC:

- While online, a background thread downloads a fresh copy every few
  minutes (and shortly after any change), only when something changed.
- When Render can't be reached, every CRM/Dialer/Calendar/Transactions
  request is answered from that copy instead, by the exact same route code
  the server runs (crm.py + crm_routes.py, loaded a second time and pointed
  at the copy).
- Changes made while offline are applied to the copy (so the screen shows
  them) and saved in an outbox. When the internet comes back, the outbox is
  sent to Render in order, then a fresh copy is downloaded.

Things that need the internet are refused offline with a clear message
(file uploads/downloads, imports, restoring backups, virtual meetings,
route optimizing). Records created offline get a temporary id on the PC;
when they're sent, the server's real id replaces it in any later queued
change that refers to them. Anything created indirectly offline (e.g. the
commission entry a closing creates) can't be changed until the internet is
back, since the server will create its own copy with a different id.
"""

import importlib.util
import json
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

import requests
from flask import Flask

import crm
import hosted_sync

OFFLINE_DIR = crm.DATA_DIR / "offline"
REPLICA_PATH = OFFLINE_DIR / "urto_crm.db"
META_PATH = OFFLINE_DIR / "copy_info.json"
OUTBOX_PATH = OFFLINE_DIR / "outbox.json"

REFRESH_EVERY = 180       # seconds between "anything new?" checks while online
PROBE_EVERY = 10          # seconds between "is the internet back?" checks while offline
CHANGE_REFRESH_DELAY = 8  # seconds after a change before pulling a fresh copy

_PROJECT_DIR = Path(__file__).resolve().parent

# Path segment -> table, for ids that appear in URLs like /api/crm/clients/12.
SEG_TABLE = {
    "clients": "clients", "members": "clients", "contact_lists": "contact_lists",
    "events": "events", "cre_units": "cre_units", "showing_days": "showing_days",
    "stops": "showing_stops", "showing_stops": "showing_stops",
    "commission_entries": "commission_entries", "waterfall_items": "waterfall_items",
    "pieces": "chess_pieces", "transactions": "transactions",
    "document_types": "document_types", "notes": "notes", "text_presets": "text_presets",
    "transaction_documents": "transaction_documents", "skiptrace_results": "skiptrace_results",
    "dialer_calls": "dialer_calls", "sequences": "text_sequences", "sends": "text_sequence_sends",
}
# Body keys that carry ids.
BODY_KEY_TABLE = {
    "client_id": "clients", "client_ids": "clients", "list_id": "contact_lists",
    "list_ids": "contact_lists", "transaction_id": "transactions", "ordered_ids": "document_types",
}

_OFFLINE_MSG = "This needs an internet connection. URTO is offline right now — try again once you're back online."

# Writes refused while offline: (method or "*", regex on the path).
_BLOCKED_WRITES = [
    ("*", r"^/api/backup/"),
    ("*", r"^/api/crm/contact_lists/import_vcard$"),
    ("*", r"^/api/crm/import_recurring_csv"),
    ("*", r"^/api/crm/duplicates/merge$"),
    ("*", r"^/api/crm/map/place$"),
    ("*", r"^/api/txn/transaction_documents/"),
    ("*", r"/template$"),
    ("*", r"/file$"),
    ("POST", r"^/api/crm/showing_days/\d+/stops$"),
    ("POST", r"^/api/crm/showing_days/\d+/optimize$"),
    ("*", r"^/api/crm/skiptrace_results$"),
]
# Reads that can't work from the copy (they serve uploaded files).
_BLOCKED_READS = [r"/template$", r"/signed$", r"/file$", r"^/api/backup/snapshots$"]

_db_lock = threading.RLock()     # guards the copy, the outbox and the offline app
_state_lock = threading.Lock()
_offline = False                 # True once Render couldn't be reached
_offline_since = None
_replaying = False
_last_refresh_try = 0.0
_change_at = 0.0                 # last time an online change went through
_wake = threading.Event()

_offline_app = None
_crm_off = None
_needs_init = True

_remote = requests.Session()
_remote_url = None
_remote_lock = threading.Lock()


class _Offline(Exception):
    pass


# ---------------------------------------------------------------- state

def is_offline() -> bool:
    return _offline


def has_copy() -> bool:
    return REPLICA_PATH.exists()


def _read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    OFFLINE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def _load_outbox() -> dict:
    box = _read_json(OUTBOX_PATH, {})
    box.setdefault("entries", [])
    box.setdefault("created", {})    # table -> [local ids created offline]
    box.setdefault("remap", {})      # table -> {local id: server id}
    box.setdefault("failed", {})     # table -> [local ids whose create was refused]
    box.setdefault("problems", [])
    return box


def pending_count() -> int:
    return len(_load_outbox()["entries"])


def mark_offline():
    global _offline, _offline_since
    with _state_lock:
        if not _offline:
            _offline = True
            _offline_since = time.time()
    _wake.set()


def note_online_change():
    """An online write just went through -- pull a fresh copy soon so the
    PC's copy includes it if the internet drops right after."""
    global _change_at
    _change_at = time.time()
    _wake.set()


def should_serve_offline() -> bool:
    """Offline, or there are offline changes still waiting to be sent (new
    requests must queue behind them so they reach Render in order)."""
    return _offline or _replaying or (OUTBOX_PATH.exists() and pending_count() > 0)


def status() -> dict:
    meta = _read_json(META_PATH, {})
    box = _load_outbox()
    return {
        "offline": _offline,
        "syncing": _replaying,
        "pending": len(box["entries"]),
        "has_copy": has_copy(),
        "copy_saved_at": meta.get("saved_at"),
        "problems": box["problems"][-5:],
    }


def clear_problems():
    with _db_lock:
        box = _load_outbox()
        box["problems"] = []
        _write_json(OUTBOX_PATH, box)


# ---------------------------------------------------------------- the offline app

def _load_offline_app():
    """Loads crm.py and crm_routes.py a second time, under different names,
    with the crm copy pointed at the offline database. Nothing here talks
    to the network: backups and every Google/Drive push are switched off."""
    global _offline_app, _crm_off
    if _offline_app is not None:
        return
    spec = importlib.util.spec_from_file_location("crm_offline", _PROJECT_DIR / "crm.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["crm_offline"] = mod
    spec.loader.exec_module(mod)
    mod.DATA_DIR = OFFLINE_DIR
    mod.DB_PATH = REPLICA_PATH
    mod.DOCUMENTS_DIR = OFFLINE_DIR / "documents"
    mod.TEMPLATES_DIR = mod.DOCUMENTS_DIR / "templates"
    mod.SIGNED_DIR = mod.DOCUMENTS_DIR / "signed"
    mod.COMMISSION_FILES_DIR = mod.DOCUMENTS_DIR / "commission_files"
    mod._DRIVE_STATE_PATH = OFFLINE_DIR / "drive_push_state.json"
    mod.disable_cloud_mirrors()
    mod._write_backup = lambda *a, **k: {}
    mod._instant_document_mirror = lambda *a, **k: None
    mod._push_instant_documents_to_google_drive_safe = lambda *a, **k: None
    mod._google_drive_api_push = lambda *a, **k: False
    mod.start_backup_in_background = lambda *a, **k: False

    real_crm = sys.modules["crm"]
    sys.modules["crm"] = mod
    try:
        rspec = importlib.util.spec_from_file_location("crm_routes_offline", _PROJECT_DIR / "crm_routes.py")
        routes = importlib.util.module_from_spec(rspec)
        sys.modules["crm_routes_offline"] = routes
        rspec.loader.exec_module(routes)
    finally:
        sys.modules["crm"] = real_crm

    app = Flask("urto_offline")
    app.register_blueprint(routes.crm_bp)
    _crm_off = mod
    _offline_app = app


def _ensure_ready():
    global _needs_init
    _load_offline_app()
    if _needs_init:
        _crm_off.init_db()
        _needs_init = False


# ---------------------------------------------------------------- id helpers

def _path_refs(path: str):
    """(table, id) pairs found in a path like /api/crm/clients/12/notes/3."""
    segs = path.strip("/").split("/")
    refs = []
    for i in range(1, len(segs)):
        if segs[i].isdigit() and segs[i - 1] in SEG_TABLE:
            refs.append((SEG_TABLE[segs[i - 1]], int(segs[i])))
    return refs


def _as_ints(value):
    vals = value if isinstance(value, list) else [value]
    out = []
    for v in vals:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            pass
    return out


def _body_refs(body):
    refs = []
    if isinstance(body, dict):
        for key, table in BODY_KEY_TABLE.items():
            if key in body and body[key] not in (None, ""):
                refs.extend((table, i) for i in _as_ints(body[key]))
    return refs


def _create_table(method: str, path: str):
    """The table a request creates a row in, or None (e.g. POST
    /api/crm/clients -> clients, POST /api/crm/clients/5/notes -> notes)."""
    if method != "POST":
        return None
    last = path.rstrip("/").split("/")[-1]
    if last in ("clients", "contact_lists", "events", "cre_units", "showing_days", "stops",
                "commission_entries", "waterfall_items", "pieces", "transactions",
                "document_types", "notes", "text_presets", "dialer_calls",
                "sequences", "sends"):
        return SEG_TABLE[last]
    return None


def _baseline() -> dict:
    return _read_json(META_PATH, {}).get("baseline", {})


def _is_locally_new(table, ident, baseline) -> bool:
    return table in baseline and ident > baseline[table]


# ---------------------------------------------------------------- serving

def _json_response(obj, status):
    return json.dumps(obj).encode(), status, {"Content-Type": "application/json", "X-URTO-Offline": "1"}


def _special_read(path: str):
    """Reads answered directly instead of from the copy."""
    if path == "/api/backup/status":
        meta = _read_json(META_PATH, {})
        return {"offline": True, "copy_saved_at": meta.get("saved_at"), "pending": pending_count()}
    if path == "/api/backup/health":
        return {"ok": True, "offline": True}
    if path == "/api/crm/meeting_providers/status":
        return {"google_meet_available": False, "google_meet_connected": False,
                "teams_available": False, "teams_connected": False, "offline": True}
    if path == "/api/crm/showing_days/ors_status":
        return {"configured": True, "offline": True}
    return None


def _request_body(flask_request):
    """(kind, body) for queuing: json dict, form pairs, or raw text."""
    if flask_request.form:
        return "form", list(flask_request.form.items(multi=True))
    raw = flask_request.get_data(cache=True)
    if not raw:
        return "none", None
    try:
        return "json", json.loads(raw)
    except ValueError:
        return "raw", raw.decode("utf-8", "replace")


def serve(flask_request, path: str):
    """Answers one CRM request from the PC's copy. Returns (body, status, headers)."""
    method = flask_request.method
    if method == "GET":
        special = _special_read(path)
        if special is not None:
            return _json_response(special, 200)
        if any(re.search(p, path) for p in _BLOCKED_READS):
            return _json_response({"error": "Opening files needs an internet connection. URTO is offline right now."}, 503)
    else:
        if path == "/api/crm/map/geocode_batch":
            return _json_response({"processed": 0, "remaining": 0, "offline": True}, 200)
        if flask_request.files or any(
            (m == "*" or m == method) and re.search(p, path) for m, p in _BLOCKED_WRITES
        ):
            return _json_response({"error": _OFFLINE_MSG}, 503)

    if not has_copy():
        return _json_response({"error": "URTO can't reach the internet, and this PC doesn't have a saved copy of your data yet. "
                                        "It saves one automatically the next time you're online."}, 503)

    kind, body = _request_body(flask_request) if method != "GET" else ("none", None)
    if kind == "json" and isinstance(body, dict) and body.get("meeting_type") == "virtual":
        return _json_response({"error": "Virtual meetings (Google Meet / Teams) need an internet connection. "
                                        "Save it as a physical meeting for now, or try again once you're online."}, 503)

    with _db_lock:
        box = _load_outbox() if method != "GET" else None
        if method != "GET":
            baseline = _baseline()
            created = {t: set(ids) for t, ids in box["created"].items()}
            body_dict = dict(body) if kind == "form" else body
            for table, ident in _path_refs(path) + _body_refs(body_dict):
                if _is_locally_new(table, ident, baseline) and ident not in created.get(table, set()):
                    return _json_response({"error": "This was created automatically while offline and can be changed once "
                                                    "URTO is back online."}, 409)

        _ensure_ready()
        client = _offline_app.test_client()
        # As text: given raw bytes, the test client reads them as a list of
        # pairs and every offline GET with a ?query crashed.
        kwargs = {"method": method, "query_string": flask_request.query_string.decode("utf-8", "replace")}
        if kind == "form":
            kwargs["data"] = dict(body)
        elif kind in ("json", "raw"):
            kwargs["data"] = flask_request.get_data(cache=True)
            kwargs["content_type"] = flask_request.content_type or "application/json"
        resp = client.open(path, **kwargs)

        if method != "GET" and resp.status_code < 400:
            entry = {"method": method, "path": path,
                     "query": flask_request.query_string.decode(),
                     "kind": kind, "body": body,
                     "content_type": flask_request.content_type, "at": time.time()}
            table = _create_table(method, path)
            if table:
                try:
                    new_id = int((resp.get_json(silent=True) or {}).get("id"))
                    entry["created"] = [table, new_id]
                    box["created"].setdefault(table, []).append(new_id)
                except (TypeError, ValueError):
                    pass
            box["entries"].append(entry)
            _write_json(OUTBOX_PATH, box)

    headers = {"X-URTO-Offline": "1"}
    for h in ("Content-Type", "Content-Disposition"):
        if h in resp.headers:
            headers[h] = resp.headers[h]
    return resp.data, resp.status_code, headers


# ---------------------------------------------------------------- talking to Render

def _remote_request(method, path, **kwargs):
    """A request to Render on this module's own session (so syncing never
    holds up what the user is doing). Raises _Offline when unreachable."""
    global _remote_url
    config = hosted_sync.load_config()
    if not config:
        raise _Offline()
    base = config["base_url"]
    kwargs.setdefault("timeout", (5, 60))
    kwargs.setdefault("allow_redirects", False)
    with _remote_lock:
        try:
            for attempt in range(2):
                if _remote_url != base:
                    r = _remote.post(f"{base}/login", data={"username": config["username"],
                                     "password": config["password"]}, timeout=(5, 20), allow_redirects=False)
                    if r.status_code not in (302, 303):
                        raise _Offline()  # wrong password or server trouble: try again later
                    _remote_url = base
                res = _remote.request(method, base + path, **kwargs)
                if res.status_code in (302, 303) and "/login" in res.headers.get("Location", "") and attempt == 0:
                    _remote_url = None
                    continue
                if res.status_code in (502, 503, 504):
                    raise _Offline()  # Render redeploying or down
                return res
        except requests.RequestException:
            raise _Offline()
    raise _Offline()


def _rewrite(entry, remap, failed):
    """Swaps ids of records created offline for the server's real ids.
    Returns (path, body) or None if it refers to something that was never
    saved on the server."""
    def fix(table, ident):
        ids = remap.get(table, {})
        if str(ident) in ids:
            return ids[str(ident)]
        if ident in failed.get(table, []):
            raise KeyError
        return ident

    try:
        segs = entry["path"].strip("/").split("/")
        for i in range(1, len(segs)):
            if segs[i].isdigit() and segs[i - 1] in SEG_TABLE:
                segs[i] = str(fix(SEG_TABLE[segs[i - 1]], int(segs[i])))
        path = "/" + "/".join(segs)

        body = entry["body"]
        if entry["kind"] == "json" and isinstance(body, dict):
            body = dict(body)
            for key, table in BODY_KEY_TABLE.items():
                if key in body and body[key] not in (None, ""):
                    if isinstance(body[key], list):
                        body[key] = [fix(table, i) for i in _as_ints(body[key])]
                    else:
                        vals = _as_ints(body[key])
                        if vals:
                            body[key] = fix(table, vals[0])
        elif entry["kind"] == "form" and body:
            new = []
            for k, v in body:
                if k in BODY_KEY_TABLE and str(v).isdigit():
                    v = str(fix(BODY_KEY_TABLE[k], int(v)))
                new.append([k, v])
            body = new
        return path, body
    except KeyError:
        return None


def _describe(entry) -> str:
    return f"{entry['method']} {entry['path']}"


def _send_outbox():
    """Sends queued offline changes to Render in order. Returns True when
    the outbox is empty afterward."""
    global _replaying
    _replaying = True
    try:
        while True:
            with _db_lock:
                box = _load_outbox()
                if not box["entries"]:
                    return True
                entry = box["entries"][0]
                rewritten = _rewrite(entry, box["remap"], box["failed"])
            problem = None
            server_id = None
            if rewritten is None:
                problem = "it depended on something else created offline that couldn't be saved"
            else:
                path, body = rewritten
                url = path + (f"?{entry['query']}" if entry.get("query") else "")
                kwargs = {}
                if entry["kind"] == "json":
                    kwargs["json"] = body
                elif entry["kind"] == "form":
                    kwargs["data"] = [(k, v) for k, v in body]
                elif entry["kind"] == "raw":
                    kwargs["data"] = body.encode()
                    if entry.get("content_type"):
                        kwargs["headers"] = {"Content-Type": entry["content_type"]}
                res = _remote_request(entry["method"], url, **kwargs)  # _Offline stops here
                if res.status_code >= 400:
                    try:
                        msg = res.json().get("error")
                    except ValueError:
                        msg = None
                    problem = msg or f"the server answered {res.status_code}"
                elif entry.get("created"):
                    try:
                        server_id = int(res.json().get("id"))
                    except (ValueError, TypeError, AttributeError):
                        server_id = None
            with _db_lock:
                box = _load_outbox()
                if not box["entries"] or box["entries"][0].get("at") != entry.get("at"):
                    continue
                box["entries"].pop(0)
                if entry.get("created"):
                    table, local_id = entry["created"]
                    if server_id is not None:
                        box["remap"].setdefault(table, {})[str(local_id)] = server_id
                    else:
                        box["failed"].setdefault(table, []).append(local_id)
                if problem:
                    box["problems"].append({"at": time.time(), "what": _describe(entry), "why": problem})
                    box["problems"] = box["problems"][-20:]
                if not box["entries"]:
                    box["created"], box["remap"], box["failed"] = {}, {}, {}
                _write_json(OUTBOX_PATH, box)
    finally:
        _replaying = False


def _table_max_ids(path: Path) -> dict:
    out = {}
    conn = sqlite3.connect(path)
    try:
        for table in set(SEG_TABLE.values()):
            try:
                out[table] = conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                pass
        conn.execute("SELECT COUNT(*) FROM clients").fetchone()
    finally:
        conn.close()
    return out


def refresh_copy(force=False) -> bool:
    """Downloads a fresh copy if Render has changes. Never replaces the
    copy while offline changes are still waiting to be sent."""
    global _last_refresh_try, _needs_init
    _last_refresh_try = time.time()
    if pending_count() > 0:
        return False
    meta = _read_json(META_PATH, {})
    headers = {}
    if meta.get("etag") and has_copy() and not force:
        headers["If-None-Match"] = meta["etag"]
    res = _remote_request("GET", "/api/backup/offline_copy", headers=headers, timeout=(5, 120))
    if res.status_code == 304:
        return True
    if res.status_code != 200 or not res.content:
        return False
    OFFLINE_DIR.mkdir(parents=True, exist_ok=True)
    download = OFFLINE_DIR / "urto_crm.download.db"
    download.write_bytes(res.content)
    try:
        baseline = _table_max_ids(download)
    except sqlite3.Error:
        download.unlink(missing_ok=True)
        return False
    with _db_lock:
        if pending_count() > 0:
            download.unlink(missing_ok=True)
            return False
        try:
            download.replace(REPLICA_PATH)
        except OSError:
            return False  # file busy (Windows); try again next round
        _needs_init = True
        _write_json(META_PATH, {"etag": res.headers.get("ETag"), "saved_at": crm._now().isoformat(timespec="seconds"),
                                "baseline": baseline})
    return True


def _go_online():
    global _offline, _offline_since
    with _state_lock:
        _offline = False
        _offline_since = None


def _sync_loop():
    while True:
        _wake.wait(PROBE_EVERY)
        _wake.clear()
        try:
            if not hosted_sync.is_configured():
                continue
            if _offline or pending_count() > 0:
                if _send_outbox():
                    refresh_copy(force=True)
                    _go_online()
                continue
            now = time.time()
            due = now - _last_refresh_try >= REFRESH_EVERY
            changed = _change_at > _last_refresh_try and now - _change_at >= CHANGE_REFRESH_DELAY
            if due or changed or not has_copy():
                refresh_copy()
        except _Offline:
            mark_offline()
        except Exception:
            pass


_started = False


def start():
    global _started
    if _started:
        return
    _started = True
    OFFLINE_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_sync_loop, daemon=True).start()
    _wake.set()
