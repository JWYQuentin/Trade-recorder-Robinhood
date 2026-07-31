"""
app.py — Streamlit dashboard for viewing synced trades.

Run with:  streamlit run app.py

This is view-only: it reads from trades.db and never touches Robinhood.
To pull in new trades, run `python sync_robinhood.py` first (or click the
"Sync now" hint in the sidebar and run it in your terminal).
"""

import pandas as pd
import streamlit as st

import db

st.set_page_config(page_title="Robinhood Trade Tracker", page_icon="📈", layout="wide")
st.title("📈 Robinhood Trade Tracker")
st.caption("Local, read-only record of your filled Robinhood trades.")


@st.cache_data(ttl=30)  # re-read the DB at most every 30 seconds
def load_trades() -> pd.DataFrame:
    """Load every trade from SQLite into a pandas DataFrame."""
    with db.get_connection() as conn:
        rows = db.get_all_trades(conn)
    return pd.DataFrame([dict(r) for r in rows])


trades = load_trades()

# ---------------------------------------------------------------- empty state
if trades.empty:
    st.info(
        "No trades synced yet. Run `python sync_robinhood.py` in your "
        "terminal, then refresh this page."
    )
    st.stop()

# ------------------------------------------------------------------- filters
st.sidebar.header("Filters")

symbols = sorted(trades["symbol"].unique())
selected_symbols = st.sidebar.multiselect("Symbol", symbols, default=symbols)

sides = sorted(trades["side"].unique())
selected_sides = st.sidebar.multiselect("Side", sides, default=sides)

st.sidebar.divider()
st.sidebar.caption("To fetch new trades, run:\n\n`python sync_robinhood.py`")

filtered = trades[
    trades["symbol"].isin(selected_symbols) & trades["side"].isin(selected_sides)
]

# ------------------------------------------------------------- summary chips
col1, col2, col3 = st.columns(3)
col1.metric("Trades shown", len(filtered))
col2.metric("Buys", int((filtered["side"] == "buy").sum()))
col3.metric("Sells", int((filtered["side"] == "sell").sum()))

# --------------------------------------------------------------- main table
# Hide raw_json from the table — it's stored for debugging, not for reading.
display_cols = [
    "symbol", "side", "quantity", "average_price",
    "status", "filled_at", "created_at", "asset_type", "robinhood_order_id",
]
st.dataframe(
    filtered[display_cols],
    use_container_width=True,
    hide_index=True,
    column_config={
        "average_price": st.column_config.NumberColumn("avg price", format="$%.2f"),
    },
)
