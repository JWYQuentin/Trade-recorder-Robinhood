# Trade Recorder

A small local Mac app that records your Robinhood **equity** activity into a
local SQLite database, shows it in a Streamlit dashboard, and exports it to
Excel. It tracks four separate things:

- **Trades** — every filled order, immutable history.
- **Holdings** — what you own right now, and the gain you haven't taken yet.
- **Realized P&L** — the gain you *have* taken, on positions already closed.
- **Performance** — how the money actually did: time- and money-weighted
  returns, drawdown, and whether your own trades beat your automatic ones.

**Scope — deliberately narrow:**

- **Read-only.** It never places, cancels, or modifies trades.
- Equities only. No options, no crypto.
- Config and credentials live in local gitignored files, never in code.
- Talks only to Robinhood's **official** Agentic Trading MCP — no scraping libraries.

## Files

| File | What it does |
|---|---|
| `db.py` | Creates/migrates `trades.db`, idempotent inserts, snapshot swaps |
| `models.py` | `Trade` dataclass + raw-order → `Trade` normalization |
| `sync_robinhood.py` | Fetches orders, positions and realized P&L (mock or real) |
| `analytics.py` | Metrics engine — returns, drawdown, attribution. Pure functions, no I/O |
| `app.py` | Streamlit dashboard — Trades / Holdings / Realized P&L / Performance tabs |
| `export_excel.py` | Writes `trading record.xlsx` from the database |
| `tests/` | pytest suite — metric unit tests plus a live-data reconciliation check |
| `Open Dashboard.app` | Double-click (or keep in the Dock) to open the dashboard |
| `Trade Dashboard.command` | The older Finder launcher; still works |
| `.env.example` | Config template — copy to `.env` |

## 1. Install

```bash
cd "Trade Recorder"
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## 2. Test with mock data first

`.env.example` defaults to `DATA_SOURCE=mock`, so this works with zero Robinhood setup:

```bash
python sync_robinhood.py    # inserts 3 sample filled trades, skips 1 cancelled
python sync_robinhood.py    # run again — 0 inserted, proves idempotency
streamlit run app.py        # dashboard at http://localhost:8501
```

Mock mode covers the **trades** pipeline only; it returns no positions or
realized P&L. That's deliberate — those two tables are replace-on-sync, and an
empty result leaves them untouched, so mock mode can never wipe a real snapshot.

> Mock rows use IDs like `mock-order-001`. Running mock mode against a database
> holding real trades **will** insert them. To clear them out:
> `sqlite3 trades.db "DELETE FROM trades WHERE robinhood_order_id LIKE 'mock-%';"`

## 3. Connect your real Robinhood account

### One-time account setup

Robinhood requires you to open a dedicated **Agentic account** before any agent
can connect — even a read-only one like this. Do it on a **desktop device**; the
phone app can't complete the flow. See
[Robinhood's Agentic Trading page](https://robinhood.com/us/en/agentic-trading/).

You don't need to fund it. It exists only to satisfy the connect flow. Once
connected, the MCP grants **read** access across *all* your accounts, which is
what this app uses — while order placement stays confined to the Agentic account.
That scoping is enforced by Robinhood, not by this code.

### Configure `.env`

```ini
DATA_SOURCE=robinhood
ROBINHOOD_ACCOUNT_NUMBER=<your everyday brokerage account, NOT the Agentic one>
ROBINHOOD_ORDERS_TOOL=get_equity_orders
ROBINHOOD_SINCE=auto
```

`ROBINHOOD_ACCOUNT_NUMBER` is **required** — the order-history tool errors without
it. Use the account holding your real trading history.

Pin `ROBINHOOD_ORDERS_TOOL` rather than relying on auto-detection. The server also
exposes `review_equity_order`, which the name heuristic does *not* rule out.

`ROBINHOOD_SINCE=auto` starts each run 5 days before the newest fill already
stored, so a scheduled sync reads one page instead of re-walking all history. It
never needs updating. Use a `YYYY-MM-DD` date for a fixed bound, or leave blank
for a full backfill.

See `.env.example` for the rest, including `ROBINHOOD_VERBOSE=1` to print the
server's full tool catalogue (suppressed by default once the tool name is pinned,
since it's ~5KB of noise in every log entry).

### First run

```bash
python sync_robinhood.py
```

A browser window opens for Robinhood's OAuth approval. Approve it, and the tokens
are cached in `.rh_tokens.json` so later runs need no browser.

**The run takes roughly 30–60 seconds and is silent for the first several** while
it negotiates OAuth and lists the server's tools. It is not frozen — don't Ctrl+C.
Interrupting mid-flight produces an alarming `anyio.WouldBlock` traceback and a
`Session termination failed: 400`, which are only teardown noise.

Set `DRY_RUN=1` to print what came back and exit without touching the database —
worth doing once before the first real write.

### What one sync actually does

It opens **two** MCP sessions, in this order:

1. **Orders** — walks every page of filled orders and inserts new ones.
2. **Portfolio** — fetches open positions, prices them with a live quote, and
   fetches realized P&L.

Two sessions rather than one is a deliberate trade-off: it costs an extra
connection, but keeps the verified orders path untouched by the newer code.

Trades are *appended* (deduped on order id). Positions and realized P&L are
*replaced* wholesale each run, because they're snapshots rather than history. An
empty result leaves the previous snapshot in place, so a transient API hiccup
can't make the dashboard read as though you own nothing.

## 4. The dashboard

```bash
streamlit run app.py
```

Or double-click **`Open Dashboard.app`** — it can live in your Dock, reuses an
already-running server instead of starting a second one, and reports problems in
a readable alert rather than a stack trace.

Four tabs:

- **Trades** — every fill, filterable by symbol, side, date range, and *placed
  by*. That last one separates manual trades from automated recurring buys, which
  matters a lot if most of your order count is dollar-cost averaging. Shows
  Invested / Proceeds / Fees plus a cumulative-net-invested chart.
- **Holdings** — positions, cost basis, market value, unrealized P&L.
- **Realized P&L** — total, per-symbol breakdown, and every closing trade.
- **Performance** — portfolio value against the capital that built it, time- and
  money-weighted returns, drawdown, realized quality (win rate, profit factor),
  and a split of discretionary trades against automatic ones.

Realized and unrealized are kept apart on purpose. One is money already received;
the other is a market estimate that changes daily. The dashboard never adds them.

The dashboard only reads `trades.db` — it never calls Robinhood. Holdings are
priced as of the last sync, so they drift with the market between runs.

## 5. Export to Excel

```bash
python export_excel.py      # writes "trading record.xlsx"
```

Six sheets:

| Sheet | What's on it |
|---|---|
| **Overview** | Where you stand, in one screen — all cross-sheet formulas |
| **Performance** | Returns, drawdown, and the discretionary/automatic split |
| **Trades** | One row per fill, with an autofilter |
| **Summary by Symbol** | Per-symbol totals over the Trades sheet |
| **Holdings** | Positions, unrealized P&L, and % of portfolio |
| **Realized P&L** | Per-symbol summary plus every closing trade |

Dollar columns are live Excel **formulas**, not numbers baked in by Python, so
the workbook recalculates if you edit it — drop in your own price on the Holdings
sheet and everything re-figures. Holdings and Realized P&L are skipped entirely
if no snapshot has been synced yet, rather than emitting empty sheets.

> Formula cells hold no cached values until Excel opens the file. Reading the
> workbook with pandas before its first open in Excel yields empty columns.

## 6. Reading the Performance tab

Two returns are shown, and they are meant to disagree.

**Time-weighted return** ignores when you added money, so it measures the
*picks*. This is the figure a fund quotes, and the fair one for comparing
against an index.

**Return on deployed capital** (money-weighted, XIRR) weights by how much was
invested when, so it measures *your outcome*. When it beats the time-weighted
figure, your contribution timing helped.

Three caveats are shown beside the numbers rather than buried here, because each
one is easy to quote out of context:

- **Annualising a short holding period overstates things.** If most of your money
  arrived recently, the average dollar has been invested for only a few months,
  and annualising that stretch produces a rate you should not expect to repeat.
  The tab warns you whenever the capital-weighted holding period is under 180
  days.
- **"Deployed capital" is not "money you transferred."** Robinhood's MCP exposes
  no transfer or ACH history, so cash sitting uninvested is invisible to the
  return figures. It is reported separately at the bottom of the tab so the gap
  stays visible.
- **The discretionary/automatic split is indicative, not audited.** A recurring
  buy and a manual sell can touch the same shares, so the two streams are not
  fully independent.

Realized and unrealized are never added together, here or anywhere else.

### The history floor

Robinhood serves no order history before **2025-09-22** (verified with an
unbounded query). That is the start of the record, not a bug. Any position opened
before that date has no buy to replay, so `opening_balances` seeds the missing
shares; those symbols have correct dollar figures but unreliable *percentage*
returns, and the app says so rather than showing a confident wrong number.

## 7. Check the numbers are trustworthy

```bash
python -m pytest tests/ -q
```

Two suites:

- **`test_analytics.py`** — unit tests for the metrics, against hand-computed
  fixtures rather than whatever the code happens to produce. XIRR is checked
  against known answers and against its defining property (NPV at the solved rate
  must be zero).
- **`test_reconciliation.py`** — runs against your *live* database. It asserts
  that replaying every stored trade reproduces the share counts Robinhood itself
  reports, and that pricing them lands within one session's drift of reported
  equity.

That second suite is the one that matters. If it fails, every figure on the
Performance tab is suspect, and it should be fixed before anything else.

## 8. Keep it synced automatically

Duplicates are impossible thanks to the `UNIQUE` constraint on
`robinhood_order_id`, so re-running is always safe.

**Use launchd, not cron.** On macOS, editing a crontab needs Full Disk Access,
whereas a user LaunchAgent needs no special permission. A LaunchAgent is
installed at `~/Library/LaunchAgents/com.meizunhong.traderecorder.sync.plist`:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.meizunhong.traderecorder.sync.plist   # enable
launchctl print gui/$(id -u)/com.meizunhong.traderecorder.sync                                     # status
launchctl bootout gui/$(id -u)/com.meizunhong.traderecorder.sync                                   # disable
```

It runs **every 6 hours**, syncs and then regenerates `trading record.xlsx`
(chained with `&&`, so the export only runs if the sync succeeded), appending
everything to `sync.log`.

A plain interval is used rather than a market-hours window on purpose: this
machine is UTC+8, where US market hours fall at 21:30–04:00 local **and shift by
an hour twice a year** with US daylight saving. Nothing here is time-critical —
trades are recorded after the fact.

`ROBINHOOD_NO_BROWSER=1` is set in the job. Access tokens expire every few days
and renew silently via the refresh token, but if that ever fails the fallback is
a browser login — which unattended would open a window nobody sees and block
until the OAuth timeout. With the flag set, the run fails immediately and says
so in the log.

## Database schema

**`trades`** — immutable history, one row per fill:

`id`, `robinhood_order_id` (UNIQUE — dedupe key), `symbol`, `side`,
`quantity`, `average_price`, `status`, `filled_at`, `created_at`,
`asset_type`, `fees`, `placed_agent`, `raw_json` (full original order).

`placed_agent` records who placed the order — `user` for a manual trade,
`recurring` for an automated investment — which is how you separate
discretionary trades from dollar-cost averaging.

**`positions`** — snapshot, replaced each sync:

`symbol` (PK), `quantity`, `average_buy_price`, `last_price`, `price_source`,
`as_of`.

`price_source` records whether the quote came from the regular session or
extended hours, so a price is never silently mixed with one from another window.
`average_buy_price` comes from Robinhood and already reflects partial sells, so
it can look unlike your trade history for a position you've mostly sold.

**`realized_pnl`** — snapshot, replaced each sync:

`id`, `timestamp`, `symbol`, `quantity`, `price`, `realized_gain`, `as_of`.

Replaced rather than appended because Robinhood returns the complete set every
time and gives no stable per-row id.

**History tables** — added so performance can be measured over time, since
`positions` is overwritten on every sync and keeps no past:

- **`position_snapshots`** — append-only archive of every positions swap, keyed
  `(snapshot_at, symbol)`.
- **`daily_prices`** — daily closes, keyed `(symbol, date)`. Fetched for every
  symbol *ever* traded, not just those currently held: valuing the portfolio on a
  past date needs prices for positions since closed. Backfilled once, then topped
  up incrementally.
- **`portfolio_snapshots`** — account equity and cash per sync. Appended, never
  replaced. There is no historical version of this endpoint, so the cash series
  necessarily starts from the first sync that recorded it.
- **`tax_lots`** — Robinhood's own per-lot cost basis, with `open_date` and
  short/long-term `term`. Replaced each sync, like `positions`.
- **`opening_balances`** — shares held before the 2025-09-22 history floor that
  no stored trade accounts for. Seeded once; cost basis is left NULL because it
  is genuinely unknowable.

`get_connection()` migrates an older database automatically: any missing column
is added and backfilled from `raw_json`, once, with no re-sync needed. Note that
`ALTER TABLE` appends, so a migrated database has the newer columns *after*
`raw_json` while a freshly created one has them before. Nothing reads by
position, so it doesn't matter.

## Files you must never commit

`.gitignore` already covers these:

- **`.env`** — your account number and config
- **`.rh_tokens.json`** — live OAuth credentials to your brokerage account
- **`trades.db`** (and any `.bak`) — your complete trading history
- **`trading record.xlsx`** — same data, different format
- **`sync.log`** (and `sync.log.1`) — holds your holdings and P&L figures

## Troubleshooting

- **Dashboard empty** → run `python sync_robinhood.py` first, then refresh.
- **Holdings or Realized P&L tab empty** → those need a portfolio snapshot; mock
  mode doesn't produce one. Sync against the real account.
- **Run appears to hang** → it doesn't print for the first several seconds. Wait.
- **`ROBINHOOD_ACCOUNT_NUMBER is not set`** → add it to `.env`; see step 3.
- **`ModuleNotFoundError: mcp`** → `pip install -r requirements.txt` inside the venv.
- **Port 3030 already in use** → change `ROBINHOOD_CALLBACK_PORT` in `.env`.
- **Browser opens on every run** → `.rh_tokens.json` isn't being written; check
  folder permissions.
- **Auth errors** → delete `.rh_tokens.json` and re-run to redo the OAuth flow.
- **Scheduled sync silently stopped after moving the folder** → the LaunchAgent
  stores an **absolute** path; nothing else in this project does. The symptom is
  `/bin/sh: ./venv/bin/python: No such file or directory` in the log and
  `last exit code = 127` from `launchctl print`. Fix all three paths in
  `~/Library/LaunchAgents/com.meizunhong.traderecorder.sync.plist`, then
  `bootout` and `bootstrap` it again. Note the log keeps being written to the
  *old* location until you do, so look there first.
- **`git push` fails with an HTTP2 framing error** → a local proxy is mangling
  HTTP/2. `git config http.version HTTP/1.1` fixes it.
- **Performance tab says "no price history"** → run the sync; it backfills daily
  closes. Mock mode does not produce them.
- **Reconstructed value differs from Holdings by 1-2%** → expected. Daily bars
  carry the previous session's close while Holdings uses a live quote, so the two
  are legitimately a day apart. A much larger gap means something is wrong, and
  `pytest tests/` will say so.
- **`sync.log` got large** → it rotates itself to `sync.log.1` past ~1 MB, at the
  start of a run. Nothing to do.
- **Reset everything** → delete `trades.db` and re-run the sync. Note this also
  discards the price history and snapshot archive, which the sync will refetch —
  except `portfolio_snapshots`, whose cash series cannot be recovered.
