"""
models.py — Data model and normalization logic.

This file defines:
1. The `Trade` dataclass — our app's clean, internal representation of one
   filled trade.
2. `normalize_order()` — converts a raw order dict (from Robinhood MCP or
   the mock data source) into a `Trade`.

Keeping normalization here (separate from the database and sync code) means
that if Robinhood ever changes its field names, you only fix ONE function.
"""

import json
from dataclasses import dataclass
from typing import Optional


@dataclass
class Trade:
    """One filled trade, in our app's standard shape."""

    robinhood_order_id: str   # Robinhood's unique ID for the order (used to dedupe)
    symbol: str               # Ticker, e.g. "AAPL"
    side: str                 # "buy" or "sell"
    quantity: float           # Number of shares filled
    average_price: float      # Average fill price per share
    status: str               # Order status, e.g. "filled"
    filled_at: Optional[str]  # ISO-8601 timestamp when the order filled
    created_at: Optional[str] # ISO-8601 timestamp when the order was placed
    asset_type: str           # "stock" for the MVP (options may appear later)
    fees: Optional[float]     # Regulatory fees on the order (often 0)
    placed_agent: Optional[str]  # Who placed it: "user", "recurring", "agentic"
    raw_json: str             # Full original order as JSON, kept for debugging


def _first(d: dict, *keys, default=None):
    """
    Return the first non-empty value found in `d` for any of `keys`.

    Different data sources name fields slightly differently
    (e.g. "average_price" vs "avg_price" vs "price"), so we check
    a few candidates in order.
    """
    for key in keys:
        value = d.get(key)
        if value not in (None, ""):
            return value
    return default


def normalize_order(raw: dict) -> Optional[Trade]:
    """
    Convert one raw order dict into a `Trade`.

    Returns None if the order is not a filled order (we only record fills)
    or if required fields are missing.
    """
    # --- 1. Only keep filled orders -------------------------------------
    status = str(_first(raw, "status", "state", default="")).lower()
    if status != "filled":
        return None

    # --- 2. Pull out each field, tolerating different key names ----------
    order_id = _first(raw, "id", "order_id", "robinhood_order_id")
    symbol = _first(raw, "symbol", "ticker", "instrument_symbol")
    side = str(_first(raw, "side", "direction", default="")).lower()

    quantity = _first(raw, "filled_quantity", "cumulative_quantity", "quantity", "qty")
    avg_price = _first(raw, "average_price", "avg_price", "average_fill_price", "price")

    filled_at = _first(raw, "filled_at", "last_transaction_at", "executed_at", "updated_at")
    created_at = _first(raw, "created_at", "submitted_at", "placed_at")
    asset_type = str(_first(raw, "asset_type", "instrument_type", default="stock")).lower()

    # Not required — an order missing either is still worth recording. Note
    # _first() skips "" but keeps 0, so a genuine zero fee survives.
    fees = _first(raw, "fees", "fee", "total_fees")
    placed_agent = _first(raw, "placed_agent", "source", "origin")

    # --- 3. Skip orders missing anything essential -----------------------
    if not order_id or not symbol or side not in ("buy", "sell"):
        return None
    if quantity is None or avg_price is None:
        return None

    return Trade(
        robinhood_order_id=str(order_id),
        symbol=str(symbol).upper(),
        side=side,
        quantity=float(quantity),
        average_price=float(avg_price),
        status=status,
        filled_at=filled_at,
        created_at=created_at,
        asset_type=asset_type,
        fees=None if fees is None else float(fees),
        placed_agent=None if placed_agent is None else str(placed_agent),
        raw_json=json.dumps(raw, default=str),
    )
