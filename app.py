"""
app.py — Streamlit dashboard for viewing synced trades.

Run with:  streamlit run app.py

This is view-only: it reads from trades.db and never touches Robinhood.
To pull in new data, run `python sync_robinhood.py` first.

Three tabs, because they answer different questions from different data:
  Trades       — immutable history of every fill.
  Holdings     — what you own now, and the gain you haven't taken yet.
  Realized P&L — the gain you HAVE taken, on positions already closed.

Keeping realized and unrealized apart matters: they come from separate
Robinhood endpoints, and adding them together would be misleading.
"""

import pandas as pd
import streamlit as st

import db

st.set_page_config(page_title="Robinhood Trade Tracker", page_icon="📈", layout="wide")
st.title("📈 Robinhood Trade Tracker")
st.caption("Local, read-only record of your Robinhood account.")


# ------------------------------------------------------------------- loaders
@st.cache_data(ttl=30)  # re-read the DB at most every 30 seconds
def load_trades() -> pd.DataFrame:
    with db.get_connection() as conn:
        return pd.DataFrame([dict(r) for r in db.get_all_trades(conn)])


@st.cache_data(ttl=30)
def load_positions() -> pd.DataFrame:
    with db.get_connection() as conn:
        return pd.DataFrame([dict(r) for r in db.get_positions(conn)])


@st.cache_data(ttl=30)
def load_realized() -> pd.DataFrame:
    with db.get_connection() as conn:
        return pd.DataFrame([dict(r) for r in db.get_realized_pnl(conn)])


trades = load_trades()
positions = load_positions()
realized = load_realized()

SYNC_HINT = "Run `python sync_robinhood.py` in your terminal, then refresh."


def money(amount: float) -> str:
    """
    Currency with the sign OUTSIDE the symbol: -$59.98, not $-59.98.

    Plain f"${x:,.2f}" puts the minus after the dollar sign, which reads
    badly on the P&L figures where negatives are the whole point.
    """
    return f"-${abs(amount):,.2f}" if amount < 0 else f"${amount:,.2f}"


tab_trades, tab_holdings, tab_realized = st.tabs(
    ["Trades", "Holdings", "Realized P&L"]
)


# =====================================================================
# Trades — every fill, filterable
# =====================================================================
with tab_trades:
    if trades.empty:
        st.info(f"No trades synced yet. {SYNC_HINT}")
    else:
        st.sidebar.header("Trade filters")
        st.sidebar.caption("These apply to the Trades tab only.")

        symbols = sorted(trades["symbol"].unique())
        selected_symbols = st.sidebar.multiselect("Symbol", symbols, default=symbols)

        sides = sorted(trades["side"].unique())
        selected_sides = st.sidebar.multiselect("Side", sides, default=sides)

        # Most of this account's activity is automated recurring buying, which
        # swamps the trades that were actual decisions. Filtering on
        # placed_agent is the only way to look at one without the other.
        # Driven by the values actually present, so a new source Robinhood
        # starts reporting (e.g. "drip") shows up instead of being dropped.
        agents = sorted(trades["placed_agent"].fillna("unknown").unique())
        selected_agents = st.sidebar.multiselect("Placed by", agents, default=agents)

        # filled_at is stored as an ISO-8601 string. Parse once, to dates, so
        # the picker compares like with like. format="ISO8601" copes with
        # timestamps that carry fractional seconds and ones that don't.
        fill_dates = pd.to_datetime(
            trades["filled_at"], format="ISO8601", utc=True
        ).dt.date
        earliest, latest = fill_dates.min(), fill_dates.max()
        picked = st.sidebar.date_input(
            "Filled between",
            value=(earliest, latest),
            min_value=earliest,
            max_value=latest,
        )
        # Streamlit returns a 1-tuple midway through picking a range, before
        # the second date is chosen. Hold the full range until both exist,
        # otherwise the table blanks out between the two clicks.
        if isinstance(picked, tuple) and len(picked) == 2:
            start_date, end_date = picked
        else:
            start_date, end_date = earliest, latest

        st.sidebar.divider()
        st.sidebar.caption(f"To fetch new data:\n\n`python sync_robinhood.py`")

        filtered = trades[
            trades["symbol"].isin(selected_symbols)
            & trades["side"].isin(selected_sides)
            & trades["placed_agent"].fillna("unknown").isin(selected_agents)
            & fill_dates.between(start_date, end_date)
        ]

        # Gross cash in and out. Fees are shown separately rather than folded
        # in, so "invested" stays comparable to the prices in the table below.
        gross = filtered["quantity"] * filtered["average_price"]
        buys = filtered["side"] == "buy"
        invested = gross[buys].sum()
        proceeds = gross[~buys].sum()
        total_fees = filtered["fees"].fillna(0).sum()

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Trades shown", f"{len(filtered):,}")
        # delta_color="off" keeps these grey — they're counts, not movements,
        # and a green/red arrow would imply a judgement.
        col2.metric("Invested", money(invested),
                    delta=f"{int(buys.sum()):,} buys", delta_color="off")
        col3.metric("Proceeds", money(proceeds),
                    delta=f"{int((~buys).sum()):,} sells", delta_color="off")
        col4.metric("Fees", money(total_fees))

        # ------------------------------------------------- cumulative curve
        # Buys deploy capital, sells return it, so the running total is how
        # much money is actually committed at any moment. One series, so the
        # heading names it and no legend is needed. Colour is validated
        # against both the light and dark chart surfaces.
        st.subheader("Cumulative net invested")
        curve = (
            pd.DataFrame({
                "date": fill_dates.loc[filtered.index],
                # Signed flow: money out on a buy, money back on a sell.
                "flow": gross.where(buys, -gross),
            })
            .groupby("date", as_index=False)["flow"].sum()
            .sort_values("date")
        )
        if curve.empty:
            st.caption("No trades match the current filters.")
        else:
            curve["Net invested"] = curve["flow"].cumsum()
            st.area_chart(
                curve.set_index("date")[["Net invested"]],
                color="#2a78d6",
                height=280,
            )
            st.caption(
                "Cost of what you bought minus what you got back on sells. "
                "It tracks capital committed, not portfolio value — for what "
                "that capital is worth today, see the Holdings tab."
            )

        # raw_json is stored for debugging, not for reading.
        display_cols = [
            "symbol", "side", "quantity", "average_price", "fees",
            "placed_agent", "status", "filled_at", "created_at",
            "asset_type", "robinhood_order_id",
        ]
        st.dataframe(
            filtered[[c for c in display_cols if c in filtered.columns]],
            use_container_width=True,
            hide_index=True,
            column_config={
                "average_price": st.column_config.NumberColumn(
                    "avg price", format="$%.2f"),
                "fees": st.column_config.NumberColumn("fees", format="$%.2f"),
                "placed_agent": st.column_config.TextColumn("placed by"),
            },
        )


# =====================================================================
# Holdings — what you own, and the gain not yet taken
# =====================================================================
with tab_holdings:
    if positions.empty:
        st.info(f"No holdings snapshot yet. {SYNC_HINT}")
    else:
        h = positions.copy()
        h["cost_basis"] = h["quantity"] * h["average_buy_price"]
        h["market_value"] = h["quantity"] * h["last_price"]
        h["unrealized"] = h["market_value"] - h["cost_basis"]
        # Guard the division: a position with no cost basis would blow up.
        h["unrealized_pct"] = h.apply(
            lambda r: (r["unrealized"] / r["cost_basis"])
            if r["cost_basis"] else None,
            axis=1,
        )

        total_cost = h["cost_basis"].sum()
        total_value = h["market_value"].sum()
        total_unrealized = h["unrealized"].sum()

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Positions", len(h))
        col2.metric("Cost basis", money(total_cost))
        col3.metric("Market value", money(total_value))
        col4.metric(
            "Unrealized P&L",
            money(total_unrealized),
            delta=f"{total_unrealized / total_cost:.2%}" if total_cost else None,
        )

        st.dataframe(
            h[["symbol", "quantity", "average_buy_price", "last_price",
               "cost_basis", "market_value", "unrealized", "unrealized_pct"]],
            use_container_width=True,
            hide_index=True,
            column_config={
                "quantity": st.column_config.NumberColumn("shares", format="%.6f"),
                "average_buy_price": st.column_config.NumberColumn(
                    "avg cost", format="$%.2f"),
                "last_price": st.column_config.NumberColumn(
                    "last price", format="$%.2f"),
                "cost_basis": st.column_config.NumberColumn(
                    "cost basis", format="$%.2f"),
                "market_value": st.column_config.NumberColumn(
                    "market value", format="$%.2f"),
                "unrealized": st.column_config.NumberColumn(
                    "unrealized P&L", format="accounting"),
                "unrealized_pct": st.column_config.NumberColumn(
                    "unrealized %", format="percent"),
            },
        )

        as_of = positions["as_of"].iloc[0] if "as_of" in positions else "unknown"
        sources = ", ".join(sorted(positions["price_source"].dropna().unique()))
        st.caption(
            f"Snapshot taken {as_of} · prices from {sources}. "
            "Unrealized P&L is a live market figure and moves with prices — "
            "it is not money you have received."
        )
        st.caption(
            "Average cost comes from Robinhood and already reflects partial "
            "sells, so it can look unlike the prices in your trade history "
            "for a position you have mostly sold out of."
        )


# =====================================================================
# Realized P&L — gains actually taken, per symbol
# =====================================================================
with tab_realized:
    if realized.empty:
        st.info(f"No realized P&L synced yet. {SYNC_HINT}")
    else:
        by_symbol = (
            realized.groupby("symbol", as_index=False)
            .agg(trades=("realized_gain", "size"),
                 realized_gain=("realized_gain", "sum"))
            .sort_values("realized_gain", ascending=False)
        )

        total = realized["realized_gain"].sum()
        winners = by_symbol[by_symbol["realized_gain"] > 0]
        losers = by_symbol[by_symbol["realized_gain"] < 0]

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total realized", money(total))
        col2.metric("Closing trades", len(realized))
        col3.metric("Symbols up",
                    f"{len(winners)}  (+{money(winners['realized_gain'].sum())})")
        col4.metric("Symbols down",
                    f"{len(losers)}  ({money(losers['realized_gain'].sum())})")

        st.subheader("By symbol")
        st.dataframe(
            by_symbol,
            use_container_width=True,
            hide_index=True,
            column_config={
                "trades": st.column_config.NumberColumn("closing trades"),
                "realized_gain": st.column_config.NumberColumn(
                    "realized P&L", format="accounting"),
            },
        )

        st.subheader("Every closing trade")
        st.dataframe(
            realized[["timestamp", "symbol", "quantity", "price", "realized_gain"]],
            use_container_width=True,
            hide_index=True,
            column_config={
                "quantity": st.column_config.NumberColumn("shares", format="%.6f"),
                "price": st.column_config.NumberColumn("close price", format="$%.2f"),
                "realized_gain": st.column_config.NumberColumn(
                    "realized P&L", format="accounting"),
            },
        )

        st.caption(
            "Realized P&L covers positions you have CLOSED — it excludes "
            "unrealized gains on what you still hold (see the Holdings tab). "
            "Figures come from Robinhood; informational, not a tax document."
        )
