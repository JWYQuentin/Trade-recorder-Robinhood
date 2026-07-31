# Trade Recorder

A small local Mac app that records your **filled** Robinhood stock trades into a
local SQLite database, shows them in a Streamlit dashboard, and exports them to
Excel.

**Scope — deliberately narrow:**

- **Read-only.** It never places, cancels, or modifies trades.
- Equities only. No options, no crypto.
- Config and credentials live in local gitignored files, never in code.
- Talks only to Robinhood's **official** Agentic Trading MCP — no scraping libraries.

## Files

| File | What it does |
|---|---|
| `db.py` | Creates/manages `trades.db` (SQLite), idempotent inserts |
| `models.py` | `Trade` dataclass + raw-order → `Trade` normalization |
| `sync_robinhood.py` | Fetches orders (mock or Robinhood MCP), stores fills |
| `app.py` | Streamlit dashboard with symbol/side filters |
| `export_excel.py` | Writes `trading record.xlsx` from the database |
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

> Mock rows use IDs like `mock-order-001`. To clear them out before syncing real
> data: `sqlite3 trades.db "DELETE FROM trades WHERE robinhood_order_id LIKE 'mock-%';"`

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
```

`ROBINHOOD_ACCOUNT_NUMBER` is **required** — the order-history tool errors without
it. Use the account holding your real trading history. Find it with the Robinhood
app, or in the tool list the sync prints on its first run.

Pin `ROBINHOOD_ORDERS_TOOL` rather than relying on auto-detection. The server also
exposes `review_equity_order`, which the name heuristic does *not* rule out.

See `.env.example` for the remaining optional settings.

### First run

```bash
python sync_robinhood.py
```

A browser window opens for Robinhood's OAuth approval. Approve it, and the tokens
are cached in `.rh_tokens.json` so later runs need no browser.

**The run takes roughly 15–30 seconds and is silent for the first several** while
it negotiates OAuth and lists the server's tools. It is not frozen — don't Ctrl+C.
Interrupting mid-flight produces an alarming `anyio.WouldBlock` traceback and a
`Session termination failed: 400`, which are only teardown noise.

Set `DRY_RUN=1` to print what came back and exit without touching the database —
worth doing once before the first real write.

## 4. Export to Excel

```bash
python export_excel.py      # writes "trading record.xlsx"
```

Two sheets: **Trades** (one row per fill) and **Summary by Symbol** (per-symbol
totals). Dollar columns are live Excel formulas, so the workbook recalculates if
you edit it. Formula cells hold no cached values until Excel opens the file — so
reading it with pandas before its first open in Excel yields empty columns.

## 5. Keep it synced automatically

Duplicates are impossible thanks to the `UNIQUE` constraint on
`robinhood_order_id`, so re-running is always safe.

Set `ROBINHOOD_SINCE=auto` first, so each run fetches one page instead of
re-walking all history.

**Use launchd, not cron.** On macOS, editing a crontab needs Full Disk Access,
whereas a user LaunchAgent needs no special permission. A LaunchAgent is
installed at `~/Library/LaunchAgents/com.meizunhong.traderecorder.sync.plist`:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.meizunhong.traderecorder.sync.plist   # enable
launchctl print gui/$(id -u)/com.meizunhong.traderecorder.sync                                     # status
launchctl bootout gui/$(id -u)/com.meizunhong.traderecorder.sync                                   # disable
```

It runs every 6 hours and appends to `sync.log`. A plain interval is used
rather than a market-hours window on purpose: this machine is UTC+8, where US
market hours fall at 21:30–04:00 local **and shift by an hour twice a year**
with US daylight saving. Nothing here is time-critical — trades are recorded
after the fact.

`ROBINHOOD_NO_BROWSER=1` is set in the job. Access tokens expire every few days
and renew silently via the refresh token, but if that ever fails the fallback is
a browser login — which unattended would open a window nobody sees and block
until the OAuth timeout. With the flag set, the run fails immediately and says
so in the log.

For scheduled runs set `ROBINHOOD_SINCE` a few days behind your newest stored
trade. A full backfill walks every page of history; a bounded one usually needs a
single page.

## Database schema (`trades` table)

`id`, `robinhood_order_id` (UNIQUE — dedupe key), `symbol`, `side`,
`quantity`, `average_price`, `status`, `filled_at`, `created_at`,
`asset_type`, `fees`, `placed_agent`, `raw_json` (full original order).

`placed_agent` records who placed the order — `user` for a manual trade,
`recurring` for an automated investment — which is how you separate
discretionary trades from dollar-cost averaging.

`get_connection()` migrates an older database automatically: any missing column
is added and backfilled from `raw_json`, once, with no re-sync needed. Note that
`ALTER TABLE` appends, so a migrated database has the newer columns *after*
`raw_json` while a freshly created one has them before. Nothing reads by
position, so it doesn't matter.

## Files you must never commit

`.gitignore` already covers these:

- **`.env`** — your account number and config
- **`.rh_tokens.json`** — live OAuth credentials to your brokerage account
- **`trades.db`** — your complete trading history

## Troubleshooting

- **Dashboard empty** → run `python sync_robinhood.py` first, then refresh.
- **Run appears to hang** → it doesn't print for the first several seconds. Wait.
- **`ROBINHOOD_ACCOUNT_NUMBER is not set`** → add it to `.env`; see step 3.
- **`ModuleNotFoundError: mcp`** → `pip install -r requirements.txt` inside the venv.
- **Port 3030 already in use** → change `ROBINHOOD_CALLBACK_PORT` in `.env`.
- **Browser opens on every run** → `.rh_tokens.json` isn't being written; check
  folder permissions.
- **Auth errors** → delete `.rh_tokens.json` and re-run to redo the OAuth flow.
- **Reset everything** → delete `trades.db` and re-run the sync.
