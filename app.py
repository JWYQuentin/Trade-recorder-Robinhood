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

        st.sidebar.divider()
        st.sidebar.caption(f"To fetch new data:\n\n`python sync_robinhood.py`")

        filtered = trades[
            trades["symbol"].isin(selected_symbols)
            & trades["side"].isin(selected_sides)
        ]

        col1, col2, col3 = st.columns(3)
        col1.metric("Trades shown", len(filtered))
        col2.metric("Buys", int((filtered["side"] == "buy").sum()))
        col3.metric("Sells", int((filtered["side"] == "sell").sum()))

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
        col2.metric("Cost basis", f"${total_cost:,.2f}")
        col3.metric("Market value", f"${total_value:,.2f}")
        col4.metric(
            "Unrealized P&L",
            f"${total_unrealized:,.2f}",
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
                    "unrealized P&L", format="$%.2f"),
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
        col1.metric("Total realized", f"${total:,.2f}")
        col2.metric("Closing trades", len(realized))
        col3.metric("Symbols up", f"{len(winners)}  (+${winners['realized_gain'].sum():,.2f})")
        col4.metric("Symbols down", f"{len(losers)}  (${losers['realized_gain'].sum():,.2f})")

        st.subheader("By symbol")
        st.dataframe(
            by_symbol,
            use_container_width=True,
            hide_index=True,
            column_config={
                "trades": st.column_config.NumberColumn("closing trades"),
                "realized_gain": st.column_config.NumberColumn(
                    "realized P&L", format="$%.2f"),
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
                    "realized P&L", format="$%.2f"),
            },
        )

        st.caption(
            "Realized P&L covers positions you have CLOSED — it excludes "
            "unrealized gains on what you still hold (see the Holdings tab). "
            "Figures come from Robinhood; informational, not a tax document."
        )
