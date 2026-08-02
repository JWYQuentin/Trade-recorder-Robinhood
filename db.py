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


# Unlike `trades`, these two are SNAPSHOTS, not history. A position changes
# every time you trade or the market moves, so appending would pile up
# near-duplicate rows forever. Each sync replaces the table wholesale.
CREATE_POSITIONS_SQL = """
CREATE TABLE IF NOT EXISTS positions (
    symbol            TEXT PRIMARY KEY,
    quantity          REAL NOT NULL,
    average_buy_price REAL,
    last_price        REAL,
    price_source      TEXT,   -- which quote field the price came from
    as_of             TEXT    -- when this snapshot was taken
);
"""

# Realized P&L is immutable history, but Robinhood returns the complete set
# every time (span=all) and gives no stable per-row id — so replacing is both
# simpler and less fragile than inventing a dedupe key.
CREATE_REALIZED_PNL_SQL = """
CREATE TABLE IF NOT EXISTS realized_pnl (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    quantity      REAL,
    price         REAL,
    realized_gain REAL,
    as_of         TEXT
);
"""


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
    conn.execute(CREATE_POSITIONS_SQL)
    conn.execute(CREATE_REALIZED_PNL_SQL)
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


def replace_positions(conn: sqlite3.Connection, rows: List[dict], as_of: str) -> int:
    """
    Swap in a fresh positions snapshot. Returns how many rows were written.

    An EMPTY list is treated as "we learned nothing" and leaves the existing
    snapshot alone — a transient API hiccup shouldn't silently wipe your
    holdings and make the dashboard read as if you own nothing.
    """
    if not rows:
        return 0

    with conn:  # one transaction: never half-replaced
        conn.execute("DELETE FROM positions")
        conn.executemany(
            """
            INSERT INTO positions
                (symbol, quantity, average_buy_price, last_price,
                 price_source, as_of)
            VALUES (:symbol, :quantity, :average_buy_price, :last_price,
                    :price_source, :as_of)
            """,
            [{**r, "as_of": as_of} for r in rows],
        )
    return len(rows)


def replace_realized_pnl(conn: sqlite3.Connection, rows: List[dict], as_of: str) -> int:
    """Swap in a fresh realized-P&L set. Empty means leave it alone."""
    if not rows:
        return 0

    with conn:
        conn.execute("DELETE FROM realized_pnl")
        conn.executemany(
            """
            INSERT INTO realized_pnl
                (timestamp, symbol, quantity, price, realized_gain, as_of)
            VALUES (:timestamp, :symbol, :quantity, :price, :realized_gain,
                    :as_of)
            """,
            [{**r, "as_of": as_of} for r in rows],
        )
    return len(rows)


def get_positions(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Current holdings snapshot, largest position first."""
    return conn.execute(
        "SELECT * FROM positions ORDER BY quantity * last_price DESC"
    ).fetchall()


def get_realized_pnl(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Every realizing trade, most recent first."""
    return conn.execute(
        "SELECT * FROM realized_pnl ORDER BY timestamp DESC"
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
