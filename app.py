"""
app.py — Streamlit dashboard for viewing synced Robinhood activity.

Run with:  streamlit run app.py     (or double-click Trade Dashboard.command)

View-only: reads trades.db, never touches Robinhood. To pull new data run
`python sync_robinhood.py` first.

Five tabs, because they answer different questions from different data:
  Trades       — immutable history of every fill.
  Holdings     — what you own now, and the gain you haven't taken yet.
  Realized P&L — the gain you HAVE taken, on positions already closed.
  Performance  — how the money did, against the index and over time.
  Stock        — one symbol's price, with your own trades marked on it.
                 Opened by clicking a symbol in Holdings or Realized P&L.

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

from datetime import date, datetime
from functools import partial

import altair as alt
import pandas as pd
import streamlit as st

import analytics as an
import db

ACCENT, GAIN, LOSS = "#1C5CAB", "#0F7A4A", "#B3261E"
INK, MUTED, PAPER = "#1A1A18", "#6B6B66", "#FBFBFA"

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
  .masthead .asof.stale { color: var(--loss); font-weight: 600; }

  /* Old data gets one ruled line under the masthead, not a coloured box:
     the red rule and the ⚠︎ say "attention" without a second alert style. */
  .stale-note {
    font-size: 13px; line-height: 1.6; color: var(--ink);
    border-left: 2px solid var(--loss); padding: 2px 0 2px var(--s3);
    margin: calc(-1 * var(--s2)) 0 var(--s4) 0;
  }
  /* Streamlit sets inline code in green, which on this page means "gain". */
  .stale-note code { font-size: 12px; color: var(--ink); background: #F2F2EF; }

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

  .note { font-size: 12px; color: var(--muted); line-height: 1.6;
          margin-top: var(--s2); }

  /* ---- widget labels: set like the section headings, kept quiet ---- */
  [data-testid="stMain"] [data-testid="stWidgetLabel"] p {
    font-family: var(--mono); font-size: 11px; letter-spacing: 0.08em;
    text-transform: uppercase; color: var(--muted);
  }
  [data-testid="stButtonGroup"] button p,
  [data-testid="stBaseButton-tertiary"] p {
    font-family: var(--mono); font-size: 12px;
  }

  /* ---- Stock tab: the quote line ----------------------------------- */
  .quote {
    display: flex; align-items: baseline; gap: var(--s3); flex-wrap: wrap;
    margin: var(--s3) 0 var(--s1) 0;
  }
  .quote-sym {
    font-family: var(--mono); font-size: 28px; font-weight: 600;
    letter-spacing: 0.02em; color: var(--ink);
  }
  .quote-px { font-family: var(--mono); font-size: 22px; color: var(--ink); }
  .quote-chg { font-family: var(--mono); font-size: 15px; }
  .quote-note { font-family: var(--mono); font-size: 12px; color: var(--muted); }
  .quote-sub {
    display: flex; align-items: center; gap: var(--s4); flex-wrap: wrap;
    margin-bottom: var(--s3);
  }

  /* 52-week range: a hairline with the latest close ticked on it, the
     way a printed quote sheet shows it — no gauge, no gradient. */
  .range52 {
    display: flex; align-items: center; gap: var(--s2);
    font-family: var(--mono); font-size: 12px; color: var(--muted);
    min-width: 280px; flex: 0 1 420px;
  }
  .range52 .track {
    position: relative; flex: 1; height: 1px; background: #B8B8B2;
  }
  .range52 .tick {
    position: absolute; top: -5px; width: 2px; height: 11px;
    background: var(--ink); transform: translateX(-1px);
  }

  /* chart key: each glyph drawn in the colour of the mark it names */
  .key {
    display: flex; flex-wrap: wrap; gap: var(--s1) var(--s4);
    font-size: 12px; color: var(--muted); margin: var(--s1) 0 var(--s2) 0;
  }
  .key b { font-weight: normal; margin-right: 6px; }
  /* the chart's B/S labels, drawn as they appear on the chart */
  .key b.badge {
    display: inline-block; width: 15px; height: 15px; box-sizing: border-box;
    border: 1.5px solid; border-radius: 50%; text-align: center;
    font: 600 9px/12px var(--mono); vertical-align: 1px;
  }
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


@st.cache_data(ttl=30)
def load_prices() -> pd.DataFrame:
    with db.get_connection() as conn:
        return pd.DataFrame([dict(r) for r in db.get_daily_prices(conn)])


@st.cache_data(ttl=30)
def load_opening() -> pd.DataFrame:
    with db.get_connection() as conn:
        return pd.DataFrame([dict(r) for r in db.get_opening_balances(conn)])


@st.cache_data(ttl=30)
def load_account() -> pd.DataFrame:
    with db.get_connection() as conn:
        return pd.DataFrame([dict(r) for r in db.get_portfolio_snapshots(conn)])


trades = load_trades()
positions = load_positions()
realized = load_realized()
prices = load_prices()
opening = load_opening()
account = load_account()

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


def ranked_bars(frame, value_col, money_label, diverging=False, pick_key=None):
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

    `pick_key`, when given, makes each bar a doorway: clicking one opens
    that symbol on the Stock tab.
    """
    colour = (
        alt.condition(alt.datum[value_col] >= 0,
                      alt.value(GAIN), alt.value(LOSS))
        if diverging else alt.value(ACCENT)
    )
    chart = (
        alt.Chart(frame)
        .mark_bar(height=14, cursor="pointer" if pick_key else "default")
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
    )
    if pick_key:
        chart = chart.add_params(
            alt.selection_point(name="pick", fields=["symbol"], on="click"))
    chart = (chart.properties(height=max(180, 26 * len(frame)))
                  .configure_view(stroke=None))
    st.altair_chart(chart, width="stretch", key=pick_key,
                    on_select=(partial(_open_from_bars, pick_key)
                               if pick_key else "ignore"))


def note(text):
    st.markdown(f'<div class="note">{text}</div>', unsafe_allow_html=True)


def shares_text(quantity: float) -> str:
    """Share counts without padding: 7, not 7.000000; 0.522477 kept whole."""
    return f"{quantity:,.6f}".rstrip("0").rstrip(".")


def holdings_table(positions: pd.DataFrame) -> pd.DataFrame:
    """Positions with cost, value, unrealized and weight worked out.

    Shared by the Holdings tab and the Stock tab's position block, so the
    two can never disagree about the same holding.
    """
    h = positions.copy()
    h["cost_basis"] = h["quantity"] * h["average_buy_price"]
    h["market_value"] = h["quantity"] * h["last_price"]
    h["unrealized"] = h["market_value"] - h["cost_basis"]
    # Guard the divide: a zero cost basis would blow up.
    h["unrealized_pct"] = h.apply(
        lambda r: (r["unrealized"] / r["cost_basis"])
        if r["cost_basis"] else None, axis=1)
    h["weight"] = h["market_value"] / h["market_value"].sum()
    return h


def fill_phrase(fill) -> str:
    """One fill in words, for chart tooltips: 'Sold 3 @ $180.20 (+$12.40)'."""
    if fill["placed_agent"] == "recurring":
        return f"Recurring buy {money(fill['amount'])}"
    verb = "Bought" if fill["side"] == "buy" else "Sold"
    text = f"{verb} {shares_text(fill['quantity'])} @ {money(fill['price'])}"
    if fill["side"] == "sell" and pd.notna(fill["realized"]):
        sign = "+" if fill["realized"] > 0 else ""
        text += f" ({sign}{money(fill['realized'])} realized)"
    return text


def chart_key(items):
    """Legend for the Stock chart: (glyph HTML, label) per mark shown."""
    parts = "".join(f"<span>{glyph}{label}</span>" for glyph, label in items)
    st.markdown(f'<div class="key">{parts}</div>', unsafe_allow_html=True)


def key_glyph(text: str, colour: str) -> str:
    return f'<b style="color:{colour}">{text}</b>'


def key_badge(letter: str, look: tuple) -> str:
    """A key entry drawn like the chart's trade label; `look` from BADGE_LOOK."""
    fill, ink, line = look
    return (f'<b class="badge" style="background:{fill};color:{ink};'
            f'border-color:{line if fill == PAPER else fill}">{letter}</b>')


def relative_age(age: pd.Timedelta) -> str:
    """How long ago, in the unit a person would say it: '5 h ago', '6 days ago'."""
    minutes = age.total_seconds() / 60
    if minutes < 2:
        return "just now"
    if minutes < 90:
        return f"{minutes:.0f} min ago"
    # Hours up to a day and a half, so the smallest day count is "2 days".
    if minutes < 36 * 60:
        return f"{minutes / 60:.0f} h ago"
    return f"{minutes / (24 * 60):.0f} days ago"


def line_chart(wide: pd.DataFrame, colours: dict, height: int = 280,
               step: bool = False) -> None:
    """
    Money over time, one line per column of `wide`, with a crosshair.

    wide:    indexed by date (YYYY-MM-DD strings or dates), one column per
             series. Column order is legend order.
    colours: series name -> colour, keyed like the columns.
    step:    hold each value flat until the next one. Right for a running
             total that only moves on days something happened — a sloped
             line between two fills would draw money moving that didn't.

    The crosshair is the Stock chart's: it snaps to the nearest day and
    lists every series for that day in one tooltip, so reading the gap
    between two lines takes one hover instead of two careful ones. A
    single series gets no legend; the section title above names it.
    """
    names = list(wide.columns)
    # Positional field names (s0, s1, …) with the real names carried as
    # titles: "Same $ into SPY" would otherwise have to survive Vega-Lite's
    # field-name parsing.
    fields = [f"s{i}" for i in range(len(names))]
    data = wide.set_axis(fields, axis=1).rename_axis("date").reset_index()
    data["date"] = pd.to_datetime(data["date"])
    long = data.melt("date", var_name="field", value_name="amount")
    long["series"] = long["field"].map(dict(zip(fields, names)))

    x = alt.X("date:T", title=None,
              axis=alt.Axis(grid=False, domain=False, labelColor=MUTED,
                            tickCount=6))
    colour = alt.Color("series:N", title=None,
                       scale=alt.Scale(domain=names,
                                       range=[colours[n] for n in names]),
                       legend=(alt.Legend(orient="top-left", labelColor=INK)
                               if len(names) > 1 else None))
    hover = alt.selection_point(name="hover", nearest=True, on="pointerover",
                                clear="pointerout", fields=["date"],
                                empty=False)

    lines = (alt.Chart(long)
             .mark_line(strokeWidth=2,
                        interpolate="step-after" if step else "linear")
             .encode(x=x, color=colour,
                     y=alt.Y("amount:Q", title=None,
                             scale=alt.Scale(zero=False, padding=10),
                             axis=alt.Axis(format="$,.0f", grid=True,
                                           gridColor="#EDEDE8", domain=False,
                                           labelColor=MUTED, tickCount=5))))
    # Below the crosshair's hit layer, as on the Stock chart, so a dot can
    # never steal the pointer from the rule that owns the tooltip.
    dots = (alt.Chart(long).mark_point(filled=True, size=36, opacity=1)
            .encode(x=x, y="amount:Q", color=colour)
            .transform_filter(hover))
    rule = (alt.Chart(data).mark_rule(color=MUTED, strokeWidth=1)
            .encode(x=x,
                    opacity=alt.condition(hover, alt.value(0.7), alt.value(0)),
                    tooltip=[alt.Tooltip("date:T", title="Date",
                                         format="%a %b %-d, %Y")]
                    + [alt.Tooltip(f"{f}:Q", title=n, format="$,.2f")
                       for f, n in zip(fields, names)])
            .add_params(hover))
    st.altair_chart(alt.layer(lines, dots, rule).properties(height=height)
                    .configure_view(stroke=None), width="stretch")


# Trade labels on the Stock chart, in pixels: badge radius, the shortest
# leader, and the clear space kept between two badges.
BADGE_R, LEADER, BADGE_GAP = 8, 14, 3
# The price plot's height is fixed, so once its y domain is fixed a pixel
# converts exactly to dollars. Its WIDTH stretches with the window and can
# only be estimated, which matters for deciding whether two labels collide.
# Guessing low merges labels that had room; guessing high lets them touch.
# The page is capped at 1280px, which puts the plot near 950px on a laptop
# and never much past 1050.
PLOT_PX_ESTIMATE = 900
# (fill, letter, line) per kind of label. A loss is outlined rather than
# filled because green/red alone fails deuteranopia (ΔE 6.0, the same pair
# as ranked_bars), so fill-vs-outline carries the sign a second time.
BADGE_LOOK = {"buy": (ACCENT, PAPER, ACCENT), "gain": (GAIN, PAPER, GAIN),
              "loss": (PAPER, LOSS, LOSS), "none": (MUTED, PAPER, MUTED)}


def outcome(side: str, realized: float) -> str:
    """Which BADGE_LOOK a fill, or a label's fills taken together, wears."""
    if side == "buy":
        return "buy"
    if pd.notna(realized) and realized > 0:
        return "gain"
    if pd.notna(realized) and realized < 0:
        return "loss"
    return "none"     # breakeven, or Robinhood had no row for it


def y_domain(needs, height):
    """
    A y domain keeping each (value, px_above, px_below) in `needs` at least
    that many pixels clear of the plot's top and bottom edges.

    The room a label needs is fixed in pixels but the domain is in dollars,
    and widening the domain changes what a pixel is worth, so this iterates
    to the fixed point. Each pass changes the span by the margins' share of
    the plot height — well under a third here — so it settles within a few.
    """
    lo = min(v for v, _, _ in needs)
    hi = max(v for v, _, _ in needs)
    span = (hi - lo) or abs(hi) * 0.02 or 1.0
    for _ in range(40):
        hi = max(v + above * span / height for v, above, _ in needs)
        lo = min(v - below * span / height for v, _, below in needs)
        span = hi - lo
    return lo, hi


def trade_labels(manual, x0, x1, needs, height, floor_px=0):
    """
    Group manual fills into B/S labels that don't overprint each other or
    another trade's dot, and find the y domain that leaves room for them.

    Every trading day starts as its own label, one per side. Two labels of
    the SAME side whose badges would touch at the estimated plot width
    merge, and so does everything between them, so a label always covers
    one unbroken run of dates. Collisions are tested in both dimensions:
    buys at $100 and $90 two days apart sit at different heights and keep
    a badge each.

    A buy and a sell can't merge, so when a label touches one of the other
    side's, or lands on a dot that isn't its own, the later label's leader
    grows by one badge instead, up to MAX_STEPS times. Past that it is left
    to overlap; the tooltips still separate them.

    Each step moves badges and so the domain, which can cause a new
    collision, so it repeats until nothing touches. Buys hang below their
    LOWEST fill and sells sit above their HIGHEST, so a label never covers
    one of its own fills. floor_px is extra room under the buys for the
    recurring-buy ticks along the bottom edge.

    Returns (labels, fills, lo, hi). `fills` is `manual` oldest first with a
    `label` column; `labels` has one row per label, with its badge centre and
    the bracket its leaders run to, in dollars.
    """
    MAX_STEPS, step = 3, 2 * BADGE_R + BADGE_GAP
    fills = manual.sort_values("filled_at").reset_index(drop=True)
    groups = [list(ix) for _, ix in
              sorted(fills.groupby(["side", "date"]).indices.items())]
    extra = [0] * len(groups)      # px added to each label's leader
    reach = LEADER + 2 * BADGE_R + 2

    def px_x(day):
        return (pd.Timestamp(day) - x0) / (x1 - x0) * PLOT_PX_ESTIMATE

    def describe(ix):
        g = fills.iloc[ix]
        side = g["side"].iloc[0]
        first, last = (pd.Timestamp(g["date"].min()),
                       pd.Timestamp(g["date"].max()))
        return dict(side=side, n=len(g), first=first, last=last,
                    mid=first + (last - first) / 2,
                    end=(g["price"].min() if side == "buy"
                         else g["price"].max()))

    def box(lab, more, lo, hi):
        down = 1 if lab["side"] == "buy" else -1
        cx = px_x(lab["mid"])
        cy = (height * (hi - lab["end"]) / (hi - lo)
              + down * (LEADER + more + BADGE_R))
        # Menlo at 11px is ~6.6px a character; the count sits right of it.
        count = len(f"×{lab['n']}") * 6.6 + 3 if lab["n"] > 1 else 0
        return cx - BADGE_R, cx + BADGE_R + count, cy - BADGE_R, cy + BADGE_R

    def touch(a, b):
        return (a[0] < b[1] + BADGE_GAP and b[0] < a[1] + BADGE_GAP
                and a[2] < b[3] + BADGE_GAP and b[2] < a[3] + BADGE_GAP)

    dot_x = fills["date"].map(px_x).to_numpy()
    while True:
        labels = [describe(ix) for ix in groups]
        lo, hi = y_domain(
            needs + [(lab["end"], 0, reach + more + floor_px)
                     if lab["side"] == "buy" else (lab["end"], reach + more, 0)
                     for lab, more in zip(labels, extra)], height)
        boxes = [box(lab, more, lo, hi) for lab, more in zip(labels, extra)]
        # Groups are sorted by side, then date, so i..j is one side's run.
        hit = next(((i, j) for i in range(len(groups))
                    for j in range(i + 1, len(groups))
                    if labels[i]["side"] == labels[j]["side"]
                    and touch(boxes[i], boxes[j])), None)
        if hit is not None:
            i, j = hit
            groups[i:j + 1] = [sum(groups[i:j + 1], [])]
            extra[i:j + 1] = [0]
            continue

        dots = [(x - 3, x + 3, y - 3, y + 3) for x, y in
                zip(dot_x, height * (hi - fills["price"].to_numpy()) / (hi - lo))]
        order = sorted(range(len(groups)), key=lambda k: labels[k]["mid"])
        rank = {k: r for r, k in enumerate(order)}
        crowded = next(
            (k for k in order if extra[k] < MAX_STEPS * step and (
                any(touch(boxes[k], boxes[j]) for j in range(len(groups))
                    if labels[j]["side"] != labels[k]["side"]
                    and rank[j] < rank[k])
                or any(touch(boxes[k], dots[f]) for f in range(len(fills))
                       if f not in groups[k]))), None)
        if crowded is None:
            break
        extra[crowded] += step

    per_px = (hi - lo) / height
    for n, (lab, ix, more) in enumerate(zip(labels, groups, extra)):
        down = -1 if lab["side"] == "buy" else 1
        g = fills.iloc[ix]
        net = g["realized"].sum(min_count=1)
        lab.update(look=outcome(lab["side"], net),
                   bracket=lab["end"] + down * (LEADER + more) * per_px,
                   centre=(lab["end"]
                           + down * (LEADER + more + BADGE_R) * per_px),
                   **label_tip(g, lab, net))
        fills.loc[ix, "label"] = n
    return pd.DataFrame(labels), fills, lo, hi


def label_tip(g, lab, net) -> dict:
    """Tooltip text for one B/S label: when, what, and how much in total."""
    if lab["first"] == lab["last"]:
        when = lab["first"].strftime("%a %b %-d, %Y")
    elif lab["first"].year == lab["last"].year:
        when = (f"{lab['first'].strftime('%b %-d')} – "
                f"{lab['last'].strftime('%b %-d, %Y')}")
    else:
        when = (f"{lab['first'].strftime('%b %-d, %Y')} – "
                f"{lab['last'].strftime('%b %-d, %Y')}")
    if len(g) == 1:
        what = fill_phrase(g.iloc[0])
    else:
        noun = "buys" if lab["side"] == "buy" else "sells"
        what = (f"{len(g)} {noun}, {shares_text(g['quantity'].sum())} shares "
                f"@ {money(g['price'].min())} – {money(g['price'].max())}")
        if lab["side"] == "sell" and pd.notna(net):
            what += f" ({'+' if net > 0 else ''}{money(net)} realized, net)"
    return dict(when=when, what=what, total=money(g["amount"].sum()))


def stock_chart(closes, fills, shares, avg_cost=None, quote=None) -> bool:
    """
    One symbol's daily close with your trades marked on it, shares beneath.
    Returns True when any B/S label covers more than one trade, so the
    chart key can explain the count.

    closes:   daily closes for the window shown (YYYY-MM-DD index).
    fills:    symbol_fills() rows inside that window.
    shares:   shares held per day, same index as `closes`.
    avg_cost: Robinhood's average cost, drawn as a dashed reference line.
    quote:    (date, price) of the snapshot quote, drawn as a hollow end
              point so the chart ends where the header price says it does.

    Encoding, and why:
      - The price line is ink, not the accent. Here the line is context;
        the trades are the story, so they get the colour.
      - Each trade you placed is a dot at its exact fill price, with a
        dashed leader to a lettered badge: B below for a buy, S above for
        a sell. The dot pins the position; the letter names it without
        a legend. Sell badges are green for a gain Robinhood booked, red
        and outlined for a loss — BADGE_LOOK says why both.
      - Trades too close to label one by one share a badge with a count
        (trade_labels): a leader still runs from every dot to a bracket
        over the badge, so no fill loses its position. Triangles sized by
        dollars were tried first and piled into one smear wherever you
        traded on consecutive days.
      - Recurring buys are ticks along the bottom edge. A daily schedule
        adds ~250 a year; drawn as triangles they would bury the price line.
      - Shares held is its own strip below, not a second y-axis on the
        price plot — two scales on one plot invent a correlation.
      - A crosshair snaps to the nearest day and lists that day's close,
        shares held and trades, so nobody has to land the pointer on a
        2px line.

    The two are separate charts rather than an alt.vconcat: Streamlit can
    stretch a single or layered view to the column, but sizes each half of
    a concat to the full width and then adds the axis on top, so the plot
    ran past the page's right edge. Lining them up instead takes a fixed
    y-axis width on both and an explicit, shared date domain — the quote
    point can sit a day past the last close, which would otherwise stretch
    only the top chart.
    """
    # "fit-x": stretch to the column's width, but let the height mean the
    # PLOT's height. Under Streamlit's default "fit" the title and date axis
    # come out of the height too, which crushed the 64px strip to ~25px and
    # clipped its dates.
    price_h, strip_h = 300, 64
    fit_x = alt.AutoSizeParams(type="fit-x", contains="padding")
    days = pd.to_datetime(closes.index)
    held_shares = shares.reindex(closes.index).to_numpy()

    def vl_date(day):
        t = pd.Timestamp(day)
        return alt.DateTime(year=t.year, month=t.month, date=t.day)

    last = max(closes.index[-1], quote[0]) if quote else closes.index[-1]
    x_scale = alt.Scale(domain=[vl_date(closes.index[0]), vl_date(last)],
                        nice=False)
    y_axis = dict(domain=False, ticks=False, labelColor=MUTED,
                  minExtent=52, maxExtent=52)

    per_day = (fills.iloc[::-1].assign(what=fills.iloc[::-1].apply(
                   fill_phrase, axis=1))
               .groupby("date")["what"].agg(" · ".join)
               if not fills.empty else pd.Series(dtype=str))
    price = pd.DataFrame({"date": days, "close": closes.to_numpy(),
                          "shares": held_shares,
                          "trades": closes.index.map(per_day).fillna("—")})

    # The y domain is set here rather than left to Vega, because the trade
    # labels are placed in pixels and need to know what a pixel is worth.
    # 14px around the price line is the padding Vega used to add.
    manual = fills[fills["placed_agent"] != "recurring"]
    recurring = fills[fills["placed_agent"] == "recurring"]
    needs = [(closes.min(), 14, 14), (closes.max(), 14, 14)]
    if avg_cost:
        needs.append((avg_cost, 18, 4))      # its label sits above the line
    if quote:
        needs.append((quote[1], 6, 6))
    if manual.empty:
        lo, hi = y_domain(needs, price_h)
    else:
        labels, placed, lo, hi = trade_labels(
            manual, days[0], pd.Timestamp(last), needs, price_h,
            floor_px=10 if not recurring.empty else 0)
    y_scale = alt.Scale(domain=[lo, hi], nice=False, zero=False)

    def y_of(field):
        return alt.Y(field, title=None, scale=y_scale)

    y = alt.Y("close:Q", title=None, scale=y_scale,
              axis=alt.Axis(format="$,.0f", grid=True, gridColor="#EDEDE8",
                            tickCount=5, **y_axis))
    x_hidden = alt.X("date:T", title=None, axis=None, scale=x_scale)
    base = alt.Chart(price).encode(x=x_hidden)

    hover = alt.selection_point(name="hover", nearest=True, on="pointerover",
                                clear="pointerout", fields=["date"],
                                empty=False)
    layers = [base.mark_line(color=INK, strokeWidth=2).encode(y=y)]

    if avg_cost:
        ref = pd.DataFrame({"close": [avg_cost], "date": [days.max()],
                            "label": [f"avg cost {money(avg_cost)}"]})
        layers += [
            alt.Chart(ref).mark_rule(color=MUTED, strokeDash=[4, 3],
                                     strokeWidth=1).encode(y=y_of("close:Q")),
            alt.Chart(ref).mark_text(align="right", baseline="bottom", dy=-4,
                                     color=MUTED, font="Menlo, monospace",
                                     fontSize=11)
            .encode(x=x_hidden, y=y_of("close:Q"), text="label:N"),
        ]

    if not recurring.empty:
        layers.append(
            alt.Chart(pd.DataFrame({"date": pd.to_datetime(recurring["date"])}))
            .mark_tick(orient="vertical", color=ACCENT, opacity=0.55,
                       thickness=1, size=9)
            .encode(x=x_hidden, y=alt.value(price_h - 6)))

    # Below the crosshair's hit layer, so it can never steal the pointer.
    layers.append(base.mark_point(filled=True, color=INK, size=36)
                  .encode(y=y_of("close:Q"))
                  .transform_filter(hover))
    layers.append(
        base.mark_rule(color=MUTED, strokeWidth=1)
        .encode(opacity=alt.condition(hover, alt.value(0.7), alt.value(0)),
                tooltip=[alt.Tooltip("date:T", title="Date",
                                     format="%a %b %-d, %Y"),
                         alt.Tooltip("close:Q", title="Close",
                                     format="$,.2f"),
                         alt.Tooltip("shares:Q", title="Shares held",
                                     format=",.4~f"),
                         alt.Tooltip("trades:N", title="Your trades")])
        .add_params(hover))

    # Trade labels sit ABOVE the crosshair layer so their own tooltips win
    # when the pointer is on one.
    grouped = False
    if not manual.empty:
        look = labels["look"].map(BADGE_LOOK)
        labels = labels.assign(
            fill=look.str[0], ink=look.str[1], line=look.str[2],
            # A filled badge gets a ring of paper so a leader or the price
            # line passing behind it stops cleanly at its edge.
            ring=[PAPER if f != PAPER else l
                  for f, l in zip(look.str[0], look.str[2])],
            letter=labels["side"].map({"buy": "B", "sell": "S"}),
            count=[f"×{n}" if n > 1 else "" for n in labels["n"]])
        dot = placed.apply(lambda f: BADGE_LOOK[outcome(f["side"],
                                                        f["realized"])],
                           axis=1)
        label = placed["label"].astype(int)
        placed = placed.assign(
            date=pd.to_datetime(placed["date"]),
            bracket=label.map(labels["bracket"]),
            line=label.map(labels["line"]),
            fill=dot.str[0],
            ring=[PAPER if f != PAPER else l
                  for f, l in zip(dot.str[0], dot.str[2])],
            what=placed.apply(fill_phrase, axis=1))

        at_fill = alt.Chart(placed).encode(x=x_hidden, y=y_of("price:Q"))
        layers.append(
            at_fill.mark_rule(strokeDash=[2, 2], strokeWidth=1, opacity=0.8)
            .encode(y2="bracket:Q", color=alt.Color("line:N", scale=None)))
        spans = labels[labels["first"] < labels["last"]]
        if not spans.empty:
            layers.append(
                alt.Chart(spans).mark_rule(strokeWidth=1, opacity=0.8)
                .encode(x=alt.X("first:T", title=None, axis=None,
                                scale=x_scale),
                        x2="last:T", y=y_of("bracket:Q"),
                        color=alt.Color("line:N", scale=None)))
        layers.append(
            at_fill.mark_point(shape="circle", filled=True, size=30,
                               opacity=1, strokeWidth=1.5, cursor="default")
            .encode(fill=alt.Fill("fill:N", scale=None),
                    stroke=alt.Stroke("ring:N", scale=None),
                    tooltip=[alt.Tooltip("date:T", title="Date",
                                         format="%a %b %-d, %Y"),
                             alt.Tooltip("what:N", title="Trade"),
                             alt.Tooltip("amount:Q", title="Amount",
                                         format="$,.2f")]))

        badge = alt.Chart(labels).encode(
            x=alt.X("mid:T", title=None, axis=None, scale=x_scale),
            y=y_of("centre:Q"),
            tooltip=[alt.Tooltip("when:N", title="When"),
                     alt.Tooltip("what:N", title="Trades"),
                     alt.Tooltip("total:N", title="Amount")])
        layers += [
            badge.mark_point(shape="circle", filled=True, opacity=1,
                             size=(2 * BADGE_R) ** 2, strokeWidth=1.5,
                             cursor="default")
            .encode(fill=alt.Fill("fill:N", scale=None),
                    stroke=alt.Stroke("ring:N", scale=None)),
            badge.mark_text(font="Menlo, monospace", fontSize=10,
                            fontWeight="bold", align="center",
                            baseline="middle", cursor="default")
            .encode(text="letter:N", color=alt.Color("ink:N", scale=None)),
            badge.mark_text(font="Menlo, monospace", fontSize=11,
                            color=MUTED, align="left", baseline="middle",
                            dx=BADGE_R + 3)
            .encode(text="count:N"),
        ]
        grouped = bool((labels["n"] > 1).any())

    if quote:
        layers.append(
            alt.Chart(pd.DataFrame({"date": [pd.Timestamp(quote[0])],
                                    "close": [quote[1]]}))
            .mark_point(shape="circle", size=56, filled=True, fill=PAPER,
                        stroke=INK, strokeWidth=1.5)
            .encode(x=x_hidden, y=y_of("close:Q"),
                    tooltip=[alt.Tooltip("close:Q", title="Quote at snapshot",
                                         format="$,.2f")]))

    st.altair_chart(alt.layer(*layers)
                    .properties(height=price_h, autosize=fit_x)
                    .configure_view(stroke=None), width="stretch")

    strip = (
        alt.Chart(pd.DataFrame({"date": days, "shares": held_shares}))
        .mark_area(interpolate="step-after", color="#E4E4DF",
                   line={"color": MUTED, "strokeWidth": 1})
        .encode(x=alt.X("date:T", title=None, scale=x_scale,
                        axis=alt.Axis(grid=False, domain=False,
                                      labelColor=MUTED, tickCount=6)),
                # ",~r" rather than "~s" or "~g": SI prefixes turn a
                # 0.05-share holding into "50m", and ~g writes 50 as "5e+1".
                y=alt.Y("shares:Q", title=None,
                        axis=alt.Axis(grid=False, tickCount=2, format=",~r",
                                      **y_axis)),
                tooltip=[alt.Tooltip("date:T", title="Date",
                                     format="%b %-d, %Y"),
                         alt.Tooltip("shares:Q", title="Shares held",
                                     format=",.4~f")])
        .properties(height=strip_h, autosize=fit_x,
                    title=alt.TitleParams("SHARES HELD", anchor="start",
                                          font="Menlo, monospace",
                                          fontSize=10, fontWeight="normal",
                                          color=MUTED))
        .configure_view(stroke=None)
    )
    st.altair_chart(strip, width="stretch")
    return grouped


# =====================================================================
# Focus — which stock the Stock tab shows
# =====================================================================
# One piece of state with several writers — a row or bar in Holdings, a
# row or bar in Realized P&L, the pickers on the Stock tab itself — and a
# single reader, the Stock tab. Jumping there from another tab relies on
# st.tabs(key=..., on_change="rerun"), which makes the active tab settable
# from code and readable afterwards. The second half of that is what lets
# the URL carry ?symbol= only while the Stock tab is actually showing: a
# refresh or bookmark reopens that stock, without every other view
# dragging the parameter around.

held_symbols = positions["symbol"].tolist() if not positions.empty else []
priced = set(prices["symbol"]) if not prices.empty else set()
traded = set(trades["symbol"]) if not trades.empty else set()
# Every symbol ever traded has prices (the sync fetches them all), but a
# position opened since the last price top-up might not, so intersect.
closed_symbols = sorted((traded & priced) - set(held_symbols))

if "focus" not in st.session_state:
    wanted = st.query_params.get("symbol", "").upper()
    if wanted in held_symbols or wanted in closed_symbols:
        st.session_state["focus"] = wanted
        st.session_state["tab"] = "Stock"
    else:
        st.session_state["focus"] = next(
            iter(held_symbols + closed_symbols), None)


def open_stock(symbol: str) -> None:
    """Point the Stock tab at `symbol` and switch to it."""
    st.session_state["focus"] = symbol
    st.session_state["tab"] = "Stock"
    # The clicked row stays selected after the jump, so coming back and
    # clicking it again would DESELECT it rather than reopen the stock.
    # Bumping this re-keys every clickable table and chart (see clickable()),
    # handing each a fresh, empty selection.
    st.session_state["jumps"] = st.session_state.get("jumps", 0) + 1


def clickable(name: str) -> str:
    """Widget key for a table or chart whose selection opens the Stock tab."""
    return f"{name}-{st.session_state.get('jumps', 0)}"


ROW_OR_CELL = ["single-row", "single-cell"]


def _open_from_rows(key: str, symbols: list) -> None:
    """on_select for a ROW_OR_CELL table; `symbols` in display order.

    Row selection alone only fires from the narrow checkbox column at the
    table's left edge, which nobody finds — people click the symbol. Cell
    selection makes a click anywhere on the row count, and either kind
    names the row by position in the data as given, whatever the sort.
    """
    selection = st.session_state[key]["selection"]
    rows = list(selection.get("rows") or [])
    rows += [cell[0] for cell in selection.get("cells") or []]
    if rows:
        open_stock(symbols[rows[0]])


def _open_from_bars(key: str) -> None:
    """on_select for a ranked_bars chart with a 'pick' selection."""
    picked = st.session_state[key]["selection"].get("pick") or []
    if picked:
        open_stock(picked[0]["symbol"])


def _pick(key: str) -> None:
    """A Stock-tab picker changed. Deselecting the current pill is ignored,
    so the tab is never left showing nothing."""
    if st.session_state.get(key):
        st.session_state["focus"] = st.session_state[key]


h_all = holdings_table(positions) if not positions.empty else pd.DataFrame()


# =====================================================================
# Masthead
# =====================================================================

# How old the data is, said loudly once it's old enough to matter. The
# scheduled sync runs every 6 hours, so 12 means at least one run in a row
# has gone missing. It needs saying because nothing else on the page
# changes when the sync stops: every figure still renders, just frozen.
# The usual cause is Robinhood asking to sign in again, which an unattended
# run can't do, and that went unnoticed for six days once.
STALE_AFTER = pd.Timedelta(hours=12)

# Both stamps are written by the same successful sync. Positions are kept
# when Robinhood returns nothing (see db.replace_positions), so their as_of
# only moves forward when a sync actually learned something.
stamps = ([positions["as_of"].max()] if not positions.empty else []) + (
    [account["snapshot_at"].max()] if not account.empty else [])
if stamps:
    synced = max(pd.Timestamp(s) for s in stamps)
    # The app only ever runs on this Mac, so the server's zone is the reader's.
    local = synced.tz_convert(datetime.now().astimezone().tzinfo)
    age = pd.Timestamp.now(tz="UTC") - synced
    stale = age > STALE_AFTER
    as_of = (f"synced {relative_age(age)} · "
             f"{local.strftime('%a %b %-d, %-I:%M %p')}")
else:
    stale, as_of = False, "not synced yet"

st.markdown(
    f'<div class="masthead"><h1>Trade Recorder</h1>'
    f'<span class="asof{" stale" if stale else ""}">Robinhood · {as_of}'
    f'</span></div>',
    unsafe_allow_html=True,
)
if stale:
    st.markdown(
        f'<div class="stale-note">⚠︎ <b>Every figure below is '
        f'{relative_age(age).removesuffix(" ago")} old.</b> The scheduled sync '
        f'runs every 6 hours, so something has stopped it: usually Robinhood '
        f'wanting you to sign in again, which it can\'t do unattended; '
        f'sometimes just a Mac that has been asleep. Run '
        f'<code>python sync_robinhood.py</code> in Terminal once, then '
        f'refresh this page.</div>',
        unsafe_allow_html=True)

tab_trades, tab_holdings, tab_realized, tab_performance, tab_stock = st.tabs(
    ["Trades", "Holdings", "Realized P&L", "Performance", "Stock"],
    key="tab", on_change="rerun",
)

if tab_stock.open and st.session_state.get("focus"):
    st.query_params["symbol"] = st.session_state["focus"]
elif "symbol" in st.query_params:
    del st.query_params["symbol"]


# =====================================================================
# Trades — every fill, filterable
# =====================================================================
with tab_trades:
    if trades.empty:
        st.info(f"No trades synced yet. {SYNC_HINT}")
    else:
        # The filters sit in one row above what they filter. In the sidebar
        # they were a second place to look, they stayed on screen over tabs
        # they didn't apply to, and they took 250px from every chart.
        #
        # Every one defaults to "everything" — an empty symbol list, "All"
        # on the rest — so an untouched page is the whole record, with no
        # row of 20 pre-selected chips. Each is bound to the URL under its
        # key (?side=Sells&by=You&range=3M), and Streamlit drops a parameter
        # again once its widget is back at the default, so a bookmark or a
        # refresh keeps a filtered view and a plain URL stays plain.
        sides = {"All": None, "Buys": "buy", "Sells": "sell"}
        # placed_agent separates trades you chose from automatic recurring
        # buys — most of the row count here is dollar-cost averaging.
        agent_names = {"user": "You", "recurring": "Recurring",
                       "unknown": "Unknown"}
        agents = trades["placed_agent"].fillna("unknown")
        order = list(agent_names)
        by = {"All": None} | {
            agent_names.get(a, a): a
            for a in sorted(agents.unique(),
                            key=lambda a: (order.index(a) if a in order
                                           else len(order), a))}
        ranges = list(an.RANGES) + ["Custom"]

        fill_dates = pd.to_datetime(
            trades["filled_at"], format="ISO8601", utc=True).dt.date
        earliest, latest = fill_dates.min(), fill_dates.max()

        def clear_filters():
            for k, v in {"symbols": [], "side": "All", "by": "All",
                         "range": "All"}.items():
                st.session_state[k] = v
            st.session_state.pop("dates", None)

        # Widths are the controls' own: narrower than ~190px and "Sells"
        # wraps onto a second line under "All | Buys".
        c_sym, c_side, c_by, c_range = st.columns([1.1, 1.0, 1.25, 2.1],
                                                  vertical_alignment="bottom")
        with c_sym:
            sel_symbols = st.multiselect(
                "Symbol", sorted(trades["symbol"].unique()),
                placeholder="All", key="symbols", bind="query-params")
        with c_side:
            side = st.segmented_control("Side", list(sides), default="All",
                                        required=True, key="side",
                                        bind="query-params")
        with c_by:
            agent = st.segmented_control(
                "Placed by", list(by), default="All", required=True,
                key="by", bind="query-params",
                help="“You” is a trade you placed; “Recurring” is an "
                     "automatic investment.")
        with c_range:
            window = st.segmented_control("Filled", ranges, default="All",
                                          required=True, key="range",
                                          bind="query-params")

        if window == "Custom":
            picked = st.date_input(
                "Filled between", value=(earliest, latest),
                min_value=earliest, max_value=latest, key="dates",
                bind="query-params", width=280)
            # Streamlit returns a 1-tuple midway through picking a range;
            # hold the full range until both ends exist or the table blanks.
            start_date, end_date = picked if (
                isinstance(picked, tuple) and len(picked) == 2
            ) else (earliest, latest)
        else:
            # Back from today, not from the newest fill: "last month" means
            # the calendar's last month even if you haven't traded lately.
            start = an.window_start(date.today().isoformat(), window)
            start_date = date.fromisoformat(start) if start else earliest
            end_date = latest

        filtered = trades[
            (trades["symbol"].isin(sel_symbols) if sel_symbols else True)
            & ((trades["side"] == sides[side]) if sides[side] else True)
            & ((agents == by[agent]) if by[agent] else True)
            & fill_dates.between(start_date, end_date)
        ]
        if sel_symbols or side != "All" or agent != "All" or window != "All":
            st.button("Clear filters", type="tertiary", icon=":material/close:",
                      on_click=clear_filters)

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
                line_chart(curve.set_index("date")[["Net invested"]],
                           {"Net invested": ACCENT}, height=260, step=True)

        section(f"Fills · {len(filtered):,} of {len(trades):,}")
        # status and asset_type are constant, created_at duplicates
        # filled_at, and the order id is 36 characters of noise — all four
        # stay in the database and the Excel export if ever needed.
        st.dataframe(
            filtered.assign(amount=gross)[
                ["filled_at", "symbol", "side", "quantity", "average_price",
                 "amount", "fees", "placed_agent"]],
            width="stretch", hide_index=True, height=420,
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
        h = h_all
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
            ranked_bars(h[["symbol", "market_value"]], "market_value", "Value",
                        pick_key=clickable("holdings-bars"))

        section("Positions")
        # Three months of closes per row: enough to say "has been falling"
        # or "just spiked" before anyone opens the Stock tab, short enough
        # to stay a glance. Drawn in ink, not green/red, because it is the
        # stock's move; the tinted columns beside it are your gain.
        if not prices.empty:
            since = an.window_start(prices["date"].max(), "3M")
            trend = {s: an.symbol_closes(prices, s, since).tolist()
                     for s in h["symbol"]}
        else:
            trend = {}
        # Percentages in points for a printf format, as in the Stock tab's
        # trade log: "percent" trims zeros, so 17.60% printed as "17.6%".
        table = h.assign(trend=h["symbol"].map(lambda s: trend.get(s, [])),
                         unrealized_pct=h["unrealized_pct"] * 100,
                         weight=h["weight"] * 100)[
            ["symbol", "quantity", "average_buy_price", "last_price", "trend",
             "cost_basis", "market_value", "unrealized", "unrealized_pct",
             "weight"]]

        def sign_colour(v):
            if pd.isna(v) or v == 0:
                return ""
            return f"color: {GAIN if v > 0 else LOSS}"

        positions_key = clickable("holdings-table")
        st.dataframe(
            table.style.map(sign_colour,
                            subset=["unrealized", "unrealized_pct"]),
            width="stretch", hide_index=True,
            key=positions_key, selection_mode=ROW_OR_CELL,
            on_select=partial(_open_from_rows, positions_key,
                              h["symbol"].tolist()),
            column_config={
                "symbol": st.column_config.TextColumn("Symbol", width="small"),
                "quantity": st.column_config.NumberColumn(
                    "Shares", format="%.6f"),
                "average_buy_price": st.column_config.NumberColumn(
                    "Avg cost", format="dollar"),
                "last_price": st.column_config.NumberColumn(
                    "Last", format="dollar"),
                "trend": st.column_config.LineChartColumn(
                    "3 months", width="small", color=INK,
                    help="Daily closes over the last three months — the "
                         "stock's price, not your return."),
                "cost_basis": st.column_config.NumberColumn(
                    "Cost", format="dollar"),
                "market_value": st.column_config.NumberColumn(
                    "Value", format="dollar"),
                "unrealized": st.column_config.NumberColumn(
                    "Unrealized", format="accounting"),
                "unrealized_pct": st.column_config.NumberColumn(
                    "Return", format="%+.2f%%"),
                "weight": st.column_config.NumberColumn(
                    "Weight", format="%.2f%%"),
            },
        )
        note("Select a row — or click a bar above — to open that stock's "
             "price history, with your trades marked on it, in the Stock tab.")
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
                        "realized_gain", "Realized", diverging=True,
                        pick_key=clickable("realized-bars"))

        section("By symbol")
        by_symbol_key = clickable("realized-table")
        st.dataframe(
            by_symbol, width="stretch", hide_index=True,
            key=by_symbol_key, selection_mode=ROW_OR_CELL,
            on_select=partial(_open_from_rows, by_symbol_key,
                              by_symbol["symbol"].tolist()),
            column_config={
                "symbol": st.column_config.TextColumn("Symbol", width="small"),
                "closing_trades": st.column_config.NumberColumn("Trades"),
                "realized_gain": st.column_config.NumberColumn(
                    "Realized", format="accounting"),
            },
        )
        note("Select a row or a bar to see where each sale landed on the "
             "stock's price chart, in the Stock tab.")

        section(f"Every closing trade · {len(realized)}")
        st.dataframe(
            realized[["timestamp", "symbol", "quantity", "price",
                      "realized_gain"]],
            width="stretch", hide_index=True,
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


# =====================================================================
# Performance — how the money actually did
# =====================================================================
#
# The other three tabs report WHAT HAPPENED. This one interprets it, which
# means every figure here needs its caveat travelling with it rather than
# sitting in a footnote nobody reads. Three in particular:
#
#   - Annualised MWR over a short effective holding period is arithmetically
#     right and rhetorically misleading, so the holding period is shown
#     beside it, not below it.
#   - Realized and unrealized stay in separate blocks, never summed.
#   - The recurring/manual split is indicative, not two audited accounts.
# =====================================================================
with tab_performance:
    if trades.empty or prices.empty:
        st.info(f"No price history yet. {SYNC_HINT}")
    else:
        value = an.portfolio_value(trades, prices, opening)
        prepared = an.prepare_trades(trades)
        flows = prepared.groupby("date")["cash_flow"].sum()

        if value.empty or len(value) < 2:
            st.info(f"Not enough price history to measure performance yet. {SYNC_HINT}")
        else:
            curve = value["value"]
            first_day, last_day = curve.index[0], curve.index[-1]
            span_days = (pd.Timestamp(last_day) - pd.Timestamp(first_day)).days

            twr = an.time_weighted_return(curve, flows)
            twr_ann = an.annualise(twr, span_days) if twr is not None else None
            mwr = an.money_weighted_return(trades, float(curve.iloc[-1]), last_day)
            held_days = an.capital_weighted_holding_days(trades, last_day)
            deployed = float(flows.sum())
            ending = float(curve.iloc[-1])

            # The range zooms the two charts and nothing else. Every figure
            # here is a whole-record measure — a 1M "TWR" would be a
            # different statistic wearing the same label — so the note says
            # so beside the control rather than leaving it to be guessed.
            c_zoom, c_zoom_note = st.columns([1.3, 2], gap="large",
                                             vertical_alignment="center")
            with c_zoom:
                zoom = st.segmented_control("Chart range", an.RANGES,
                                            default="All", required=True,
                                            key="perf_range")
            with c_zoom_note:
                note(f"Zooms the two charts only. Every figure on this tab "
                     f"covers the whole record, {first_day} to {last_day}.")
            zoom_start = an.window_start(last_day, zoom)
            zoom_label = "" if zoom == "All" else f" · {zoom}"

            def zoomed(frame):
                return frame[frame.index >= zoom_start] if zoom_start else frame

            # --- headline: value against the capital that built it --------
            left, right = st.columns([1, 1.6], gap="large")
            with left:
                statement("Position", [
                    ("Capital deployed", money(deployed), None, False),
                    ("Portfolio value", money(ending), None, False),
                    ("Gain", tinted(ending - deployed), None, True),
                    ("Unweighted", tinted(ending - deployed,
                                          f"{(ending / deployed - 1):+.2%}")
                     if deployed > 0 else "—", None, False),
                ])
                note("Capital deployed is money put into positions, not money "
                     "transferred to the broker — Robinhood exposes no transfer "
                     "history. Cash sitting uninvested is invisible here.")

            with right:
                section(f"Portfolio value against capital deployed{zoom_label}")
                # The GAP between these two lines is unrealized gain, read
                # visually. Plotting them together beats a derived third
                # line because it also shows the capital ramp that explains
                # why the annualised figures below run hot.
                # Cumulated over the whole record BEFORE zooming, so a zoomed
                # capital line sits at its true level instead of restarting
                # from zero at the window's edge.
                basis = flows.reindex(curve.index, fill_value=0.0).cumsum()
                line_chart(zoomed(pd.DataFrame({
                    "Portfolio value": curve,
                    "Capital deployed": basis,
                })), {"Portfolio value": ACCENT, "Capital deployed": "#A8A8A2"})
                note("The gap between the lines is unrealized gain. Where the "
                     "grey line steps up, capital was added — that is a "
                     "deposit, not performance.")

            st.divider()

            # --- against the index --------------------------------------
            # Placed before the return figures because it answers the
            # blunter question: was any of this worth it?
            bench = an.benchmark_comparison(trades, prices, curve,
                                            db.BENCHMARK_SYMBOL)
            if bench:
                section(f"Against {bench['symbol']} — same money, same days")
                b_left, b_right = st.columns([1, 1.6], gap="large")
                with b_left:
                    ahead = bench["difference"] >= 0
                    statement("Counterfactual", [
                        ("Your portfolio", money(bench["portfolio_value"]),
                         f"{bench['portfolio_return']:+.2%}", False),
                        (f"Same $ into {bench['symbol']}",
                         money(bench["benchmark_value"]),
                         f"{bench['benchmark_return']:+.2%}", False),
                        ("Ahead by" if ahead else "Behind by",
                         tinted(bench["difference"]),
                         f"{bench['excess_return']:+.2%}", True),
                    ])
                    note("Every dollar you put into a position, on the day you "
                         "put it in, buying the index instead. That is the fair "
                         "comparison for irregular contributions — measuring "
                         f"against {bench['symbol']}'s headline return would "
                         "assume all your money was invested from day one.")

                with b_right:
                    section(f"Both curves, same cash flows{zoom_label}")
                    index_name = f"Same $ into {bench['symbol']}"
                    line_chart(zoomed(pd.DataFrame({
                        "Your portfolio": curve.reindex(bench["curve"].index),
                        index_name: bench["curve"],
                    })), {"Your portfolio": ACCENT, index_name: "#C08A3E"})
                    note("Dividends are excluded on both sides, so neither is "
                         "given an edge the other doesn't get.")

                st.divider()

            # --- the two returns, and why they differ --------------------
            col_twr, col_mwr, col_dd = st.columns(3, gap="large")

            with col_twr:
                statement("Time-weighted return", [
                    ("Cumulative", tinted(twr or 0, f"{twr:+.2%}")
                     if twr is not None else "—", None, True),
                    ("Annualised", tinted(twr_ann or 0, f"{twr_ann:+.2%}")
                     if twr_ann is not None else "—", None, False),
                    ("Period", f"{span_days} days", None, False),
                ])
                note("Strips out when you added money, so it measures the "
                     "picks. This is what a fund quotes.")

            with col_mwr:
                statement("Return on deployed capital", [
                    ("Annualised (MWR)", tinted(mwr or 0, f"{mwr:+.2%}")
                     if mwr is not None else "—", None, True),
                    ("Avg dollar invested",
                     f"{held_days:,.0f} days" if held_days else "—", None, False),
                ])
                if held_days and held_days < 180:
                    note(f"⚠︎ Read with care. The average dollar has been "
                         f"invested {held_days:,.0f} days, so this annualises a "
                         f"short stretch — correct arithmetic, but not a rate "
                         f"you should expect to repeat.")
                else:
                    note("Weights by how much was invested when, so it "
                         "measures your outcome rather than the picks.")

            with col_dd:
                dd = an.max_drawdown(curve)
                depth, current = dd["depth"], dd["current"]
                statement("Drawdown", [
                    ("Worst decline", tinted(depth or 0, f"{depth:.2%}")
                     if depth is not None else "—", None, True),
                    ("Peak to trough",
                     f"{dd['peak_date']} → {dd['trough_date']}"
                     if dd["peak_date"] else "—", None, False),
                    ("Off peak now", tinted(current or 0, f"{current:.2%}")
                     if current is not None else "—", None, False),
                ])
                note("Worst peak-to-trough fall in portfolio value. Early "
                     "readings flatter: while contributions dominate, almost "
                     "every day sets a new high.")

            if twr is not None and mwr is not None:
                verdict = ("Your timing helped: money was working hardest while "
                           "the portfolio rose."
                           if mwr > (twr_ann or twr) else
                           "Your timing cost you: more money was invested during "
                           "the weaker stretches than the strong ones.")
                note(f"**Why the two differ.** {verdict} Time-weighted ignores "
                     f"contribution timing; money-weighted does not.")

            st.divider()

            # --- realized vs unrealized, deliberately not summed ---------
            rs = an.realized_summary(realized)
            us = an.unrealized_summary(positions)

            col_r, col_u = st.columns(2, gap="large")
            with col_r:
                statement("Realized — money already received", [
                    ("Net", tinted(rs["net"]), None, True),
                    ("Gross wins", tinted(rs["gross_wins"]),
                     f"{rs['wins']} trades", False),
                    ("Gross losses", tinted(-rs["gross_losses"]),
                     f"{rs['losses']} trades", False),
                    ("Win rate",
                     f"{rs['win_rate']:.0%}" if rs["win_rate"] is not None else "—",
                     None, False),
                    ("Profit factor",
                     f"{rs['profit_factor']:.2f}"
                     if rs["profit_factor"] is not None else "—",
                     "$ won per $ lost", False),
                ])
                if rs["gross_losses"] > 0 and rs["net"] != 0:
                    ratio = (rs["gross_wins"] + rs["gross_losses"]) / abs(rs["net"])
                    if ratio > 3:
                        note(f"The net is a small difference between much larger "
                             f"gross figures ({money(rs['gross_wins'])} won against "
                             f"{money(rs['gross_losses'])} lost), so it is noisier "
                             f"than the single number suggests.")

            with col_u:
                statement("Unrealized — a market estimate", [
                    ("Cost basis", money(us["cost_basis"]), None, False),
                    ("Market value", money(us["market_value"]), None, False),
                    ("Unrealized P&L", tinted(us["unrealized"]), None, True),
                    ("Return",
                     tinted(us["unrealized"], f"{us['return_pct']:+.2%}")
                     if us["return_pct"] is not None else "—", None, False),
                ])
                note("Not money you have. It moves every day and is never "
                     "added to the realized figure above.")

            st.divider()

            # --- the question this account can uniquely answer -----------
            section("Discretionary trading against automatic investing")
            attribution = an.attribution(trades, prices, opening)
            if attribution.empty:
                note("No placed_agent information on these trades.")
            else:
                labels = {"user": "You (discretionary)",
                          "recurring": "Autopilot (recurring)",
                          "unknown": "Unattributed"}
                display = attribution.copy()
                display["Stream"] = display["placed_agent"].map(
                    lambda a: labels.get(a, a))
                st.dataframe(
                    display[["Stream", "fills", "net_deployed", "ending_value",
                             "gain", "return_pct"]],
                    width="stretch", hide_index=True,
                    column_config={
                        "Stream": st.column_config.TextColumn("Stream"),
                        "fills": st.column_config.NumberColumn("Fills", format="%d"),
                        "net_deployed": st.column_config.NumberColumn(
                            "Capital deployed", format="dollar"),
                        "ending_value": st.column_config.NumberColumn(
                            "Value now", format="dollar"),
                        "gain": st.column_config.NumberColumn("Gain", format="dollar"),
                        "return_pct": st.column_config.NumberColumn(
                            "Return", format="percent"),
                    },
                )
                note("Each stream is valued as if it were its own portfolio, so "
                     "the returns are comparable despite very different amounts "
                     "of money. **Indicative, not audited:** a recurring buy and "
                     "a manual sell can touch the same shares, so the two streams "
                     "are not fully independent. Note also that fill counts run "
                     "opposite to the money — most fills are small recurring buys.")

            by_agent = an.realized_by_agent(trades, realized)
            if not by_agent.empty:
                section("Who booked the realized gains")
                for _, row in by_agent.iterrows():
                    label = {"user": "You (discretionary)",
                             "recurring": "Autopilot (recurring)",
                             "unmatched": "Unmatched to a sell"}.get(
                        row["placed_agent"], row["placed_agent"])
                    st.markdown(
                        f'<div class="stmt-row"><span class="stmt-label">{label}'
                        f'</span><span class="stmt-val">{tinted(row["realized"])}'
                        f'<span class="stmt-note">{int(row["trades"])} closing '
                        f'trades</span></span></div>',
                        unsafe_allow_html=True)
                note("Attributed to whoever placed the sell — the decision that "
                     "books the gain.")

            # --- cash drag, once there is a series to show ---------------
            if not account.empty and account["cash"].notna().any():
                latest = account.iloc[-1]
                cash, equity = latest["cash"] or 0, latest["equity_value"] or 0
                if cash > 0 and equity > 0:
                    st.divider()
                    section("Cash")
                    share = cash / (cash + equity)
                    statement("Uninvested", [
                        ("Cash", money(cash), f"{share:.1%} of account", True),
                        ("Equity", money(equity), None, False),
                    ])
                    note(f"Uninvested cash earns nothing but counts as capital "
                         f"you committed. It is excluded from the return figures "
                         f"above, which therefore flatter you by however long it "
                         f"has sat idle. Recorded only from the first sync that "
                         f"captured it — Robinhood serves no history for this.")


# =====================================================================
# Stock — one symbol's price, with your own trades marked on it
# =====================================================================
#
# Everything else on this page aggregates; this tab is the one place a
# single decision can be seen in context — where you bought, where you
# sold, and what the price did either side. Two rules carry over from the
# rest of the page: realized and unrealized sit in separate blocks and are
# never summed, and every percentage that could be misread says what it is
# (the header's range change is the STOCK's move, not your return).
# =====================================================================
with tab_stock:
    focus = st.session_state.get("focus")
    if not focus or prices.empty:
        st.info(f"No price history yet. {SYNC_HINT}")
    else:
        # Mirror the focus into whichever picker owns it, so the pickers
        # always agree with whatever opened this tab — a table row, a bar,
        # or the URL.
        st.session_state["pick_held"] = focus if focus in held_symbols else None
        st.session_state["pick_closed"] = (focus if focus in closed_symbols
                                           else None)
        p_left, p_right = st.columns([3, 1], gap="large",
                                     vertical_alignment="bottom")
        with p_left:
            if held_symbols:
                st.segmented_control(
                    "Holdings · largest first", held_symbols, key="pick_held",
                    on_change=_pick, args=("pick_held",))
        with p_right:
            st.selectbox(
                "Closed positions", closed_symbols, index=None,
                placeholder="Choose a symbol", key="pick_closed",
                on_change=_pick, args=("pick_closed",))

        all_closes = an.symbol_closes(prices, focus)
        if len(all_closes) < 2:
            note(f"No price history for {focus} yet. {SYNC_HINT}")
        else:
            pos = (h_all[h_all["symbol"] == focus].iloc[0]
                   if focus in held_symbols else None)
            last_day = all_closes.index[-1]
            # The header price, the chart's end point and every "price
            # since" figure all use the same latest price: the live quote
            # for a holding, the last close for anything since sold.
            if pos is not None and pd.notna(pos["last_price"]):
                latest = float(pos["last_price"])
                quote_day = positions["as_of"].iloc[0][:10]
                latest_label = f"quote at snapshot {quote_day}"
                quote = (quote_day, latest)
            else:
                latest, latest_label, quote = (
                    float(all_closes.iloc[-1]), f"close {last_day}", None)

            fills = an.symbol_fills(trades, realized, focus,
                                    latest_close=latest)
            has_recurring = (fills["placed_agent"] == "recurring").any()

            header = st.container()   # filled below, once the range is known

            c_range, c_toggle = st.columns([3, 1], gap="large",
                                           vertical_alignment="bottom")
            with c_range:
                window = st.segmented_control(
                    "Range", an.RANGES, default="1Y", key="stock_range",
                    required=True)
            with c_toggle:
                show_recurring = has_recurring and st.toggle(
                    "Recurring buys", value=True, key="stock_recurring",
                    help="Automatic investments, drawn as ticks along the "
                         "bottom of the chart and listed in the trade log.")

            start = an.window_start(last_day, window)
            closes = all_closes[all_closes.index >= start] if start else all_closes
            shown = fills[fills["date"] >= closes.index[0]]
            if not show_recurring:
                shown = shown[shown["placed_agent"] != "recurring"]

            # -- header: price, the stock's own move, 52-week range -------
            change = closes.iloc[-1] / closes.iloc[0] - 1.0
            span = (f"since {closes.index[0]}" if window == "All"
                    else f"over {window}")
            year = all_closes[all_closes.index >= an.window_start(last_day, "1Y")]
            low, high = float(year.min()), float(year.max())
            at = ((all_closes.iloc[-1] - low) / (high - low)
                  if high > low else 0.5)
            with header:
                st.markdown(
                    f'<div class="quote"><span class="quote-sym">{focus}</span>'
                    f'<span class="quote-px">{money(latest)}</span>'
                    f'<span class="quote-note">{latest_label}</span></div>'
                    f'<div class="quote-sub">'
                    f'<span><span class="quote-chg">'
                    f'{tinted(change, f"{change:+.2%}")}</span>'
                    f'<span class="quote-note"> &nbsp;price change {span} — '
                    f'the stock, not your return</span></span>'
                    f'<span class="range52"><span>52-wk close</span>'
                    f'<span>{money(low)}</span><span class="track">'
                    f'<span class="tick" style="left:{at:.1%}"></span></span>'
                    f'<span>{money(high)}</span></span></div>',
                    unsafe_allow_html=True)

            # -- the chart -----------------------------------------------
            # The key goes above the chart but is finished after it: only
            # the chart knows whether any label had to group trades.
            key_slot = st.empty()
            key = []
            manual = shown[shown["placed_agent"] != "recurring"]
            if (manual["side"] == "buy").any():
                key.append((key_badge("B", BADGE_LOOK["buy"]),
                            "Buy you placed"))
            sold = set(outcome("sell", r)
                       for r in manual.loc[manual["side"] == "sell", "realized"])
            for kind, words in [("gain", "Sell, gain booked"),
                                ("loss", "Sell, loss booked"),
                                ("none", "Sell, no gain or loss")]:
                if kind in sold:
                    key.append((key_badge("S", BADGE_LOOK[kind]), words))
            if show_recurring and (shown["placed_agent"] == "recurring").any():
                key.append((key_glyph("|", ACCENT), "Recurring buy"))
            if pos is not None and pd.notna(pos["average_buy_price"]):
                key.append((key_glyph("- -", MUTED), "Your average cost"))
            if quote:
                key.append((key_glyph("○", INK), "Quote at snapshot"))

            held_series = an.holdings_over_time(trades, prices, opening)
            shares = (held_series[focus] if focus in held_series
                      else pd.Series(0.0, index=all_closes.index))
            grouped = stock_chart(
                closes, shown, shares,
                avg_cost=(float(pos["average_buy_price"])
                          if pos is not None
                          and pd.notna(pos["average_buy_price"]) else None),
                quote=quote)
            if grouped:
                key.append((key_glyph("×3", MUTED),
                            "trades too close together share a label; "
                            "each keeps its own dot"))
            with key_slot:
                chart_key(key)

            if not opening.empty and focus in set(opening["symbol"]):
                carried = float(opening.loc[opening["symbol"] == focus,
                                            "quantity"].sum())
                note(f"{focus} was already held when records begin on "
                     f"{db.HISTORY_STARTS}: {shares_text(carried)} share(s) "
                     f"arrived with no buy on record. The shares strip "
                     f"counts them; the trade log can't show where they "
                     f"were bought. Realized dollars come from Robinhood "
                     f"and are complete.")

            # -- position and realized, side by side, never summed ---------
            left, right = st.columns(2, gap="large")
            with left:
                if pos is not None:
                    pct = pos["unrealized_pct"]
                    statement("Your position", [
                        ("Shares", shares_text(pos["quantity"]), None, False),
                        ("Average cost", money(pos["average_buy_price"]),
                         None, False),
                        ("Cost basis", money(pos["cost_basis"]), None, False),
                        ("Market value", money(pos["market_value"]),
                         f"{pos['weight']:.1%} of holdings", False),
                        ("Unrealized", tinted(pos["unrealized"]),
                         tinted(pos["unrealized"], f"{pct:+.2%}")
                         if pd.notna(pct) else None, True),
                    ])
                    note("A market estimate at the snapshot quote — not money "
                         "you have.")
                else:
                    last_sold = fills.loc[fills["side"] == "sell", "date"].max()
                    statement("Your position", [
                        ("Shares", "0 · closed",
                         f"last sold {last_sold}" if pd.notna(last_sold)
                         else None, True),
                    ])
            with right:
                rs = an.realized_summary(
                    realized[realized["symbol"] == focus]
                    if not realized.empty else realized)
                if rs["trades"]:
                    def sells_text(n):
                        return f"{n} sell" if n == 1 else f"{n} sells"
                    statement(f"Realized on {focus} — all time", [
                        ("Closing trades", f"{rs['trades']}", None, False),
                        ("Gains", tinted(1, money(rs["gross_wins"])),
                         sells_text(rs["wins"]), False),
                        ("Losses", tinted(-1, money(-rs["gross_losses"])),
                         sells_text(rs["losses"]), False),
                        ("Net realized", tinted(rs["net"]), None, True),
                    ])
                else:
                    statement(f"Realized on {focus} — all time", [
                        ("Net realized", money(0), "nothing sold yet", True),
                    ])
                note("Robinhood's own figures. Never added to the unrealized "
                     "estimate beside it.")

            # -- the trade log: the chart's table twin ---------------------
            label = "all" if window == "All" else window
            section(f"Trades in {focus} · {label} · {len(shown)} of "
                    f"{len(fills)}")
            if shown.empty:
                note("No trades in this range.")
            else:
                # In percentage points for a printf format: Streamlit's
                # "percent" trims trailing zeros, so 4.00% printed as "4%"
                # in a column of two-decimal figures.
                st.dataframe(
                    shown.assign(move_since=shown["move_since"] * 100)[
                        ["filled_at", "side", "placed_agent", "quantity",
                         "price", "amount", "realized", "move_since"]],
                    width="stretch", hide_index=True, placeholder="—",
                    **({"height": 420} if len(shown) > 12 else {}),
                    column_config={
                        "filled_at": st.column_config.DatetimeColumn(
                            "Filled", format="YYYY-MM-DD HH:mm"),
                        "side": st.column_config.TextColumn("Side",
                                                            width="small"),
                        "placed_agent": st.column_config.TextColumn(
                            "Placed by"),
                        "quantity": st.column_config.NumberColumn(
                            "Shares", format="%.6f"),
                        "price": st.column_config.NumberColumn(
                            "Price", format="dollar"),
                        "amount": st.column_config.NumberColumn(
                            "Amount", format="dollar"),
                        "realized": st.column_config.NumberColumn(
                            "Realized", format="accounting",
                            help="Robinhood's booked gain or loss on this "
                                 "sell."),
                        "move_since": st.column_config.NumberColumn(
                            "Price since", format="%+.2f%%",
                            help=f"Change from this fill's price to "
                                 f"{money(latest)} ({latest_label}). A price "
                                 f"move, not your return on the trade."),
                    },
                )
                note("“Price since” is what the stock did after each trade: "
                     "positive after a buy means it has risen since you "
                     "bought; positive after a sell means it kept rising "
                     "after you sold.")
