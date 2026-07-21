"""SQLite data layer for the wardrobe app.

Single-user, local. Auto-creates the schema and seeds Mario's starting
wardrobe + the "Office" collection on first run.
"""

import os
import sqlite3
from itertools import product

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wardrobe.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT '',
    color       TEXT NOT NULL DEFAULT '',
    color_hex   TEXT NOT NULL DEFAULT '',
    brand       TEXT NOT NULL DEFAULT '',
    material    TEXT NOT NULL DEFAULT '',
    temp_min    INTEGER,
    temp_max    INTEGER,
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS collections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    temp_min    INTEGER,
    temp_max    INTEGER,
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS outfits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL DEFAULT '',
    collection_id INTEGER,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS outfit_items (
    outfit_id INTEGER NOT NULL,
    item_id   INTEGER NOT NULL,
    PRIMARY KEY (outfit_id, item_id),
    FOREIGN KEY (outfit_id) REFERENCES outfits(id) ON DELETE CASCADE,
    FOREIGN KEY (item_id)   REFERENCES items(id)   ON DELETE CASCADE
);
"""


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    # Seed only if the wardrobe is empty.
    if conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0:
        _seed(conn)
    conn.close()


# --- Seed data -----------------------------------------------------------

# Starting wardrobe. color_hex is an approximate swatch for the UI.
SEED_ITEMS = [
    # name, category, color, color_hex, brand, material, temp_min, temp_max, notes
    ("Grey Slacks", "Pants", "Grey", "#8a8d91", "", "", 60, 78, ""),
    ("Khaki Slacks", "Pants", "Khaki", "#c3a877", "", "", 62, 82, ""),
    ("Long Sleeve Polo — Dark Green", "Long Sleeve Polo", "Dark Green",
     "#1f4d34", "Ralph Lauren", "", 60, 74, ""),
    ("Long Sleeve Polo — Light Green", "Long Sleeve Polo", "Light Green",
     "#5a8f5f", "Ralph Lauren", "", 60, 74, ""),
    ("Long Sleeve Polo — Cream", "Long Sleeve Polo", "Cream",
     "#efe6cf", "Ralph Lauren", "", 60, 74, ""),
    ("Long Sleeve Polo — Blue", "Long Sleeve Polo", "Blue",
     "#3a6ea5", "Ralph Lauren", "", 60, 74, ""),
    ("Quarter Zip — Light Red", "Quarter Zip", "Light Red",
     "#d1595c", "Ralph Lauren", "", 55, 70, ""),
    ("Long Sleeve Polo Shirt — Dark Wine Red", "Long Sleeve Polo",
     "Dark Wine Red", "#6e2233", "J.Crew", "", 58, 72, ""),
]

# Names of the four Ralph Lauren long-sleeve polos that belong in "Office".
OFFICE_SHIRT_NAMES = [
    "Long Sleeve Polo — Dark Green",
    "Long Sleeve Polo — Light Green",
    "Long Sleeve Polo — Cream",
    "Long Sleeve Polo — Blue",
]
OFFICE_PANT_NAMES = ["Grey Slacks", "Khaki Slacks"]


def _seed(conn):
    ids = {}
    for row in SEED_ITEMS:
        cur = conn.execute(
            """INSERT INTO items
               (name, category, color, color_hex, brand, material,
                temp_min, temp_max, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            row,
        )
        ids[row[0]] = cur.lastrowid

    # "Office" collection: every pants x every RL long-sleeve polo.
    cur = conn.execute(
        "INSERT INTO collections (name, temp_min, temp_max) VALUES (?, ?, ?)",
        ("Office", 65, 73),
    )
    coll_id = cur.lastrowid

    for pant, shirt in product(OFFICE_PANT_NAMES, OFFICE_SHIRT_NAMES):
        pant_id, shirt_id = ids[pant], ids[shirt]
        # Nice readable outfit name, e.g. "Grey Slacks + Dark Green Polo"
        shirt_color = shirt.split("—")[-1].strip()
        pant_short = pant.replace(" Slacks", "")
        oname = f"{pant_short} + {shirt_color}"
        oc = conn.execute(
            "INSERT INTO outfits (name, collection_id) VALUES (?, ?)",
            (oname, coll_id),
        )
        oid = oc.lastrowid
        conn.executemany(
            "INSERT INTO outfit_items (outfit_id, item_id) VALUES (?, ?)",
            [(oid, pant_id), (oid, shirt_id)],
        )

    conn.commit()


# --- Item queries --------------------------------------------------------

def list_items():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM items ORDER BY category, name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_item(data):
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO items
           (name, category, color, color_hex, brand, material,
            temp_min, temp_max, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data.get("name", "").strip(),
            data.get("category", "").strip(),
            data.get("color", "").strip(),
            data.get("color_hex", "").strip(),
            data.get("brand", "").strip(),
            data.get("material", "").strip(),
            _int_or_none(data.get("temp_min")),
            _int_or_none(data.get("temp_max")),
            data.get("notes", "").strip(),
        ),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def update_item(item_id, data):
    conn = get_conn()
    conn.execute(
        """UPDATE items SET
             name = ?, category = ?, color = ?, color_hex = ?, brand = ?,
             material = ?, temp_min = ?, temp_max = ?, notes = ?
           WHERE id = ?""",
        (
            data.get("name", "").strip(),
            data.get("category", "").strip(),
            data.get("color", "").strip(),
            data.get("color_hex", "").strip(),
            data.get("brand", "").strip(),
            data.get("material", "").strip(),
            _int_or_none(data.get("temp_min")),
            _int_or_none(data.get("temp_max")),
            data.get("notes", "").strip(),
            item_id,
        ),
    )
    conn.commit()
    conn.close()


def delete_item(item_id):
    conn = get_conn()
    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()


# --- Collection / outfit queries -----------------------------------------

def list_collections():
    conn = get_conn()
    colls = [dict(r) for r in conn.execute(
        "SELECT * FROM collections ORDER BY created_at").fetchall()]
    for c in colls:
        c["outfits"] = _outfits_for_collection(conn, c["id"])
    conn.close()
    return colls


def _outfits_for_collection(conn, coll_id):
    outfits = [dict(r) for r in conn.execute(
        "SELECT * FROM outfits WHERE collection_id = ? ORDER BY id",
        (coll_id,)).fetchall()]
    for o in outfits:
        o["items"] = [dict(r) for r in conn.execute(
            """SELECT i.* FROM items i
               JOIN outfit_items oi ON oi.item_id = i.id
               WHERE oi.outfit_id = ?
               ORDER BY i.category""",
            (o["id"],)).fetchall()]
    return outfits


def add_collection(data):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO collections (name, temp_min, temp_max, notes) VALUES (?, ?, ?, ?)",
        (
            data.get("name", "").strip(),
            _int_or_none(data.get("temp_min")),
            _int_or_none(data.get("temp_max")),
            data.get("notes", "").strip(),
        ),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def update_collection(coll_id, data):
    conn = get_conn()
    conn.execute(
        "UPDATE collections SET name = ?, temp_min = ?, temp_max = ?, notes = ? WHERE id = ?",
        (
            data.get("name", "").strip(),
            _int_or_none(data.get("temp_min")),
            _int_or_none(data.get("temp_max")),
            data.get("notes", "").strip(),
            coll_id,
        ),
    )
    conn.commit()
    conn.close()


def delete_collection(coll_id):
    conn = get_conn()
    conn.execute("DELETE FROM collections WHERE id = ?", (coll_id,))
    conn.commit()
    conn.close()


def add_outfit(data):
    """Create an outfit from a list of item ids, optionally in a collection."""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO outfits (name, collection_id) VALUES (?, ?)",
        (data.get("name", "").strip(), _int_or_none(data.get("collection_id"))),
    )
    oid = cur.lastrowid
    item_ids = data.get("item_ids", []) or []
    conn.executemany(
        "INSERT OR IGNORE INTO outfit_items (outfit_id, item_id) VALUES (?, ?)",
        [(oid, int(i)) for i in item_ids],
    )
    conn.commit()
    conn.close()
    return oid


def delete_outfit(outfit_id):
    conn = get_conn()
    conn.execute("DELETE FROM outfits WHERE id = ?", (outfit_id,))
    conn.commit()
    conn.close()


def _int_or_none(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None
