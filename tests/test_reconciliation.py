"""
The correctness check for the whole Phase 1 reconstruction.

Every performance metric is computed by replaying trades forward from
`HISTORY_STARTS` and pricing the result. If that replay doesn't reproduce the
share counts Robinhood itself reports, every number downstream is quietly
wrong — so this test asserts it directly against the live database.

Run with:  pytest tests/ -v
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db

# Share counts are floats built from many fractional fills; compare with a
# tolerance well below a meaningful fraction of a share.
TOLERANCE = 1e-6


@pytest.fixture(scope="module")
def conn():
    c = db.get_connection()
    yield c
    c.close()


def replay_holdings(conn) -> dict:
    """
    Reconstruct current share counts from trade history alone, seeded with
    the pre-history opening balances.
    """
    held = {r["symbol"]: r["quantity"] for r in db.get_opening_balances(conn)}
    for row in conn.execute("SELECT symbol, side, quantity FROM trades"):
        delta = row["quantity"] if row["side"] == "buy" else -row["quantity"]
        held[row["symbol"]] = held.get(row["symbol"], 0.0) + delta
    return held


def test_replay_matches_reported_positions(conn):
    """Replaying every trade must reproduce Robinhood's own share counts."""
    replayed = replay_holdings(conn)
    actual = {r["symbol"]: r["quantity"] for r in db.get_positions(conn)}

    mismatches = []
    for symbol in sorted(set(replayed) | set(actual)):
        want = actual.get(symbol, 0.0)
        got = replayed.get(symbol, 0.0)
        if abs(got - want) > TOLERANCE:
            mismatches.append(f"{symbol}: replayed {got:.6f}, reported {want:.6f}")

    assert not mismatches, "trade replay disagrees with reported positions:\n" + \
        "\n".join(mismatches)


def test_no_position_is_negative(conn):
    """
    A negative holding means shares were sold that the history never bought —
    the signature of a position opened before HISTORY_STARTS that has no
    opening_balances row.
    """
    negative = {s: q for s, q in replay_holdings(conn).items() if q < -TOLERANCE}
    assert not negative, (
        f"negative holdings imply missing opening balances: {negative}")


def test_opening_balances_are_only_pre_history(conn):
    """Opening balances exist to cover the history floor, nothing else."""
    for row in db.get_opening_balances(conn):
        assert row["as_of"] == db.HISTORY_STARTS
        assert row["quantity"] > 0
        assert row["note"], f"{row['symbol']} needs a note explaining why it exists"


def test_price_history_covers_every_traded_symbol(conn):
    """
    Valuing the portfolio on a past date needs prices for symbols since sold,
    not just those currently held.
    """
    traded = {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM trades")}
    priced = set(db.price_coverage(conn))
    missing = traded - priced
    assert not missing, f"no price history for: {sorted(missing)}"


def test_price_history_reaches_back_to_the_floor(conn):
    """Prices must start at/near HISTORY_STARTS, or early valuations are blank."""
    coverage = db.price_coverage(conn)
    if not coverage:
        pytest.skip("no price history synced yet")
    # Allow a few days' slack: HISTORY_STARTS may fall on a weekend/holiday,
    # and a symbol first traded later legitimately starts later.
    latest_start = max(lo for lo, _hi, _n in coverage.values())
    assert latest_start <= "2025-10-01", (
        f"price history starts too late ({latest_start})")


def test_snapshot_archive_agrees_with_current_positions(conn):
    """
    The newest archived snapshot must match the live positions table — they
    are written from the same fetch, so divergence means a write bug.
    """
    newest = conn.execute(
        "SELECT MAX(snapshot_at) FROM position_snapshots").fetchone()[0]
    if newest is None:
        pytest.skip("no snapshot archived yet")

    archived = {r["symbol"]: r["quantity"] for r in conn.execute(
        "SELECT symbol, quantity FROM position_snapshots WHERE snapshot_at = ?",
        (newest,))}
    current = {r["symbol"]: r["quantity"] for r in db.get_positions(conn)}
    assert archived == pytest.approx(current, abs=TOLERANCE)


def test_reconstructed_value_matches_reported_equity(conn):
    """
    Pricing the replayed holdings must land close to the equity value
    Robinhood reports.

    A tolerance is unavoidable and is NOT slack for a broken replay: daily
    bars carry the previous session's close, while positions.last_price is a
    live intraday quote, so the two are legitimately a day apart. Anything
    beyond a few percent means the reconstruction itself is wrong.
    """
    row = conn.execute(
        "SELECT equity_value FROM portfolio_snapshots ORDER BY snapshot_at DESC"
    ).fetchone()
    if row is None or not row[0]:
        pytest.skip("no portfolio snapshot recorded yet")
    reported = row[0]

    latest_close = {
        r["symbol"]: r["close"]
        for r in conn.execute(
            """
            SELECT symbol, close FROM daily_prices
            WHERE (symbol, date) IN
                (SELECT symbol, MAX(date) FROM daily_prices GROUP BY symbol)
            """
        )
    }

    value = sum(
        qty * latest_close[symbol]
        for symbol, qty in replay_holdings(conn).items()
        if abs(qty) > TOLERANCE and symbol in latest_close
    )

    drift = abs(value - reported) / reported
    assert drift < 0.05, (
        f"reconstructed ${value:,.2f} vs reported ${reported:,.2f} "
        f"({drift:.1%} apart) — larger than one session's price movement")
