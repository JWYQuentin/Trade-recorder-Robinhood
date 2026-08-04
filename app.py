"""
app.py — Streamlit dashboard for viewing synced Robinhood activity.

Run with:  streamlit run app.py     (or double-click Trade Dashboard.command)

View-only: reads trades.db, never touches Robinhood. To pull new data run
`python sync_robinhood.py` first.

Three tabs, because they answer different questions from different data:
  Trades       — immutable history of every fill.
  Holdings     — what you own now, and the gain you haven't taken yet.
  Realized P&L — the gain you HAVE taken, on positions already closed.

Keeping realized and unrealized apart matters: they come from separate
Robinhood endpoints, and adding them together would be misleading.

DESIGN NOTE — the look is a brokerage statement, not a SaaS dashboard.
Money is set in a monospace face so decimal points line up in a column,
which is the whole reason statements have always been set that way.
Hairline rules and whitespace do the separating; there are no cards, no
shadows, no rounded corners. Colour is spent only where it carries
meaning: green/red on gains and losses, one blue on the active tab and
the charts. Everything else is ink, muted ink, and paper.
"""

import altair as alt
import pandas as pd
import streamlit as st

import db

ACCENT, GAIN, LOSS = "#1C5CAB", "#0F7A4A", "#B3261E"

st.set_page_config(page_title="Trade Recorder", page_icon="📈", layout="wide")


# =====================================================================
# Styling
# =====================================================================
# Streamlit ships a generic look; these rules impose the statement
# aesthetic on top of it. Selectors use documented data-testid hooks
# where possible, but Streamlit's internal DOM does shift between
# versions — if a rule stops applying after an upgrade it degrades to
# default styling rather than breaking the page.

st.markdown("""
<style>
  :root {
    --ink:    #1A1A18;
    --muted:  #6B6B66;
    --rule:   #E3E3DE;
    --accent: #1C5CAB;
    --gain:   #0F7A4A;
    --loss:   #B3261E;
    --mono: ui-monospace, "SF Mono", Menlo, "JetBrains Mono", monospace;
    --s1: 4px;  --s2: 8px;  --s3: 16px;  --s4: 24px;  --s5: 40px;
  }

  /* Streamlit's "Deploy" button and hamburger sit directly above the
     masthead and compete with it. This app only ever runs locally, so
     neither is useful here. */
  [data-testid="stToolbar"], [data-testid="stDecoration"] { display: none; }

  .block-container { padding-top: var(--s4); max-width: 1280px; }

  /* ---- masthead ---------------------------------------------------- */
  .masthead {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: var(--s3); flex-wrap: wrap;
    border-bottom: 2px solid var(--ink);
    padding-bottom: var(--s2); margin-bottom: var(--s4);
  }
  .masthead h1 {
    font-family: var(--mono);
    font-size: 20px; font-weight: 600; letter-spacing: 0.02em;
    margin: 0; color: var(--ink); text-transform: uppercase;
  }
  .masthead .asof {
    font-family: var(--mono); font-size: 12px; color: var(--muted);
  }

  /* ---- the statement block: the signature element ------------------ */
  .stmt { margin: 0 0 var(--s4) 0; }
  .stmt-title {
    font-family: var(--mono); font-size: 11px; letter-spacing: 0.12em;
    text-transform: uppercase; color: var(--muted);
    border-bottom: 1px solid var(--rule);
    padding-bottom: var(--s1); margin-bottom: var(--s2);
  }
  .stmt-row {
    display: flex; align-items: baseline; justify-content: space-between;
    gap: var(--s3); padding: 5px 0;
    border-bottom: 1px solid var(--rule);
  }
  .stmt-row:last-child { border-bottom: none; }
  .stmt-row.lead { padding-top: var(--s2); }
  .stmt-label { font-size: 13px; color: var(--muted); }
  .stmt-row.lead .stmt-label { color: var(--ink); font-weight: 600; }
  /* tabular-nums is what actually makes a column of figures line up */
  .stmt-val {
    font-family: var(--mono); font-variant-numeric: tabular-nums;
    font-size: 14px; color: var(--ink); text-align: right;
  }
  .stmt-row.lead .stmt-val { font-size: 19px; font-weight: 600; }
  .stmt-note {
    font-family: var(--mono); font-size: 12px;
    color: var(--muted); margin-left: var(--s2);
  }
  .gain { color: var(--gain); }
  .loss { color: var(--loss); }

  /* ---- section headings -------------------------------------------- */
  .sec {
    font-family: var(--mono); font-size: 11px; letter-spacing: 0.12em;
    text-transform: uppercase; color: var(--muted);
    border-bottom: 1px solid var(--rule);
    padding-bottom: var(--s1);
    margin: var(--s5) 0 var(--s2) 0;
  }

  /* ---- tabs: a ruled strip, not pills ------------------------------ */
  .stTabs [data-baseweb="tab-list"] {
    gap: var(--s4); border-bottom: 1px solid var(--rule);
  }
  .stTabs [data-baseweb="tab"] {
    font-family: var(--mono); font-size: 12px; letter-spacing: 0.08em;
    text-transform: uppercase; padding: var(--s2) 0; color: var(--muted);
  }
  .stTabs [aria-selected="true"] { color: var(--ink) !important; }

  /* ---- figures inside Streamlit's own widgets ---------------------- */
  [data-testid="stDataFrame"] { font-variant-numeric: tabular-nums; }

  /* ---- sidebar: a form, kept quiet --------------------------------- */
  [data-testid="stSidebar"] { border-right: 1px solid var(--rule); }
  [data-testid="stSidebar"] label {
    font-family: var(--mono); font-size: 11px; letter-spacing: 0.08em;
    text-transform: uppercase; color: var(--muted);
  }

  .note { font-size: 12px; color: var(--muted); line-height: 1.6;
          margin-top: var(--s2); }
</style>
""", unsafe_allow_html=True)


# =====================================================================
# Loaders
# =====================================================================

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


# =====================================================================
# Formatting helpers
# =====================================================================

def money(amount: float) -> str:
    """Currency with the sign OUTSIDE the symbol: -$59.98, not $-59.98."""
    return f"-${abs(amount):,.2f}" if amount < 0 else f"${amount:,.2f}"


def tinted(amount: float, text: str = None) -> str:
    """A figure wrapped in its gain/loss colour. Plain ink at exactly zero."""
    cls = "gain" if amount > 0 else ("loss" if amount < 0 else "")
    return f'<span class="{cls}">{text or money(amount)}</span>'


def statement(title, rows):
    """
    Render a ruled label/figure block — the brokerage-statement motif.

    rows: (label, value, note, lead) where `note` is an optional smaller
    figure set to the right, and `lead` promotes the row to the block's
    headline number.
    """
    html = [f'<div class="stmt"><div class="stmt-title">{title}</div>']
    for label, value, note_text, lead in rows:
        cls = " lead" if lead else ""
        note_html = f'<span class="stmt-note">{note_text}</span>' if note_text else ""
        html.append(
            f'<div class="stmt-row{cls}">'
            f'<span class="stmt-label">{label}</span>'
            f'<span class="stmt-val">{value}{note_html}</span></div>'
        )
    html.append("</div>")
    st.markdown("".join(html), unsafe_allow_html=True)


def section(label):
    st.markdown(f'<div class="sec">{label}</div>', unsafe_allow_html=True)


def ranked_bars(frame, value_col, money_label, diverging=False):
    """
    Horizontal bars, ordered biggest-first.

    st.bar_chart orders the category axis alphabetically, which destroys
    the only reading these charts exist for — which position dominates,
    which symbol lost the most. Altair lets the order follow the value.
    Height grows with the row count so nothing is silently cut off.

    `diverging` colours gains green and losses red. That pairing is weak
    for red-green colour blindness (ΔE 6.0 deutan), so it is only used
    where a second, independent cue carries the same information: bars
    run right from zero for a gain and left for a loss, so direction
    already says the sign and colour merely reinforces it.
    """
    colour = (
        alt.condition(alt.datum[value_col] >= 0,
                      alt.value(GAIN), alt.value(LOSS))
        if diverging else alt.value(ACCENT)
    )
    chart = (
        alt.Chart(frame)
        .mark_bar(height=14)
        .encode(
            x=alt.X(f"{value_col}:Q", title=None,
                    axis=alt.Axis(format="$,.0f", grid=True,
                                  gridColor="#EDEDE8", domain=False,
                                  tickCount=5, labelColor="#6B6B66")),
            y=alt.Y("symbol:N", title=None, sort="-x",
                    axis=alt.Axis(domain=False, ticks=False,
                                  labelColor="#1A1A18", labelFontSize=12)),
            color=colour,
            tooltip=[alt.Tooltip("symbol:N", title="Symbol"),
                     alt.Tooltip(f"{value_col}:Q", title=money_label,
                                 format="$,.2f")],
        )
        .properties(height=max(180, 26 * len(frame)))
        .configure_view(stroke=None)
    )
    st.altair_chart(chart, use_container_width=True)


def note(text):
    st.markdown(f'<div class="note">{text}</div>', unsafe_allow_html=True)


# =====================================================================
# Masthead
# =====================================================================

as_of = (positions["as_of"].iloc[0][:16].replace("T", " ")
         if not positions.empty else "not synced")
st.markdown(
    f'<div class="masthead"><h1>Trade Recorder</h1>'
    f'<span class="asof">Robinhood · snapshot {as_of} UTC</span></div>',
    unsafe_allow_html=True,
)

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
        st.sidebar.markdown(
            '<div class="sec" style="margin-top:0">Filters</div>',
            unsafe_allow_html=True)
        st.sidebar.caption("Applies to the Trades tab only.")

        # Each filter defaults to EMPTY meaning "everything". Defaulting to
        # every value selected filled the sidebar with 20 symbol chips and
        # buried the controls beneath them.
        def pick(label, options, help=None):
            chosen = st.sidebar.multiselect(
                label, options, default=[], placeholder="All", help=help)
            return chosen or list(options)

        symbols = sorted(trades["symbol"].unique())
        sel_symbols = pick("Symbol", symbols)
        sel_sides = pick("Side", sorted(trades["side"].unique()))
        # placed_agent separates trades you chose from automatic recurring
        # buys — most of the row count here is dollar-cost averaging.
        agents = sorted(trades["placed_agent"].fillna("unknown").unique())
        sel_agents = pick("Placed by", agents,
                          help="'user' is a trade you placed; 'recurring' is "
                               "an automatic investment.")

        fill_dates = pd.to_datetime(
            trades["filled_at"], format="ISO8601", utc=True).dt.date
        earliest, latest = fill_dates.min(), fill_dates.max()
        picked = st.sidebar.date_input(
            "Filled between", value=(earliest, latest),
            min_value=earliest, max_value=latest)
        # Streamlit returns a 1-tuple midway through picking a range; hold
        # the full range until both ends exist or the table blanks out.
        start_date, end_date = picked if (
            isinstance(picked, tuple) and len(picked) == 2
        ) else (earliest, latest)

        st.sidebar.divider()
        st.sidebar.caption("New data: `python sync_robinhood.py`")

        filtered = trades[
            trades["symbol"].isin(sel_symbols)
            & trades["side"].isin(sel_sides)
            & trades["placed_agent"].fillna("unknown").isin(sel_agents)
            & fill_dates.between(start_date, end_date)
        ]

        gross = filtered["quantity"] * filtered["average_price"]
        buys = filtered["side"] == "buy"
        invested = gross[buys].sum()
        proceeds = gross[~buys].sum()

        left, right = st.columns([1, 1.6], gap="large")
        with left:
            statement("Activity", [
                ("Bought", money(invested), f"{int(buys.sum()):,} fills", False),
                ("Sold", money(proceeds), f"{int((~buys).sum()):,} fills", False),
                ("Fees", money(filtered["fees"].fillna(0).sum()), None, False),
                ("Net invested", money(invested - proceeds), None, True),
            ])
            note("Capital committed — what you paid in, less what you took "
                 "back out. Not portfolio value; see Holdings for that.")

        with right:
            section("Net invested over time")
            curve = (
                pd.DataFrame({
                    "date": fill_dates.loc[filtered.index],
                    "flow": gross.where(buys, -gross),
                })
                .groupby("date", as_index=False)["flow"].sum()
                .sort_values("date")
            )
            if curve.empty:
                note("No trades match the current filters.")
            else:
                curve["Net invested"] = curve["flow"].cumsum()
                st.area_chart(curve.set_index("date")[["Net invested"]],
                              color="#1C5CAB", height=260)

        section(f"Fills · {len(filtered):,} of {len(trades):,}")
        # status and asset_type are constant, created_at duplicates
        # filled_at, and the order id is 36 characters of noise — all four
        # stay in the database and the Excel export if ever needed.
        st.dataframe(
            filtered.assign(amount=gross)[
                ["filled_at", "symbol", "side", "quantity", "average_price",
                 "amount", "fees", "placed_agent"]],
            use_container_width=True, hide_index=True, height=420,
            column_config={
                "filled_at": st.column_config.DatetimeColumn(
                    "Filled", format="YYYY-MM-DD HH:mm"),
                "symbol": st.column_config.TextColumn("Symbol", width="small"),
                "side": st.column_config.TextColumn("Side", width="small"),
                "quantity": st.column_config.NumberColumn(
                    "Shares", format="%.6f"),
                "average_price": st.column_config.NumberColumn(
                    "Price", format="dollar"),
                "amount": st.column_config.NumberColumn(
                    "Amount", format="dollar"),
                "fees": st.column_config.NumberColumn("Fees", format="dollar"),
                "placed_agent": st.column_config.TextColumn("Placed by"),
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
        # Guard the divide: a zero cost basis would blow up.
        h["unrealized_pct"] = h.apply(
            lambda r: (r["unrealized"] / r["cost_basis"])
            if r["cost_basis"] else None, axis=1)
        h["weight"] = h["market_value"] / h["market_value"].sum()

        cost = h["cost_basis"].sum()
        gain = h["unrealized"].sum()
        top = h.iloc[0]

        left, right = st.columns([1, 1.6], gap="large")
        with left:
            statement("Portfolio", [
                ("Positions", f"{len(h)}", None, False),
                ("Cost basis", money(cost), None, False),
                ("Market value", money(h["market_value"].sum()), None, False),
                ("Unrealized", tinted(gain),
                 tinted(gain, f"{gain / cost:+.2%}") if cost else None, True),
            ])
            statement("Concentration", [
                ("Largest", top["symbol"], f"{top['weight']:.1%}", False),
                ("Top three", ", ".join(h["symbol"].head(3)),
                 f"{h['weight'].head(3).sum():.1%}", False),
            ])
            note("Unrealized P&L moves with the market and is not money you "
                 "have received. Priced as of the snapshot above.")

        with right:
            section("Market value by position")
            ranked_bars(h[["symbol", "market_value"]], "market_value", "Value")

        section("Positions")
        st.dataframe(
            h[["symbol", "quantity", "average_buy_price", "last_price",
               "cost_basis", "market_value", "unrealized", "unrealized_pct",
               "weight"]],
            use_container_width=True, hide_index=True,
            column_config={
                "symbol": st.column_config.TextColumn("Symbol", width="small"),
                "quantity": st.column_config.NumberColumn(
                    "Shares", format="%.6f"),
                "average_buy_price": st.column_config.NumberColumn(
                    "Avg cost", format="dollar"),
                "last_price": st.column_config.NumberColumn(
                    "Last", format="dollar"),
                "cost_basis": st.column_config.NumberColumn(
                    "Cost", format="dollar"),
                "market_value": st.column_config.NumberColumn(
                    "Value", format="dollar"),
                "unrealized": st.column_config.NumberColumn(
                    "Unrealized", format="accounting"),
                "unrealized_pct": st.column_config.NumberColumn(
                    "Return", format="percent"),
                "weight": st.column_config.NumberColumn(
                    "Weight", format="percent"),
            },
        )
        note("Avg cost comes from Robinhood and already reflects partial "
             "sells, so it can look unlike your trade history for a position "
             "you have mostly sold out of.")


# =====================================================================
# Realized P&L — gains actually taken, per symbol
# =====================================================================
with tab_realized:
    if realized.empty:
        st.info(f"No realized P&L synced yet. {SYNC_HINT}")
    else:
        by_symbol = (
            realized.groupby("symbol", as_index=False)
            .agg(closing_trades=("realized_gain", "size"),
                 realized_gain=("realized_gain", "sum"))
            .sort_values("realized_gain", ascending=False)
        )
        total = realized["realized_gain"].sum()
        winners = by_symbol[by_symbol["realized_gain"] > 0]
        losers = by_symbol[by_symbol["realized_gain"] < 0]

        left, right = st.columns([1, 1.6], gap="large")
        with left:
            statement("Realized", [
                ("Closing trades", f"{len(realized)}", None, False),
                ("Gains", tinted(1, money(winners["realized_gain"].sum())),
                 f"{len(winners)} symbols", False),
                ("Losses", tinted(-1, money(losers["realized_gain"].sum())),
                 f"{len(losers)} symbols", False),
                ("Net realized", tinted(total), None, True),
            ])
            note("Money already received on positions you have closed. "
                 "Separate from unrealized gains on what you still hold — "
                 "the two are never added together here.")

        with right:
            section("Realized by symbol")
            ranked_bars(by_symbol[["symbol", "realized_gain"]],
                        "realized_gain", "Realized", diverging=True)

        section("By symbol")
        st.dataframe(
            by_symbol, use_container_width=True, hide_index=True,
            column_config={
                "symbol": st.column_config.TextColumn("Symbol", width="small"),
                "closing_trades": st.column_config.NumberColumn("Trades"),
                "realized_gain": st.column_config.NumberColumn(
                    "Realized", format="accounting"),
            },
        )

        section(f"Every closing trade · {len(realized)}")
        st.dataframe(
            realized[["timestamp", "symbol", "quantity", "price",
                      "realized_gain"]],
            use_container_width=True, hide_index=True,
            column_config={
                "timestamp": st.column_config.DatetimeColumn(
                    "Closed", format="YYYY-MM-DD HH:mm"),
                "symbol": st.column_config.TextColumn("Symbol", width="small"),
                "quantity": st.column_config.NumberColumn(
                    "Shares", format="%.6f"),
                "price": st.column_config.NumberColumn(
                    "Close price", format="dollar"),
                "realized_gain": st.column_config.NumberColumn(
                    "Realized", format="accounting"),
            },
        )
        note("Figures come from Robinhood's own realized-P&L endpoint, not "
             "recomputed here — informational, not a tax document.")
