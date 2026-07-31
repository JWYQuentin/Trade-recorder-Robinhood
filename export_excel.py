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

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

FOLDER = Path(__file__).parent
DB_PATH = FOLDER / "trades.db"
OUT_PATH = FOLDER / "trading record.xlsx"


# =====================================================================
# 1. Pull the trades out of SQLite
# =====================================================================

def load_records() -> list[dict]:
    """Read every trade out of the database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trades ORDER BY filled_at DESC").fetchall()
    conn.close()

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
# 5. Entry point
# =====================================================================

def export() -> None:
    records = load_records()
    if not records:
        print("No trades in the database. Run sync_robinhood.py first.")
        return

    symbols = sorted({r["symbol"] for r in records})

    wb = Workbook()
    first, last = build_trades_sheet(wb.active, records)
    wb.active.title = "Trades"
    build_summary_sheet(wb.create_sheet("Summary by Symbol"),
                        records, symbols, first, last)
    wb.save(OUT_PATH)

    print(f"Wrote {OUT_PATH.name} — {len(records)} trades, {len(symbols)} symbols.")
    print("Formula cells have no cached values until Excel opens and "
          "recalculates the file.")


if __name__ == "__main__":
    export()
