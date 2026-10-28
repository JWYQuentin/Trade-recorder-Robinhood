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


# Append-only archive of every positions snapshot ever taken. `positions`
# above stays the "what do I hold right now" table; this one is the history
# that makes performance-over-time possible at all.
#
# Written in the same transaction as the positions swap, so the two can never
# disagree about what a given sync saw.
CREATE_POSITION_SNAPSHOTS_SQL = """
CREATE TABLE IF NOT EXISTS position_snapshots (
    snapshot_at       TEXT NOT NULL,   -- the sync's as_of, shared by the whole batch
    symbol            TEXT NOT NULL,
    quantity          REAL NOT NULL,
    average_buy_price REAL,
    last_price        REAL,
    price_source      TEXT,
    PRIMARY KEY (snapshot_at, symbol)
);
"""

# Daily closes, the raw material for valuing the portfolio on any past date.
# Backfilled once from get_equity_historicals and topped up each sync.
#
# `close` is stored as REAL though Robinhood sends strings with absurd
# precision ("500.465000") — _as_float() in the sync handles the conversion.
CREATE_DAILY_PRICES_SQL = """
CREATE TABLE IF NOT EXISTS daily_prices (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,   -- YYYY-MM-DD, the bar's begins_at date
    close  REAL NOT NULL,
    PRIMARY KEY (symbol, date)
);
"""

# Account-level totals per sync: equity, cash, and the sum. Appended, never
# replaced — this is the series that shows idle cash dragging on returns.
#
# There is no way to recover this for the past: the MCP exposes no historical
# portfolio or transfer endpoint (checked 2026-09-01, all 67 tools). So the
# cash series necessarily begins the day this table does, while the equity
# series can be reconstructed backwards from daily_prices.
CREATE_PORTFOLIO_SNAPSHOTS_SQL = """
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    snapshot_at  TEXT PRIMARY KEY,
    total_value  REAL,
    equity_value REAL,
    cash         REAL
);
"""

# Robinhood's own per-lot cost basis for open positions. Replaced each sync
# like `positions`, and for the same reason: lots are consumed by sells, so
# appending would accumulate lots that no longer exist.
#
# Preferred over reconstructing basis from trade history because it is what
# Robinhood will actually report on a tax form, and it carries the short/long
# term split, which a naive replay cannot know.
CREATE_TAX_LOTS_SQL = """
CREATE TABLE IF NOT EXISTS tax_lots (
    open_lot_id   TEXT PRIMARY KEY,
    symbol        TEXT NOT NULL,
    quantity      REAL,
    cost_per_share REAL,
    cost_basis    REAL,
    open_date     TEXT,
    term          TEXT,   -- "st" (short) or "lt" (long)
    as_of         TEXT
);
"""

# Shares held on 2025-09-22 that no trade in this database accounts for.
#
# Robinhood serves no order history before that date (verified with an
# unbounded query: hundreds of filled orders, none earlier) and no tax lot opens
# before it either, so these positions cannot be recovered from any API.
# Replaying trades alone therefore drives these two symbols negative.
#
# The quantities below are exactly what the replay is short, which is the
# only defensible reconstruction available: we know the shares existed
# because they were sold. Cost basis is genuinely unknown and left NULL —
# dollar P&L for these symbols comes from Robinhood's realized_pnl and is
# correct; percentage returns are not, and the dashboard marks them.
#
# Together they are well under 1% of the account's equity. If Robinhood ever
# widens its history window, delete this table and the replay stands alone.
CREATE_OPENING_BALANCES_SQL = """
CREATE TABLE IF NOT EXISTS opening_balances (
    symbol     TEXT PRIMARY KEY,
    quantity   REAL NOT NULL,
    as_of      TEXT NOT NULL,
    cost_basis REAL,          -- NULL: unknowable, predates available history
    note       TEXT
);
"""

# The history floor. Everything before this date is outside what Robinhood
# will serve; analytics should not imply a record that starts earlier.
HISTORY_STARTS = "2025-09-22"

# What "the market" means for the benchmark comparison. SPY rather than VOO or
# QQQ because it is the most widely understood proxy for US large-cap, and the
# question being answered is "would I have done better doing nothing clever?".
#
# It is stored in daily_prices like any other symbol — it needs no separate
# table, and keeping it there means the same price-coverage checks apply. It is
# excluded from holdings replay by never appearing in `trades`.
BENCHMARK_SYMBOL = "SPY"

OPENING_BALANCE_ROWS = [
    ("SPCX", 1.0, HISTORY_STARTS, None,
     "Sold with no recorded buy; position opened before history."),
    ("TSLA", 0.016904, HISTORY_STARTS, None,
     "Small holding carried in from before history."),
]


def _seed_opening_balances(conn: sqlite3.Connection) -> None:
    """
    Insert the pre-history holdings once. Idempotent, and never overwrites:
    if a future Robinhood backfill makes these unnecessary, deleting the rows
    is enough — this won't put them back unless the table is empty.
    """
    existing = conn.execute("SELECT COUNT(*) FROM opening_balances").fetchone()[0]
    if existing:
        return
    conn.executemany(
        """
        INSERT OR IGNORE INTO opening_balances
            (symbol, quantity, as_of, cost_basis, note)
        VALUES (?, ?, ?, ?, ?)
        """,
        OPENING_BALANCE_ROWS,
    )
    conn.commit()


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
    conn.execute(CREATE_POSITION_SNAPSHOTS_SQL)
    conn.execute(CREATE_DAILY_PRICES_SQL)
    conn.execute(CREATE_PORTFOLIO_SNAPSHOTS_SQL)
    conn.execute(CREATE_TAX_LOTS_SQL)
    conn.execute(CREATE_OPENING_BALANCES_SQL)
    _migrate(conn)
    _seed_opening_balances(conn)
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


# =====================================================================
# Phase 1 additions: history that makes performance measurable
# =====================================================================

def archive_positions(conn: sqlite3.Connection, rows: List[dict], as_of: str) -> int:
    """
    Append this sync's positions to the permanent archive.

    Unlike replace_positions(), nothing is deleted — this is the series that
    later answers "what was I holding, and what was it worth, back then".
    INSERT OR REPLACE keeps a re-run of the same sync idempotent.
    """
    if not rows:
        return 0
    with conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO position_snapshots
                (snapshot_at, symbol, quantity, average_buy_price,
                 last_price, price_source)
            VALUES (:as_of, :symbol, :quantity, :average_buy_price,
                    :last_price, :price_source)
            """,
            [{**r, "as_of": as_of} for r in rows],
        )
    return len(rows)


def upsert_daily_prices(conn: sqlite3.Connection, rows: List[dict]) -> int:
    """
    Store daily closes. Existing (symbol, date) rows are overwritten, so a
    re-run of the backfill corrects rather than duplicates.
    """
    if not rows:
        return 0
    with conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO daily_prices (symbol, date, close)
            VALUES (:symbol, :date, :close)
            """,
            rows,
        )
    return len(rows)


def record_portfolio(conn: sqlite3.Connection, totals: Optional[dict],
                     as_of: str) -> int:
    """Append one account-level row (equity / cash / total) for this sync."""
    if not totals:
        return 0
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO portfolio_snapshots
                (snapshot_at, total_value, equity_value, cash)
            VALUES (:as_of, :total_value, :equity_value, :cash)
            """,
            {**totals, "as_of": as_of},
        )
    return 1


def replace_tax_lots(conn: sqlite3.Connection, rows: List[dict], as_of: str) -> int:
    """Swap in a fresh set of open tax lots. Empty means leave it alone."""
    if not rows:
        return 0
    with conn:
        conn.execute("DELETE FROM tax_lots")
        conn.executemany(
            """
            INSERT OR REPLACE INTO tax_lots
                (open_lot_id, symbol, quantity, cost_per_share, cost_basis,
                 open_date, term, as_of)
            VALUES (:open_lot_id, :symbol, :quantity, :cost_per_share,
                    :cost_basis, :open_date, :term, :as_of)
            """,
            [{**r, "as_of": as_of} for r in rows],
        )
    return len(rows)


def get_daily_prices(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Every stored daily close, oldest first."""
    return conn.execute(
        "SELECT symbol, date, close FROM daily_prices ORDER BY date, symbol"
    ).fetchall()


def get_opening_balances(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Pre-history holdings the trade replay can't account for."""
    return conn.execute("SELECT * FROM opening_balances").fetchall()


def get_portfolio_snapshots(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Account-level equity/cash series, oldest first."""
    return conn.execute(
        "SELECT * FROM portfolio_snapshots ORDER BY snapshot_at"
    ).fetchall()


def get_tax_lots(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Open tax lots, newest open_date first."""
    return conn.execute(
        "SELECT * FROM tax_lots ORDER BY symbol, open_date DESC"
    ).fetchall()


def prices_start_from(conn: sqlite3.Connection, symbols: List[str],
                      floor: str) -> str:
    """
    The date a price fetch should start from to fill everything missing.

    A full backfill is only needed once. After that the newest stored close
    is the sensible lower bound — but only if EVERY symbol reaches that far,
    since a newly traded symbol has no history at all and would be silently
    left with a gap. Any shortfall drops back to the floor.
    """
    coverage = price_coverage(conn)
    if not coverage or any(s not in coverage for s in symbols):
        return floor

    # The oldest "newest close" across symbols: fetching from here tops up
    # whichever symbol has fallen furthest behind.
    oldest_latest = min(hi for _lo, hi, _n in coverage.values())
    return max(floor, oldest_latest)


def price_coverage(conn: sqlite3.Connection) -> dict:
    """Per-symbol (first_date, last_date, n) for the stored price history."""
    return {
        r["symbol"]: (r["lo"], r["hi"], r["n"])
        for r in conn.execute(
            """
            SELECT symbol, MIN(date) lo, MAX(date) hi, COUNT(*) n
            FROM daily_prices GROUP BY symbol
            """
        )
    }


if __name__ == "__main__":
    # Running `python db.py` directly just creates the database file.
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    print(f"Database ready at {DB_PATH} ({count} trades stored).")
