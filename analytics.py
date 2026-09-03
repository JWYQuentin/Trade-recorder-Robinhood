"""
analytics.py — the metrics engine.

Pure functions over DataFrames: no I/O, no Streamlit, no database. That is
deliberate and worth preserving — this is the one part of the project where a
silent bug produces confidently wrong numbers rather than an obvious crash,
so it has to stay testable in isolation.

The inputs all come from tables built in Phase 1:
    trades           -> cash flows and the share replay
    daily_prices     -> what the holdings were worth on any past date
    opening_balances -> shares held before the history floor
    realized_pnl     -> booked gains, signed (losses are negative)

VOCABULARY, because these are easy to conflate:

  Realized P&L    money already received on closed positions. Net: gains
                  minus losses, not a sum of magnitudes.
  Unrealized P&L  market value minus cost basis on what is still held. A
                  daily-moving estimate, never added to realized.
  TWR             time-weighted return. Neutralises deposit timing, so it
                  measures the PICKS. This is what a fund quotes.
  MWR (XIRR)      money-weighted return. Weights by how much was invested
                  when, so it measures YOUR OUTCOME.

TWR and MWR answer different questions and will disagree; showing both is
the point. When MWR trails TWR, the timing of contributions hurt.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Optional

import numpy as np
import pandas as pd

# A year, for annualising. 365 rather than 252: cash flows land on calendar
# days, not trading days.
DAYS_PER_YEAR = 365.0


# =====================================================================
# Shared preparation
# =====================================================================

def _as_date(series: pd.Series) -> pd.Series:
    """ISO-8601 strings (mixed offsets, sometimes 'Z') -> YYYY-MM-DD string.

    Dates are kept as strings, not date objects, throughout. They are only
    ever used as index labels and for sorting, both of which ISO strings do
    correctly, and it keeps the trade calendar and the price calendar the
    same dtype — mixing the two makes an index union unsortable.
    """
    return pd.to_datetime(series, format="ISO8601", utc=True).dt.strftime(
        "%Y-%m-%d")


def _date_strings(values) -> pd.Index:
    """Normalise any date-ish index to sorted YYYY-MM-DD strings."""
    out = pd.Index([v.isoformat() if hasattr(v, "isoformat") else str(v)
                    for v in values], name="date")
    return out


def prepare_trades(trades: pd.DataFrame) -> pd.DataFrame:
    """
    Add the derived columns every metric needs.

    `signed_shares` is +qty on a buy, -qty on a sell.
    `cash_flow` is the money leaving your pocket: positive on a buy,
    negative on a sell (a sale returns cash to you). Fees always cost you,
    so they add to the outflow on both sides.
    """
    if trades.empty:
        return trades.assign(date=None, signed_shares=None, cash_flow=None,
                             gross=None)

    out = trades.copy()
    out["date"] = _as_date(out["filled_at"])
    is_buy = out["side"] == "buy"
    out["gross"] = out["quantity"] * out["average_price"]
    out["signed_shares"] = out["quantity"].where(is_buy, -out["quantity"])
    fees = out["fees"].fillna(0) if "fees" in out else 0
    out["cash_flow"] = out["gross"].where(is_buy, -out["gross"]) + fees
    return out


def holdings_over_time(trades: pd.DataFrame, prices: pd.DataFrame,
                       opening: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """
    Shares held per symbol on every trading day in `prices`.

    Built by cumulative-summing signed shares onto the price calendar. Two
    details matter and are easy to get wrong:

    1. Symbols sold to zero must STAY zero rather than carrying phantom
       shares forward — handled by summing per (date, symbol) before the
       cumsum, so a fully-closing sell nets the running total to 0.
    2. Positions opened before the history floor have no buy to replay, so
       `opening` seeds them; without it those symbols go negative.
    """
    if prices.empty:
        return pd.DataFrame()

    calendar = _date_strings(prices["date"].unique()).unique().sort_values()
    prepared = prepare_trades(trades)
    if prepared.empty:
        return pd.DataFrame(index=calendar)

    daily = (prepared.groupby(["date", "symbol"])["signed_shares"].sum()
                     .unstack(fill_value=0.0))
    daily.index = _date_strings(daily.index)
    # Trades can fall on days the price calendar lacks (and vice versa);
    # union then reindex so nothing is dropped before the cumsum.
    daily = daily.reindex(calendar.union(daily.index), fill_value=0.0).sort_index()
    held = daily.cumsum()

    if opening is not None and not opening.empty:
        for _, row in opening.iterrows():
            symbol = row["symbol"]
            if symbol not in held.columns:
                held[symbol] = 0.0
            held[symbol] = held[symbol] + row["quantity"]

    # Floating-point dust: a position closed to exactly zero can land at
    # 1e-17 and render as a non-zero holding.
    held[held.abs() < 1e-9] = 0.0
    return held.reindex(calendar)


def portfolio_value(trades: pd.DataFrame, prices: pd.DataFrame,
                    opening: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """
    Daily market value of everything held.

    Returns a frame indexed by date with `value` and `shares_priced`, the
    latter being how many symbols actually had a price that day — useful for
    spotting a gap rather than silently valuing a position at zero.
    """
    held = holdings_over_time(trades, prices, opening)
    if held.empty:
        return pd.DataFrame(columns=["value", "shares_priced"])

    priced = prices.copy()
    priced["date"] = _date_strings(priced["date"])
    wide = (priced.pivot_table(index="date", columns="symbol", values="close",
                               aggfunc="last")
                  .sort_index().reindex(held.index).ffill())

    symbols = [s for s in held.columns if s in wide.columns]
    valued = held[symbols] * wide[symbols]
    return pd.DataFrame({
        "value": valued.sum(axis=1),
        "shares_priced": (held[symbols].abs() > 0).sum(axis=1),
    })


# =====================================================================
# Money-weighted return (XIRR)
# =====================================================================

def xnpv(rate: float, flows: list[tuple[date, float]]) -> float:
    """
    Net present value of dated cash flows at `rate`.

    Sign convention: money you PUT IN is negative (it left you), money that
    came back is positive. `xirr` handles the conversion from the trade-level
    convention, where a buy is a positive outflow.
    """
    if not flows:
        return 0.0
    t0 = flows[0][0]
    total = 0.0
    for when, amount in flows:
        years = (when - t0).days / DAYS_PER_YEAR
        # (1+rate) can go non-positive during the search; clamp rather than
        # raising, so the bisection can walk back out of the bad region.
        base = 1.0 + rate
        if base <= 0:
            return math.inf if amount > 0 else -math.inf
        total += amount / (base ** years)
    return total


def xirr(flows: list[tuple[date, float]], *, lo: float = -0.9999,
         hi: float = 10.0, tol: float = 1e-7, max_iter: int = 200
         ) -> Optional[float]:
    """
    Internal rate of return for irregularly dated flows, as an annual rate.

    Bisection rather than Newton-Raphson: slower, but it cannot diverge, and
    a wrong-but-plausible return number is far worse here than a slow one.
    Returns None when no rate can explain the flows — which is a real answer,
    not a failure, e.g. when every flow points the same way.

    `flows` uses the standard convention: negative = invested, positive =
    returned.
    """
    if len(flows) < 2:
        return None
    amounts = [a for _, a in flows]
    if not (any(a > 0 for a in amounts) and any(a < 0 for a in amounts)):
        # No sign change means no root: you cannot earn a return on flows
        # that only ever went one direction.
        return None

    ordered = sorted(flows, key=lambda f: f[0])
    f_lo, f_hi = xnpv(lo, ordered), xnpv(hi, ordered)
    if math.isnan(f_lo) or math.isnan(f_hi) or f_lo * f_hi > 0:
        return None

    for _ in range(max_iter):
        mid = (lo + hi) / 2
        f_mid = xnpv(mid, ordered)
        if abs(f_mid) < tol or (hi - lo) / 2 < tol:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def capital_weighted_holding_days(trades: pd.DataFrame,
                                  ending_date: str) -> Optional[float]:
    """
    How long the AVERAGE DOLLAR was actually invested.

    Annualising is only honest over a decent span, and the span that matters
    for MWR is not the calendar period — it's how long the money was in.
    Someone whose capital all arrived last quarter has a short effective
    horizon no matter how long the account has existed, and annualising a
    few months of a rising market produces a headline number that will not
    repeat. Callers should surface this next to any annualised MWR.
    """
    prepared = prepare_trades(trades)
    if prepared.empty:
        return None
    per_day = prepared.groupby("date")["cash_flow"].sum()
    total = per_day.sum()
    if total <= 0:
        return None

    end = datetime.strptime(ending_date, "%Y-%m-%d").date()
    weighted = sum(
        amount * (end - datetime.strptime(d, "%Y-%m-%d").date()).days
        for d, amount in per_day.items()
    )
    return weighted / total


def money_weighted_return(trades: pd.DataFrame, ending_value: float,
                          ending_date: Optional[date] = None) -> Optional[float]:
    """
    Annualised MWR on deployed capital.

    NAMING MATTERS: this is return on capital deployed INTO POSITIONS, not
    return on the money you transferred to the broker. Robinhood's MCP
    exposes no transfers endpoint, so idle cash is invisible here and the
    figure is flattered by however long money sat uninvested. Label it
    "return on deployed capital" wherever it is shown.

    The final holding is treated as a closing inflow — you would receive it
    if you liquidated today.
    """
    prepared = prepare_trades(trades)
    if prepared.empty:
        return None

    # Trade convention (buy = +outflow) -> XIRR convention (invested = -).
    # Dates travel as strings elsewhere; XIRR needs real dates to measure
    # elapsed time, so convert at the boundary.
    per_day = prepared.groupby("date")["cash_flow"].sum()
    flows = [(datetime.strptime(d, "%Y-%m-%d").date(), -amount)
             for d, amount in per_day.items()]
    if ending_value:
        last = ending_date or max(f[0] for f in flows)
        if isinstance(last, str):
            last = datetime.strptime(last, "%Y-%m-%d").date()
        flows.append((last, float(ending_value)))
    return xirr(flows)


# =====================================================================
# Time-weighted return
# =====================================================================

def time_weighted_return(values: pd.Series, flows: pd.Series
                         ) -> Optional[float]:
    """
    TWR over the whole period: the product of per-day growth factors, with
    each day's external flow removed before measuring growth.

    This is what strips out deposit timing. A day's return is

        (V_end - flow) / V_start

    so money arriving that day doesn't count as performance. Days where the
    portfolio was empty are skipped — no capital at risk, no return to
    measure, and dividing by zero would poison the whole chain.

    `values` is end-of-day market value; `flows` is net cash INTO the
    portfolio that day (buys positive, sells negative), aligned to `values`.
    """
    if values.empty or len(values) < 2:
        return None

    aligned = flows.reindex(values.index, fill_value=0.0)
    growth = 1.0
    counted = 0
    prior = values.iloc[0]

    for i in range(1, len(values)):
        current, flow = values.iloc[i], aligned.iloc[i]
        if prior <= 0:
            # Portfolio was empty entering this day; a deposit today starts
            # the clock rather than producing a return.
            prior = current
            continue
        factor = (current - flow) / prior
        if factor > 0:
            growth *= factor
            counted += 1
        prior = current

    return growth - 1.0 if counted else None


def annualise(total_return: float, days: float) -> Optional[float]:
    """
    Convert a cumulative return to an annual rate.

    Refuses periods under ~a month: annualising a few days' move produces
    absurd numbers (a 2% week becomes 180%/yr) that look authoritative.
    """
    if days < 30 or total_return <= -1:
        return None
    return (1.0 + total_return) ** (DAYS_PER_YEAR / days) - 1.0


# =====================================================================
# Drawdown
# =====================================================================

def drawdown_series(values: pd.Series) -> pd.DataFrame:
    """
    Peak-to-date, drawdown fraction, and the running high-water mark.

    Only meaningful once the portfolio has stopped growing purely by
    contribution — early on, every day is an all-time high simply because
    money keeps arriving. Read alongside the value curve, not alone.
    """
    if values.empty:
        return pd.DataFrame(columns=["value", "peak", "drawdown"])
    peak = values.cummax()
    return pd.DataFrame({
        "value": values,
        "peak": peak,
        "drawdown": np.where(peak > 0, values / peak - 1.0, 0.0),
    })


def max_drawdown(values: pd.Series) -> dict:
    """Worst peak-to-trough decline, and where it happened."""
    frame = drawdown_series(values)
    if frame.empty or frame["drawdown"].isna().all():
        return {"depth": None, "trough_date": None, "peak_date": None,
                "current": None}

    trough = frame["drawdown"].idxmin()
    depth = float(frame.loc[trough, "drawdown"])
    before = frame.loc[:trough]
    peak_date = before["value"].idxmax() if not before.empty else None
    return {
        "depth": depth,
        "trough_date": trough,
        "peak_date": peak_date,
        "current": float(frame["drawdown"].iloc[-1]),
    }


# =====================================================================
# Realized P&L quality
# =====================================================================

def realized_summary(realized: pd.DataFrame) -> dict:
    """
    Break the net realized figure into its parts.

    A single net number hides how it was earned: $441 net can be $441 of
    steady gains or $1,962 of wins against $1,521 of losses. The second is a
    far noisier process and deserves to be visible.

    `realized_gain` is already signed — losses negative — so the net is a
    plain sum, never a sum of magnitudes.
    """
    if realized.empty or "realized_gain" not in realized:
        return {"net": 0.0, "gross_wins": 0.0, "gross_losses": 0.0,
                "wins": 0, "losses": 0, "win_rate": None,
                "profit_factor": None, "largest_win": None,
                "largest_loss": None, "trades": 0}

    gains = realized["realized_gain"].dropna()
    wins, losses = gains[gains > 0], gains[gains < 0]
    gross_losses = float(-losses.sum())
    decided = len(wins) + len(losses)

    return {
        "net": float(gains.sum()),
        "gross_wins": float(wins.sum()),
        "gross_losses": gross_losses,
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "win_rate": (len(wins) / decided) if decided else None,
        # How many dollars won per dollar lost. >1 is profitable; None when
        # there are no losses to divide by (undefined, not infinite).
        "profit_factor": (float(wins.sum()) / gross_losses
                          if gross_losses > 0 else None),
        "largest_win": float(gains.max()) if len(gains) else None,
        "largest_loss": float(gains.min()) if len(gains) else None,
        "trades": int(len(gains)),
    }


def realized_by_symbol(realized: pd.DataFrame) -> pd.DataFrame:
    """Per-symbol net realized P&L, worst first."""
    if realized.empty:
        return pd.DataFrame(columns=["symbol", "realized", "trades"])
    grouped = (realized.groupby("symbol")["realized_gain"]
                       .agg(realized="sum", trades="count")
                       .reset_index()
                       .sort_values("realized"))
    return grouped


# =====================================================================
# Attribution: discretionary trading vs automated investing
# =====================================================================
#
# `placed_agent` marks who placed each order — "user" for a trade you chose,
# "recurring" for an automatic investment. Splitting on it answers a question
# most people cannot ask of their own account: is my stock-picking actually
# beating my autopilot?
#
# An honest comparison has to be like-for-like. Simply comparing dollar P&L
# rewards whichever stream happens to have more money in it, so returns are
# computed per stream on that stream's own capital.
# =====================================================================

def split_by_agent(trades: pd.DataFrame) -> dict:
    """Group trades by placed_agent, with missing values as 'unknown'."""
    if trades.empty or "placed_agent" not in trades:
        return {}
    labelled = trades.copy()
    labelled["placed_agent"] = labelled["placed_agent"].fillna("unknown")
    return {agent: frame for agent, frame in labelled.groupby("placed_agent")}


def attribution(trades: pd.DataFrame, prices: pd.DataFrame,
                opening: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """
    Per-stream capital, current value, and return.

    Each stream is valued as if it were its own portfolio: replay only that
    stream's trades, price the resulting holdings, and compare with the
    capital that stream deployed.

    CAVEAT worth surfacing in any UI built on this: streams are not truly
    independent. A recurring buy and a manual sell can touch the same shares,
    and attributing the sale to one stream leaves the other holding a
    position it did not fully fund. Treat the split as indicative of which
    behaviour is working, not as two audited sub-accounts.
    """
    streams = split_by_agent(trades)
    if not streams or prices.empty:
        return pd.DataFrame()

    rows = []
    for agent, frame in streams.items():
        # Opening balances are pre-history and belong to no known stream, so
        # they are deliberately excluded here; they would otherwise credit
        # one stream with shares it never bought.
        value = portfolio_value(frame, prices, opening=None)
        prepared = prepare_trades(frame)
        deployed = float(prepared["cash_flow"].sum())
        ending = float(value["value"].iloc[-1]) if not value.empty else 0.0
        gross_buys = float(prepared.loc[prepared["side"] == "buy", "gross"].sum())

        rows.append({
            "placed_agent": agent,
            "fills": int(len(frame)),
            "gross_invested": gross_buys,
            "net_deployed": deployed,
            "ending_value": ending,
            "gain": ending - deployed,
            # Return on that stream's own capital. Meaningless when a stream
            # is net-flat or negative (everything sold), so left as NaN.
            "return_pct": (ending / deployed - 1.0) if deployed > 0 else np.nan,
        })

    return pd.DataFrame(rows).sort_values("net_deployed", ascending=False)


def realized_by_agent(trades: pd.DataFrame,
                      realized: pd.DataFrame) -> pd.DataFrame:
    """
    Net realized P&L attributed to the stream that placed the SELL.

    Realized rows carry no placed_agent of their own, so each is matched to a
    sell trade on the same symbol and date. A closing trade is by definition
    a sale, and it is the sell decision that books the gain — so this
    attributes to whoever chose to sell, which is the behaviour being judged.
    Unmatched rows are reported as "unmatched" rather than silently dropped.
    """
    if realized.empty or trades.empty:
        return pd.DataFrame(columns=["placed_agent", "realized", "trades"])

    sells = prepare_trades(trades)
    sells = sells[sells["side"] == "sell"][["symbol", "date", "placed_agent"]]
    sells = sells.drop_duplicates(subset=["symbol", "date"])

    marked = realized.copy()
    marked["date"] = _as_date(marked["timestamp"])
    merged = marked.merge(sells, on=["symbol", "date"], how="left")
    merged["placed_agent"] = merged["placed_agent"].fillna("unmatched")

    return (merged.groupby("placed_agent")["realized_gain"]
                  .agg(realized="sum", trades="count")
                  .reset_index()
                  .sort_values("realized", ascending=False))


# =====================================================================
# Unrealized P&L
# =====================================================================

def unrealized_summary(positions: pd.DataFrame) -> dict:
    """
    Cost basis, market value and the gap, for what is still held.

    Kept strictly apart from realized: this moves every day and is not money
    you have. Never sum the two into one "total P&L" figure.
    """
    if positions.empty:
        return {"cost_basis": 0.0, "market_value": 0.0, "unrealized": 0.0,
                "return_pct": None, "positions": 0}

    frame = positions.copy()
    frame["cost_basis"] = frame["quantity"] * frame["average_buy_price"]
    frame["market_value"] = frame["quantity"] * frame["last_price"]
    cost = float(frame["cost_basis"].sum())
    value = float(frame["market_value"].sum())
    return {
        "cost_basis": cost,
        "market_value": value,
        "unrealized": value - cost,
        "return_pct": (value / cost - 1.0) if cost else None,
        "positions": int(len(frame)),
    }
