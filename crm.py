"""
Local CRM data layer for URTO. SQLite-backed, single file on disk
(urto_crm.db), never synced anywhere — this module owns all client and
follow-up data and schedule math.
"""

import calendar
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "urto_crm.db"

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
    conn.commit()
    conn.close()


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
    return client_to_dict(row)


def delete_client(client_id):
    conn = get_conn()
    conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    conn.commit()
    conn.close()


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
                    "client_id": c["id"], "client_name": c["name"],
                    "date": d.isoformat(), "status": status,
                })
            d -= timedelta(days=interval)

        # Walk forward from the anchor for indefinitely-recurring future ones.
        d = anchor + timedelta(days=interval)
        while d <= month_end:
            events.append({
                "client_id": c["id"], "client_name": c["name"],
                "date": d.isoformat(), "status": "upcoming",
            })
            d += timedelta(days=interval)

    events.sort(key=lambda e: e["date"])
    return events
