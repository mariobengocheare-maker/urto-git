"""
Local CRM data layer for URTO. SQLite-backed, single file on disk
(urto_crm.db), never synced anywhere — this module owns all client and
follow-up data and schedule math.
"""

import calendar
import os
import shutil
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "urto_crm.db"

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
_last_backup = {"at": None, "path": None}


def resolve_backup_dir() -> Path:
    onedrive = os.environ.get("OneDriveConsumer") or os.environ.get("OneDrive")
    base = Path(onedrive) / "URTO Backups" if onedrive else Path(__file__).parent / "backups"
    base.mkdir(parents=True, exist_ok=True)
    return base


def mark_dirty():
    global _dirty
    _dirty = True


def _write_backup(reason, snapshot: bool, keep=30) -> dict:
    """Copy the live DB to the backup dir. Always refreshes the live mirror;
    when snapshot=True also writes a timestamped copy and prunes old ones.
    Copying a plain (non-WAL) sqlite file with no open write transaction is a
    consistent, safe file copy."""
    global _dirty, _last_backup
    with _backup_lock:
        if not DB_PATH.exists():
            return _last_backup
        backup_dir = resolve_backup_dir()

        mirror = backup_dir / LATEST_NAME
        shutil.copy2(DB_PATH, mirror)
        dest = mirror

        if snapshot:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = backup_dir / f"urto_crm_{stamp}.db"
            shutil.copy2(DB_PATH, dest)
            snaps = sorted(backup_dir.glob("urto_crm_[0-9]*.db"))
            for old in snaps[:-keep]:
                old.unlink(missing_ok=True)

        _dirty = False
        _last_backup = {"at": datetime.now().isoformat(timespec="seconds"), "path": str(dest), "reason": reason}
        return _last_backup


def backup_now(reason="manual", keep=30) -> dict:
    """Full backup: refresh the live mirror and write a timestamped snapshot."""
    return _write_backup(reason, snapshot=True, keep=keep)


def backup_instant(reason="save") -> dict:
    """Instant backup after a data change: refresh the live mirror only, so the
    current state (including deletions) is on OneDrive immediately without
    churning through the capped snapshot history on every single edit."""
    return _write_backup(reason, snapshot=False)


def on_data_changed(reason="save"):
    """Called after any CRM mutation: mirror to OneDrive right away, and flag
    the DB so the slow watcher also lays down a timestamped history snapshot."""
    mark_dirty()
    try:
        backup_instant(reason)
    except Exception:
        # A backup failure (e.g. OneDrive folder briefly locked) must never
        # break the actual save — the dirty flag means the watcher retries.
        pass


def backup_if_dirty():
    if _dirty:
        backup_now("auto")


def get_backup_status() -> dict:
    onedrive = os.environ.get("OneDriveConsumer") or os.environ.get("OneDrive")
    return {
        "backup_dir": str(resolve_backup_dir()),
        "onedrive_detected": bool(onedrive),
        "last_backup_at": _last_backup["at"],
        "last_backup_path": _last_backup["path"],
    }

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
            next_followup_date TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lookup_stats (
            week_start TEXT PRIMARY KEY,
            count INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


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


def client_to_dict(row) -> dict:
    d = dict(row)
    today = date.today()
    if d.get("next_followup_date"):
        due = date.fromisoformat(d["next_followup_date"])
        d["followup_status"] = "overdue" if due < today else ("due_today" if due == today else "upcoming")
    else:
        d["followup_status"] = None
    return d


def create_client(name, phone, contact_info, address, frequency_key, custom_amount, custom_unit) -> dict:
    label, interval_days = resolve_interval_days(frequency_key, custom_amount, custom_unit)
    created_at = date.today().isoformat()
    next_followup_date = None
    if interval_days:
        next_followup_date = (date.today() + timedelta(days=interval_days)).isoformat()

    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO clients
           (name, phone, contact_info, address, created_at, frequency_label, interval_days, next_followup_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, phone, contact_info, address, created_at, label, interval_days, next_followup_date),
    )
    conn.commit()
    client_id = cur.lastrowid
    row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    conn.close()
    on_data_changed("client added")
    return client_to_dict(row)


def list_clients() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM clients ORDER BY name COLLATE NOCASE").fetchall()
    conn.close()
    return [client_to_dict(r) for r in rows]


def get_client(client_id) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    conn.close()
    return client_to_dict(row) if row else None


def update_client(client_id, name, phone, contact_info, address, frequency_key, custom_amount, custom_unit) -> dict:
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
           clients.address AS client_address
    FROM events LEFT JOIN clients ON clients.id = events.client_id
"""


def _event_to_dict(row) -> dict:
    d = dict(row)
    d["kind"] = "manual"
    return d


def create_event(client_id, title, date_str, time_str, notes) -> dict:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO events (client_id, title, date, time, notes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (client_id or None, title, date_str, time_str or None, notes or "", datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    row = conn.execute(EVENT_JOIN_SELECT + " WHERE events.id = ?", (cur.lastrowid,)).fetchone()
    conn.close()
    on_data_changed("event added")
    return _event_to_dict(row)


def update_event(event_id, client_id, title, date_str, time_str, notes) -> dict:
    conn = get_conn()
    existing = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not existing:
        conn.close()
        return None
    conn.execute(
        "UPDATE events SET client_id=?, title=?, date=?, time=?, notes=? WHERE id=?",
        (client_id or None, title, date_str, time_str or None, notes or "", event_id),
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

    # Manually-added events (the "click a day to add an event" feature) are
    # merged in alongside the automatic occurrences above, not stored as
    # part of the recurring schedule math.
    for e in list_events_for_month(year, month):
        events.append({
            "kind": "manual", "event_id": e["id"],
            "client_id": e["client_id"], "client_name": e.get("client_name"),
            "date": e["date"], "time": e["time"], "title": e["title"],
            "notes": e.get("notes") or "", "status": "manual",
        })

    events.sort(key=lambda e: (e["date"], e.get("time") or ""))
    return events
