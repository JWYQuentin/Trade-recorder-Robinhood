"""
db.py — SQLite database layer.

Responsibilities:
1. Create/open the local `trades.db` file.
2. Create the `trades` table if it doesn't exist.
3. Insert trades IDEMPOTENTLY (same robinhood_order_id is never duplicated).
4. Read trades back out for the dashboard.

SQLite ships with Python — no server or install needed. The database is a
single file (`trades.db`) that lives next to this script.
"""

import sqlite3
from pathlib import Path
from typing import List, Optional

from models import Trade

# The database file lives in the same folder as this script.
DB_PATH = Path(__file__).parent / "trades.db"

# UNIQUE on robinhood_order_id is what makes inserts idempotent:
# SQLite itself refuses to store the same order twice.
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    robinhood_order_id TEXT NOT NULL UNIQUE,
    symbol             TEXT NOT NULL,
    side               TEXT NOT NULL,
    quantity           REAL NOT NULL,
    average_price      REAL NOT NULL,
    status             TEXT NOT NULL,
    filled_at          TEXT,
    created_at         TEXT,
    asset_type         TEXT,
    fees               REAL,
    placed_agent       TEXT,
    raw_json           TEXT
);
"""

# Columns added after the first version of this table shipped. SQLite has no
# "ADD COLUMN IF NOT EXISTS", so _migrate() checks before adding. The value for
# each already lives inside raw_json, so existing rows can be backfilled
# without re-syncing from Robinhood.
#
# Note: ALTER TABLE appends, so a MIGRATED database has these two after
# raw_json while a FRESH one (from CREATE_TABLE_SQL) has them before it.
# Nothing depends on column order — every read is by name — but don't be
# surprised by the difference when comparing two databases.
ADDED_COLUMNS = {
    "fees": "REAL",
    "placed_agent": "TEXT",
}


def _migrate(conn: sqlite3.Connection) -> None:
    """
    Bring an older trades.db up to the current schema.

    Idempotent, like CREATE TABLE IF NOT EXISTS above: adding a column and
    backfilling it happen together, exactly once, the first time a database
    written by an older version is opened.
    """
    import json

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}

    for column, sql_type in ADDED_COLUMNS.items():
        if column in existing:
            continue

        conn.execute(f"ALTER TABLE trades ADD COLUMN {column} {sql_type}")

        # Backfill from the raw payload we already stored.
        updates = []
        for row in conn.execute(
            "SELECT id, raw_json FROM trades WHERE raw_json IS NOT NULL"
        ):
            raw = json.loads(row["raw_json"])
            value = raw.get("fees" if column == "fees" else "placed_agent")
            if value is None:
                continue
            updates.append((float(value) if column == "fees" else str(value), row["id"]))

        if updates:
            conn.executemany(
                f"UPDATE trades SET {column} = ? WHERE id = ?", updates
            )
        conn.commit()
        print(f"db: added column '{column}', backfilled {len(updates)} row(s)")


def get_connection() -> sqlite3.Connection:
    """Open a connection and make sure the schema is current."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # lets us access columns by name
    conn.execute(CREATE_TABLE_SQL)
    _migrate(conn)
    return conn


def insert_trade(conn: sqlite3.Connection, trade: Trade) -> bool:
    """
    Insert one trade. Returns True if inserted, False if it already existed.

    "INSERT OR IGNORE" tells SQLite: if the UNIQUE constraint on
    robinhood_order_id would be violated, silently skip the row.
    """
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO trades
            (robinhood_order_id, symbol, side, quantity, average_price,
             status, filled_at, created_at, asset_type, fees, placed_agent,
             raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trade.robinhood_order_id,
            trade.symbol,
            trade.side,
            trade.quantity,
            trade.average_price,
            trade.status,
            trade.filled_at,
            trade.created_at,
            trade.asset_type,
            trade.fees,
            trade.placed_agent,
            trade.raw_json,
        ),
    )
    conn.commit()
    # rowcount is 1 if a row was inserted, 0 if it was ignored (duplicate).
    return cursor.rowcount == 1


def get_all_trades(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Return every synced trade, newest fills first."""
    return conn.execute(
        "SELECT * FROM trades ORDER BY filled_at DESC"
    ).fetchall()


def newest_filled_at(conn: sqlite3.Connection) -> Optional[str]:
    """
    The most recent fill timestamp we've stored, or None if empty.

    Used by the sync to work out how far back it needs to ask Robinhood,
    so a scheduled run doesn't re-walk the entire history every time.
    """
    row = conn.execute("SELECT MAX(filled_at) FROM trades").fetchone()
    return row[0] if row and row[0] else None


if __name__ == "__main__":
    # Running `python db.py` directly just creates the database file.
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    print(f"Database ready at {DB_PATH} ({count} trades stored).")
