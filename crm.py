"""
Local CRM data layer for URTO. SQLite-backed, single file on disk
(urto_crm.db), never synced anywhere — this module owns all client and
follow-up data and schedule math.
"""

import calendar
import csv
import json
import os
import re
import shutil
import string
import sqlite3
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from werkzeug.utils import secure_filename

# Keep this in sync with urto_updater.pyw's REPO_ZIP_URL — same repo/branch,
# just also surfaced as a plain link inside the backups themselves so the
# code is recoverable even starting from nothing but a Google Drive/OneDrive
# folder (e.g. after losing the PC entirely).
GITHUB_REPO_URL = "https://github.com/mariobengocheare-maker/urto-git"
GITHUB_ZIP_URL = "https://codeload.github.com/mariobengocheare-maker/urto-git/zip/refs/heads/claude/context-window-dgen3k"

_PROJECT_DIR = Path(__file__).parent


def _resolve_data_dir() -> Path:
    """Where urto_crm.db and uploaded documents actually live. Deliberately
    kept OUTSIDE the project folder (same fix already used by the sibling
    Wardrobe app's db.py) — every update (the URTO Updater OR a manual
    ZIP-and-replace) installs into/over the project folder, so real client
    data living inside it was never actually safe from an update wiping it
    out. `URTO_DATA_DIR`, when set, wins over everything else — this is how
    the hosted deployment (hosted_app.py, see CLAUDE.md "Hosted CRM/Dialer")
    points storage at Render's persistent disk instead of guessing from
    LOCALAPPDATA, which doesn't exist on a Linux host. Falls back to living
    alongside this file when neither is set (e.g. this sandbox)."""
    explicit = os.environ.get("URTO_DATA_DIR")
    if explicit:
        return Path(explicit)
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "URTO"
    return _PROJECT_DIR


DATA_DIR = _resolve_data_dir()
# sqlite3.connect() does NOT create missing parent directories -- on desktop
# this was always masked by %LOCALAPPDATA%\URTO already existing from a
# prior run, but a genuinely fresh disk (a brand-new Render persistent disk,
# or a truly first-ever desktop install) has no such folder yet, and
# get_conn() would fail outright with "unable to open database file" before
# _migrate_legacy_data_dir() ever runs (it only creates DATA_DIR when an old
# legacy DB is found to migrate, which isn't the case here). Ensure it
# unconditionally up front instead.
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "urto_crm.db"

# Transaction Manager's uploaded document files (blank templates + signed
# copies) — gitignored, same reasoning as urto_crm.db: real files, never
# committed. Mirrored into the backup dir alongside the DB (see below).
DOCUMENTS_DIR = DATA_DIR / "documents"
TEMPLATES_DIR = DOCUMENTS_DIR / "templates"
SIGNED_DIR = DOCUMENTS_DIR / "signed"


def _migrate_legacy_data_dir():
    """One-time move of an existing urto_crm.db/documents folder from their
    OLD location (inside the project folder, before this fix) into the new
    stable DATA_DIR — only runs when DATA_DIR actually changed (LOCALAPPDATA
    present) and the new location doesn't already have a DB. This is exactly
    the bug that reset Mario's weekly lookup counter: the DB lived inside
    the folder an update replaces, so nothing carried it forward. Safe to
    run on every startup — it's a no-op once the DB already exists at the
    new location."""
    if DATA_DIR == _PROJECT_DIR or DB_PATH.exists():
        return
    legacy_db = _PROJECT_DIR / "urto_crm.db"
    if not legacy_db.exists():
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(legacy_db, DB_PATH)
    legacy_docs = _PROJECT_DIR / "documents"
    if legacy_docs.exists() and not DOCUMENTS_DIR.exists():
        shutil.copytree(legacy_docs, DOCUMENTS_DIR)


_migrate_legacy_data_dir()

# ---- Automatic backup ----
# urto_crm.db lives only on this PC and is gitignored on purpose (it holds
# real client data). If Windows' OneDrive sync is present (the standard
# OneDrive env var it sets for every signed-in install), back up there so a
# dead/stolen PC doesn't mean losing every client — otherwise fall back to a
# local backups/ folder, which still protects against file corruption.
#
# Two things live in the backup folder:
#   * urto_crm_latest.db — a *live mirror* rewritten the instant anything
#     changes (client saved, note added, contact deleted). This is always the
#     current state, so a deletion is reflected on OneDrive immediately.
#   * urto_crm_YYYYMMDD_HHMMSS.db — periodic timestamped snapshots kept for
#     point-in-time history (startup, the "Backup Now" button, and a slow
#     watcher), capped so they don't accumulate forever.
LATEST_NAME = "urto_crm_latest.db"
_backup_lock = threading.Lock()
_dirty = False
_last_backup = {"at": None, "destinations": None}


def _detect_google_drive_dir():
    """Best-effort detection of Google Drive for Desktop's local **Mirror**
    folder — NOT the default Stream/virtual-drive mode, which isn't a real
    writable local path the same way OneDrive's synced folder is. Google
    Drive doesn't set an env var the way OneDrive does, so this just checks
    the common default locations; a manual override set via
    set_google_drive_dir() (surfaced in the CRM's backup status bar) always
    wins over this guess."""
    candidates = [Path.home() / "Google Drive", Path.home() / "My Drive"]
    for letter in string.ascii_uppercase:
        candidates.append(Path(f"{letter}:/My Drive"))
    for c in candidates:
        try:
            if c.exists() and c.is_dir():
                return c
        except OSError:
            continue
    return None


def get_google_drive_dir():
    """A saved manual path (app_settings) takes priority over auto-detection
    — Mario can point this at wherever his Google Drive folder actually is
    if auto-detect guesses wrong, without touching a console."""
    conn = get_conn()
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'google_drive_dir'").fetchone()
    conn.close()
    if row and row["value"]:
        p = Path(row["value"])
        if p.exists() and p.is_dir():
            return p
    return _detect_google_drive_dir()


def set_google_drive_dir(path_str: str):
    p = Path(path_str)
    if not p.exists() or not p.is_dir():
        raise ValueError(f"'{path_str}' isn't a folder that exists on this PC")
    conn = get_conn()
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('google_drive_dir', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(p),),
    )
    conn.commit()
    conn.close()


def resolve_backup_dirs() -> list:
    """Every place a backup actually gets written. Always includes the
    local backups/ folder as a guaranteed baseline (never depends on either
    cloud being present), plus OneDrive and/or Google Drive whenever
    detected/configured — Mario wants both clouds simultaneously, not
    either/or, so this is a list rather than the single-destination design
    the original OneDrive-only backup used. Returns [{"label", "path"}, ...]."""
    dests = []
    onedrive = os.environ.get("OneDriveConsumer") or os.environ.get("OneDrive")
    if onedrive:
        p = Path(onedrive) / "URTO Backups"
        p.mkdir(parents=True, exist_ok=True)
        dests.append({"label": "OneDrive", "path": p})
    gdrive = get_google_drive_dir()
    if gdrive:
        p = gdrive / "URTO Backups"
        p.mkdir(parents=True, exist_ok=True)
        dests.append({"label": "Google Drive", "path": p})
    # "Local" normally lives next to the code (_PROJECT_DIR) -- fine on
    # desktop, where the URTO Updater explicitly protects backups/ from
    # being wiped by an update (see its NEVER_TOUCH set). On the hosted
    # deployment there IS no updater and no protected folder: Render
    # rebuilds the whole code checkout from git on every deploy, so
    # anything written next to hosted_app.py/crm.py is gone on the next
    # push. `URTO_DATA_DIR` being explicitly set only ever happens for the
    # hosted deployment (see _resolve_data_dir()) -- when it's set, put
    # "Local" on the same persistent disk as the live DB instead, so
    # snapshots actually survive a redeploy. Desktop's LOCALAPPDATA-based
    # DATA_DIR is untouched by this check and keeps its existing behavior
    # exactly as before.
    local_base = DATA_DIR if os.environ.get("URTO_DATA_DIR") else _PROJECT_DIR
    local = local_base / "backups"
    local.mkdir(parents=True, exist_ok=True)
    dests.append({"label": "Local", "path": local})
    return dests


def mark_dirty():
    global _dirty
    _dirty = True


def _copy_if_newer(src: Path, dest: Path, expected: set):
    """Shared copy step for the human-organized document mirror below —
    tracks every dest path actually expected to exist so leftover files
    (renamed/removed documents) can be cleaned up afterward."""
    expected.add(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not dest.exists() or dest.stat().st_mtime < src.stat().st_mtime:
            shutil.copy2(src, dest)
    except OSError:
        pass


def _mirror_documents_folder(backup_dir: Path):
    """Mirrors Transaction Manager's uploaded files into TWO human-browsable
    folders (real names, not the internal stored/uuid filenames) rather than
    a flat raw copy of DOCUMENTS_DIR:

      * 'Transaction Checklist Documents/<Type>/<Document Name><ext>' — the
        shared blank template for each document on a type's checklist.
      * 'Transactions/<Transaction Title> (id)/<Document Name> - Template
        <ext>' and '... - Signed<ext>' — one folder per actual transaction
        (across every folder: active/closings/archive), using that
        transaction's own snapshotted document names.

    Full diff each run: copies new/changed files AND deletes anything in
    the backup no longer expected (deleted document, renamed transaction,
    removed signed upload, transaction itself deleted), same principle as
    the CRM's instant-mirror deletions elsewhere."""
    checklist_root = backup_dir / "Transaction Checklist Documents"
    txns_root = backup_dir / "Transactions"
    expected = set()

    for txn_type, label in TRANSACTION_TYPES:
        type_dir = checklist_root / _safe_name(label, txn_type)
        for dt in list_document_types(txn_type):
            if not dt.get("template_filename"):
                continue
            src = TEMPLATES_DIR / dt["template_filename"]
            if not src.exists():
                continue
            ext = Path(dt.get("template_original_name") or dt["template_filename"]).suffix
            dest = type_dir / f"{_safe_name(dt['name'], 'Document')}{ext}"
            _copy_if_newer(src, dest, expected)

    for folder in TRANSACTION_FOLDERS:
        for txn in list_transactions(folder):
            txn_dir = txns_root / f"{_safe_name(txn['title'], 'Transaction')} ({txn['id']})"
            for doc in txn.get("documents") or []:
                doc_name = _safe_name(doc.get("name") or "Document", "Document")
                # Template: only the shared type-level file, if the doc's
                # link to a document_type still exists (ON DELETE SET NULL
                # when the type-level doc is removed — see build order #22).
                dt_id = doc.get("document_type_id")
                if dt_id:
                    dt = get_document_type(dt_id)
                    if dt and dt.get("template_filename"):
                        src = TEMPLATES_DIR / dt["template_filename"]
                        if src.exists():
                            ext = Path(dt.get("template_original_name") or dt["template_filename"]).suffix
                            _copy_if_newer(src, txn_dir / f"{doc_name} - Template{ext}", expected)
                if doc.get("signed_filename"):
                    src = SIGNED_DIR / doc["signed_filename"]
                    if src.exists():
                        ext = Path(doc.get("signed_original_name") or doc["signed_filename"]).suffix
                        _copy_if_newer(src, txn_dir / f"{doc_name} - Signed{ext}", expected)

    for root in (checklist_root, txns_root):
        if not root.exists():
            continue
        for existing in list(root.rglob("*")):
            if existing.is_file() and existing not in expected:
                existing.unlink(missing_ok=True)
        for existing in sorted([p for p in root.rglob("*") if p.is_dir()], key=lambda p: -len(str(p))):
            try:
                existing.rmdir()  # only removes it if now empty
            except OSError:
                pass


def _safe_name(name: str, fallback: str) -> str:
    """Filesystem-safe version of a human name for a backup file/folder —
    strips characters Windows rejects but keeps spaces/apostrophes/parens so
    the result still reads naturally when Mario browses the backup folder."""
    cleaned = re.sub(r'[<>:"/\\|?*]', "", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or fallback


def _write_crm_contacts_export(backup_dir: Path):
    """One human-readable .txt file per client under 'CRM Contacts/' — so
    Mario's contacts are actually browsable/readable straight from OneDrive
    or Google Drive, not just locked inside the raw urto_crm.db file. Cheap
    (small text files), so this runs on the INSTANT mirror path, not just
    the periodic snapshot — deletion propagates the same way as everywhere
    else: anything not in the current client list gets removed."""
    dest_root = backup_dir / "CRM Contacts"
    dest_root.mkdir(parents=True, exist_ok=True)

    clients = list_clients()
    expected = set()
    for c in clients:
        fname = f"{_safe_name(c['name'], 'Unnamed Client')} ({c['id']}).txt"
        expected.add(fname)
        lines = [
            c["name"] or "(no name)",
            f"Phone: {c.get('phone') or '—'}",
            f"Address: {c.get('address') or '—'}",
            f"Contact info: {c.get('contact_info') or '—'}",
            f"Client since: {c.get('created_at') or '—'}",
            "",
        ]
        if c.get("frequency_label"):
            lines.append(f"Individual follow-up: {c['frequency_label']} — next: {c.get('next_followup_date') or '—'}")
        for lf in c.get("list_followups") or []:
            lines.append(f"Contact List follow-up ({lf['name']}): next {lf['next_call_date']}")
        if not c.get("frequency_label") and not c.get("list_followups"):
            lines.append("No automatic follow-up.")
        lines.append("")
        lines.append("--- Notes ---")
        notes = list_notes(c["id"])
        if notes:
            for n in notes:
                lines.append(f"[{n.get('created_at', '')}] {n.get('text', '')}")
        else:
            lines.append("(no notes yet)")
        try:
            (dest_root / fname).write_text("\n".join(lines), encoding="utf-8")
        except OSError:
            pass

    for existing in dest_root.glob("*.txt"):
        if existing.name not in expected:
            existing.unlink(missing_ok=True)


def _write_full_data_export(backup_dir: Path):
    """Single JSON file with EVERY piece of URTO data — clients, notes,
    contact lists + membership, calendar events, transactions + their
    documents, document checklists, and the weekly lookup count. Meant to
    be pasted straight into a fresh Claude conversation to describe the
    entire current state of Mario's CRM (e.g. to rebuild/restore it, or
    just as a complete point-in-time reference) — cheap to regenerate
    (a handful of SELECTs + json.dumps), so it runs on every single instant
    mirror, not just the periodic snapshot."""
    conn = get_conn()
    export = {
        "_note": (
            "Full data export of URTO (Mario Bengochea's real-estate CRM/tool). "
            "This file is regenerated automatically every time any data changes. "
            "It describes every client, note, follow-up, contact list, calendar "
            "event, and transaction currently in the app."
        ),
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "clients": [dict(r) for r in conn.execute("SELECT * FROM clients ORDER BY id").fetchall()],
        "notes": [dict(r) for r in conn.execute("SELECT * FROM notes ORDER BY id").fetchall()],
        "call_log": [dict(r) for r in conn.execute("SELECT * FROM call_log ORDER BY id").fetchall()],
        "contact_lists": [dict(r) for r in conn.execute("SELECT * FROM contact_lists ORDER BY id").fetchall()],
        "contact_list_members": [dict(r) for r in conn.execute("SELECT * FROM contact_list_members").fetchall()],
        "events": [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id").fetchall()],
        "document_types": [dict(r) for r in conn.execute("SELECT * FROM document_types ORDER BY id").fetchall()],
        "transactions": [dict(r) for r in conn.execute("SELECT * FROM transactions ORDER BY id").fetchall()],
        "transaction_documents": [
            dict(r) for r in conn.execute("SELECT * FROM transaction_documents ORDER BY id").fetchall()
        ],
        "lookup_stats": [dict(r) for r in conn.execute("SELECT * FROM lookup_stats ORDER BY week_start").fetchall()],
        "text_presets": [dict(r) for r in conn.execute("SELECT * FROM text_presets ORDER BY sort_order, id").fetchall()],
    }
    conn.close()
    try:
        (backup_dir / "URTO_Full_Data_Export.json").write_text(
            json.dumps(export, indent=2, default=str), encoding="utf-8"
        )
    except OSError:
        pass


def _insert_export_rows(conn, table: str, rows: list) -> int:
    """Inserts rows exactly as exported, using whichever columns are
    actually present in each row dict (the export is literally `dict(r)`
    off a `SELECT *`, so the keys always match the real column names —
    safer than hand-typing a column list here that could silently drift
    out of sync with the schema)."""
    if not rows:
        return 0
    columns = list(rows[0].keys())
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    for row in rows:
        conn.execute(sql, [row.get(c) for c in columns])
    return len(rows)


def import_full_data_export(data: dict) -> dict:
    """One-time bulk import of the URTO_Full_Data_Export.json backup format
    (see _write_full_data_export above) into a fresh database — used by the
    hosted deployment (hosted_app.py) to bring Mario's real existing CRM
    data over instead of starting empty. Preserves every original row id
    exactly, since notes/events/contact_list_members/transaction_documents
    all reference each other by id — remapping ids would mean rewriting
    every cross-reference in the export. Refuses to run at all if the
    database already has any clients, so this can only ever seed a
    genuinely empty database, never silently merge into or duplicate real
    data that's already there. Insert order respects every foreign key
    (clients before notes/events, contact_lists before its members,
    document_types/transactions before transaction_documents)."""
    conn = get_conn()
    existing = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    if existing > 0:
        conn.close()
        return {
            "imported": False,
            "error": "This database already has clients — import only runs on an empty database, to avoid id collisions or duplicating data.",
        }

    table_order = [
        "clients", "notes", "call_log", "contact_lists", "contact_list_members",
        "events", "document_types", "transactions", "transaction_documents",
        "lookup_stats", "text_presets",
    ]
    counts = {}
    try:
        # init_db() auto-seeds document_types with the four default
        # Transaction Manager checklists (see DEFAULT_DOCUMENT_TYPES) even on
        # a database with zero clients, so a "genuinely fresh" DB still has
        # rows here whose ids collide with the exported ones. This import is
        # a full takeover of a fresh instance, so the seeded placeholders are
        # exactly what's meant to be replaced by Mario's real (possibly
        # customized) checklist from the export.
        conn.execute("DELETE FROM document_types")
        for table in table_order:
            counts[table] = _insert_export_rows(conn, table, data.get(table) or [])
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return {"imported": False, "error": str(e)}
    conn.close()
    on_data_changed("full data import")
    return {"imported": True, "counts": counts}


def _write_code_download_link(backup_dir: Path):
    """A plain-text pointer to the app's own code, sitting right next to the
    data backups — so recovering from losing this PC entirely is genuinely
    two steps (GitHub for the code, this backup folder for the data), not
    'go find the GitHub link from somewhere else.' Static content, so this
    is nearly free to rewrite on every instant mirror."""
    text = (
        "URTO — code recovery\n"
        "=====================\n\n"
        "This folder has your DATA (client/CRM database, documents, this "
        "text file). The APP ITSELF (the code) lives on GitHub, not here:\n\n"
        f"Download link (ZIP): {GITHUB_ZIP_URL}\n"
        f"Repository page: {GITHUB_REPO_URL}\n\n"
        "If you ever lose this PC or need a fresh install:\n"
        "  1. Download the ZIP link above and extract it — that's the app.\n"
        "  2. Copy urto_crm.db and the documents/ folder from this backup "
        "folder into the new install's data location (URTO migrates it "
        "into place automatically the first time it runs).\n"
        "  3. Re-run 'Create URTO Updater Desktop Icon.vbs' once so future "
        "updates go back to being one click.\n"
    )
    try:
        (backup_dir / "CODE_DOWNLOAD_LINK.txt").write_text(text, encoding="utf-8")
    except OSError:
        pass


def _instant_document_mirror():
    """Mirrors the Transaction Manager document folders to every backup
    destination right away — called from the specific document-mutation
    functions (upload/remove a template or signed file, create/delete a
    transaction) rather than from the general on_data_changed() instant
    path, so routine note/follow-up saves don't pay for real file I/O on
    every single edit — only actual document changes do."""
    for dest_info in resolve_backup_dirs():
        try:
            _mirror_documents_folder(dest_info["path"])
        except Exception:
            pass


def _write_backup(reason, snapshot: bool, keep=30) -> dict:
    """Copy the live DB to every backup destination (OneDrive/Google
    Drive/Local — see resolve_backup_dirs()). Always refreshes the live
    mirror, the human-readable CRM Contacts export, and the full JSON data
    export (all cheap); when snapshot=True also writes a timestamped DB
    copy, prunes old ones, and mirrors the Transaction Manager documents
    folders (kept off the high-frequency instant-mirror path since it can
    involve real file I/O — see _instant_document_mirror() for the
    document-specific exception). One destination failing (e.g. a cloud
    folder briefly locked) never blocks the others."""
    global _dirty, _last_backup
    with _backup_lock:
        if not DB_PATH.exists():
            return _last_backup
        results = []
        for dest_info in resolve_backup_dirs():
            backup_dir = dest_info["path"]
            try:
                mirror = backup_dir / LATEST_NAME
                shutil.copy2(DB_PATH, mirror)
                dest_path = mirror

                if snapshot:
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    dest_path = backup_dir / f"urto_crm_{stamp}.db"
                    shutil.copy2(DB_PATH, dest_path)
                    snaps = sorted(backup_dir.glob("urto_crm_[0-9]*.db"))
                    for old in snaps[:-keep]:
                        old.unlink(missing_ok=True)

                try:
                    _write_crm_contacts_export(backup_dir)
                except Exception:
                    pass
                try:
                    _write_full_data_export(backup_dir)
                except Exception:
                    pass
                try:
                    _write_code_download_link(backup_dir)
                except Exception:
                    pass
                if snapshot:
                    try:
                        _mirror_documents_folder(backup_dir)
                    except Exception:
                        pass  # never let a documents-folder hiccup break the DB backup

                results.append({"label": dest_info["label"], "path": str(dest_path)})
            except Exception:
                continue

        _dirty = False
        _last_backup = {"at": datetime.now().isoformat(timespec="seconds"), "destinations": results, "reason": reason}
        return _last_backup


def backup_now(reason="manual", keep=30) -> dict:
    """Full backup: refresh the live mirror and write a timestamped snapshot."""
    return _write_backup(reason, snapshot=True, keep=keep)


def backup_instant(reason="save") -> dict:
    """Instant backup after a data change: refresh the live mirror only, so the
    current state (including deletions) is on every backup destination
    immediately without churning through the capped snapshot history on
    every single edit."""
    return _write_backup(reason, snapshot=False)


class _deferred_backup:
    """Context manager for a loop that calls create_client/update_client/
    add_note many times in one request (a bulk import) — each of those
    normally calls on_data_changed() itself, which does a FULL instant
    mirror (whole-DB copy + a re-written CRM Contacts .txt per existing
    client + the full JSON export, to every backup destination). Doing
    that once per row in a 50+ row import means dozens of full mirror
    passes in a single request, which on a real OneDrive/Google Drive
    synced folder (or just a CRM with a few hundred existing clients) is
    slow enough to look hung. Inside this block, on_data_changed() is
    swapped for the cheap mark_dirty()-only version; the real one-time
    mirror runs once when the block exits."""
    def __enter__(self):
        global on_data_changed
        self._real = on_data_changed
        on_data_changed = lambda reason="save": mark_dirty()
        return self

    def __exit__(self, *exc):
        global on_data_changed
        on_data_changed = self._real
        on_data_changed("bulk import")
        return False


def on_data_changed(reason="save"):
    """Called after any CRM mutation: mirror everywhere right away, and flag
    the DB so the slow watcher also lays down a timestamped history snapshot."""
    mark_dirty()
    try:
        backup_instant(reason)
    except Exception:
        # A backup failure (e.g. a cloud folder briefly locked) must never
        # break the actual save — the dirty flag means the watcher retries.
        pass


def backup_if_dirty():
    if _dirty:
        backup_now("auto")


def get_backup_status() -> dict:
    onedrive = os.environ.get("OneDriveConsumer") or os.environ.get("OneDrive")
    gdrive = get_google_drive_dir()
    return {
        "destinations": [{"label": d["label"], "path": str(d["path"])} for d in resolve_backup_dirs()],
        "onedrive_detected": bool(onedrive),
        "google_drive_detected": bool(gdrive),
        "google_drive_dir": str(gdrive) if gdrive else None,
        "last_backup_at": _last_backup["at"],
        "last_backup_destinations": _last_backup.get("destinations"),
    }


def _count_clients_in_db(db_path: Path):
    """Read-only client count from an arbitrary sqlite file — used to judge
    backup snapshots without ever risking a write to them. Returns None if
    the file doesn't exist or isn't a valid URTO database (a corrupt/partial
    copy, or a version too old to have the clients table)."""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute("SELECT COUNT(*) AS c FROM clients").fetchone()
        conn.close()
        return row[0]
    except Exception:
        return None


def list_available_snapshots() -> list:
    """Every DB backup file (the always-current live mirror + every
    timestamped snapshot) across every backup destination, newest first,
    each annotated with how many clients it actually contains. Powers both
    check_data_health() below and the in-app 'Restore from backup' tool —
    Mario should never need a console or a manual file copy to recover from
    a bad state."""
    results = []
    for dest in resolve_backup_dirs():
        backup_dir = dest["path"]
        if not backup_dir.exists():
            continue
        candidates = []
        latest = backup_dir / LATEST_NAME
        if latest.exists():
            candidates.append(latest)
        candidates.extend(sorted(backup_dir.glob("urto_crm_[0-9]*.db"), reverse=True))
        for f in candidates:
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")
            except OSError:
                mtime = None
            results.append({
                "destination": dest["label"],
                "filename": f.name,
                "path": str(f),
                "modified_at": mtime,
                "client_count": _count_clients_in_db(f),
            })
    results.sort(key=lambda r: r["modified_at"] or "", reverse=True)
    return results


def check_data_health() -> dict:
    """Compares the LIVE database's client count against the most recent
    known backup mirror (urto_crm_latest.db, whichever destination has the
    newest one) — under normal operation these two should always be in
    lockstep, since on_data_changed() rewrites the mirror the instant
    anything changes (client added/edited/deleted). If the live DB somehow
    has FEWER clients than that mirror, something bypassed the normal
    save-and-mirror path entirely — e.g. a stale or freshly-recreated
    database file got swapped into place (exactly what happened to Mario:
    contacts added while running one URTO folder never carried over when a
    different folder/location ended up being the one actually running
    later). A genuine, intentional deletion by Mario is invisible to this
    check, since the mirror would already have been updated to match at the
    same moment the deletion happened."""
    current_count = _count_clients_in_db(DB_PATH) or 0
    mirrors = [
        s for s in list_available_snapshots()
        if s["filename"] == LATEST_NAME and s["client_count"] is not None
    ]
    if not mirrors:
        return {"ok": True, "current_count": current_count, "best_known_count": current_count}
    best = max(s["client_count"] for s in mirrors)
    return {"ok": current_count >= best, "current_count": current_count, "best_known_count": best}


def restore_from_snapshot(snapshot_path: str) -> dict:
    """Restores the live database from a chosen backup file — self-service
    recovery, no console, no manual file copying (per the standing 'no
    console' rule). Only ever restores from a path list_available_snapshots()
    itself returned, never an arbitrary path. Takes a fresh safety snapshot
    of whatever's currently live FIRST, so restoring the wrong one is itself
    trivially recoverable (it just becomes one more entry in the list)."""
    known_paths = {s["path"] for s in list_available_snapshots()}
    if snapshot_path not in known_paths:
        raise ValueError("That's not a recognized backup file.")
    src = Path(snapshot_path)
    if not src.exists() or not src.is_file():
        raise ValueError("That backup file no longer exists.")
    # Read the chosen backup into memory BEFORE taking the safety snapshot —
    # if the chosen file IS urto_crm_latest.db, backup_now() below would
    # otherwise overwrite it with the current (possibly broken) live state
    # before we ever get a chance to copy from it.
    restore_bytes = src.read_bytes()
    backup_now("pre-restore safety snapshot")
    DB_PATH.write_bytes(restore_bytes)
    on_data_changed("restored from backup")
    return {"ok": True, "restored_from": str(src)}

FREQUENCY_PRESETS = {
    "2_weeks": ("2 Weeks", 14),
    "3_weeks": ("3 Weeks", 21),
    "4_weeks": ("4 Weeks", 28),
    "2_months": ("2 Months", 60),
    "6_months": ("6 Months", 180),
}


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            contact_info TEXT,
            address TEXT,
            created_at TEXT NOT NULL,
            frequency_label TEXT,
            interval_days INTEGER,
            next_followup_date TEXT,
            last_called_at TEXT
        )
    """)
    # Migration for a clients table from before last_called_at existed (see
    # build order #55) — same guarded-ALTER pattern as events.list_id above.
    clients_cols = {row["name"] for row in conn.execute("PRAGMA table_info(clients)").fetchall()}
    if "last_called_at" not in clients_cols:
        conn.execute("ALTER TABLE clients ADD COLUMN last_called_at TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS call_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            called_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
            list_id INTEGER REFERENCES contact_lists(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        )
    """)
    # Migration for an events table from before list_id existed (a manual
    # calendar event linked to a whole Contact List, not just one client —
    # see build order #54) — ALTER TABLE ADD COLUMN would error on a DB that
    # already has it, so only run it if the column is actually missing.
    events_cols = {row["name"] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
    if "list_id" not in events_cols:
        conn.execute("ALTER TABLE events ADD COLUMN list_id INTEGER REFERENCES contact_lists(id) ON DELETE CASCADE")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contact_lists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            frequency_label TEXT,
            interval_days INTEGER,
            next_call_date TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contact_list_members (
            list_id INTEGER NOT NULL REFERENCES contact_lists(id) ON DELETE CASCADE,
            client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            PRIMARY KEY (list_id, client_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lookup_stats (
            week_start TEXT PRIMARY KEY,
            count INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS document_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_type TEXT NOT NULL,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            template_filename TEXT,
            template_original_name TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_type TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            archived INTEGER NOT NULL DEFAULT 0,
            folder TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        )
    """)
    # Migrations for a urto_crm.db from before these columns existed — ALTER
    # TABLE ADD COLUMN would error on a DB that already has them, so only run
    # it if the column is actually missing.
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    if "archived" not in existing_cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
    if "folder" not in existing_cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN folder TEXT NOT NULL DEFAULT 'active'")
        # Preserve anything already moved to the old single "Closings" folder
        # (the `archived` flag) under the new three-way `folder` column.
        conn.execute("UPDATE transactions SET folder = 'closings' WHERE archived = 1")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transaction_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
            document_type_id INTEGER REFERENCES document_types(id) ON DELETE SET NULL,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            signed_filename TEXT,
            signed_original_name TEXT,
            signed_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # Web Push subscriptions -- only ever written/read by the hosted phone
    # app (hosted_app.py); the desktop app has no service worker and never
    # touches this table. Lives here anyway so it rides along with the same
    # single-file SQLite backup as everything else.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint TEXT NOT NULL UNIQUE,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS text_presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    _seed_document_types(conn)
    conn.close()


# Only the documents Mario named explicitly (Listing Agreement, Compensation
# Agreement, Escrow Letter) are seeded — everything else is left for him to
# add himself via the checklist manager, rather than guessing at his actual
# required paperwork for the other transaction types. Gated by a one-time
# app_settings marker (not "is the table empty"), so deliberately deleting
# every row down to zero doesn't cause them to reappear on the next restart.
DEFAULT_DOCUMENT_TYPES = {
    "listing": ["Listing Agreement", "Compensation Agreement", "Escrow Letter"],
}
_SEED_MARKER_KEY = "document_types_seeded"


def _seed_document_types(conn):
    already = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (_SEED_MARKER_KEY,)
    ).fetchone()
    if already:
        return
    now = datetime.now().isoformat(timespec="seconds")
    for txn_type, names in DEFAULT_DOCUMENT_TYPES.items():
        for i, name in enumerate(names):
            conn.execute(
                "INSERT INTO document_types (transaction_type, name, sort_order, created_at) VALUES (?, ?, ?, ?)",
                (txn_type, name, i, now),
            )
    conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)", (_SEED_MARKER_KEY, "1"))
    conn.commit()


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())  # date.weekday(): Monday == 0


def get_weekly_lookup_count() -> dict:
    """Current Monday-to-Sunday week's lookup count. Keyed by that Monday's
    date, so the count "resets" naturally when the week rolls over — no
    explicit reset needed, a new week just hasn't got a row yet."""
    week_start = _monday_of(date.today())
    week_end = week_start + timedelta(days=6)
    conn = get_conn()
    row = conn.execute("SELECT count FROM lookup_stats WHERE week_start = ?", (week_start.isoformat(),)).fetchone()
    conn.close()
    return {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "count": row["count"] if row else 0,
    }


def increment_weekly_lookup_count(n: int = 1):
    """Called once per actual FOREWARN search attempt (not for rows that
    were skipped before ever reaching FOREWARN) — see lookup_engine.py."""
    week_start = _monday_of(date.today()).isoformat()
    conn = get_conn()
    conn.execute(
        """INSERT INTO lookup_stats (week_start, count) VALUES (?, ?)
           ON CONFLICT(week_start) DO UPDATE SET count = count + excluded.count""",
        (week_start, n),
    )
    conn.commit()
    conn.close()
    on_data_changed("lookup counted")


def resolve_interval_days(frequency_key: str, custom_amount, custom_unit) -> tuple:
    """Returns (frequency_label, interval_days) or (None, None) for no follow-up."""
    if frequency_key == "none" or not frequency_key:
        return None, None
    if frequency_key in FREQUENCY_PRESETS:
        return FREQUENCY_PRESETS[frequency_key]
    if frequency_key == "custom":
        amount = int(custom_amount)
        unit = custom_unit or "days"
        days_per_unit = {"days": 1, "weeks": 7, "months": 30}.get(unit, 1)
        interval_days = amount * days_per_unit
        label = f"Every {amount} {unit}"
        return label, interval_days
    return None, None


class DuplicatePhoneError(Exception):
    """Raised when a client save would give two CRM clients the same phone
    number. A person can belong to multiple Contact Lists, but two separate
    CRM clients sharing one phone number is almost always the same person
    getting added twice — see Mario's explicit request."""
    def __init__(self, existing_client_name):
        self.existing_client_name = existing_client_name
        super().__init__(f"This phone number is already in your CRM, under {existing_client_name}.")


def _normalize_phone_key(phone: str) -> str:
    """Digits-only, US-country-code-stripped, so '(305) 555-1212',
    '305-555-1212', and '+13055551212' all compare equal. Returns '' for no
    phone at all — an empty phone never counts as a duplicate."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def find_client_by_phone(phone: str, exclude_id=None):
    """Returns the existing client dict whose phone matches (ignoring
    formatting), or None. Used both for duplicate-prevention on save and for
    merging a dial list into a Contact List without creating duplicate
    clients."""
    key = _normalize_phone_key(phone)
    if not key:
        return None
    conn = get_conn()
    rows = conn.execute("SELECT * FROM clients").fetchall()
    conn.close()
    for r in rows:
        if exclude_id is not None and r["id"] == exclude_id:
            continue
        if _normalize_phone_key(r["phone"]) == key:
            return client_to_dict(r)
    return None


def client_to_dict(row) -> dict:
    d = dict(row)
    today = date.today()
    if d.get("next_followup_date"):
        due = date.fromisoformat(d["next_followup_date"])
        d["followup_status"] = "overdue" if due < today else ("due_today" if due == today else "upcoming")
    else:
        d["followup_status"] = None
    return d


def create_client(name, phone, contact_info, address, frequency_key, custom_amount, custom_unit, list_ids=None, start_date=None) -> dict:
    dup = find_client_by_phone(phone)
    if dup:
        raise DuplicatePhoneError(dup["name"])
    label, interval_days = resolve_interval_days(frequency_key, custom_amount, custom_unit)
    created_at = date.today().isoformat()
    next_followup_date = None
    if interval_days:
        # start_date lets a caller anchor the schedule to an explicit date
        # (e.g. "follow up every 4 weeks starting next Monday" from the
        # voice-add-client feature) instead of the default today+interval.
        next_followup_date = start_date if start_date else (date.today() + timedelta(days=interval_days)).isoformat()

    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO clients
           (name, phone, contact_info, address, created_at, frequency_label, interval_days, next_followup_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, phone, contact_info, address, created_at, label, interval_days, next_followup_date),
    )
    client_id = cur.lastrowid
    if list_ids is not None:
        for list_id in list_ids:
            conn.execute(
                "INSERT OR IGNORE INTO contact_list_members (list_id, client_id) VALUES (?, ?)",
                (list_id, client_id),
            )
    conn.commit()
    row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    conn.close()
    on_data_changed("client added")
    return client_to_dict(row)


def _attach_list_followups(conn, clients: list):
    """A client on a Contact List with a recurring schedule has no
    individual `frequency_label` of their own — their call schedule lives
    on the shared list instead (see complete_list_round()). Attaches
    `list_followups` (name + next_call_date per list) to each client dict
    so the UI can show "next <list> Follow Up on <date>" instead of "No
    automatic follow-up" for a list member."""
    if not clients:
        return
    rows = conn.execute("""
        SELECT contact_list_members.client_id, contact_lists.name, contact_lists.next_call_date
        FROM contact_list_members
        JOIN contact_lists ON contact_lists.id = contact_list_members.list_id
        WHERE contact_lists.interval_days IS NOT NULL AND contact_lists.next_call_date IS NOT NULL
    """).fetchall()
    by_client = {}
    for r in rows:
        by_client.setdefault(r["client_id"], []).append({"name": r["name"], "next_call_date": r["next_call_date"]})
    for c in clients:
        c["list_followups"] = by_client.get(c["id"], [])


def list_clients() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM clients ORDER BY name COLLATE NOCASE").fetchall()
    clients = [client_to_dict(r) for r in rows]
    _attach_list_followups(conn, clients)
    conn.close()
    return clients


def get_client(client_id) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not row:
        conn.close()
        return None
    client = client_to_dict(row)
    _attach_list_followups(conn, [client])
    conn.close()
    return client


def update_client(client_id, name, phone, contact_info, address, frequency_key, custom_amount, custom_unit, list_ids=None) -> dict:
    dup = find_client_by_phone(phone, exclude_id=client_id)
    if dup:
        raise DuplicatePhoneError(dup["name"])
    label, interval_days = resolve_interval_days(frequency_key, custom_amount, custom_unit)

    conn = get_conn()
    existing = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not existing:
        conn.close()
        return None

    # Only reset the schedule anchor if the frequency actually changed.
    if existing["interval_days"] == interval_days and existing["frequency_label"] == label:
        next_followup_date = existing["next_followup_date"]
    elif interval_days:
        next_followup_date = (date.today() + timedelta(days=interval_days)).isoformat()
    else:
        next_followup_date = None

    conn.execute(
        """UPDATE clients SET name=?, phone=?, contact_info=?, address=?,
           frequency_label=?, interval_days=?, next_followup_date=? WHERE id=?""",
        (name, phone, contact_info, address, label, interval_days, next_followup_date, client_id),
    )
    if list_ids is not None:
        conn.execute("DELETE FROM contact_list_members WHERE client_id = ?", (client_id,))
        for list_id in list_ids:
            conn.execute(
                "INSERT OR IGNORE INTO contact_list_members (list_id, client_id) VALUES (?, ?)",
                (list_id, client_id),
            )
    conn.commit()
    row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    conn.close()
    on_data_changed("client updated")
    return client_to_dict(row)


def delete_client(client_id):
    conn = get_conn()
    conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    conn.commit()
    conn.close()
    # Refresh the OneDrive mirror right away so the deleted contact is gone
    # from the backup too, not just the local DB.
    on_data_changed("client deleted")


def find_duplicate_clients() -> list:
    """Groups clients that share the exact same name (case/whitespace-
    insensitive) — the shape of duplicate a phone-less CSV import row
    produces when it's run before it can match anything yet (see build
    order #62/#63: a phone-less row that finds no match creates a fresh
    client every time, so re-importing before that matching existed left
    real duplicate rows behind). Only returns groups with 2+ members, so
    a normal single "Eduardo" with no duplicates never shows up. Each
    member includes its note/call-log counts so the UI (and the default
    keep-pick below) can tell a fleshed-out record from a bare one."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM clients").fetchall()
    groups = {}
    for r in rows:
        key = (r["name"] or "").strip().lower()
        if not key:
            continue
        groups.setdefault(key, []).append(dict(r))

    result = []
    for members in groups.values():
        if len(members) < 2:
            continue
        for m in members:
            m["note_count"] = conn.execute(
                "SELECT COUNT(*) FROM notes WHERE client_id = ?", (m["id"],)
            ).fetchone()[0]
            m["call_count"] = conn.execute(
                "SELECT COUNT(*) FROM call_log WHERE client_id = ?", (m["id"],)
            ).fetchone()[0]
        # Default "keep" pick: whichever member actually has a phone number
        # wins first (it's the more complete record), then more notes, then
        # whichever was created first.
        members.sort(key=lambda m: (m["phone"] is None or m["phone"] == "", -m["note_count"], m["created_at"]))
        result.append({
            "name": members[0]["name"],
            "members": [
                {
                    "id": m["id"], "phone": m["phone"], "address": m["address"],
                    "created_at": m["created_at"], "note_count": m["note_count"],
                    "call_count": m["call_count"], "frequency_label": m["frequency_label"],
                }
                for m in members
            ],
        })
    conn.close()
    result.sort(key=lambda g: g["name"].lower())
    return result


def merge_clients(keep_id, remove_ids) -> dict:
    """Merges each client in remove_ids into keep_id: reassigns their
    notes, call log, calendar events, and Contact List memberships onto
    keep_id (never lost, just relocated), fills in keep_id's phone/
    address/schedule only where keep_id doesn't already have one (a
    duplicate's data never overwrites a real value keep_id already has),
    then deletes the now-merged-away duplicate rows. Wrapped in
    _deferred_backup since a big cleanup pass can touch many clients at
    once, same reasoning as the bulk CSV imports."""
    with _deferred_backup():
        conn = get_conn()
        keep = conn.execute("SELECT * FROM clients WHERE id = ?", (keep_id,)).fetchone()
        if not keep:
            conn.close()
            return None
        for rid in remove_ids:
            if rid == keep_id:
                continue
            dup = conn.execute("SELECT * FROM clients WHERE id = ?", (rid,)).fetchone()
            if not dup:
                continue
            conn.execute("UPDATE notes SET client_id = ? WHERE client_id = ?", (keep_id, rid))
            conn.execute("UPDATE call_log SET client_id = ? WHERE client_id = ?", (keep_id, rid))
            conn.execute("UPDATE events SET client_id = ? WHERE client_id = ?", (keep_id, rid))
            conn.execute(
                """INSERT OR IGNORE INTO contact_list_members (list_id, client_id)
                   SELECT list_id, ? FROM contact_list_members WHERE client_id = ?""",
                (keep_id, rid),
            )
            conn.execute("DELETE FROM contact_list_members WHERE client_id = ?", (rid,))
            if not keep["phone"] and dup["phone"]:
                conn.execute("UPDATE clients SET phone = ? WHERE id = ?", (dup["phone"], keep_id))
            if not keep["address"] and dup["address"]:
                conn.execute("UPDATE clients SET address = ? WHERE id = ?", (dup["address"], keep_id))
            if keep["interval_days"] is None and dup["interval_days"] is not None:
                conn.execute(
                    "UPDATE clients SET frequency_label = ?, interval_days = ?, next_followup_date = ? WHERE id = ?",
                    (dup["frequency_label"], dup["interval_days"], dup["next_followup_date"], keep_id),
                )
            conn.execute("DELETE FROM clients WHERE id = ?", (rid,))
            keep = conn.execute("SELECT * FROM clients WHERE id = ?", (keep_id,)).fetchone()
        conn.commit()
        row = conn.execute("SELECT * FROM clients WHERE id = ?", (keep_id,)).fetchone()
        conn.close()
    return client_to_dict(row)


def log_call(client_id) -> dict:
    """Stamps a client as called just now — fired whenever Mario actually
    places a call from the Dialer (there's no reliable way to detect a
    tel: link's call was answered/completed, so 'clicked to call' is the
    same approximation the rest of the Dialer already uses). Every call is
    also appended to call_log (full history), not just the latest
    timestamp — see list_call_history().

    If the client's automatic follow-up was overdue or due today at the
    moment of the call, this also advances next_followup_date the same way
    complete_followup() does — this is what makes a client drop off the
    Dialer's "Missed Follow-ups"/"Today's Follow-ups" sources the instant
    Mario actually calls them, instead of staying listed until he separately
    opens the client card and hits "Mark Follow-up Complete"."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not row:
        conn.close()
        return None
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("UPDATE clients SET last_called_at = ? WHERE id = ?", (now, client_id))
    conn.execute("INSERT INTO call_log (client_id, called_at) VALUES (?, ?)", (client_id, now))
    if row["interval_days"] and row["next_followup_date"]:
        current_due = date.fromisoformat(row["next_followup_date"])
        today = date.today()
        if current_due <= today:
            # Catch the schedule all the way up to the next date that isn't
            # itself already in the past -- a single +interval hop (what the
            # client-detail "Mark Follow-up Complete" button does) isn't
            # enough here: a client missed for several stacked intervals
            # would still show up overdue by less than one interval and
            # never actually leave the Dialer's "Missed Follow-ups" list.
            # Calling them must always clear the overdue status outright.
            new_due = current_due
            while new_due <= today:
                new_due += timedelta(days=row["interval_days"])
            conn.execute("UPDATE clients SET next_followup_date = ? WHERE id = ?", (new_due.isoformat(), client_id))
    conn.commit()
    row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    conn.close()
    on_data_changed("call logged")
    return client_to_dict(row)


def get_missed_followups() -> list:
    """Automatic follow-ups that are past their due date and still haven't
    been called — the Dialer's "Missed Follow-ups" source. A client stays
    listed here, showing the ORIGINAL date it was due, for every day it goes
    uncalled (next_followup_date only ever moves once log_call() actually
    fires — see above), sorted oldest-missed-first. Calling a client via the
    Dialer removes them from this list automatically (log_call() advances
    their schedule); it is never a separate step."""
    results = []
    for c in list_clients():
        if c["followup_status"] == "overdue":
            results.append({
                "client_id": c["id"], "client_name": c["name"],
                "phone": c["phone"], "address": c["address"],
                "missed_date": c["next_followup_date"],
            })
    results.sort(key=lambda i: i["missed_date"])
    return results


def list_call_history(client_id) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, called_at FROM call_log WHERE client_id = ? ORDER BY called_at DESC, id DESC",
        (client_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def complete_followup(client_id) -> dict:
    """Advance the client's next follow-up date forward by one interval,
    from whichever due date is currently on the books (handles overdue
    follow-ups correctly instead of drifting off today's date)."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not row or not row["interval_days"] or not row["next_followup_date"]:
        conn.close()
        return None

    current_due = date.fromisoformat(row["next_followup_date"])
    new_due = current_due + timedelta(days=row["interval_days"])
    conn.execute("UPDATE clients SET next_followup_date = ? WHERE id = ?", (new_due.isoformat(), client_id))
    conn.commit()
    updated = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    conn.close()
    on_data_changed("follow-up completed")
    return client_to_dict(updated)


def _list_call_status(next_call_date) -> str:
    if not next_call_date:
        return None
    due = date.fromisoformat(next_call_date)
    today = date.today()
    return "overdue" if due < today else ("due_today" if due == today else "upcoming")


def list_contact_lists() -> list:
    """A Contact List is a saved group of CRM clients (e.g. every unit in a
    building) with ONE shared recurring call schedule for the whole group —
    distinct from each client's own individual follow-up schedule."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM contact_lists ORDER BY name COLLATE NOCASE").fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["call_status"] = _list_call_status(d["next_call_date"])
        d["member_count"] = conn.execute(
            "SELECT COUNT(*) AS n FROM contact_list_members WHERE list_id = ?", (row["id"],)
        ).fetchone()["n"]
        result.append(d)
    conn.close()
    return result


def create_contact_list(name, frequency_key, custom_amount, custom_unit) -> dict:
    label, interval_days = resolve_interval_days(frequency_key, custom_amount, custom_unit)
    next_call_date = (date.today() + timedelta(days=interval_days)).isoformat() if interval_days else None
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO contact_lists (name, frequency_label, interval_days, next_call_date, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, label, interval_days, next_call_date, date.today().isoformat()),
    )
    conn.commit()
    list_id = cur.lastrowid
    conn.close()
    on_data_changed("contact list created")
    return get_contact_list(list_id)


def get_contact_list(list_id) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM contact_lists WHERE id = ?", (list_id,)).fetchone()
    if not row:
        conn.close()
        return None
    d = dict(row)
    d["call_status"] = _list_call_status(d["next_call_date"])
    members = conn.execute(
        """SELECT clients.* FROM clients
           JOIN contact_list_members ON contact_list_members.client_id = clients.id
           WHERE contact_list_members.list_id = ?
           ORDER BY clients.name COLLATE NOCASE""",
        (list_id,),
    ).fetchall()
    conn.close()
    d["members"] = [client_to_dict(m) for m in members]
    d["member_count"] = len(d["members"])
    return d


def update_contact_list(list_id, name, frequency_key, custom_amount, custom_unit) -> dict:
    label, interval_days = resolve_interval_days(frequency_key, custom_amount, custom_unit)
    conn = get_conn()
    existing = conn.execute("SELECT * FROM contact_lists WHERE id = ?", (list_id,)).fetchone()
    if not existing:
        conn.close()
        return None
    if existing["interval_days"] == interval_days and existing["frequency_label"] == label:
        next_call_date = existing["next_call_date"]
    elif interval_days:
        next_call_date = (date.today() + timedelta(days=interval_days)).isoformat()
    else:
        next_call_date = None
    conn.execute(
        "UPDATE contact_lists SET name=?, frequency_label=?, interval_days=?, next_call_date=? WHERE id=?",
        (name, label, interval_days, next_call_date, list_id),
    )
    conn.commit()
    conn.close()
    on_data_changed("contact list updated")
    return get_contact_list(list_id)


def delete_contact_list(list_id):
    conn = get_conn()
    conn.execute("DELETE FROM contact_lists WHERE id = ?", (list_id,))
    conn.commit()
    conn.close()
    on_data_changed("contact list deleted")


def set_list_members(list_id, client_ids) -> dict:
    """Replaces a list's entire membership with the given client ids —
    simpler for a checkbox-style membership editor than incremental
    add/remove calls."""
    conn = get_conn()
    conn.execute("DELETE FROM contact_list_members WHERE list_id = ?", (list_id,))
    for client_id in client_ids:
        conn.execute(
            "INSERT OR IGNORE INTO contact_list_members (list_id, client_id) VALUES (?, ?)",
            (list_id, client_id),
        )
    conn.commit()
    conn.close()
    on_data_changed("contact list membership updated")
    return get_contact_list(list_id)


def add_list_members(list_id, client_ids) -> dict:
    """Adds client_ids to a list's membership WITHOUT touching anyone
    already there — unlike set_list_members (a full replace, built for the
    checkbox-style Manage Members editor which always sends the FULL
    desired set), this is for merging a batch in without disturbing
    existing members. Used by the Dialer's 'Save This List to a Contact
    List' feature — saving a dial list into an EXISTING list must never
    wipe out members who simply weren't on that particular dial list."""
    conn = get_conn()
    row = conn.execute("SELECT id FROM contact_lists WHERE id = ?", (list_id,)).fetchone()
    if not row:
        conn.close()
        return None
    for client_id in client_ids:
        conn.execute(
            "INSERT OR IGNORE INTO contact_list_members (list_id, client_id) VALUES (?, ?)",
            (list_id, client_id),
        )
    conn.commit()
    conn.close()
    on_data_changed("contact list membership updated")
    return get_contact_list(list_id)


def remove_list_member(list_id, client_id) -> dict:
    """Removes a single client from a list without touching anyone else —
    the counterpart to add_list_members(), for the Manage Members view's
    per-row Remove button (which shows only current members, not a
    full-membership checkbox editor — see build order #64)."""
    conn = get_conn()
    row = conn.execute("SELECT id FROM contact_lists WHERE id = ?", (list_id,)).fetchone()
    if not row:
        conn.close()
        return None
    conn.execute(
        "DELETE FROM contact_list_members WHERE list_id = ? AND client_id = ?", (list_id, client_id)
    )
    conn.commit()
    conn.close()
    on_data_changed("contact list member removed")
    return get_contact_list(list_id)


def get_client_list_ids(client_id) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT list_id FROM contact_list_members WHERE client_id = ?", (client_id,)
    ).fetchall()
    conn.close()
    return [r["list_id"] for r in rows]


def set_client_lists(client_id, list_ids):
    """Replaces which lists a single client belongs to — used from the
    client add/edit form so list membership can be set from either side."""
    conn = get_conn()
    conn.execute("DELETE FROM contact_list_members WHERE client_id = ?", (client_id,))
    for list_id in list_ids:
        conn.execute(
            "INSERT OR IGNORE INTO contact_list_members (list_id, client_id) VALUES (?, ?)",
            (list_id, client_id),
        )
    conn.commit()
    conn.close()
    on_data_changed("contact list membership updated")


def complete_list_round(list_id) -> dict:
    """Advances the WHOLE list's shared call schedule forward by one
    interval, once you've finished dialing through it this round."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM contact_lists WHERE id = ?", (list_id,)).fetchone()
    if not row or not row["interval_days"] or not row["next_call_date"]:
        conn.close()
        return None
    current_due = date.fromisoformat(row["next_call_date"])
    new_due = current_due + timedelta(days=row["interval_days"])
    conn.execute("UPDATE contact_lists SET next_call_date = ? WHERE id = ?", (new_due.isoformat(), list_id))
    conn.commit()
    conn.close()
    on_data_changed("contact list round completed")
    return get_contact_list(list_id)


def parse_vcard_contacts(vcf_text: str) -> list:
    """Parses (name, phone) pairs out of a phone's exported vCard (.vcf)
    text — the standard format iPhone/Android "Share/Export Contact"
    produces. Skips any card missing a name or a phone number rather than
    guessing."""
    contacts = []
    for card in vcf_text.split("BEGIN:VCARD")[1:]:
        fn_match = re.search(r"^FN:(.+)$", card, re.M)
        tel_match = re.search(r"^TEL[^:]*:(.+)$", card, re.M)
        if fn_match and tel_match:
            contacts.append((fn_match.group(1).strip(), tel_match.group(1).strip()))
    return contacts


CONTACT_NAME_ALIASES = {"name", "full name", "contact name", "client name", "contact"}
CONTACT_PHONE_ALIASES = {"phone", "phone number", "mobile", "cell", "telephone", "tel", "number"}
CONTACT_ADDRESS_ALIASES = {"address", "street address", "home address", "property address"}
_PHONE_RE = re.compile(r"(\+?\d[\d\-.() ]{7,}\d)")


def parse_contact_import(raw_text: str) -> list:
    """Best-effort contact parser accepting whatever format a phone/contacts
    app happens to export — a vCard (.vcf), a CSV (with a recognized header
    row, or a headerless "Name, Phone[, Address]" shape), or plain pasted
    text. Same by-meaning-not-exact-name matching philosophy as
    input_parser.py / the Dialer's paste box: never guesses column order on
    an unrecognized shape, just falls through to the next, simpler format.
    Returns (name, phone, address) tuples; address is "" when not present
    in the source."""
    text = raw_text.strip()
    if "BEGIN:VCARD" in text:
        return [(name, phone, "") for name, phone in parse_vcard_contacts(text)]

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []

    try:
        rows = list(csv.reader(lines))
    except csv.Error:
        rows = [[ln] for ln in lines]

    # A recognized header row remaps which column is which for every row
    # below it; otherwise each row is judged on its own shape as we go, so
    # one malformed or differently-shaped line never drops the rest of a
    # mixed file — same per-line-fallback principle as the Dialer's paste box.
    name_idx = phone_idx = address_idx = None
    body_rows = rows
    if rows and len(rows[0]) > 1:
        header = [h.strip().lower() for h in rows[0]]
        name_idx = next((i for i, h in enumerate(header) if h in CONTACT_NAME_ALIASES), None)
        phone_idx = next((i for i, h in enumerate(header) if h in CONTACT_PHONE_ALIASES), None)
        address_idx = next((i for i, h in enumerate(header) if h in CONTACT_ADDRESS_ALIASES), None)
        if name_idx is not None and phone_idx is not None:
            body_rows = rows[1:]
        else:
            name_idx = phone_idx = address_idx = None

    contacts = []
    for row, line in zip(body_rows, lines[len(rows) - len(body_rows):]):
        if name_idx is not None and phone_idx is not None and len(row) > max(name_idx, phone_idx):
            name, phone = row[name_idx].strip(), row[phone_idx].strip()
            address = row[address_idx].strip() if address_idx is not None and len(row) > address_idx else ""
            if name and phone:
                contacts.append((name, phone, address))
                continue
        # Headerless shape: Name, Phone[, Address, ...].
        if len(row) >= 2 and row[0].strip() and _PHONE_RE.search(row[1]):
            contacts.append((row[0].strip(), row[1].strip(), row[2].strip() if len(row) > 2 else ""))
            continue
        # Last resort: a bare phone number found anywhere in the raw line
        # (name falls back to the phone number itself).
        m = _PHONE_RE.search(line)
        if m:
            phone = m.group(1).strip()
            name = (line[:m.start()] + line[m.end():]).strip(" ,-\t")
            contacts.append((name or phone, phone, ""))
    return contacts


def import_contact_list_file(name, address, frequency_key, custom_amount, custom_unit, raw_text) -> dict:
    """One drag-and-drop: parses whatever contacts-export format was
    dropped in, creates a CRM client for each named contact with a phone
    number, and files them all under a brand-new Contact List on the given
    recurring schedule. A per-contact address from the source (e.g. a CSV
    with its own address column) wins over the shared address typed in the
    import form. Returns None if the file had no usable contacts, so
    callers can bail out before creating an empty list."""
    contacts = parse_contact_import(raw_text)
    if not contacts:
        return None
    client_ids = []
    with _deferred_backup():
        for contact_name, phone, contact_address in contacts:
            client = create_client(
                name=contact_name, phone=phone, contact_info="", address=contact_address or address or "",
                frequency_key="none", custom_amount=None, custom_unit=None,
            )
            client_ids.append(client["id"])
        contact_list = create_contact_list(name, frequency_key, custom_amount, custom_unit)
        result = set_list_members(contact_list["id"], client_ids)
    return result


RECURRING_IMPORT_FIELDS = {"name", "phone", "address", "notes", "frequency_key", "custom_amount", "custom_unit"}
RECURRING_IMPORT_NOTE_PREFIX = "Imported from Outlook calendar"


def _parse_recurring_import_rows(raw_text: str):
    """Shared row parser for both the preview and the commit step, so the
    row a given index refers to is guaranteed identical between the two
    (the preview's checkbox selections are keyed by this same row index)."""
    reader = csv.DictReader(raw_text.splitlines())
    if not reader.fieldnames or not RECURRING_IMPORT_FIELDS.issubset(set(reader.fieldnames)):
        return None
    rows = []
    for row in reader:
        name = (row.get("name") or "").strip()
        notes = (row.get("notes") or "").strip()
        rows.append({
            "name": name,
            "phone": (row.get("phone") or "").strip(),
            "address": (row.get("address") or "").strip(),
            "notes": notes,
            "frequency_key": (row.get("frequency_key") or "none").strip() or "none",
            "custom_amount": (row.get("custom_amount") or "").strip() or None,
            "custom_unit": (row.get("custom_unit") or "").strip() or None,
            "note_text": f"{RECURRING_IMPORT_NOTE_PREFIX}: {notes}" if notes else f"{RECURRING_IMPORT_NOTE_PREFIX}.",
        })
    return rows


def _find_recurring_import_match(name, phone, address):
    """Finds the existing client this row would be merged into, if any.
    Phone is the primary key (find_client_by_phone, same as everywhere
    else). A phone-less row (most of this export -- Outlook reminders
    rarely have a number in the body) falls back to matching a client
    that was already created by a PRIOR run of this exact import (same
    name + address, and already carries an "Imported from Outlook
    calendar" note) -- narrow enough to never match a client Mario
    entered by hand under a common first name, but catches a re-run of
    the same file so it merges instead of duplicating (see build order
    #62 -- a re-run before this fix created real duplicate clients for
    every phone-less name)."""
    if phone:
        return find_client_by_phone(phone)
    if not name:
        return None
    conn = get_conn()
    rows = conn.execute(
        """SELECT DISTINCT clients.* FROM clients
           JOIN notes ON notes.client_id = clients.id
           WHERE notes.text LIKE ? AND lower(clients.name) = lower(?)
             AND lower(coalesce(clients.address, '')) = lower(?)""",
        (f"{RECURRING_IMPORT_NOTE_PREFIX}%", name, address),
    ).fetchall()
    conn.close()
    return client_to_dict(rows[0]) if rows else None


def preview_recurring_followups_csv(raw_text: str) -> list:
    """Dry run: parses the CSV and reports what each row WOULD do, with no
    writes -- lets the CRM Clients dropzone show a confirm screen (every
    row pre-checked to import/replace, Mario can uncheck any he doesn't
    want touched) before committing anything."""
    rows = _parse_recurring_import_rows(raw_text)
    if rows is None:
        return None
    preview = []
    for i, row in enumerate(rows):
        if not row["name"]:
            action, matched_name = "skip", None
        else:
            match = _find_recurring_import_match(row["name"], row["phone"], row["address"])
            if not match:
                action, matched_name = "create", None
            elif match["interval_days"] is None:
                action, matched_name = "update", match["name"]
            else:
                action, matched_name = "note_only", match["name"]
        preview.append({
            "index": i, "name": row["name"], "phone": row["phone"], "address": row["address"],
            "action": action, "matched_client_name": matched_name,
        })
    return preview


def import_recurring_followups_csv(raw_text: str, selected_indices=None) -> dict:
    """Imports the specific CSV shape produced by the Outlook recurring-
    follow-up export (name/phone/address/notes/frequency_key/custom_amount/
    custom_unit columns) — a one-time backfill, not a general contacts
    import (see import_contact_list_file/parse_contact_import for that).

    `selected_indices`, when given, restricts the import to just those row
    indices (matching preview_recurring_followups_csv's numbering) — the
    confirm screen's per-row checkboxes. None means "import every row",
    same as before that screen existed.

    Dedup tries phone first (find_client_by_phone, same as everywhere else
    in this file), then falls back to matching a client already created by
    a prior run of this same import (see _find_recurring_import_match) —
    so re-running the same file is always safe. To avoid silently
    clobbering data Mario already entered by hand, an existing client's
    schedule/address are only filled in where they're currently
    blank/unset — the imported note is always appended regardless, and
    never overwrites a client's own name."""
    rows = _parse_recurring_import_rows(raw_text)
    if rows is None:
        return {"created": 0, "updated": 0, "skipped": 0, "errors": ["File doesn't match the expected recurring-follow-up export columns."]}

    created = updated = skipped = 0
    errors = []
    with _deferred_backup():
        for i, row in enumerate(rows):
            if selected_indices is not None and i not in selected_indices:
                skipped += 1
                continue
            name = row["name"]
            if not name:
                skipped += 1
                continue
            phone, address, note_text = row["phone"], row["address"], row["note_text"]
            frequency_key, custom_amount, custom_unit = row["frequency_key"], row["custom_amount"], row["custom_unit"]

            existing = _find_recurring_import_match(name, phone, address)
            try:
                if existing:
                    # Only fill in a schedule/address the client doesn't already
                    # have — never clobber a schedule Mario already set by hand,
                    # preset or custom alike. The imported note is added either way.
                    if existing["interval_days"] is None:
                        client = update_client(
                            existing["id"], name=existing["name"], phone=existing["phone"],
                            contact_info=existing["contact_info"], address=existing["address"] or address,
                            frequency_key=frequency_key, custom_amount=custom_amount, custom_unit=custom_unit,
                        )
                    else:
                        client = existing
                    add_note(client["id"], note_text)
                    updated += 1
                else:
                    client = create_client(
                        name=name, phone=phone, contact_info="", address=address,
                        frequency_key=frequency_key, custom_amount=custom_amount, custom_unit=custom_unit,
                    )
                    add_note(client["id"], note_text)
                    created += 1
            except DuplicatePhoneError:
                skipped += 1
            except (ValueError, TypeError) as exc:
                errors.append(f"{name}: {exc}")
                skipped += 1

    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}


def add_note(client_id, text) -> dict:
    conn = get_conn()
    created_at = date.today().isoformat() + " " + _now_time()
    cur = conn.execute(
        "INSERT INTO notes (client_id, text, created_at) VALUES (?, ?, ?)",
        (client_id, text, created_at),
    )
    conn.commit()
    note = conn.execute("SELECT * FROM notes WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    on_data_changed("note added")
    return dict(note)


def list_notes(client_id) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM notes WHERE client_id = ? ORDER BY id DESC", (client_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _now_time():
    from datetime import datetime
    return datetime.now().strftime("%H:%M")


EVENT_JOIN_SELECT = """
    SELECT events.*, clients.name AS client_name, clients.phone AS client_phone,
           clients.address AS client_address, contact_lists.name AS list_name
    FROM events
    LEFT JOIN clients ON clients.id = events.client_id
    LEFT JOIN contact_lists ON contact_lists.id = events.list_id
"""


def _event_to_dict(row) -> dict:
    d = dict(row)
    d["kind"] = "manual"
    return d


def create_event(client_id, title, date_str, time_str, notes, list_id=None) -> dict:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO events (client_id, list_id, title, date, time, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (client_id or None, list_id or None, title, date_str, time_str or None, notes or "",
         datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    row = conn.execute(EVENT_JOIN_SELECT + " WHERE events.id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    on_data_changed("event added")
    return _event_to_dict(row)


def update_event(event_id, client_id, title, date_str, time_str, notes, list_id=None) -> dict:
    conn = get_conn()
    existing = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not existing:
        conn.close()
        return None
    conn.execute(
        "UPDATE events SET client_id=?, list_id=?, title=?, date=?, time=?, notes=? WHERE id=?",
        (client_id or None, list_id or None, title, date_str, time_str or None, notes or "", event_id),
    )
    conn.commit()
    row = conn.execute(EVENT_JOIN_SELECT + " WHERE events.id = ?", (event_id,)).fetchone()
    conn.close()
    on_data_changed("event updated")
    return _event_to_dict(row)


def delete_event(event_id):
    conn = get_conn()
    conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    conn.commit()
    conn.close()
    on_data_changed("event deleted")


def list_events_for_month(year: int, month: int) -> list:
    month_start = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    month_end = date(year, month, last_day)
    conn = get_conn()
    rows = conn.execute(
        EVENT_JOIN_SELECT + " WHERE events.date >= ? AND events.date <= ? ORDER BY events.date, events.time",
        (month_start.isoformat(), month_end.isoformat()),
    ).fetchall()
    conn.close()
    return [_event_to_dict(r) for r in rows]


def list_events_for_date(date_str: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        EVENT_JOIN_SELECT + " WHERE events.date = ? ORDER BY events.time", (date_str,)
    ).fetchall()
    conn.close()
    return [_event_to_dict(r) for r in rows]


def get_today_followups() -> list:
    """Everything due for a call today: any client whose automatic follow-up
    schedule has them overdue or due today, plus any manually-added event
    dated today (whether or not it's linked to a client) — deduped so a
    client showing up both ways (e.g. a manual reminder added for a client
    who's also automatically due) only appears once, with the manual
    event's title/time/notes taking precedence since it's the more specific
    entry."""
    today_str = date.today().isoformat()

    items_by_client = {}
    standalone = []

    for c in list_clients():
        if c["followup_status"] in ("overdue", "due_today"):
            items_by_client[c["id"]] = {
                "kind": "auto", "client_id": c["id"], "client_name": c["name"],
                "phone": c["phone"], "address": c["address"],
                "title": f"Follow up with {c['name']}", "time": None,
                "status": c["followup_status"], "notes": "", "event_id": None,
            }

    for e in list_events_for_date(today_str):
        item = {
            "kind": "manual", "client_id": e["client_id"], "client_name": e.get("client_name"),
            "phone": e.get("client_phone"), "address": e.get("client_address"),
            "list_id": e["list_id"], "list_name": e.get("list_name"),
            "title": e["title"], "time": e["time"],
            "status": "manual", "notes": e.get("notes") or "", "event_id": e["id"],
        }
        if e["client_id"]:
            items_by_client[e["client_id"]] = item
        else:
            standalone.append(item)

    results = list(items_by_client.values()) + standalone
    results.sort(key=lambda i: (i["time"] or "99:99"))
    return results


def get_calendar_events(year: int, month: int) -> list:
    """Computes every follow-up occurrence (past-completed, next-due, and
    indefinitely-recurring future ones) that falls within the given month,
    from each client's stored (next_followup_date, interval_days) anchor —
    no per-occurrence rows are stored, so this works forever without
    unbounded storage."""
    month_start = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    month_end = date(year, month, last_day)
    today = date.today()

    conn = get_conn()
    clients = conn.execute(
        "SELECT * FROM clients WHERE interval_days IS NOT NULL AND next_followup_date IS NOT NULL"
    ).fetchall()
    contact_lists = conn.execute(
        "SELECT * FROM contact_lists WHERE interval_days IS NOT NULL AND next_call_date IS NOT NULL"
    ).fetchall()
    conn.close()

    events = []
    for c in clients:
        interval = c["interval_days"]
        anchor = date.fromisoformat(c["next_followup_date"])
        created = date.fromisoformat(c["created_at"])

        # Walk backward from the anchor for already-completed occurrences.
        # The creation date itself is excluded unless it happens to *be*
        # the anchor — otherwise a brand-new client shows a phantom
        # "completed" follow-up on the day they were simply added.
        d = anchor
        while d >= created:
            if (d == anchor or d > created) and month_start <= d <= month_end:
                if d == anchor:
                    status = "overdue" if anchor < today else ("due_today" if anchor == today else "next_due")
                else:
                    status = "completed"
                events.append({
                    "kind": "auto", "client_id": c["id"], "client_name": c["name"],
                    "date": d.isoformat(), "status": status,
                    "event_id": None, "title": None, "time": None, "notes": None,
                })
            d -= timedelta(days=interval)

        # Walk forward from the anchor for indefinitely-recurring future ones.
        d = anchor + timedelta(days=interval)
        while d <= month_end:
            events.append({
                "kind": "auto", "client_id": c["id"], "client_name": c["name"],
                "date": d.isoformat(), "status": "upcoming",
                "event_id": None, "title": None, "time": None, "notes": None,
            })
            d += timedelta(days=interval)

    # Contact List call rounds — same walk-backward/forward math as an
    # individual client's follow-up, but anchored on the list's own shared
    # next_call_date/interval_days rather than any one client's schedule.
    for l in contact_lists:
        interval = l["interval_days"]
        anchor = date.fromisoformat(l["next_call_date"])
        created = date.fromisoformat(l["created_at"])

        d = anchor
        while d >= created:
            if (d == anchor or d > created) and month_start <= d <= month_end:
                if d == anchor:
                    status = "overdue" if anchor < today else ("due_today" if anchor == today else "next_due")
                else:
                    status = "completed"
                events.append({
                    "kind": "list", "list_id": l["id"], "list_name": l["name"],
                    "date": d.isoformat(), "status": status,
                    "event_id": None, "title": None, "time": None, "notes": None,
                })
            d -= timedelta(days=interval)

        d = anchor + timedelta(days=interval)
        while d <= month_end:
            events.append({
                "kind": "list", "list_id": l["id"], "list_name": l["name"],
                "date": d.isoformat(), "status": "upcoming",
                "event_id": None, "title": None, "time": None, "notes": None,
            })
            d += timedelta(days=interval)

    # Manually-added events (the "click a day to add an event" feature) are
    # merged in alongside the automatic occurrences above, not stored as
    # part of the recurring schedule math.
    for e in list_events_for_month(year, month):
        events.append({
            "kind": "manual", "event_id": e["id"],
            "client_id": e["client_id"], "client_name": e.get("client_name"),
            "list_id": e["list_id"], "list_name": e.get("list_name"),
            "date": e["date"], "time": e["time"], "title": e["title"],
            "notes": e.get("notes") or "", "status": "manual",
        })

    events.sort(key=lambda e: (e["date"], e.get("time") or ""))
    return events


# ===================== Transaction Manager =====================
# Four fixed transaction types (not a user-editable list — only the
# required-document checklist per type is editable). Kept as a plain tuple
# of (key, label) rather than a DB table since Mario asked to customize the
# document checklist, not add arbitrary new transaction types.
TRANSACTION_TYPES = [
    ("listing", "Listing"),
    ("rental_listing", "Rental Listing"),
    ("buyer_representation", "Buyer Representation"),
    ("rental", "Rental"),
]
TRANSACTION_TYPE_KEYS = {k for k, _ in TRANSACTION_TYPES}
TRANSACTION_STATUSES = {"active", "under_contract", "closed"}


def _store_upload(file_storage, dest_dir: Path) -> tuple:
    """Saves an uploaded file under a collision-safe generated name, keeping
    the original filename separately for display/download. Returns
    (stored_filename, original_name)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    original_name = file_storage.filename or "document"
    safe = secure_filename(original_name) or "document"
    stored = f"{uuid.uuid4().hex[:12]}_{safe}"
    file_storage.save(dest_dir / stored)
    return stored, original_name


def _delete_stored_file(dest_dir: Path, stored_filename):
    if not stored_filename:
        return
    try:
        (dest_dir / stored_filename).unlink(missing_ok=True)
    except OSError:
        pass


# ---- Document type checklists (shared per transaction type) ----

def list_document_types(transaction_type: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM document_types WHERE transaction_type = ? ORDER BY sort_order, id",
        (transaction_type,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_document_type(document_type_id: int) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM document_types WHERE id = ?", (document_type_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_transaction_document(transaction_document_id: int) -> dict:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM transaction_documents WHERE id = ?", (transaction_document_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def add_document_type(transaction_type: str, name: str) -> dict:
    conn = get_conn()
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) AS m FROM document_types WHERE transaction_type = ?",
        (transaction_type,),
    ).fetchone()["m"]
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO document_types (transaction_type, name, sort_order, created_at) VALUES (?, ?, ?, ?)",
        (transaction_type, name, max_order + 1, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM document_types WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    on_data_changed("document type added")
    return dict(row)


def reorder_document_types(transaction_type: str, ordered_ids: list) -> list:
    """Persists a new sort_order for a type's checklist from a drag-reorder
    in the UI. Only touches rows that actually belong to transaction_type,
    so a stale/foreign id in the list can't reorder or affect anything
    else. Existing transactions already snapshotted their own row order at
    creation time and are unaffected either way."""
    conn = get_conn()
    valid_ids = {
        r["id"] for r in conn.execute(
            "SELECT id FROM document_types WHERE transaction_type = ?", (transaction_type,)
        ).fetchall()
    }
    for i, doc_id in enumerate(ordered_ids):
        if doc_id in valid_ids:
            conn.execute("UPDATE document_types SET sort_order = ? WHERE id = ?", (i, doc_id))
    conn.commit()
    conn.close()
    on_data_changed("document checklist reordered")
    return list_document_types(transaction_type)


def delete_document_type(document_type_id: int):
    """Removes a document from a type's checklist — only affects NEW
    transactions created after this point. Existing transactions keep their
    own snapshotted row (name + any signed upload) regardless, since
    transaction_documents.document_type_id is ON DELETE SET NULL rather than
    CASCADE — the row just loses its link to a (now-gone) shared template."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM document_types WHERE id = ?", (document_type_id,)).fetchone()
    if row:
        _delete_stored_file(TEMPLATES_DIR, row["template_filename"])
    conn.execute("DELETE FROM document_types WHERE id = ?", (document_type_id,))
    conn.commit()
    conn.close()
    on_data_changed("document type removed")
    _instant_document_mirror()


def set_document_type_template(document_type_id: int, file_storage) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM document_types WHERE id = ?", (document_type_id,)).fetchone()
    if not row:
        conn.close()
        return None
    _delete_stored_file(TEMPLATES_DIR, row["template_filename"])
    stored, original = _store_upload(file_storage, TEMPLATES_DIR)
    conn.execute(
        "UPDATE document_types SET template_filename = ?, template_original_name = ? WHERE id = ?",
        (stored, original, document_type_id),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM document_types WHERE id = ?", (document_type_id,)).fetchone()
    conn.close()
    on_data_changed("document template uploaded")
    _instant_document_mirror()
    return dict(updated)


def remove_document_type_template(document_type_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM document_types WHERE id = ?", (document_type_id,)).fetchone()
    if row:
        _delete_stored_file(TEMPLATES_DIR, row["template_filename"])
        conn.execute(
            "UPDATE document_types SET template_filename = NULL, template_original_name = NULL WHERE id = ?",
            (document_type_id,),
        )
        conn.commit()
    conn.close()
    on_data_changed("document template removed")
    _instant_document_mirror()


# ---- Transactions ----

def _transaction_to_dict(row, documents=None) -> dict:
    d = dict(row)
    if documents is not None:
        d["documents"] = documents
        d["total_docs"] = len(documents)
        d["signed_docs"] = sum(1 for doc in documents if doc["signed_filename"])
    return d


TRANSACTION_FOLDERS = {"active", "closings", "archive"}


def list_transactions(folder: str = "active") -> list:
    """Lists transactions filed in the given folder. 'active' is the default
    working list; 'closings' and 'archive' are two separate, explicit places
    to file a transaction away (see set_transaction_folder())."""
    if folder not in TRANSACTION_FOLDERS:
        folder = "active"
    conn = get_conn()
    txns = conn.execute(
        "SELECT * FROM transactions WHERE folder = ? ORDER BY created_at DESC, id DESC",
        (folder,),
    ).fetchall()
    result = []
    for t in txns:
        docs = conn.execute(
            "SELECT * FROM transaction_documents WHERE transaction_id = ? ORDER BY sort_order, id",
            (t["id"],),
        ).fetchall()
        result.append(_transaction_to_dict(t, [dict(d) for d in docs]))
    conn.close()
    return result


def create_transaction(transaction_type: str, title: str) -> dict:
    """Creates a transaction and snapshots the type's CURRENT document
    checklist into its own independent rows — later edits to the type's
    checklist (add/remove/rename) never retroactively change an already-
    created transaction, so an uploaded signed document is never orphaned
    by someone editing the shared template afterward."""
    conn = get_conn()
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO transactions (transaction_type, title, status, created_at) VALUES (?, ?, 'active', ?)",
        (transaction_type, title, now),
    )
    txn_id = cur.lastrowid

    doc_types = conn.execute(
        "SELECT * FROM document_types WHERE transaction_type = ? ORDER BY sort_order, id",
        (transaction_type,),
    ).fetchall()
    for i, dt in enumerate(doc_types):
        conn.execute(
            "INSERT INTO transaction_documents (transaction_id, document_type_id, name, sort_order) VALUES (?, ?, ?, ?)",
            (txn_id, dt["id"], dt["name"], i),
        )
    conn.commit()
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    docs = conn.execute(
        "SELECT * FROM transaction_documents WHERE transaction_id = ? ORDER BY sort_order, id", (txn_id,)
    ).fetchall()
    conn.close()
    on_data_changed("transaction created")
    _instant_document_mirror()
    return _transaction_to_dict(row, [dict(d) for d in docs])


# Joins each transaction_document row with its (possibly still-linked)
# document_type, so the transaction detail view can show the shared
# template's filename without the transaction owning its own copy.
TXN_DOC_JOIN_SELECT = """
    SELECT transaction_documents.*,
           document_types.template_filename AS template_filename,
           document_types.template_original_name AS template_original_name
    FROM transaction_documents
    LEFT JOIN document_types ON document_types.id = transaction_documents.document_type_id
"""


def get_transaction(transaction_id: int) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    if not row:
        conn.close()
        return None
    docs = conn.execute(
        TXN_DOC_JOIN_SELECT + " WHERE transaction_documents.transaction_id = ? ORDER BY transaction_documents.sort_order, transaction_documents.id",
        (transaction_id,),
    ).fetchall()
    conn.close()
    return _transaction_to_dict(row, [dict(d) for d in docs])


def update_transaction(transaction_id: int, title: str, status: str) -> dict:
    conn = get_conn()
    existing = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    if not existing:
        conn.close()
        return None
    conn.execute(
        "UPDATE transactions SET title = ?, status = ? WHERE id = ?",
        (title, status, transaction_id),
    )
    conn.commit()
    conn.close()
    on_data_changed("transaction updated")
    return get_transaction(transaction_id)


def set_transaction_folder(transaction_id: int, folder: str) -> dict:
    """Files a transaction into 'active', 'closings', or 'archive' — a
    reversible way to put a transaction away (or bring it back) without
    deleting it. Nothing else about the transaction changes."""
    if folder not in TRANSACTION_FOLDERS:
        return None
    conn = get_conn()
    existing = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    if not existing:
        conn.close()
        return None
    conn.execute(
        "UPDATE transactions SET folder = ? WHERE id = ?",
        (folder, transaction_id),
    )
    conn.commit()
    conn.close()
    on_data_changed(f"transaction moved to {folder}")
    return get_transaction(transaction_id)


def delete_transaction(transaction_id: int):
    conn = get_conn()
    docs = conn.execute(
        "SELECT * FROM transaction_documents WHERE transaction_id = ?", (transaction_id,)
    ).fetchall()
    for d in docs:
        _delete_stored_file(SIGNED_DIR, d["signed_filename"])
    conn.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))  # cascades transaction_documents
    conn.commit()
    conn.close()
    on_data_changed("transaction deleted")
    _instant_document_mirror()


def upload_signed_document(transaction_document_id: int, file_storage) -> dict:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM transaction_documents WHERE id = ?", (transaction_document_id,)
    ).fetchone()
    if not row:
        conn.close()
        return None
    _delete_stored_file(SIGNED_DIR, row["signed_filename"])
    stored, original = _store_upload(file_storage, SIGNED_DIR)
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE transaction_documents SET signed_filename = ?, signed_original_name = ?, signed_at = ? WHERE id = ?",
        (stored, original, now, transaction_document_id),
    )
    conn.commit()
    updated = conn.execute(
        TXN_DOC_JOIN_SELECT + " WHERE transaction_documents.id = ?", (transaction_document_id,)
    ).fetchone()
    conn.close()
    on_data_changed("signed document uploaded")
    _instant_document_mirror()
    return dict(updated)


def remove_signed_document(transaction_document_id: int) -> dict:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM transaction_documents WHERE id = ?", (transaction_document_id,)
    ).fetchone()
    if not row:
        conn.close()
        return None
    _delete_stored_file(SIGNED_DIR, row["signed_filename"])
    conn.execute(
        "UPDATE transaction_documents SET signed_filename = NULL, signed_original_name = NULL, signed_at = NULL WHERE id = ?",
        (transaction_document_id,),
    )
    conn.commit()
    updated = conn.execute(
        TXN_DOC_JOIN_SELECT + " WHERE transaction_documents.id = ?", (transaction_document_id,)
    ).fetchone()
    conn.close()
    on_data_changed("signed document removed")
    _instant_document_mirror()
    return dict(updated)


# ---- Follow-up text presets ----
# Reusable preset messages for the "Follow-up Text" button (client detail
# view + Dialer) — opens an sms: link pre-filled with the chosen client's
# phone number and either a preset or a custom-typed message. No telephony
# account/cost involved: this just hands off to whatever the OS/Phone Link
# already has registered for sms: links (Mario confirmed Phone Link handles
# real sending for his iPhone over Bluetooth).

def list_text_presets() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM text_presets ORDER BY sort_order, id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_text_preset(text: str) -> dict:
    conn = get_conn()
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) AS m FROM text_presets").fetchone()["m"]
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO text_presets (text, sort_order, created_at) VALUES (?, ?, ?)",
        (text, max_order + 1, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM text_presets WHERE id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    on_data_changed("text preset added")
    return dict(row)


def delete_text_preset(preset_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM text_presets WHERE id = ?", (preset_id,))
    conn.commit()
    conn.close()
    on_data_changed("text preset removed")


def save_push_subscription(endpoint: str, p256dh: str, auth: str):
    """INSERT OR REPLACE on endpoint (unique) -- re-subscribing the same
    device (e.g. after the browser rotates its push endpoint) just updates
    the existing row instead of accumulating stale duplicates."""
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO push_subscriptions (endpoint, p256dh, auth, created_at) VALUES (?, ?, ?, ?)",
        (endpoint, p256dh, auth, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def list_push_subscriptions() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM push_subscriptions").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def remove_push_subscription(endpoint: str):
    conn = get_conn()
    conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    conn.commit()
    conn.close()
