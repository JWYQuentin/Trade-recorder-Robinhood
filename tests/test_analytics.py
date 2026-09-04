"""
Unit tests for the metrics engine.

These use hand-built fixtures with answers worked out independently, not
whatever the code happens to produce. A metric that returns a plausible wrong
number is the failure mode worth guarding against.
"""

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analytics as an


def make_trades(rows):
    """rows: (filled_at, symbol, side, qty, price, fees)"""
    return pd.DataFrame(
        [{"filled_at": f, "symbol": s, "side": d, "quantity": q,
          "average_price": p, "fees": fee} for f, s, d, q, p, fee in rows])


def make_prices(rows):
    """rows: (date, symbol, close)"""
    return pd.DataFrame([{"date": d, "symbol": s, "close": c}
                         for d, s, c in rows])


# ---------------------------------------------------------------- XIRR

def test_xirr_simple_doubling_over_one_year():
    """$100 in, $200 back exactly a year later = 100% annual."""
    flows = [(date(2025, 1, 1), -100.0), (date(2026, 1, 1), 200.0)]
    assert an.xirr(flows) == pytest.approx(1.0, abs=1e-4)


def test_xirr_ten_percent_over_one_year():
    flows = [(date(2025, 1, 1), -1000.0), (date(2026, 1, 1), 1100.0)]
    assert an.xirr(flows) == pytest.approx(0.10, abs=1e-4)


def test_xirr_loss_is_negative():
    flows = [(date(2025, 1, 1), -1000.0), (date(2026, 1, 1), 900.0)]
    result = an.xirr(flows)
    assert result == pytest.approx(-0.10, abs=1e-4)
    assert result < 0


def test_xirr_npv_is_zero_at_the_solved_rate():
    """The defining property: NPV discounted at the IRR must vanish."""
    flows = [(date(2025, 1, 15), -500.0), (date(2025, 6, 3), -250.0),
             (date(2025, 11, 20), -125.0), (date(2026, 4, 1), 1000.0)]
    rate = an.xirr(flows)
    assert rate is not None
    assert an.xnpv(rate, sorted(flows)) == pytest.approx(0.0, abs=1e-5)


def test_xirr_timing_changes_the_answer():
    """
    Same money in and out; later deposit = higher rate, because the capital
    worked for less time to earn the same gain. This is the whole point of
    money-weighting.
    """
    early = an.xirr([(date(2025, 1, 1), -1000.0), (date(2026, 1, 1), 1200.0)])
    late = an.xirr([(date(2025, 7, 1), -1000.0), (date(2026, 1, 1), 1200.0)])
    assert late > early


def test_xirr_returns_none_without_a_sign_change():
    """Only-outflows has no return to compute; None, not a bogus number."""
    assert an.xirr([(date(2025, 1, 1), -100.0),
                    (date(2025, 6, 1), -100.0)]) is None


def test_xirr_returns_none_for_a_single_flow():
    assert an.xirr([(date(2025, 1, 1), -100.0)]) is None


# ------------------------------------------------------------ Holdings

def test_holdings_accumulate_and_close_to_zero():
    """A full sell must leave zero, not carry phantom shares forward."""
    trades = make_trades([
        ("2025-01-02T15:00:00Z", "AAA", "buy", 10, 100.0, 0),
        ("2025-01-03T15:00:00Z", "AAA", "sell", 10, 110.0, 0),
    ])
    prices = make_prices([("2025-01-02", "AAA", 100.0),
                          ("2025-01-03", "AAA", 110.0),
                          ("2025-01-06", "AAA", 120.0)])
    held = an.holdings_over_time(trades, prices)
    assert held.loc["2025-01-02", "AAA"] == pytest.approx(10.0)
    assert held.loc["2025-01-03", "AAA"] == pytest.approx(0.0)
    # The day AFTER closing: still zero, and not valued at $120.
    assert held.loc["2025-01-06", "AAA"] == pytest.approx(0.0)


def test_opening_balances_seed_pre_history_shares():
    """A sell with no buy goes negative unless seeded."""
    trades = make_trades([("2025-01-03T15:00:00Z", "BBB", "sell", 1, 50.0, 0)])
    prices = make_prices([("2025-01-02", "BBB", 48.0),
                          ("2025-01-03", "BBB", 50.0)])

    unseeded = an.holdings_over_time(trades, prices)
    assert unseeded.loc["2025-01-03", "BBB"] < 0

    opening = pd.DataFrame([{"symbol": "BBB", "quantity": 1.0}])
    seeded = an.holdings_over_time(trades, prices, opening)
    assert seeded.loc["2025-01-03", "BBB"] == pytest.approx(0.0)
    assert seeded.loc["2025-01-02", "BBB"] == pytest.approx(1.0)


def test_portfolio_value_prices_each_day():
    trades = make_trades([("2025-01-02T15:00:00Z", "AAA", "buy", 10, 100.0, 0)])
    prices = make_prices([("2025-01-02", "AAA", 100.0),
                          ("2025-01-03", "AAA", 110.0)])
    value = an.portfolio_value(trades, prices)
    assert value.loc["2025-01-02", "value"] == pytest.approx(1000.0)
    assert value.loc["2025-01-03", "value"] == pytest.approx(1100.0)


# ----------------------------------------------------------------- TWR

def test_twr_ignores_a_deposit():
    """
    Day 1: $100 -> $110 (+10%). Day 2: $100 deposited, ends $210.
    The deposit is not performance, so TWR stays +10%, not +110%.
    """
    values = pd.Series([100.0, 110.0, 210.0], index=["d1", "d2", "d3"])
    flows = pd.Series([100.0, 0.0, 100.0], index=["d1", "d2", "d3"])
    assert an.time_weighted_return(values, flows) == pytest.approx(0.10, abs=1e-9)


def test_twr_compounds_two_periods():
    """+10% then +10% compounds to +21%, not +20%."""
    values = pd.Series([100.0, 110.0, 121.0], index=["d1", "d2", "d3"])
    flows = pd.Series([0.0, 0.0, 0.0], index=["d1", "d2", "d3"])
    assert an.time_weighted_return(values, flows) == pytest.approx(0.21, abs=1e-9)


def test_twr_handles_a_loss():
    values = pd.Series([100.0, 90.0], index=["d1", "d2"])
    flows = pd.Series([0.0, 0.0], index=["d1", "d2"])
    assert an.time_weighted_return(values, flows) == pytest.approx(-0.10, abs=1e-9)


def test_twr_skips_days_starting_from_an_empty_portfolio():
    """Opening from zero must not divide by zero or invent a return."""
    values = pd.Series([0.0, 100.0, 110.0], index=["d1", "d2", "d3"])
    flows = pd.Series([0.0, 100.0, 0.0], index=["d1", "d2", "d3"])
    assert an.time_weighted_return(values, flows) == pytest.approx(0.10, abs=1e-9)


def test_twr_and_mwr_disagree_when_timing_matters():
    """
    Flat then up, with most money arriving only for the good stretch. MWR
    should beat TWR because the big deposit caught the rise.
    """
    values = pd.Series([100.0, 100.0, 1100.0, 1210.0],
                       index=["d1", "d2", "d3", "d4"])
    flows = pd.Series([100.0, 0.0, 1000.0, 0.0],
                      index=["d1", "d2", "d3", "d4"])
    twr = an.time_weighted_return(values, flows)
    assert twr == pytest.approx(0.10, abs=1e-9)


# ----------------------------------------------------------- Annualise

def test_annualise_one_year_is_unchanged():
    assert an.annualise(0.10, 365) == pytest.approx(0.10, abs=1e-9)


def test_annualise_half_year_compounds_up():
    assert an.annualise(0.10, 182.5) == pytest.approx(0.21, abs=1e-3)


def test_annualise_refuses_a_short_window():
    """A week's move must not be extrapolated into an annual headline."""
    assert an.annualise(0.02, 7) is None


# ------------------------------------------------------------ Drawdown

def test_max_drawdown_finds_the_worst_decline():
    values = pd.Series([100.0, 120.0, 60.0, 90.0],
                       index=["d1", "d2", "d3", "d4"])
    result = an.max_drawdown(values)
    assert result["depth"] == pytest.approx(-0.5)     # 120 -> 60
    assert result["trough_date"] == "d3"
    assert result["peak_date"] == "d2"
    assert result["current"] == pytest.approx(-0.25)  # 90 vs peak 120


def test_drawdown_is_zero_on_a_rising_series():
    values = pd.Series([100.0, 110.0, 120.0], index=["d1", "d2", "d3"])
    assert an.max_drawdown(values)["depth"] == pytest.approx(0.0)


# ------------------------------------------------------------- Realized

def test_realized_summary_nets_wins_against_losses():
    """The headline must be net, never a sum of magnitudes."""
    realized = pd.DataFrame({"symbol": list("ABCD"),
                             "realized_gain": [100.0, -40.0, 25.0, -10.0]})
    s = an.realized_summary(realized)
    assert s["net"] == pytest.approx(75.0)          # not 175
    assert s["gross_wins"] == pytest.approx(125.0)
    assert s["gross_losses"] == pytest.approx(50.0)
    assert s["wins"] == 2 and s["losses"] == 2
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["profit_factor"] == pytest.approx(2.5)


def test_profit_factor_is_none_without_losses():
    """Undefined, not infinite — dividing by zero losses means nothing."""
    realized = pd.DataFrame({"symbol": ["A"], "realized_gain": [50.0]})
    assert an.realized_summary(realized)["profit_factor"] is None


def test_realized_summary_handles_empty():
    s = an.realized_summary(pd.DataFrame())
    assert s["net"] == 0.0 and s["trades"] == 0


# ----------------------------------------------------------- Unrealized

def test_unrealized_summary_is_value_minus_cost():
    positions = pd.DataFrame({
        "symbol": ["AAA", "BBB"], "quantity": [10.0, 5.0],
        "average_buy_price": [100.0, 200.0], "last_price": [110.0, 180.0]})
    s = an.unrealized_summary(positions)
    assert s["cost_basis"] == pytest.approx(2000.0)
    assert s["market_value"] == pytest.approx(2000.0)
    assert s["unrealized"] == pytest.approx(0.0)
    assert s["return_pct"] == pytest.approx(0.0)


# ------------------------------------------------------------ Cash flow

def test_cash_flow_signs_and_fees():
    """Buys cost money, sells return it, fees always cost."""
    trades = make_trades([
        ("2025-01-02T15:00:00Z", "AAA", "buy", 10, 100.0, 1.0),
        ("2025-01-03T15:00:00Z", "AAA", "sell", 10, 110.0, 2.0),
    ])
    prepared = an.prepare_trades(trades)
    assert prepared.iloc[0]["cash_flow"] == pytest.approx(1001.0)
    assert prepared.iloc[1]["cash_flow"] == pytest.approx(-1098.0)


def test_capital_weighted_holding_days_reflects_late_deposits():
    """
    Money arriving late shortens the effective horizon, even if the account
    is old. This is the guard against annualising a short burst of capital.
    """
    trades = make_trades([
        ("2025-01-01T15:00:00Z", "AAA", "buy", 1, 100.0, 0),    # small, early
        ("2025-12-01T15:00:00Z", "AAA", "buy", 100, 100.0, 0),  # large, late
    ])
    days = an.capital_weighted_holding_days(trades, "2026-01-01")
    # 100 of 10100 dollars sat a year; the rest ~31 days.
    assert 30 < days < 45, days


def test_capital_weighted_holding_days_matches_a_single_flow():
    trades = make_trades([("2025-01-01T15:00:00Z", "AAA", "buy", 1, 100.0, 0)])
    assert an.capital_weighted_holding_days(trades, "2025-01-31") == pytest.approx(30)


# ---------------------------------------------------------- Attribution

def test_split_by_agent_separates_streams():
    trades = make_trades([
        ("2025-01-02T15:00:00Z", "AAA", "buy", 1, 100.0, 0),
        ("2025-01-03T15:00:00Z", "AAA", "buy", 1, 100.0, 0),
    ])
    trades["placed_agent"] = ["user", "recurring"]
    streams = an.split_by_agent(trades)
    assert set(streams) == {"user", "recurring"}
    assert len(streams["user"]) == 1


def test_split_by_agent_labels_missing_as_unknown():
    trades = make_trades([("2025-01-02T15:00:00Z", "AAA", "buy", 1, 100.0, 0)])
    trades["placed_agent"] = [None]
    assert set(an.split_by_agent(trades)) == {"unknown"}


def test_attribution_computes_return_per_stream():
    """
    Each stream is judged on its own capital: the manual buy doubles, the
    recurring buy is flat, so returns must be +100% and 0% regardless of
    the different amounts deployed.
    """
    trades = make_trades([
        ("2025-01-02T15:00:00Z", "AAA", "buy", 10, 100.0, 0),   # user
        ("2025-01-02T15:00:00Z", "BBB", "buy", 1, 100.0, 0),    # recurring
    ])
    trades["placed_agent"] = ["user", "recurring"]
    prices = make_prices([("2025-01-02", "AAA", 100.0), ("2025-01-02", "BBB", 100.0),
                          ("2025-01-03", "AAA", 200.0), ("2025-01-03", "BBB", 100.0)])

    result = an.attribution(trades, prices).set_index("placed_agent")
    assert result.loc["user", "return_pct"] == pytest.approx(1.0)
    assert result.loc["recurring", "return_pct"] == pytest.approx(0.0)
    assert result.loc["user", "gain"] == pytest.approx(1000.0)


def test_realized_by_agent_attributes_to_the_seller():
    """The gain is booked by whoever chose to sell."""
    trades = make_trades([
        ("2025-01-02T15:00:00Z", "AAA", "buy", 10, 100.0, 0),
        ("2025-03-05T15:00:00Z", "AAA", "sell", 10, 120.0, 0),
    ])
    trades["placed_agent"] = ["recurring", "user"]
    realized = pd.DataFrame({"timestamp": ["2025-03-05T15:00:00Z"],
                             "symbol": ["AAA"], "realized_gain": [200.0]})
    result = an.realized_by_agent(trades, realized).set_index("placed_agent")
    assert result.loc["user", "realized"] == pytest.approx(200.0)
    assert "recurring" not in result.index


def test_realized_by_agent_flags_unmatched_rows():
    """A realized row with no matching sell is surfaced, not dropped."""
    trades = make_trades([("2025-01-02T15:00:00Z", "AAA", "buy", 1, 100.0, 0)])
    trades["placed_agent"] = ["user"]
    realized = pd.DataFrame({"timestamp": ["2024-06-01T15:00:00Z"],
                             "symbol": ["ZZZ"], "realized_gain": [50.0]})
    result = an.realized_by_agent(trades, realized).set_index("placed_agent")
    assert result.loc["unmatched", "realized"] == pytest.approx(50.0)


# ------------------------------------------------------------ Benchmark

def test_benchmark_buys_shares_with_the_same_cash():
    """$1000 in at $100/share = 10 shares; at $200 later it's worth $2000."""
    trades = make_trades([("2025-01-02T15:00:00Z", "AAA", "buy", 10, 100.0, 0)])
    prices = make_prices([("2025-01-02", "SPY", 100.0),
                          ("2025-01-03", "SPY", 200.0)])
    curve = an.benchmark_curve(trades, prices, "SPY")
    assert curve.loc["2025-01-02", "shares"] == pytest.approx(10.0)
    assert curve.loc["2025-01-03", "value"] == pytest.approx(2000.0)


def test_benchmark_sells_on_an_outflow():
    """Taking money out of the portfolio sells the equivalent index dollars."""
    trades = make_trades([
        ("2025-01-02T15:00:00Z", "AAA", "buy", 10, 100.0, 0),   # +$1000
        ("2025-01-03T15:00:00Z", "AAA", "sell", 5, 100.0, 0),   # -$500
    ])
    prices = make_prices([("2025-01-02", "SPY", 100.0),
                          ("2025-01-03", "SPY", 100.0)])
    curve = an.benchmark_curve(trades, prices, "SPY")
    # 10 shares bought, 5 sold back at the same price.
    assert curve.loc["2025-01-03", "shares"] == pytest.approx(5.0)
    assert curve.loc["2025-01-03", "invested"] == pytest.approx(500.0)


def test_benchmark_respects_contribution_timing():
    """
    The whole point: money that arrived after a rise does NOT get credited
    with that rise. Buying $100 of index at 200 gets half a share, not the
    return from 100.
    """
    trades = make_trades([("2025-01-03T15:00:00Z", "AAA", "buy", 1, 100.0, 0)])
    prices = make_prices([("2025-01-02", "SPY", 100.0),
                          ("2025-01-03", "SPY", 200.0)])
    curve = an.benchmark_curve(trades, prices, "SPY")
    assert curve.loc["2025-01-03", "shares"] == pytest.approx(0.5)
    assert curve.loc["2025-01-03", "value"] == pytest.approx(100.0)


def test_benchmark_flow_on_a_closed_market_day_is_not_lost():
    """A fill dated on a holiday must roll into the next trading day."""
    trades = make_trades([("2025-01-04T15:00:00Z", "AAA", "buy", 1, 100.0, 0)])
    prices = make_prices([("2025-01-03", "SPY", 100.0),
                          ("2025-01-06", "SPY", 100.0)])   # 4th/5th closed
    curve = an.benchmark_curve(trades, prices, "SPY")
    assert curve.loc["2025-01-06", "invested"] == pytest.approx(100.0)
    assert curve.loc["2025-01-06", "shares"] == pytest.approx(1.0)


def test_benchmark_comparison_reports_the_gap():
    """Portfolio beat the index: difference and excess must both be positive."""
    trades = make_trades([("2025-01-02T15:00:00Z", "AAA", "buy", 10, 100.0, 0)])
    prices = make_prices([("2025-01-02", "SPY", 100.0),
                          ("2025-01-03", "SPY", 110.0)])
    portfolio = pd.Series({"2025-01-02": 1000.0, "2025-01-03": 1500.0})
    result = an.benchmark_comparison(trades, prices, portfolio, "SPY")
    assert result["benchmark_value"] == pytest.approx(1100.0)
    assert result["portfolio_value"] == pytest.approx(1500.0)
    assert result["difference"] == pytest.approx(400.0)
    assert result["benchmark_return"] == pytest.approx(0.10)
    assert result["portfolio_return"] == pytest.approx(0.50)
    assert result["excess_return"] == pytest.approx(0.40)


def test_benchmark_comparison_reports_underperformance():
    """Losing to the index must read as a negative difference, not an absolute."""
    trades = make_trades([("2025-01-02T15:00:00Z", "AAA", "buy", 10, 100.0, 0)])
    prices = make_prices([("2025-01-02", "SPY", 100.0),
                          ("2025-01-03", "SPY", 130.0)])
    portfolio = pd.Series({"2025-01-02": 1000.0, "2025-01-03": 1100.0})
    result = an.benchmark_comparison(trades, prices, portfolio, "SPY")
    assert result["difference"] == pytest.approx(-200.0)
    assert result["excess_return"] < 0


def test_benchmark_comparison_none_without_prices():
    trades = make_trades([("2025-01-02T15:00:00Z", "AAA", "buy", 10, 100.0, 0)])
    prices = make_prices([("2025-01-02", "AAA", 100.0)])   # no SPY
    portfolio = pd.Series({"2025-01-02": 1000.0})
    assert an.benchmark_comparison(trades, prices, portfolio, "SPY") is None
