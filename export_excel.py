"""
export_excel.py — Write every stored trade out to `trading record.xlsx`.

Two sheets:
  "Trades"            one row per filled trade, newest first.
  "Summary by Symbol" per-symbol totals, built from SUMIFS/COUNTIF formulas
                      that point back at the Trades sheet.

The dollar columns are Excel FORMULAS, not numbers baked in by Python, so the
workbook recalculates if you edit it.

Run it with:  python export_excel.py      (after sync_robinhood.py)
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import db

FOLDER = Path(__file__).parent
OUT_PATH = FOLDER / "trading record.xlsx"


# =====================================================================
# 1. Pull the data out of SQLite
# =====================================================================
#
# Everything goes through db.get_connection() rather than a raw sqlite3
# connect, so the schema is created/migrated first — otherwise exporting
# from a database written by an older version would fail on the tables
# that didn't exist yet.
# =====================================================================

def load_records() -> list[dict]:
    """Read every trade out of the database."""
    with db.get_connection() as conn:
        rows = db.get_all_trades(conn)

    records = []
    for r in rows:
        records.append({
            # Timestamps arrive as ISO-8601 with a trailing Z; Excel wants a
            # naive datetime, so convert to UTC and drop the tzinfo.
            "filled_at": datetime.fromisoformat(
                r["filled_at"].replace("Z", "+00:00")
            ).astimezone(timezone.utc).replace(tzinfo=None),
            "symbol": r["symbol"],
            "side": r["side"],
            "quantity": r["quantity"],
            "price": r["average_price"],
            "fees": float(r["fees"] or 0),
            "placed_by": r["placed_agent"] or "",
            "order_id": r["robinhood_order_id"],
        })
    return records


def load_positions() -> list[dict]:
    """Current holdings snapshot, largest position first."""
    with db.get_connection() as conn:
        return [dict(r) for r in db.get_positions(conn)]


def load_realized() -> list[dict]:
    """Every realizing (closing) trade, most recent first."""
    with db.get_connection() as conn:
        return [dict(r) for r in db.get_realized_pnl(conn)]


# =====================================================================
# 2. Styling constants
# =====================================================================

FONT = "Arial"
HEAD_FILL = PatternFill("solid", fgColor="1F3864")
HEAD_FONT = Font(name=FONT, bold=True, color="FFFFFF", size=10)
BODY = Font(name=FONT, size=10)
BOLD = Font(name=FONT, size=10, bold=True)
TITLE = Font(name=FONT, size=13, bold=True, color="1F3864")
NOTE = Font(name=FONT, size=9, italic=True, color="595959")
TOP = Border(top=Side(style="thin", color="A6A6A6"))

MONEY = '$#,##0.00;($#,##0.00);-'
MONEY0 = '$#,##0;($#,##0);-'
QTY = '#,##0.000000'
INT = '#,##0'
PCT = '0.0%;(0.0%);-'
DATE = 'yyyy-mm-dd hh:mm'


def write_header(ws, headers, row=1):
    for col, name in enumerate(headers, start=1):
        c = ws.cell(row=row, column=col, value=name)
        c.font = HEAD_FONT
        c.fill = HEAD_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


# =====================================================================
# 3. Sheet 1 — every trade
# =====================================================================

def build_trades_sheet(ws, records):
    """One row per trade. Returns the first and last data row numbers."""
    write_header(ws, ["Date Filled (UTC)", "Symbol", "Side", "Quantity",
                      "Avg Price", "Gross Amount", "Fees", "Net Amount",
                      "Placed By", "Order ID"])

    for i, rec in enumerate(records):
        row = i + 2
        ws.cell(row=row, column=1, value=rec["filled_at"]).number_format = DATE
        ws.cell(row=row, column=2, value=rec["symbol"])
        ws.cell(row=row, column=3, value=rec["side"])
        ws.cell(row=row, column=4, value=rec["quantity"]).number_format = QTY
        ws.cell(row=row, column=5, value=rec["price"]).number_format = MONEY
        # Gross = quantity x average price.
        ws.cell(row=row, column=6, value=f"=D{row}*E{row}").number_format = MONEY
        ws.cell(row=row, column=7, value=rec["fees"]).number_format = MONEY
        # A buy costs you gross + fees; a sell nets you gross - fees.
        ws.cell(row=row, column=8,
                value=f'=IF(C{row}="buy",F{row}+G{row},F{row}-G{row})'
                ).number_format = MONEY
        ws.cell(row=row, column=9, value=rec["placed_by"])
        ws.cell(row=row, column=10, value=rec["order_id"])
        for col in range(1, 11):
            ws.cell(row=row, column=col).font = BODY

    first, last = 2, len(records) + 1

    # Totals strip. Gross here spans buys AND sells, hence the explicit label.
    t = last + 1
    ws.cell(row=t, column=1, value="TOTAL (buys + sells)").font = BOLD
    for col, fmt in ((4, QTY), (6, MONEY), (7, MONEY)):
        L = get_column_letter(col)
        c = ws.cell(row=t, column=col, value=f"=SUM({L}{first}:{L}{last})")
        c.font = BOLD
        c.number_format = fmt
    for col in range(1, 11):
        ws.cell(row=t, column=col).border = TOP

    ws.cell(row=t + 2, column=1,
            value="Source: trades.db, synced from Robinhood's Agentic Trading MCP "
                  "(get_equity_orders, state=filled).").font = NOTE
    ws.cell(row=t + 3, column=1,
            value="Gross Amount and Net Amount are calculated; Quantity, Avg Price "
                  "and Fees come from Robinhood. Timestamps are UTC.").font = NOTE

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:J{last}"
    for col, width in zip(range(1, 11), (19, 10, 8, 14, 12, 14, 10, 14, 11, 38)):
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[1].height = 28

    return first, last


# =====================================================================
# 4. Sheet 2 — per-symbol summary, entirely formula-driven
# =====================================================================

def build_summary_sheet(ws, records, symbols, first, last):
    ws.cell(row=1, column=1, value="Trading Record — Summary by Symbol").font = TITLE
    ws.cell(row=2, column=1,
            value=f"{len(records)} filled trades · {len(symbols)} symbols · "
                  f"{min(r['filled_at'] for r in records):%Y-%m-%d} to "
                  f"{max(r['filled_at'] for r in records):%Y-%m-%d} · "
                  f"generated {datetime.now():%Y-%m-%d}").font = NOTE

    HEAD = 4
    write_header(ws, ["Symbol", "Trades", "Buys", "Sells", "Shares Bought",
                      "Shares Sold", "Net Shares", "Total Bought", "Total Sold",
                      "Fees Paid"], row=HEAD)

    # Absolute ranges on the Trades sheet, so the formulas survive copying.
    SYM = f"Trades!$B${first}:$B${last}"
    SIDE = f"Trades!$C${first}:$C${last}"
    QTYR = f"Trades!$D${first}:$D${last}"
    GROSS = f"Trades!$F${first}:$F${last}"
    FEES = f"Trades!$G${first}:$G${last}"

    for i, sym in enumerate(symbols):
        row = HEAD + 1 + i
        ws.cell(row=row, column=1, value=sym).font = BOLD
        for col, (formula, fmt) in {
            2: (f'=COUNTIF({SYM},$A{row})', INT),
            3: (f'=COUNTIFS({SYM},$A{row},{SIDE},"buy")', INT),
            4: (f'=COUNTIFS({SYM},$A{row},{SIDE},"sell")', INT),
            5: (f'=SUMIFS({QTYR},{SYM},$A{row},{SIDE},"buy")', QTY),
            6: (f'=SUMIFS({QTYR},{SYM},$A{row},{SIDE},"sell")', QTY),
            7: (f'=E{row}-F{row}', QTY),
            8: (f'=SUMIFS({GROSS},{SYM},$A{row},{SIDE},"buy")', MONEY),
            9: (f'=SUMIFS({GROSS},{SYM},$A{row},{SIDE},"sell")', MONEY),
            10: (f'=SUMIFS({FEES},{SYM},$A{row})', MONEY),
        }.items():
            c = ws.cell(row=row, column=col, value=formula)
            c.font = BODY
            c.number_format = fmt

    tot = HEAD + 1 + len(symbols)
    ws.cell(row=tot, column=1, value="TOTAL").font = BOLD
    for col in range(2, 11):
        L = get_column_letter(col)
        c = ws.cell(row=tot, column=col, value=f"=SUM({L}{HEAD + 1}:{L}{tot - 1})")
        c.font = BOLD
        c.number_format = INT if col <= 4 else (QTY if col <= 7 else MONEY0)
    for col in range(1, 11):
        ws.cell(row=tot, column=col).border = TOP

    ws.cell(row=tot + 2, column=1,
            value="Every figure here is a SUMIFS/COUNTIF over the Trades sheet — "
                  "edit or re-sync Trades and these update automatically.").font = NOTE
    ws.cell(row=tot + 3, column=1,
            value="Net Shares = bought minus sold, so it ignores any shares held "
                  "before the earliest synced trade.").font = NOTE

    ws.freeze_panes = f"A{HEAD + 1}"
    for col, width in zip(range(1, 11), (11, 9, 9, 9, 15, 14, 14, 15, 15, 12)):
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[HEAD].height = 28


# =====================================================================
# 5. Sheet 3 — current holdings and unrealized P&L
# =====================================================================

def build_holdings_sheet(ws, positions):
    """What you own now, what it cost, and the gain you haven't taken."""
    ws.cell(row=1, column=1, value="Holdings — unrealized P&L").font = TITLE
    as_of = positions[0].get("as_of") or "unknown"
    sources = ", ".join(sorted({p.get("price_source") or "?" for p in positions}))
    ws.cell(row=2, column=1,
            value=f"{len(positions)} open positions · snapshot {as_of} · "
                  f"prices from {sources}").font = NOTE

    HEAD = 4
    tot = HEAD + 1 + len(positions)   # needed now: the % column divides by it
    write_header(ws, ["Symbol", "Shares", "Avg Cost", "Last Price",
                      "Cost Basis", "Market Value", "Unrealized P&L",
                      "Unrealized %", "% of Portfolio"], row=HEAD)

    for i, p in enumerate(positions):
        row = HEAD + 1 + i
        ws.cell(row=row, column=1, value=p["symbol"]).font = BOLD
        ws.cell(row=row, column=2, value=p["quantity"]).number_format = QTY
        ws.cell(row=row, column=3, value=p["average_buy_price"]).number_format = MONEY
        ws.cell(row=row, column=4, value=p["last_price"]).number_format = MONEY
        # Cost, value and gain are all derived — leave them as formulas so
        # you can drop in your own price and watch the sheet re-figure.
        ws.cell(row=row, column=5, value=f"=B{row}*C{row}").number_format = MONEY
        ws.cell(row=row, column=6, value=f"=B{row}*D{row}").number_format = MONEY
        ws.cell(row=row, column=7, value=f"=F{row}-E{row}").number_format = MONEY
        # Guard the divide: a zero cost basis would otherwise give #DIV/0!.
        ws.cell(row=row, column=8,
                value=f'=IF(E{row}=0,"",G{row}/E{row})').number_format = PCT
        # Share of the portfolio by market value — the quickest read on
        # concentration, which the per-position dollar figures hide.
        ws.cell(row=row, column=9,
                value=f'=IF($F${tot}=0,"",F{row}/$F${tot})').number_format = PCT
        for col in range(2, 10):
            ws.cell(row=row, column=col).font = BODY

    ws.cell(row=tot, column=1, value="TOTAL").font = BOLD
    for col, fmt in ((5, MONEY), (6, MONEY), (7, MONEY)):
        L = get_column_letter(col)
        c = ws.cell(row=tot, column=col,
                    value=f"=SUM({L}{HEAD + 1}:{L}{tot - 1})")
        c.font = BOLD
        c.number_format = fmt
    c = ws.cell(row=tot, column=8, value=f'=IF(E{tot}=0,"",G{tot}/E{tot})')
    c.font = BOLD
    c.number_format = PCT
    c = ws.cell(row=tot, column=9, value=f"=SUM(I{HEAD + 1}:I{tot - 1})")
    c.font = BOLD
    c.number_format = PCT
    for col in range(1, 10):
        ws.cell(row=tot, column=col).border = TOP

    ws.cell(row=tot + 2, column=1,
            value="Unrealized P&L is a live market figure — it moves with prices "
                  "and is not money you have received. Realized gains are on "
                  "their own sheet.").font = NOTE
    ws.cell(row=tot + 3, column=1,
            value="Avg Cost comes from Robinhood and already reflects partial "
                  "sells, so it can look unlike your trade history for a "
                  "position you have mostly sold out of.").font = NOTE

    ws.freeze_panes = f"A{HEAD + 1}"
    for col, width in zip(range(1, 10), (11, 14, 12, 12, 14, 14, 16, 13, 15)):
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[HEAD].height = 28
    return tot


# =====================================================================
# 6. Sheet 4 — realized P&L, per symbol and per closing trade
# =====================================================================

def build_realized_sheet(ws, realized):
    """
    Gains actually taken, on positions already closed.

    Laid out summary-first: the per-symbol block at the top is what you
    read, and it's built from SUMIF/COUNTIF over the detail rows below,
    so the two can never drift apart.
    """
    symbols = sorted({r["symbol"] for r in realized})

    ws.cell(row=1, column=1, value="Realized P&L — closed positions").font = TITLE
    ws.cell(row=2, column=1,
            value=f"{len(realized)} closing trades across {len(symbols)} "
                  f"symbols. Excludes gains on what you still hold — "
                  f"see the Holdings sheet.").font = NOTE

    # Work out where the detail block lands before writing the summary,
    # because the summary formulas point down into it.
    SUM_HEAD = 4
    sum_total = SUM_HEAD + 1 + len(symbols)
    DET_HEAD = sum_total + 3
    det_first, det_last = DET_HEAD + 1, DET_HEAD + len(realized)

    SYM_R = f"$B${det_first}:$B${det_last}"
    GAIN_R = f"$E${det_first}:$E${det_last}"

    # ---- summary block -------------------------------------------------
    write_header(ws, ["Symbol", "Closing Trades", "Realized P&L"], row=SUM_HEAD)
    for i, sym in enumerate(symbols):
        row = SUM_HEAD + 1 + i
        ws.cell(row=row, column=1, value=sym).font = BOLD
        c = ws.cell(row=row, column=2, value=f"=COUNTIF({SYM_R},$A{row})")
        c.font, c.number_format = BODY, INT
        c = ws.cell(row=row, column=3, value=f"=SUMIF({SYM_R},$A{row},{GAIN_R})")
        c.font, c.number_format = BODY, MONEY

    ws.cell(row=sum_total, column=1, value="TOTAL").font = BOLD
    for col, fmt in ((2, INT), (3, MONEY)):
        L = get_column_letter(col)
        c = ws.cell(row=sum_total, column=col,
                    value=f"=SUM({L}{SUM_HEAD + 1}:{L}{sum_total - 1})")
        c.font, c.number_format = BOLD, fmt
    for col in range(1, 4):
        ws.cell(row=sum_total, column=col).border = TOP

    # ---- detail block --------------------------------------------------
    ws.cell(row=DET_HEAD - 1, column=1, value="Every closing trade").font = TITLE
    write_header(ws, ["Date (UTC)", "Symbol", "Shares", "Close Price",
                      "Realized P&L"], row=DET_HEAD)

    for i, r in enumerate(realized):
        row = det_first + i
        stamp = r["timestamp"]
        ws.cell(row=row, column=1, value=datetime.fromisoformat(
            stamp.replace("Z", "+00:00")
        ).astimezone(timezone.utc).replace(tzinfo=None)).number_format = DATE
        ws.cell(row=row, column=2, value=r["symbol"])
        ws.cell(row=row, column=3, value=r["quantity"]).number_format = QTY
        ws.cell(row=row, column=4, value=r["price"]).number_format = MONEY
        ws.cell(row=row, column=5, value=r["realized_gain"]).number_format = MONEY
        for col in range(1, 6):
            ws.cell(row=row, column=col).font = BODY

    ws.cell(row=det_last + 2, column=1,
            value="Figures come from Robinhood's own realized-P&L endpoint, not "
                  "recomputed here — informational, not a tax document.").font = NOTE

    ws.freeze_panes = f"A{SUM_HEAD + 1}"
    for col, width in zip(range(1, 6), (19, 16, 14, 14, 16)):
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[SUM_HEAD].height = 28
    ws.row_dimensions[DET_HEAD].height = 28
    return sum_total


# =====================================================================
# 7. Sheet 0 — the overview you land on
# =====================================================================

def build_overview_sheet(ws, trades_total_row, holdings_total_row,
                         realized_total_row, counts):
    """
    One screen answering "where do I stand", pulled from the other sheets.

    Every figure is a cross-sheet formula, never a copied number, so this
    can't drift from the sheets it summarises. Realized and unrealized are
    deliberately kept in separate blocks and never added together: one is
    money received, the other moves with the market every day.
    """
    T, H, R = trades_total_row, holdings_total_row, realized_total_row
    n_trades, n_pos, n_closed = counts

    ws.cell(row=1, column=1, value="Trading Record — Overview").font = TITLE
    ws.cell(row=2, column=1,
            value=f"Generated {datetime.now():%Y-%m-%d %H:%M}. Every figure "
                  f"below is a live reference to another sheet.").font = NOTE

    def block(row, heading):
        c = ws.cell(row=row, column=1, value=heading)
        c.font = HEAD_FONT
        c.fill = HEAD_FILL
        ws.cell(row=row, column=2).fill = HEAD_FILL

    def line(row, label, formula, fmt=MONEY, bold=False):
        ws.cell(row=row, column=1, value=label).font = BOLD if bold else BODY
        c = ws.cell(row=row, column=2, value=formula)
        c.font = BOLD if bold else BODY
        c.number_format = fmt

    # --- what you hold now -------------------------------------------
    block(4, "Portfolio today")
    line(5, "Open positions", n_pos, INT)
    line(6, "Cost basis", f"=Holdings!E{H}")
    line(7, "Market value", f"=Holdings!F{H}")
    line(8, "Unrealized P&L", f"=Holdings!G{H}", bold=True)
    line(9, "Unrealized return", f"=Holdings!H{H}", PCT, bold=True)
    # Holdings is sorted by market value, so row 5 is the biggest position.
    line(10, "Largest position", "=Holdings!A5", "General")
    line(11, "  as % of portfolio", f"=Holdings!I5", PCT)

    # --- what you already banked --------------------------------------
    block(13, "Realized (closed positions)")
    line(14, "Closing trades", n_closed, INT)
    line(15, "Realized P&L", f"='Realized P&L'!C{R}", bold=True)

    # --- everything you've traded --------------------------------------
    block(17, "All activity")
    line(18, "Fills recorded", n_trades, INT)
    line(19, "Total bought", f"=SUMIF(Trades!C2:C{T - 1},\"buy\",Trades!F2:F{T - 1})")
    line(20, "Total sold", f"=SUMIF(Trades!C2:C{T - 1},\"sell\",Trades!F2:F{T - 1})")
    line(21, "Net invested", "=B19-B20", bold=True)
    line(22, "Fees paid", f"=Trades!G{T}")

    ws.cell(row=24, column=1,
            value="Realized and unrealized are shown separately on purpose. "
                  "Realized P&L is money already received; unrealized is a "
                  "market estimate that changes daily. Adding them together "
                  "would imply a certainty neither has.").font = NOTE
    ws.cell(row=25, column=1,
            value='"Net invested" is capital committed (bought minus sold), '
                  "not portfolio value — compare it with Market value above "
                  "to see what that capital has become.").font = NOTE

    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 18


# =====================================================================
# 7b. Performance — the interpreted numbers
# =====================================================================

def build_performance_sheet(ws, metrics: dict) -> None:
    """
    Return figures, drawdown, and the discretionary/autopilot split.

    Unlike every other sheet here, these are VALUES rather than formulas.
    TWR chains a growth factor across 239 rows and XIRR is an iterative
    root-find; expressing either as a spreadsheet formula would mean
    shipping a fragile array formula that Excel recalculates differently
    from the Python. The workbook stays the record; this sheet is the
    reading of it, computed once by analytics.py.

    Each caveat sits next to its figure rather than in a footnote, for the
    same reason it does in the dashboard: an annualised rate over a short
    holding period is the kind of number that gets quoted out of context.
    """
    ws.cell(row=1, column=1, value="Performance").font = TITLE
    ws.cell(row=2, column=1,
            value=f"Computed {datetime.now():%Y-%m-%d %H:%M} from stored price "
                  f"history. Values, not formulas — see the note at the bottom."
            ).font = NOTE

    row = [4]

    def block(heading):
        c = ws.cell(row=row[0], column=1, value=heading)
        c.font = HEAD_FONT
        c.fill = HEAD_FILL
        for col in (2, 3):
            ws.cell(row=row[0], column=col).fill = HEAD_FILL
        row[0] += 1

    def line(label, value, fmt=MONEY, bold=False, comment=None):
        ws.cell(row=row[0], column=1, value=label).font = BOLD if bold else BODY
        c = ws.cell(row=row[0], column=2, value=value)
        c.font = BOLD if bold else BODY
        c.number_format = fmt
        if comment:
            ws.cell(row=row[0], column=3, value=comment).font = NOTE
        row[0] += 1

    def gap():
        row[0] += 1

    # --- position ------------------------------------------------------
    block("Position")
    line("Capital deployed", metrics["deployed"],
         comment="Money put into positions — not transfers to the broker.")
    line("Portfolio value", metrics["ending"])
    line("Gain", metrics["ending"] - metrics["deployed"], bold=True)
    if metrics["deployed"]:
        line("Unweighted return",
             metrics["ending"] / metrics["deployed"] - 1, PCT)
    gap()

    # --- returns -------------------------------------------------------
    block("Returns")
    if metrics["twr"] is not None:
        line("Time-weighted (cumulative)", metrics["twr"], PCT, bold=True,
             comment="Ignores when you added money — measures the picks.")
    if metrics["twr_ann"] is not None:
        line("Time-weighted (annualised)", metrics["twr_ann"], PCT)
    if metrics["mwr"] is not None:
        line("Return on deployed capital", metrics["mwr"], PCT, bold=True,
             comment="Money-weighted (XIRR). Idle cash is invisible to it.")
    if metrics["held_days"]:
        caution = ("Short — annualising this stretch overstates a repeatable rate."
                   if metrics["held_days"] < 180 else "")
        line("Avg dollar invested", metrics["held_days"], INT,
             comment=f"days. {caution}".strip())
    line("Period measured", metrics["span_days"], INT, comment="days")
    gap()

    # --- benchmark ------------------------------------------------------
    bench = metrics.get("benchmark")
    if bench:
        block(f"Against {bench['symbol']}")
        line("Your portfolio", bench["portfolio_value"],
             comment=f"{bench['portfolio_return']:+.2%} on deployed capital")
        line(f"Same $ into {bench['symbol']}", bench["benchmark_value"],
             comment=f"{bench['benchmark_return']:+.2%}")
        line("Ahead by" if bench["difference"] >= 0 else "Behind by",
             bench["difference"], bold=True,
             comment=f"{bench['excess_return']:+.2%} excess")
        ws.cell(row=row[0], column=1,
                value="Every dollar put into a position, on the day it went in, "
                      "buying the index instead. Comparing against the index's "
                      "headline return would assume all the money was invested "
                      "from day one. Dividends excluded on both sides."
                ).font = NOTE
        row[0] += 1
        gap()

    # --- drawdown ------------------------------------------------------
    block("Drawdown")
    if metrics["dd_depth"] is not None:
        line("Worst decline", metrics["dd_depth"], PCT, bold=True)
        line("Peak", metrics["dd_peak"] or "-", "General")
        line("Trough", metrics["dd_trough"] or "-", "General")
        line("Off peak now", metrics["dd_current"], PCT)
    gap()

    # --- realized quality ----------------------------------------------
    rs = metrics["realized"]
    block("Realized quality")
    line("Net realized", rs["net"], bold=True,
         comment="Gains minus losses — never a sum of magnitudes.")
    line("Gross wins", rs["gross_wins"], comment=f"{rs['wins']} trades")
    line("Gross losses", -rs["gross_losses"], comment=f"{rs['losses']} trades")
    if rs["win_rate"] is not None:
        line("Win rate", rs["win_rate"], PCT)
    if rs["profit_factor"] is not None:
        line("Profit factor", rs["profit_factor"], '0.00',
             comment="Dollars won per dollar lost.")
    gap()

    # --- attribution ----------------------------------------------------
    block("Discretionary vs automatic")
    ws.cell(row=row[0], column=1, value="Stream").font = BOLD
    ws.cell(row=row[0], column=2, value="Deployed").font = BOLD
    ws.cell(row=row[0], column=3, value="Value now").font = BOLD
    ws.cell(row=row[0], column=4, value="Gain").font = BOLD
    ws.cell(row=row[0], column=5, value="Return").font = BOLD
    for cell in ws[row[0]][:5]:
        cell.border = TOP
    row[0] += 1

    labels = {"user": "You (discretionary)", "recurring": "Autopilot (recurring)"}
    for entry in metrics["attribution"]:
        ws.cell(row=row[0], column=1,
                value=labels.get(entry["placed_agent"], entry["placed_agent"])
                ).font = BODY
        for col, key, fmt in ((2, "net_deployed", MONEY),
                              (3, "ending_value", MONEY),
                              (4, "gain", MONEY),
                              (5, "return_pct", PCT)):
            c = ws.cell(row=row[0], column=col, value=entry[key])
            c.font = BODY
            c.number_format = fmt
        row[0] += 1

    gap()
    for text in (
        "Each stream is valued as its own portfolio, so returns compare fairly "
        "despite very different amounts of money. Indicative, not audited: a "
        "recurring buy and a manual sell can touch the same shares.",
        "Realized and unrealized are never added together. One is money "
        "received; the other is a market estimate that changes daily.",
        "These cells are values, not formulas: time-weighted return and XIRR "
        "are iterative and do not survive translation into a spreadsheet "
        "formula. Re-run export_excel.py after a sync to refresh them.",
    ):
        ws.cell(row=row[0], column=1, value=text).font = NOTE
        row[0] += 1

    ws.column_dimensions["A"].width = 28
    for col in "BCDE":
        ws.column_dimensions[col].width = 16
    ws.column_dimensions["F"].width = 60


def collect_performance() -> Optional[dict]:
    """
    Run the metrics engine over the stored history.

    Returns None when there is not enough price history to say anything,
    so the caller can skip the sheet rather than emit a page of dashes.
    """
    import pandas as pd

    import analytics as an

    with db.get_connection() as conn:
        trades = pd.DataFrame([dict(r) for r in db.get_all_trades(conn)])
        prices = pd.DataFrame([dict(r) for r in db.get_daily_prices(conn)])
        opening = pd.DataFrame([dict(r) for r in db.get_opening_balances(conn)])
        positions = pd.DataFrame([dict(r) for r in db.get_positions(conn)])
        realized = pd.DataFrame([dict(r) for r in db.get_realized_pnl(conn)])

    if trades.empty or prices.empty:
        return None

    value = an.portfolio_value(trades, prices, opening)
    if value.empty or len(value) < 2:
        return None

    curve = value["value"]
    flows = an.prepare_trades(trades).groupby("date")["cash_flow"].sum()
    first, last = curve.index[0], curve.index[-1]
    span_days = (pd.Timestamp(last) - pd.Timestamp(first)).days

    twr = an.time_weighted_return(curve, flows)
    dd = an.max_drawdown(curve)
    attribution = an.attribution(trades, prices, opening)

    return {
        "deployed": float(flows.sum()),
        "ending": float(curve.iloc[-1]),
        "twr": twr,
        "twr_ann": an.annualise(twr, span_days) if twr is not None else None,
        "mwr": an.money_weighted_return(trades, float(curve.iloc[-1]), last),
        "held_days": an.capital_weighted_holding_days(trades, last),
        "span_days": span_days,
        "dd_depth": dd["depth"],
        "dd_peak": dd["peak_date"],
        "dd_trough": dd["trough_date"],
        "dd_current": dd["current"],
        "realized": an.realized_summary(realized),
        "unrealized": an.unrealized_summary(positions),
        "attribution": attribution.to_dict("records"),
        "benchmark": an.benchmark_comparison(trades, prices, curve,
                                             db.BENCHMARK_SYMBOL),
    }


# =====================================================================
# 8. Entry point
# =====================================================================

def export() -> None:
    records = load_records()
    if not records:
        print("No trades in the database. Run sync_robinhood.py first.")
        return

    symbols = sorted({r["symbol"] for r in records})
    positions = load_positions()
    realized = load_realized()

    wb = Workbook()
    trades_ws = wb.active
    first, last = build_trades_sheet(trades_ws, records)
    trades_ws.title = "Trades"
    build_summary_sheet(wb.create_sheet("Summary by Symbol"),
                        records, symbols, first, last)

    # Holdings and realized P&L only exist once a portfolio snapshot has
    # been synced. Skip rather than emit an empty sheet, so the workbook
    # never implies you own nothing.
    holdings_total = realized_total = None
    if positions:
        holdings_total = build_holdings_sheet(wb.create_sheet("Holdings"),
                                              positions)
    if realized:
        realized_total = build_realized_sheet(wb.create_sheet("Realized P&L"),
                                              realized)

    # The overview references the others, so it is built last but moved to
    # the front — opening on 576 raw trade rows buries the answer.
    if holdings_total and realized_total:
        overview = wb.create_sheet("Overview")
        build_overview_sheet(overview, last + 1, holdings_total, realized_total,
                             (len(records), len(positions), len(realized)))
        wb.move_sheet(overview, offset=-len(wb.sheetnames) + 1)
        wb.active = 0

    # Performance is computed rather than referenced, so it needs no row
    # indices from the other sheets — but it does need price history, which
    # only exists after a sync that captured it.
    performance = collect_performance()
    if performance:
        build_performance_sheet(wb.create_sheet("Performance"), performance)
        # Sits right after Overview: it answers "how am I doing", which is
        # the second question after "where do I stand".
        if "Overview" in wb.sheetnames:
            wb.move_sheet("Performance",
                          offset=-len(wb.sheetnames) + 2)

    wb.save(OUT_PATH)

    print(f"Wrote {OUT_PATH.name}")
    print(f"  Trades            {len(records):>4} fills, {len(symbols)} symbols")
    print(f"  Holdings          {len(positions):>4} positions"
          f"{'  (skipped — no snapshot)' if not positions else ''}")
    print(f"  Realized P&L      {len(realized):>4} closing trades"
          f"{'  (skipped — none synced)' if not realized else ''}")
    if performance:
        print(f"  Performance        TWR {performance['twr']:+.2%}, "
              f"deployed ${performance['deployed']:,.0f}")
    else:
        print("  Performance          (skipped — no price history)")
    print("Formula cells have no cached values until Excel opens and "
          "recalculates the file.")


if __name__ == "__main__":
    export()
