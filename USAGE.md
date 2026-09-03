# Trade Recorder — How to use it

Everything runs on your Mac. Nothing is uploaded anywhere, and nothing here can
place, cancel, or modify a trade — the connection to Robinhood is read-only.

**If you only remember one thing:** double-click **`Trade Dashboard.command`**.
The sync already runs by itself every 6 hours.

---

## 1. Open the dashboard

**The easy way** — double-click `Trade Dashboard.command` in Finder. Your browser
opens automatically. Leave the black terminal window open while you use it; close
it (or press `Ctrl+C`) to stop.

> First time only: macOS may say the file is from an unidentified developer.
> Right-click it → **Open** → **Open**. It only asks once.

**From a terminal:**

```bash
cd "path/to/Trade Recorder"
./venv/bin/streamlit run app.py
```

It appears at **http://localhost:8501**.

### What's on each tab

| Tab | Answers |
|---|---|
| **Trades** | Every fill. Filter by symbol, side, date, or who placed it. |
| **Holdings** | What you own now, and the gain you haven't taken. |
| **Realized P&L** | Gains you *have* taken, per symbol and per closing trade. |
| **Performance** | How you're actually doing — returns, drawdown, your picks vs autopilot. |

The sidebar filters apply to the **Trades tab only**.

The dashboard never contacts Robinhood — it reads the local database. Holdings are
priced as of the last sync, so they drift with the market between runs.

---

## 2. Get fresh data

**You usually don't need to.** A background job syncs every 6 hours on its own.

To pull new data right now:

```bash
cd "path/to/Trade Recorder"
./venv/bin/python sync_robinhood.py
```

Then refresh the dashboard in your browser. (It caches for 30 seconds, so wait a
moment if numbers look stale.)

**What to expect:** it takes 30–60 seconds and is **silent for the first several**
while it authenticates. It is not frozen — don't Ctrl+C. Running it twice is
always safe; duplicates are impossible.

The first run after a long gap may open a browser window asking you to approve
Robinhood access. Approve it once and it won't ask again for days.

---

## 3. Update the Excel file

```bash
./venv/bin/python export_excel.py
```

Writes **`trading record.xlsx`** in this folder. This happens automatically after
each scheduled sync, so it's usually already current.

Six sheets:

| Sheet | What's on it |
|---|---|
| **Overview** | Where you stand, one screen |
| **Performance** | Returns, drawdown, discretionary vs automatic |
| **Trades** | One row per fill |
| **Summary by Symbol** | Per-symbol totals |
| **Holdings** | Positions and unrealized P&L |
| **Realized P&L** | Closed-position gains |

Dollar figures are **live Excel formulas** — change a price on the Holdings sheet
and everything recalculates. (The Performance sheet is the exception: those are
computed values, because the return math can't be expressed as a formula.)

> Opening the file in Excel for the first time is what fills in the formula
> results. Before that, reading it with other tools shows blank columns.

---

## 4. Check the automatic sync

It runs every 6 hours. To see whether it's healthy:

```bash
launchctl print gui/$(id -u)/com.meizunhong.traderecorder.sync | grep "last exit code"
```

`last exit code = 0` means the last run succeeded. Anything else means it failed —
look at the end of `sync.log` to see why.

```bash
tail -30 sync.log        # what the last run did
```

Turn it off / back on:

```bash
launchctl bootout    gui/$(id -u)/com.meizunhong.traderecorder.sync   # stop
launchctl bootstrap  gui/$(id -u) ~/Library/LaunchAgents/com.meizunhong.traderecorder.sync.plist   # start
```

---

## 5. Check that the data is trustworthy

This verifies your trade history still reproduces the share counts Robinhood
itself reports, and that prices cover every symbol:

```bash
./venv/bin/python -m pytest tests/ -q
```

You want `39 passed`. If anything fails, the numbers on the Performance tab are
suspect — that's exactly what these checks exist to catch.

---

## Reading the Performance tab honestly

The tab shows caveats next to each figure. The three that matter most:

**Two different returns, and they disagree on purpose.**
*Time-weighted* ignores when you added money — it measures your **picks**.
*Return on deployed capital* (money-weighted) accounts for timing — it measures
**your outcome**. When the second is higher, your timing helped.

**The annualised money-weighted figure runs hot.** Most of your money arrived
recently, so the average dollar has only been invested ~100 days. Annualising
that is correct arithmetic but not a rate to expect repeating. The tab warns you
whenever this applies.

**Realized and unrealized are never added together.** One is money you have; the
other is a market estimate that changes daily. Any tool that sums them is
flattering you.

Also worth knowing: **"return on deployed capital" is not return on your
transfers.** Robinhood exposes no transfer history, so cash sitting uninvested is
invisible to that number. Your idle cash is shown separately at the bottom of the
tab so the gap stays visible.

---

## Things you should never share

These live in this folder and are excluded from git, but don't email or upload them:

- **`.rh_tokens.json`** — live credentials to your brokerage account
- **`.env`** — your account number
- **`trades.db`** — your complete trading history
- **`trading record.xlsx`** and **`sync.log`** — the same data in other forms

---

## When something goes wrong

| Symptom | Fix |
|---|---|
| Dashboard is empty | Run the sync, then refresh. |
| Performance tab says "no price history" | Run the sync — it fetches prices. |
| Numbers look stale | Wait 30 seconds and refresh; the dashboard caches briefly. |
| Sync seems frozen | It's silent for the first several seconds. Wait. |
| Browser opens on every sync | Delete `.rh_tokens.json` and run once by hand to re-authorize. |
| `Session termination failed: 400` | Harmless disconnect noise. Ignore it. |
| Scheduled sync stopped after moving the folder | The job stores absolute paths. Fix the three paths in `~/Library/LaunchAgents/com.meizunhong.traderecorder.sync.plist`, then `bootout` and `bootstrap` it. |
| Port 8501 already in use | `./venv/bin/streamlit run app.py --server.port 8502` |

**Two notes on limits, so nothing surprises you.** Robinhood serves no order
history before **2025-09-22** — that's the start of your record, not a bug. And
SPCX and TSLA were partly held before then, so their dollar figures are right but
their *percentage* returns aren't; the app flags this rather than hiding it.
